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

  INV-3  a search round is offered only if  P + CALL_ALLOWANCE + RESULT_MIN + F + margin <= W  after removals;
         otherwise the round is the final answer, without the proxy's internal tools, and the model is told why.
         A recall result is cut so that P + result + F + margin <= W (`Proxy._fit_result`).

Termination / why the final call is admissible:
  P0 = prompt prepared before the loop (masking, segments): prepare() switches segment when P0 + response + reserve
  > W. Everything the loop adds is a removable piece; if removing pieces is not enough, whole rounds are dropped,
  oldest first (stage C2), down to prompt = P0. Each round either fits or the loop goes to the final round. If even
  P0 + 1 + margin > W (switch impossible) the request is refused (WindowExceeded, HTTP 400): never sent over.
  Removal is a pure deterministic function of the pieces (same input -> same output).

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
PRIVATE = ("_receipt", "_reasoning_cut", "_final_note", "_keep")
CALL_ALLOWANCE = 256   # INV-3: tokens assumed for the call (+ receipt) of one more search round
RESULT_MIN = 512       # INV-3: a search round is offered only if at least this much result fits after it
RECEIPT_IT = "[{rn} {args}: risultato tolto dal contesto per spazio ({n} token); richiamalo con {rn} se serve]"
RECEIPT_EN = "[{rn} {args}: result removed from the context for space ({n} tokens); call {rn} again if needed]"
FINAL_NOTE_IT = ("\n[gestore del contesto: spazio per altre ricerche esaurito. Rispondi ora con le informazioni già "
                 "recuperate (o di' che non le hai).]")
FINAL_NOTE_EN = ("\n[context manager: no room left for more searches. Answer now with the information already "
                 "retrieved (or say that you do not have it).]")


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
        self.where = ""              # label of the call in progress (journal)

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
            except ValueError:
                pass
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


def pieces(loop: list) -> list[dict]:
    """Removable pieces of the messages the recall loop added: reasoning of each round (kind 'reasoning') and each
    result (kind 'result'). round = index of the round (0 = oldest). Results already turned into receipts and empty
    reasoning are not pieces."""
    out, rnd = [], -1
    for i, m in enumerate(loop):
        if m.get("role") == "assistant":
            rnd += 1
            if isinstance(m.get("reasoning_content"), str) and m["reasoning_content"].strip() \
                    and not m.get("_reasoning_cut"):
                out.append({"i": i, "kind": "reasoning", "round": rnd, "text": m["reasoning_content"]})
        elif m.get("role") == "tool" and not m.get("_receipt") and not m.get("_keep"):
            out.append({"i": i, "kind": "result", "round": rnd, "text": _text(m.get("content"))})
    return out


def asked_idents(pc: dict, question: str) -> set:
    """Identifiers of the piece that also appear in the current question (empty = the piece carries nothing the
    question asks about). A piece with a non-empty set is `protected`: the duplication rule never discounts it and
    it is removed only when nothing else can free the room (0.3.4, ALB w16k: the receipts with `0031`)."""
    return idents(pc["text"]) & idents(question)


def piece_value(pc: dict, question: str, loop: list, n_rounds: int, seen_before: set | None = None) -> float:
    """Value of a piece (higher = keep). Only signals available BEFORE the decision (no recall_use.overlap: it counts
    words like 'recall' as use): kind, age, identifiers of the current request present in the piece (x3),
    duplication (a piece whose identifiers all appeared in an older piece is worth little - but a piece that carries
    identifiers of the current question is NEVER discounted: it is the answer). Reasoning weighs 1/4
    of a result (the model already acted on it; only its extracted conclusion line is kept)."""
    q = idents(question)
    ids = idents(pc["text"])
    dup = bool(ids) and seen_before is not None and ids <= seen_before and not (ids & q)
    v = 3.0 * len(ids & q) + 1.0 + (pc["round"] + 1) / float(max(n_rounds, 1))
    if dup:
        v *= 0.25
    return v * (0.25 if pc["kind"] == "reasoning" else 1.0)


def freed(pc: dict, ntok, rn: str = "recall", lang: str = "en") -> int:
    """Tokens a removal really frees: size minus what replaces it (receipt line / extracted conclusion)."""
    n = ntok(pc["text"])
    if pc["kind"] == "reasoning":
        return max(1, n - ntok(conclusion(pc["text"])))
    return max(1, n - ntok((RECEIPT_IT if lang == "it" else RECEIPT_EN).format(rn=rn, args="", n=n)) - 30)


def removal_order(loop: list, ntok, question: str = "", policy: str = "value", kinds=("reasoning", "result"),
                  rn: str = "recall", lang: str = "en") -> list:
    """Pure, deterministic order in which the pieces are removed (first = removed first).
    age: oldest round first (reasoning before result within a round); reasoning: all reasoning (oldest first) then
    results (oldest first); value: ascending value per freed token, ties -> oldest first.
    Whatever the policy, a piece that carries identifiers of the current question (`protected`: the question asks
    about what the piece contains) is removed LAST: first the pieces without them (reasoning, then results), and the
    results with them only when nothing else can free the room. The window invariant wins over this rule: the loop
    that consumes this order keeps removing until the request fits, so a protected piece is removed too when it is
    the only one left (the journal records it under `protected`).
    Each piece carries `tokens` (freed), `value`, `protected` for the caller's journal."""
    ps = [p for p in pieces(loop) if p["kind"] in kinds]
    n_rounds = sum(1 for m in loop if m.get("role") == "assistant")
    q = idents(question)
    seen: set = set()
    for p in sorted(ps, key=lambda p: p["i"]):
        p["tokens"] = freed(p, ntok, rn, lang)
        ids = idents(p["text"])
        p["protected"] = bool(ids & q)
        p["value"] = piece_value(p, question, loop, n_rounds, seen if p["kind"] == "result" else None)
        if p["kind"] == "result":
            seen |= ids
    if policy == "age":
        key = lambda p: (p["round"], 0 if p["kind"] == "reasoning" else 1, p["i"])
    elif policy == "reasoning":
        key = lambda p: (0 if p["kind"] == "reasoning" else 1, p["round"], p["i"])
    else:
        key = lambda p: (p["value"] / p["tokens"], p["round"], p["i"])
    # 0 in front: the pieces without the question's identifiers go first, the protected ones last
    return sorted(ps, key=lambda p: (1 if p["protected"] else 0,) + key(p))


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
    (the recall can be repeated: the archive is untouched)."""
    m = loop[pc["i"]]
    if pc["kind"] == "reasoning":
        line = conclusion(m.get("reasoning_content")) if keep_line else ""
        loop[pc["i"]] = {**m, "reasoning_content": line, "_reasoning_cut": True}
    else:
        n = ntok(pc["text"])
        rec = (RECEIPT_IT if lang == "it" else RECEIPT_EN).format(rn=rn, args=call_args(loop, pc["i"]), n=n)
        loop[pc["i"]] = {**m, "content": rec, "_receipt": True}


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
