# Author: Maurizio Verde — LastReload
"""0.3.3 window guard - blanket sweep (BRIEF-WINDOW-GUARD test 2): random scenarios with fixed seeds against a strict
worst-case fake engine. Any request with prompt + max_tokens > n_ctx is a violation (the engine rejects it with 400).

The engine always generates the maximum: reasoning up to the thinking budget (or max_tokens - 64), then recall calls
(1-3 per round, sometimes mixed with a client tool call, sometimes a residual recall in the final round) or the
answer. Scenarios vary W in {8K, 16K, 24K, 32K, 128K}, history length and tool-output sizes, client max_tokens,
stream / non-stream, answerfix on/off, leaf selector on/off, removal policy, reasoning masking.

    python3 tests/sweep_window_guard.py --n 2000 --workers 16 [--tokdir DIR] [--out sweep.json]

--tokdir: pack tokenizer: proxy AND engine count exactly (engine count is independent: render_pieces + its own
tokenizer instance). Without it both use the chars/3.5 counter (plumbing only, guarantee=estimated).
--estimated-proxy: engine exact, proxy WITHOUT tokenizer (measures the estimated mode; violations possible).

0.3.5 (correction 2): ~30% of the recall calls ask for a REAL archive id seen in the prompt (whole, a page, id +
query) or repeat an earlier call of the same answer, so verified copies and distinct deliveries are exercised.
Failure criteria (exit 1 on any): a request with prompt + max_tokens + margin > W (the engine checks with the margin);
an exception; an answer below F when F was reachable (no floor_short); evidence lost - a recall result the model has
SEEN that a later request of the same answer no longer carries, while its lines are not all still present in another
result (engine-side check, independent of the proxy's bookkeeping); a false deduplication (such a loss on a piece the
proxy removed as a copy); the tokenizer not loaded in every scenario (with --tokdir); an exact-count error; no scenario
with >= 2 distinct recall results delivered together.

0.3.5 (final recall retry): with probability SWEEP_RESIDUAL (default 0.3) the fake model calls recall also in a round
where it is not offered (the final round, and again in the one new attempt). Failure criteria: an avoidable empty
answer - the client gets no content and no call while the new attempt was neither impossible (final_recall_retry
used=false: it did not fit with F) nor answered by recall again (recall_dropped retry=true); no retry exercised at all.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
import tempfile
import time
from multiprocessing import Pool

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("MNEMONIC_PROXY_ALLOW_SMALL_WINDOW", "1")

from ctxproxy import engines  # noqa: E402
from ctxproxy.core import RECALL_NAME, Config, Journal, Store, TokenCounter  # noqa: E402
from ctxproxy.fake_engine import FakeEngine  # noqa: E402
from ctxproxy.render import render_pieces, template_kwargs  # noqa: E402
from ctxproxy.server import Proxy  # noqa: E402
from ctxproxy.upstream import Upstream  # noqa: E402

WINDOWS = (8192, 16384, 24576, 32768, 131072)
# probability that the fake model calls recall in a round where it is NOT offered (final round, and the one new attempt)
RESIDUAL = float(os.environ.get("SWEEP_RESIDUAL", "0.3"))
TOOLS = [{"type": "function", "function": {"name": "bash", "description": "run a command",
                                           "parameters": {"type": "object",
                                                          "properties": {"command": {"type": "string"}}}}}]
WORDS = ("alpha", "beta", "gamma", "delta", "port", "host", "error", "request", "shard", "latency", "status", "user")


def scaled_cfg(w: int, rng: random.Random) -> dict:
    """The thresholds engines.setup would scale for a window w (defaults are for 131072)."""
    base = Config()
    ratio = w / float(engines.BASE_WINDOW)
    d = {"window": w}
    for f in engines.SCALED:
        v = getattr(base, f, None)
        if isinstance(v, int) and v > 0:
            nv = max(64, engines.SCALED_MIN.get(f, 0) if ratio < 1 else 0, int(v * ratio))
            d[f] = min(nv, v) if ratio < 1 else nv
    d["mask_reasoning"] = rng.random() < 0.5
    d["recall_leaf_select"] = rng.random() < 0.5
    d["recall_removal"] = rng.choice(("value", "age", "reasoning"))
    d["max_recall_rounds"] = rng.choice((2, 4, 6))
    if os.environ.get("SWEEP_GUARD_OFF"):
        d["window_guard"] = False
    if os.environ.get("SWEEP_SLACK"):
        d["window_estimate_slack"] = float(os.environ["SWEEP_SLACK"])
    if rng.random() < 0.5:
        d["answer_thinking_after_recall"] = True
        d["answer_thinking_budget"] = rng.choice((0, 512, 2048, 8192))
    if rng.random() < 0.3:
        d["truncation_guard"] = True
    if rng.random() < 0.3:
        d["response_floor"] = rng.choice((1024, 4096))
    if rng.random() < 0.3:
        d["recall_struct"] = d["recall_flex"] = d["recall_multi"] = True
    return d


def text_block(rng: random.Random, k: int, tokens: int) -> str:
    lines, n = [], 0
    while n < tokens * 3.5:
        ln = "svc-%d %s %s id-%d-%04d port %d %s\n" % (k, rng.choice(WORDS), rng.choice(WORDS), k, rng.randint(0, 9999),
                                                      rng.randint(30000, 39999), rng.choice(WORDS))
        lines.append(ln)
        n += len(ln)
    return "".join(lines)


def run_one(args) -> dict:
    seed, tokdir, est_proxy = args
    rng = random.Random(seed)
    w = rng.choice(WINDOWS)
    cfgd = scaled_cfg(w, rng)
    stream = rng.random() < 0.5
    client_mt = rng.choice((None, 256, 1024, 2048, 4096, 16384))
    rounds = rng.randint(0, 8)
    res = {"seed": seed, "window": w, "stream": stream, "client_max_tokens": client_mt, "rounds_asked": rounds,
           "policy": cfgd["recall_removal"], "answerfix": bool(cfgd.get("answer_thinking_after_recall")),
           "leaf": cfgd["recall_leaf_select"]}
    tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
    state = {"k": None, "answers": [], "where": [], "asked": []}
    holder = {}

    def policy(body):
        msgs = body["messages"]
        last = msgs[-1]
        names = {((t or {}).get("function") or {}).get("name") for t in body.get("tools") or []}
        mt = int(body.get("max_completion_tokens") or body.get("max_tokens") or 0)
        rb = body.get("reasoning_budget_tokens")
        where = holder["px"].wguard.where if "px" in holder else ""
        if mt <= 1:
            return {"role": "assistant", "content": "."}
        # worst case: reasoning fills the budget (or max_tokens - 64)
        rtok = max(0, min(int(rb) if isinstance(rb, int) and rb > 0 else mt, mt - 64))
        reasoning = ("considering svc and ports carefully. " * (int(rtok * 3.5) // 37 + 1))[:int(rtok * 3.5)]
        if last.get("role") == "user" and "QUESTION" in str(last.get("content")):
            state["k"] = 0
        offered = RECALL_NAME in names
        residual = (not offered) and state["k"] is not None and state["k"] < rounds and rng.random() < RESIDUAL
        if state["k"] is not None and state["k"] < rounds and (offered or residual):
            state["k"] += 1
            calls = []
            real = sorted(set(re.findall(r"\br[0-9a-f]{12}\b", json.dumps(msgs))))
            for j in range(rng.randint(1, 3)):
                u = rng.random()
                if u < 0.15 and state["asked"]:
                    q = dict(rng.choice(state["asked"]))                        # repeat an earlier call
                elif u < 0.30 and real:
                    rid = rng.choice(real)                                      # a real archive id
                    q = rng.choice(({"id": rid}, {"id": rid, "offset": rng.choice((0, 600, 2000))},
                                    {"id": rid, "query": "svc-%d" % rng.randint(1, 30)}))
                elif u < 0.79:
                    q = {"query": "svc-%d" % rng.randint(1, 30)}
                else:
                    q = {"id": "r" + "%012x" % rng.getrandbits(48)}
                state["asked"].append(q)
                calls.append({"id": "rc%d_%d" % (state["k"], j), "type": "function",
                              "function": {"name": RECALL_NAME, "arguments": json.dumps(q)}})
            if rng.random() < 0.1:
                calls.append({"id": "cb%d" % state["k"], "type": "function",
                              "function": {"name": "bash", "arguments": json.dumps({"command": "ls"})}})
            return {"role": "assistant", "content": "", "reasoning_content": reasoning, "tool_calls": calls}
        if where in ("answer", "recall_round", "final"):
            state["answers"].append({"where": where, "max_tokens": mt, "rb": rb})
        ans = ("the answer is port 30007. " * 40)[:max(1, int((mt - rtok) * 3.5))]
        return {"role": "assistant", "content": ans, "reasoning_content": reasoning}

    margin = int(cfgd.get("window_margin", 64))
    eng = FakeEngine(policy, n_ctx=w - margin)     # 0.3.5: the engine rejects prompt + max_tokens + margin > W
    eng.strict_window = True
    exact = bool(tokdir)
    ref = TokenCounter(3.5, tokdir) if exact else None
    if exact and ref.exact:
        def engine_count(body):
            msgs = [{k: v for k, v in m.items() if k != "extra"} for m in body.get("messages") or []]
            return sum(ref.raw(t) for _, t in render_pieces(msgs, body.get("tools"), template_kwargs(body)))
        eng.counter = engine_count
    url = eng.start()
    t0 = time.time()
    try:
        cfg = Config.from_dict(cfgd)
        tc = TokenCounter(3.5, tokdir if exact and not est_proxy else None)
        jp = os.path.join(tmp.name, "j.jsonl")
        px = Proxy(cfg, Upstream(url), Store(os.path.join(tmp.name, "a.sqlite")), Journal(jp), tc)
        holder["px"] = px
        if eng.counter is None:
            # no usable tokenizer (module missing or no --tokdir): the proxy counts with its own estimate, so the
            # engine must use the same counter, or the sweep reports overflows that are only a counting mismatch
            eng.counter = lambda body: px.wguard.count_body(body)
        h = [{"role": "system", "content": "you are a coding agent. " * rng.randint(1, 40)},
             {"role": "user", "content": "inspect the services"}]
        statuses = []
        steps = rng.randint(2, 30)
        big = rng.choice((200, 800, 3000, 8000))
        for k in range(1, steps + 1):
            cid = "call_%d" % k
            am = {"role": "assistant", "content": "", "tool_calls": [{"id": cid, "type": "function", "function": {
                "name": "bash", "arguments": json.dumps({"command": "cat svc-%d.log" % k})}}]}
            if rng.random() < 0.5:
                am["reasoning_content"] = "step %d: " % k + "thinking " * rng.randint(10, 600)
            h += [am, {"role": "tool", "tool_call_id": cid, "content": text_block(rng, k, rng.randint(20, big))},
                  {"role": "assistant", "content": "read svc-%d" % k}, {"role": "user", "content": "next"}]
            if rng.random() < 0.5 or k == steps:
                statuses.append(send(px, h, client_mt, stream))
        h.append({"role": "user", "content": "QUESTION: which port did svc-%d id-%d use? use recall if needed"
                  % (rng.randint(1, steps), rng.randint(0, 9999))})
        n0 = len(eng.requests)
        st_q, r_q = send(px, h, client_mt, stream, full=True)
        statuses.append(st_q)
        turn = eng.requests[n0:]
        lost = info_lost(turn)
        distinct = max([len({str(m.get("content")) for m in b["messages"] if m.get("role") == "tool"
                             and str(m.get("tool_call_id", "")).startswith("rc") and RECEIPT_MARK not in
                             str(m.get("content"))}) for b in turn] or [0])
        ev = []
        if os.path.exists(jp):
            with open(jp) as f:
                ev = [json.loads(l) for l in f]
        wg = [e for e in ev if e["event"] == "window_guard"]
        floor = px._answer_floor({"max_tokens": client_mt} if client_mt else {})
        floor_short = any(e.get("action") == "floor_short" for e in wg)
        copies = [x for e in wg for k in ("removed_search", "removed_final", "newest_group", "c2")
                  for x in (e.get(k) or []) if x.get("copy")]
        copy_ids = {x.get("tool_call_id") for x in copies}
        # 0.3.5 (final recall retry): an empty answer to the question is admissible only when the one new attempt was
        # not possible (it did not fit: used=false) or the model called recall again in it (recall_dropped retry)
        qmsg = ((r_q or {}).get("choices") or [{}])[0].get("message") or {}
        empty = st_q == 200 and not (qmsg.get("content") or "").strip() and not qmsg.get("tool_calls")
        fr = [e for e in ev if e["event"] == "final_recall_retry"]
        again = [e for e in ev if e["event"] == "recall_dropped" and e.get("retry")]
        explained = any(not e.get("used") for e in fr) or bool(again)
        res.update({"final_recall_retry": len(fr), "retry_used": sum(1 for e in fr if e.get("used")),
                    "retry_no_room": sum(1 for e in fr if not e.get("used")), "retry_recall_again": len(again),
                    "retry_reasons": sorted({e.get("reason") for e in fr}),
                    "empty_answer": int(empty), "empty_avoidable": int(empty and not explained),
                    "final_recall_dropped": sum(1 for e in ev if e["event"] == "recall_dropped" and e.get("final")
                                                and "reason" not in e        # not the calls left without room
                                                and not e.get("mixed") and not e.get("retry")
                                                and not e.get("retry_no_room"))})
        res.update({
            "requests": eng.checked, "violations": len(eng.violations), "violation_detail": eng.violations[:3],
            "guard_violations": px.wguard.violations, "refused": sum(1 for e in wg if e.get("action") == "refused"),
            "client_status": statuses, "client_400": sum(1 for s in statuses if s == 400),
            "client_other_err": sum(1 for s in statuses if s not in (200, 400)),
            "clamps": sum(1 for e in wg if e.get("action") == "clamp"),
            "removes": sum(1 for e in wg if e.get("action") == "remove"),
            "finals": sum(1 for e in wg if e.get("action") == "final"),
            "budget_caps": sum(1 for e in wg if e.get("action") == "budget_cap"),
            "notes_source": sum(1 for e in wg if e.get("action") == "notes_source"),
            "switches": sum(1 for e in ev if e["event"] == "switch"),
            "recalls": sum(1 for e in ev if e["event"] == "recall"),
            "recall_empty": sum(1 for e in ev if e["event"] == "recall" and not e.get("tokens")),
            "floor": floor, "answers": len(state["answers"]),
            "answer_below_floor": sum(1 for a in state["answers"] if a["max_tokens"] < floor),
            "floor_short": floor_short,
            "answer_below_floor_admissible": 0 if floor_short else
            sum(1 for a in state["answers"] if a["max_tokens"] < floor),
            "evidence_lost": len(lost), "lost_ids": lost[:5],
            "false_dedup": sum(1 for t in lost if t in copy_ids),
            "copies_removed": len(copies),
            "journal_evidence_lost": max([e.get("evidence_lost") or 0 for e in wg] or [0]),
            "noncopy_search": sum(e.get("noncopy_search") or 0 for e in wg),
            "c2": sum(len(e.get("c2") or []) for e in wg),
            "newest_group": sum(len(e.get("newest_group") or []) for e in wg),
            "distinct_delivered": distinct,
            "count_errors": getattr(px.wguard, "count_errors", 0),
            "answer_min_max_tokens": min([a["max_tokens"] for a in state["answers"]] or [0]),
            "exact": exact and not est_proxy, "tokenizer": bool(ref and ref.exact), "secs": round(time.time() - t0, 2)})
        px.store.db.close()
    except Exception as e:  # noqa: BLE001
        res["exception"] = repr(e)[:500]
    finally:
        eng.stop()
        tmp.cleanup()
    return res


RECEIPT_MARK = "result removed from the context for space"


def _lines(text: str) -> list:
    """Information lines of a recall result: header/notice lines out, the line marker of id+query out."""
    out = []
    for l in (text or "").splitlines():
        l = l.strip().lstrip(">").strip()
        if not l or l.startswith("[") or l.startswith("--- id=") or l == "\u22ee":
            continue
        out.append(l)
    return out


def info_lost(requests: list) -> list:
    """Engine-side check, independent of the proxy's bookkeeping (0.3.5): the recall results the model has SEEN
    (present in a request of this answer) that a later request no longer carries verbatim, while some of their
    information lines are not present in any other result still there. Returns their tool_call ids."""
    seen, lost = {}, []
    for body in requests:
        cur = {m.get("tool_call_id"): str(m.get("content") or "") for m in body["messages"] if m.get("role") == "tool"}
        for tid, txt in seen.items():
            now = cur.get(tid)
            if (now is not None and now.startswith(txt)) or tid in lost:
                continue
            others = "\n".join(re.sub(r"(?m)^\s*>", "", v) for k, v in cur.items() if k != tid and RECEIPT_MARK not in v)
            if all(l in others for l in _lines(txt)) and _lines(txt):
                continue                       # every line still present in another result: a real copy
            if not _lines(txt) and any(v == txt for k, v in cur.items() if k != tid):
                continue
            lost.append(tid)
        for tid, txt in cur.items():
            if tid and str(tid).startswith("rc") and RECEIPT_MARK not in txt and tid not in seen:
                seen[tid] = txt
    return lost


def send(px, h, mt, stream, full=False):
    req = {"model": "m", "messages": list(h), "tools": TOOLS}
    if mt:
        req["max_tokens"] = mt
    if stream:
        st, r, _ = px.chat(req, emit=lambda c: None)
    else:
        st, r, _ = px.chat(req)
    return (st, r) if full else st


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=2000)
    ap.add_argument("--seed0", type=int, default=1000)
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--tokdir", default="")
    ap.add_argument("--estimated-proxy", action="store_true")
    ap.add_argument("--out", default="")
    a = ap.parse_args()
    t0 = time.time()
    jobs = [(a.seed0 + i, a.tokdir, a.estimated_proxy) for i in range(a.n)]
    with Pool(a.workers) as pool:
        rows = pool.map(run_one, jobs, chunksize=4)
    summ = summarize(rows)
    summ["secs"] = round(time.time() - t0, 1)
    summ["args"] = vars(a)
    if a.out:
        with open(a.out, "w") as f:
            json.dump({"summary": summ, "rows": rows}, f, indent=1)
    print(json.dumps(summ, indent=1))
    return 0 if not summ["failures"] else 1


def summarize(rows):
    def tot(k, rs=rows):
        return sum(r.get(k, 0) or 0 for r in rs)
    s = {"scenarios": len(rows), "requests": tot("requests"), "violations": tot("violations"),
         "guard_violations": tot("guard_violations"), "exceptions": sum(1 for r in rows if "exception" in r),
         "refused": tot("refused"), "client_400": tot("client_400"), "client_other_err": tot("client_other_err"),
         "clamps": tot("clamps"), "removes": tot("removes"), "finals": tot("finals"), "budget_caps": tot("budget_caps"),
         "notes_source": tot("notes_source"), "switches": tot("switches"), "recalls": tot("recalls"),
         "recall_empty": tot("recall_empty"), "answers": tot("answers"),
         "answer_below_floor": tot("answer_below_floor"),
         "scenarios_with_violation": [r["seed"] for r in rows if r.get("violations")][:20],
         "scenarios_with_exception": [(r["seed"], r["exception"]) for r in rows if "exception" in r][:5],
         "tokenizer_loaded": sum(1 for r in rows if r.get("tokenizer")),
         "floor_short": sum(1 for r in rows if r.get("floor_short")),
         "answer_below_floor_admissible": tot("answer_below_floor_admissible"),
         "evidence_lost": tot("evidence_lost"), "false_dedup": tot("false_dedup"),
         "copies_removed": tot("copies_removed"), "journal_evidence_lost": tot("journal_evidence_lost"),
         "noncopy_search": tot("noncopy_search"), "c2": tot("c2"), "newest_group": tot("newest_group"),
         "count_errors": tot("count_errors"),
         "final_recall_retry": tot("final_recall_retry"), "retry_used": tot("retry_used"),
         "retry_no_room": tot("retry_no_room"), "retry_recall_again": tot("retry_recall_again"),
         "retry_space": sum(1 for r in rows if "space" in (r.get("retry_reasons") or [])),
         "retry_round_limit": sum(1 for r in rows if "round_limit" in (r.get("retry_reasons") or [])),
         "empty_answer": tot("empty_answer"), "empty_avoidable": tot("empty_avoidable"),
         "final_recall_dropped_without_retry": tot("final_recall_dropped"),
         "scenarios_empty_avoidable": [r["seed"] for r in rows if r.get("empty_avoidable")][:20],
         "scenarios_distinct_ge2": sum(1 for r in rows if (r.get("distinct_delivered") or 0) >= 2),
         "scenarios_with_loss": [(r["seed"], r.get("lost_ids")) for r in rows if r.get("evidence_lost")][:20],
         "per_window": {}}
    for w in WINDOWS:
        rs = [r for r in rows if r["window"] == w]
        s["per_window"][w] = {"scenarios": len(rs), "requests": tot("requests", rs), "violations": tot("violations", rs),
                              "client_400": tot("client_400", rs), "finals": tot("finals", rs),
                              "removes": tot("removes", rs), "answer_below_floor": tot("answer_below_floor", rs),
                              "evidence_lost": tot("evidence_lost", rs), "copies_removed": tot("copies_removed", rs),
                              "distinct_ge2": sum(1 for r in rs if (r.get("distinct_delivered") or 0) >= 2)}
    fail = []
    if s["violations"]:
        fail.append("over the window (prompt + max_tokens + margin > W)")
    if s["exceptions"]:
        fail.append("exceptions")
    if s["answer_below_floor_admissible"]:
        fail.append("final answer below F although F was reachable")
    if s["evidence_lost"]:
        fail.append("evidence seen by the model lost")
    if s["false_dedup"]:
        fail.append("false deduplication")
    if s["count_errors"]:
        fail.append("exact count errors")
    if s["empty_avoidable"]:
        fail.append("avoidable empty answers (final round of internal calls only, no new attempt)")
    if not os.environ.get("SWEEP_GUARD_OFF") and not s["retry_used"]:
        fail.append("no final recall retry exercised")
    if not s["scenarios_distinct_ge2"]:
        fail.append("no scenario with >= 2 distinct recall results delivered")
    if any(r.get("exact") for r in rows) and s["tokenizer_loaded"] != len(rows):
        fail.append("tokenizer not loaded in every scenario")
    s["failures"] = fail
    return s


if __name__ == "__main__":
    sys.exit(main())
