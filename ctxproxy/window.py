# Author: Maurizio Verde — LastReload
"""Window guard: no request the proxy sends to the engine may exceed the window (0.3.3).

Every chat request the proxy makes to the engine (replayed client turns, recall rounds, handoff notes, anchor and
seal requests, answerfix retries, pass-through mode) goes through ONE place: `WindowGuard.enforce`, hooked into
`Upstream.chat` / `Upstream.chat_stream`. Before sending it counts the exact prompt (chat template + pack tokenizer,
the same count the engine makes) and enforces

  INV-1  prompt + max_tokens + margin <= W        (otherwise max_tokens is lowered; if not even 1 token fits the
                                                   request is NOT sent: WindowExceeded, journal event window_guard)
  INV-2  reasoning_budget_tokens <= max_tokens - answer_min   (when the request carries a thinking budget; Strata)

The recall loop of the server (`Proxy._plan_round`) keeps INV-1 with a useful answer budget, by removing what the
loop itself added (reasoning of past rounds -> one conclusion line, recall results -> one-line receipts) in order of
value per freed token (`removal_order`), before it ever lowers the answer below the floor F:

  INV-3  (0.3.5) a search round is offered only if  P + max(S, CALL_ALLOWANCE + R + F) + H + margin <= W  after
         removing ONLY verified copies and finished reasoning (bands 0-1); otherwise the round is the final answer,
         without the proxy's internal tools, and the model is told why. S = max(2048, largest complete generation of
         the previous internal rounds + 256), never decreasing; R = 1024 per round (the results of the round take it
         in sequence); H = frame of one result + the closing note. A recall result is cut so that
         P + result + note + F + margin <= W (`Proxy._fit_result`), again freeing only bands 0-1.
  INV-4  (0.3.5) a search round never costs evidence already acquired: during the search only verified copies
         (band 0) and finished reasoning (band 1) are removed. If the new group (call + results) does not fit with F
         it is reduced or cancelled first (tool protocol kept coherent), and only then the final call; dropping whole
         older rounds (C2) is an emergency, recorded in the journal with its cause and sources.

Removal bands (`band`): 0 = verified copy (a result whose delivered text is identical to an older result still
present, or whose certified coverage - exact spans / line sets of the same record, never cut - is inside the coverage
of older results still present); 1 = reasoning of a finished round; 2 = result that is not a copy; 3 = result that is
not a copy and carries identifiers of the question (`protected`; never the reasoning). Unknown coverage (no offset,
empty sources, grouped "same text also in messages ...", a cut) is never a copy; receipts are never witnesses.

Termination / why the final call is admissible:
  P0 = prompt prepared before the loop (masking, segments). P0 + F + margin <= W is checked before the loop (journal
  `floor_short` when it does not hold: no search, the answer gets what is left). Every round either fits or the loop
  goes to the final round; the final call fits by construction of INV-3 (the cap of each result keeps F + note free).
  If it does not (an estimate gone wrong), the newest group is reduced/cancelled, then C2 drops whole rounds (rounds
  without protected pieces first) down to prompt = P0. If even P0 + 1 + margin > W the request is refused
  (WindowExceeded, HTTP 400): never sent over. Removal is a pure deterministic function of the pieces.

Counting: exact with --tokenizer (pack tokenizer: render_pieces + tokenizer, the engine's own count); otherwise the
calibrated chars/3.5 estimate (+ `window_estimate_slack`) and every guard event carries guarantee=estimated.
"""
from __future__ import annotations

import json
import math
import re

from .render import render_pieces, template_kwargs
from .upstream import UpstreamError

IDENT_RAW = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_./:\-]{2,}[A-Za-z0-9]")


def idents(text: str) -> set:
    """Identifiers of a text: tokens with a digit, an inner _ - . / :, or an inner capital (ids, paths, numbers,
    CamelCase). Plain words ('recall', 'contains') are not identifiers: they made recall_use.overlap noisy."""
    out = set()
    for t in IDENT_RAW.findall(text or ""):
        if any(c.isdigit() for c in t) or any(c in "_./:-" for c in t[1:-1]) or any(c.isupper() for c in t[1:]):
            out.add(t)
    return out


POLICIES = ("value", "reasoning", "age")
PRIVATE = ("_receipt", "_reasoning_cut", "_final_note", "_keep", "_src")
CALL_ALLOWANCE = 256   # INV-3: tokens assumed for the call (+ receipt) of one more search round
RESULT_MIN = 512       # 0.3.3/0.3.4 INV-3 (kept for reference; 0.3.5 reserves RESULT_R per round)
RESULT_R = 1024        # INV-3 (0.3.5): result room reserved per search round (taken in sequence by its calls)
S_MIN = 2048           # INV-3 (0.3.5): minimum generation assumed for a search round
S_MARGIN = 256         # INV-3 (0.3.5): margin over the largest complete generation of the previous rounds
NOTICE_MIN = 256       # a result that does not fit in less than this becomes the "no room" notice (and the final)
SEARCH_BANDS = (0, 1)  # what a search round may remove: verified copies, finished reasoning
ALL_BANDS = (0, 1, 2, 3)
RECEIPT_IT = "[{rn} {args}: risultato tolto dal contesto per spazio ({n} token); richiamalo con {rn} se serve]"
RECEIPT_EN = "[{rn} {args}: result removed from the context for space ({n} tokens); call {rn} again if needed]"
FINAL_NOTE_IT = ("\n[gestore del contesto: spazio per altre ricerche esaurito. Rispondi ora con le informazioni già "
                 "recuperate (o di' che non le hai).]")
FINAL_NOTE_EN = ("\n[context manager: no room left for more searches. Answer now with the information already "
                 "retrieved (or say that you do not have it).]")
LIMIT_NOTE_IT = ("\n[gestore del contesto: limite di ricerche raggiunto per questa risposta. Rispondi ora con le "
                 "informazioni già recuperate (o di' che non le hai).]")
LIMIT_NOTE_EN = ("\n[context manager: search limit reached for this answer. Answer now with the information already "
                 "retrieved (or say that you do not have it).]")
NO_ROOM_IT = ("[{rn}: risultato non consegnato, nessuno spazio rimasto nel contesto ({n} token). Rispondi ora con le "
              "informazioni già recuperate (o di' che non le hai).]")
NO_ROOM_EN = ("[{rn}: result not delivered, no room left in the context ({n} tokens). Answer now with the information "
              "already retrieved (or say that you do not have it).]")


class SearchState:
    """State of the recall loop of ONE client request (0.3.5), kept across its rounds.
    force_final: the search is closed (a result found no room, a call was dropped): the next round is final.
    s_est: S of INV-3, never decreasing. gens: complete generations of the internal rounds (usage.completion_tokens;
    the ones ended by `length` are censored: they only raise the lower bound). evidence_lost: non-copy results
    removed outside the newest group (must stay empty)."""

    def __init__(self):
        self.force_final = False
        self.reason = ""
        self.s_est = S_MIN
        self.gens: list = []
        self.evidence_lost: list = []
        self.floor_short = False
        self.final_reason = ""       # reason of the last final round: space / round_limit / the closure's reason

    def saw_generation(self, n, finish: str = "") -> None:
        if isinstance(n, int) and not isinstance(n, bool) and n >= 0:
            self.gens.append([n, finish or ""])
            self.s_est = max(self.s_est, n + S_MARGIN)

    def close(self, reason: str) -> None:
        if not self.force_final:
            self.force_final, self.reason = True, reason


class WindowExceeded(UpstreamError):
    """The request cannot be sent without exceeding the window (fail-closed). An UpstreamError with status 400, so
    every caller that handles engine errors handles this one too (the client gets a clear 400)."""

    def __init__(self, prompt: int, window: int, margin: int, where: str = ""):
        msg = ("mnemonic-proxy window guard: prompt %d + margin %d leaves no room in the window %d%s; "
               "the request was not sent" % (prompt, margin, window, (" (%s)" % where) if where else ""))
        super().__init__(400, json.dumps({"error": {"message": msg, "type": "context_window_exceeded",
                                                    "code": "window_guard"}}).encode("utf-8"))
        self.prompt, self.window, self.margin = prompt, window, margin


def mt_of(body: dict) -> int:
    """Generation budget the engine will use: Strata reads max_completion_tokens first, then max_tokens."""
    try:
        return int(body.get("max_completion_tokens") or body.get("max_tokens") or 0)
    except (TypeError, ValueError):
        return 0


def set_mt(body: dict, v: int) -> dict:
    out = dict(body)
    out["max_tokens"] = int(v)
    if "max_completion_tokens" in out:
        out["max_completion_tokens"] = int(v)
    return out


class WindowGuard:
    """Exact prompt count + INV-1/INV-2 for one request. Needs the Manager (token counter, estimate)."""

    def __init__(self, cfg, mgr, journal):
        self.cfg, self.mgr, self.journal = cfg, mgr, journal
        self.violations = 0          # requests that would have been sent over the window (must stay 0)
        self.refused = 0
        self.count_errors = 0        # exact mode: prompts the tokenizer/template could not count (must stay 0)
        self.where = ""              # label of the call in progress (journal)
        self.last_count: int | None = None   # 0.3.8: prompt count of the last request sent (without slack)

    @property
    def exact(self) -> bool:
        return bool(self.mgr.tc.exact)

    @property
    def margin(self) -> int:
        return int(getattr(self.cfg, "window_margin", 64) or 0)

    def count(self, messages: list, tools, kw: dict | None = None) -> int:
        """Prompt tokens of a request. Exact: render_pieces (the engine's template) + tokenizer, the same count as
        the engine. Estimate: the calibrated counter + slack."""
        msgs = [{k: v for k, v in m.items() if k != "extra"} for m in messages or []]
        if self.exact:
            try:
                return sum(self.mgr.piece_tokens(t) for _, t in render_pieces(msgs, tools, kw or {}))
            except ValueError as e:
                # 0.3.5: never a silent fallback in exact mode - counted and journaled (the sweep fails on it)
                self.count_errors += 1
                self._log(action="count_error", where=self.where or "count", error=str(e)[:200])
        est, _ = self.mgr.estimate(msgs, tools)
        return int(math.ceil(est * (1.0 + float(getattr(self.cfg, "window_estimate_slack", 0.0) or 0.0))))

    def count_body(self, body: dict) -> int:
        return self.count(body.get("messages") or [], body.get("tools"), template_kwargs(body))

    def room(self, prompt: int) -> int:
        return int(self.cfg.window) - self.margin - int(prompt)

    def _log(self, **kw):
        if self.journal is not None:
            return self.journal.log("window_guard", guarantee="exact" if self.exact else "estimated",
                                    window=self.cfg.window, margin=self.margin, **kw)
        return None

    def enforce(self, body: dict, dry: bool = False) -> dict:
        """Upstream hook: returns the body to send (max_tokens set/lowered, thinking budget capped) or raises
        WindowExceeded. Never sends a request with prompt + max_tokens + margin > W. dry: no journal, no counters
        (answerfix asks how much room there is)."""
        if not getattr(self.cfg, "window_guard", True) or not self.cfg.window:
            return body
        p = self.count_body(body)
        if not dry:
            # 0.3.8: the count without the slack, for the engine-truncation watch (Upstream.truncwatch)
            sl = 0.0 if self.exact else float(getattr(self.cfg, "window_estimate_slack", 0.0) or 0.0)
            self.last_count = int(p / (1.0 + sl)) if sl > 0 else p
        room = self.room(p)
        mt = mt_of(body)
        where = self.where or "call"
        if room < 1:
            if not dry:
                self.refused += 1
                self._log(action="refused", where=where, prompt=p, max_tokens=mt)
            raise WindowExceeded(p, self.cfg.window, self.margin, where)
        out = body
        if mt <= 0:
            out = set_mt(body, room)           # "the rest": explicit, so no engine can run past the window
        elif mt > room:
            out = set_mt(body, room)
            if not dry:
                self._log(action="clamp", where=where, prompt=p, max_tokens=room, was=mt)
        return self.cap_budget(out, dry)

    def cap_budget(self, body: dict, dry: bool = False) -> dict:
        """INV-2: a thinking budget never leaves less than answer_min tokens of answer (Strata: the budget closes
        the thinking; 0 would mean 'no budget', so at least 1)."""
        rb = body.get("reasoning_budget_tokens")
        if not isinstance(rb, int) or isinstance(rb, bool) or rb <= 0:
            return body
        mt = mt_of(body)
        amin = int(getattr(self.cfg, "answer_min_tokens", 512) or 0)
        cap = mt - amin if mt - amin >= 32 else max(1, mt // 2)
        if rb > cap:
            if not dry:
                self._log(action="budget_cap", where=self.where or "call", max_tokens=mt, budget=cap, was=rb)
            return {**body, "reasoning_budget_tokens": cap}
        return body

    def fit_text(self, text: str, cap: int, note: str) -> str:
        """Text cut so that its token count (notice included) is <= cap; cap below the notice -> ''. Strict."""
        tc = self.mgr.tc
        if tc.count(text) <= cap:
            return text
        if cap <= 0 or tc.count(note) + 1 > cap:
            return ""
        keep = cap - tc.count(note) - 1
        lo, hi = 0, len(text)
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if tc.count(text[:mid]) <= keep:
                lo = mid
            else:
                hi = mid - 1
        out = text[:lo] + note
        while out and tc.count(out) > cap:      # token boundaries at the join: shorten until it really fits
            lo = max(0, lo - 16)
            out = text[:lo] + note if lo else ""
        return out


# ------------------------------------------------------------------------------------------------- removal (G2)
def _text(c) -> str:
    if c is None:
        return ""
    if isinstance(c, str):
        return c
    return "\n".join(p.get("text") or "" for p in c if isinstance(p, dict)) if isinstance(c, list) else str(c)


def delivered_text(m: dict) -> str:
    """Text of a result as the model received it from the recall (without the closing note the guard appended)."""
    t = _text(m.get("content"))
    note = m.get("_final_note")
    if isinstance(note, str) and note and t.endswith(note):
        t = t[:-len(note)]
    return t


def sources_from_meta(meta, cut: bool = False) -> list:
    """Coverage of a recall result from its `meta` (0.3.5), i.e. what the model really received:
    ["span", rid, a, b]  characters [a, b) of record rid, verbatim and whole (offset + chars recorded, no cut);
    ["lines", rid, [..]] / ["leaf", rid, [..]]  exact line sets of record rid (id+query / leaf selector);
    ["cut"]  the piece was cut after `meta` was written (covers nothing, never a copy);
    ["opaque"]  coverage unknown (no offset, grouped provenance, summaries): covers nothing, never a copy.
    cut=True: the whole result was cut afterwards (`_fit_result`): every source is ["cut"]."""
    out = []
    for e in meta or []:
        rid = e.get("rid") if isinstance(e, dict) else None
        if not rid:
            out.append(["opaque"])
        elif cut or e.get("cut"):
            out.append(["cut"])
        elif e.get("grouped"):
            out.append(["opaque"])
        elif isinstance(e.get("offset"), int) and isinstance(e.get("chars"), int) and e["chars"] > 0:
            out.append(["span", rid, int(e["offset"]), int(e["offset"]) + int(e["chars"])])
        elif isinstance(e.get("line_set"), (list, tuple)) and e["line_set"]:
            out.append(["lines", rid, sorted(int(x) for x in e["line_set"])])
        elif isinstance(e.get("leaf_lines"), (list, tuple)) and e["leaf_lines"]:
            out.append(["leaf", rid, sorted(int(x) for x in e["leaf_lines"])])
        else:
            out.append(["opaque"])
    return out


def _covered(srcs: list, witnesses: list) -> bool:
    """Every source of a piece inside the union of the certified sources of the witnesses (same record, same
    kind). Empty or unknown sources -> False (all([]) would make an error or an empty result a 'copy')."""
    if not srcs:
        return False
    spans: dict = {}
    lines: dict = {}
    for w in witnesses:
        for s in w:
            if s and s[0] == "span":
                spans.setdefault(s[1], []).append((s[2], s[3]))
            elif s and s[0] in ("lines", "leaf"):
                lines.setdefault((s[0], s[1]), set()).update(s[2])
    for s in srcs:
        if not s or s[0] not in ("span", "lines", "leaf"):
            return False
        if s[0] == "span":
            a, b = s[2], s[3]
            pos = a
            for x, y in sorted(spans.get(s[1], [])):
                if x <= pos < y:
                    pos = y
                if pos >= b:
                    break
            if pos < b:
                return False
        elif not set(s[2]) <= lines.get((s[0], s[1]), set()):
            return False
    return True


def copy_of(loop: list) -> dict:
    """{index of a result: index of its witness} for the results of `loop` that are verified copies (0.3.5):
    the delivered text is identical to the one of an older result still present, or the result's certified
    coverage is inside the coverage of the older results still present. Receipts are never witnesses (nor copies).
    A result is a copy only of OLDER results, so two equal results never certify each other away."""
    out: dict = {}
    seen: list = []          # (i, text, sources) of the older results still present
    for i, m in enumerate(loop):
        if m.get("role") != "tool" or m.get("_receipt"):
            continue
        t = delivered_text(m)
        src = m.get("_src") if isinstance(m.get("_src"), list) else []
        w = next((j for j, tj, _ in seen if t.strip() and tj == t), None)
        if w is None and _covered(src, [sj for _, _, sj in seen]):
            w = seen[-1][0]
        if w is not None:
            out[i] = w
        seen.append((i, t, src))
    return out


def pieces(loop: list) -> list[dict]:
    """Removable pieces of the messages the recall loop added: reasoning of each round (kind 'reasoning') and each
    result (kind 'result'). round = index of the round (0 = oldest). Results already turned into receipts and empty
    reasoning are not pieces. A result carries `copy` (index of its witness, or None)."""
    copies = copy_of(loop)
    out, rnd = [], -1
    for i, m in enumerate(loop):
        if m.get("role") == "assistant":
            rnd += 1
            if isinstance(m.get("reasoning_content"), str) and m["reasoning_content"].strip() \
                    and not m.get("_reasoning_cut"):
                out.append({"i": i, "kind": "reasoning", "round": rnd, "text": m["reasoning_content"], "copy": None})
        elif m.get("role") == "tool" and not m.get("_receipt") and not m.get("_keep"):
            out.append({"i": i, "kind": "result", "round": rnd, "text": _text(m.get("content")),
                        "copy": copies.get(i), "src": m.get("_src") if isinstance(m.get("_src"), list) else [],
                        "tcid": m.get("tool_call_id")})
    return out


def asked_idents(pc: dict, question: str) -> set:
    """Identifiers of the piece that also appear in the current question (empty = the piece carries nothing the
    question asks about)."""
    return idents(pc["text"]) & idents(question)


def piece_value(pc: dict, question: str, loop: list, n_rounds: int, seen_before: set | None = None) -> float:
    """Value of a piece (higher = keep), only signals available BEFORE the decision: kind, age, identifiers of the
    current question present in the piece (x3). Reasoning weighs 1/4 of a result (the model already acted on it;
    its extracted conclusion line is kept). 0.3.5: no duplication discount any more - identifiers already seen do
    not prove redundancy (only a verified copy does, and that is band 0, not a score). `seen_before` is ignored
    (kept for callers of the 0.3.4 signature)."""
    q = idents(question)
    ids = idents(pc["text"])
    v = 3.0 * len(ids & q) + 1.0 + (pc["round"] + 1) / float(max(n_rounds, 1))
    return v * (0.25 if pc["kind"] == "reasoning" else 1.0)


def freed(pc: dict, ntok, rn: str = "recall", lang: str = "en") -> int:
    """Tokens a removal really frees: size minus what replaces it (receipt line / extracted conclusion)."""
    n = ntok(pc["text"])
    if pc["kind"] == "reasoning":
        return max(1, n - ntok(conclusion(pc["text"])))
    return max(1, n - ntok((RECEIPT_IT if lang == "it" else RECEIPT_EN).format(rn=rn, args="", n=n)) - 30)


def band(pc: dict) -> int:
    """Removal band (0.3.5): 0 verified copy, 1 reasoning, 2 result not a copy, 3 result not a copy that carries
    identifiers of the question (protected)."""
    if pc["kind"] == "reasoning":
        return 1
    if pc.get("copy") is not None:
        return 0
    return 3 if pc.get("protected") else 2


def removal_order(loop: list, ntok, question: str = "", policy: str = "value", kinds=("reasoning", "result"),
                  rn: str = "recall", lang: str = "en", bands=ALL_BANDS) -> list:
    """Pure, deterministic order in which the pieces are removed (first = removed first), 0.3.5:
    bands first - 0 verified copies, 1 finished reasoning, 2 results that are not copies, 3 results that are not
    copies and carry identifiers of the question (`protected`, never the reasoning, never a copy); `bands` keeps
    only some of them (a search round: SEARCH_BANDS). Inside a band the policy decides - age: oldest round first;
    reasoning: oldest first; value: ascending value per freed token, ties -> oldest first.
    Each piece carries `tokens` (freed), `value`, `protected`, `copy`, `band` for the caller's journal."""
    ps = [p for p in pieces(loop) if p["kind"] in kinds]
    n_rounds = sum(1 for m in loop if m.get("role") == "assistant")
    q = idents(question)
    for p in ps:
        p["tokens"] = freed(p, ntok, rn, lang)
        p["protected"] = p["kind"] == "result" and p.get("copy") is None and bool(idents(p["text"]) & q)
        p["value"] = piece_value(p, question, loop, n_rounds)
        p["band"] = band(p)
    ps = [p for p in ps if p["band"] in bands]
    if policy == "age":
        key = lambda p: (p["round"], 0 if p["kind"] == "reasoning" else 1, p["i"])
    elif policy == "reasoning":
        key = lambda p: (p["round"], p["i"])
    else:
        key = lambda p: (p["value"] / p["tokens"], p["round"], p["i"])
    return sorted(ps, key=lambda p: (p["band"],) + key(p))


def conclusion(reasoning: str, max_chars: int = 200) -> str:
    """At most one line of conclusion of a removed reasoning (its last non-empty line, shortened)."""
    lines = [l.strip() for l in (reasoning or "").splitlines() if l.strip()]
    if not lines:
        return ""
    last = lines[-1]
    return last if len(last) <= max_chars else "…" + last[-max_chars:]


def call_args(loop: list, tool_idx: int) -> str:
    """Short arguments of the call that produced the result at loop[tool_idx]."""
    tid = loop[tool_idx].get("tool_call_id")
    for m in loop[:tool_idx][::-1]:
        for c in m.get("tool_calls") or []:
            if c.get("id") == tid:
                a = (c.get("function") or {}).get("arguments") or ""
                a = a if isinstance(a, str) else json.dumps(a, ensure_ascii=False)
                return a if len(a) <= 120 else a[:117] + "..."
    return ""


def remove_piece(loop: list, pc: dict, rn: str, lang: str, ntok, keep_line: bool = True) -> None:
    """Replace a piece in place: reasoning -> one line of conclusion (or nothing); result -> a one-line receipt
    (the recall can be repeated: the archive is untouched). A closing note on the result stays on its receipt."""
    m = loop[pc["i"]]
    if pc["kind"] == "reasoning":
        line = conclusion(m.get("reasoning_content")) if keep_line else ""
        loop[pc["i"]] = {**m, "reasoning_content": line, "_reasoning_cut": True}
    else:
        n = ntok(pc["text"])
        rec = (RECEIPT_IT if lang == "it" else RECEIPT_EN).format(rn=rn, args=call_args(loop, pc["i"]), n=n)
        note = m.get("_final_note")
        if isinstance(note, str) and note:
            rec += note
        loop[pc["i"]] = {k: v for k, v in {**m, "content": rec, "_receipt": True}.items() if k != "_src"}


def removal_entry(pc: dict, cause: str, size: int | None = None) -> dict:
    """Journal record of one removal (0.3.5): what (size in tokens, tokens freed), why, from where (records)."""
    srcs = pc.get("src") or []
    return {"kind": pc["kind"], "round": pc["round"], "tokens": size if size is not None else pc.get("tokens"),
            "freed": pc.get("tokens"), "band": pc.get("band"),
            "copy": pc.get("copy") is not None, "protected": bool(pc.get("protected")), "cause": cause,
            "sources": sorted({s[1] for s in srcs if len(s) > 1})[:8], "tool_call_id": pc.get("tcid")}


def find_guard(up):
    """The WindowGuard hooked into an Upstream (through the Tracker / ArchivingUpstream wrappers), or None."""
    for _ in range(4):
        if up is None:
            return None
        g = getattr(up, "window_guard", None)
        if g is not None:
            return g
        up = getattr(up, "up", None)
    return None


def notes_source(guard, phys: list, tools, instruction: dict, kw: dict, min_tokens: int) -> list:
    """Messages for the handoff-notes request: the frozen prompt A when A + instruction + min_tokens fits the window,
    otherwise A shortened from the end (newest messages first: they stay in the tail of B anyway) to a boundary that
    leaves no open tool call. Same prefix as A, so the engine cache is still used."""
    msgs = list(phys)
    if guard is None or not getattr(guard.cfg, "window_guard", True):
        return msgs
    while msgs and guard.room(guard.count(msgs + [instruction], tools, kw)) < min_tokens:
        # drop the newest group: a call with all its results (never an orphan result or an unanswered call)
        m = msgs.pop()
        while m.get("role") == "tool" and msgs:
            m = msgs.pop()
            if m.get("role") != "tool":
                break
    return msgs


def clean(msgs: list) -> list:
    """Messages without the guard's private markers (what is sent to the engine and remembered)."""
    out = []
    for m in msgs:
        if any(k in m for k in PRIVATE):
            m = {k: v for k, v in m.items() if k not in PRIVATE}
        out.append(m)
    return out
