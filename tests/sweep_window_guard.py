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
"""
from __future__ import annotations

import argparse
import json
import os
import random
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
    state = {"k": None, "answers": [], "where": []}
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
        residual = (not offered) and state["k"] is not None and state["k"] < rounds and rng.random() < 0.3
        if state["k"] is not None and state["k"] < rounds and (offered or residual):
            state["k"] += 1
            calls = []
            for j in range(rng.randint(1, 3)):
                q = {"query": "svc-%d" % rng.randint(1, 30)} if rng.random() < 0.7 else \
                    {"id": "r" + "%012x" % rng.getrandbits(48)}
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

    eng = FakeEngine(policy, n_ctx=w)
    eng.strict_window = True
    exact = bool(tokdir)
    if exact:
        ref = TokenCounter(3.5, tokdir)

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
        if not exact:
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
        statuses.append(send(px, h, client_mt, stream))
        ev = []
        if os.path.exists(jp):
            with open(jp) as f:
                ev = [json.loads(l) for l in f]
        wg = [e for e in ev if e["event"] == "window_guard"]
        floor = px._answer_floor({"max_tokens": client_mt} if client_mt else {})
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
            "answer_min_max_tokens": min([a["max_tokens"] for a in state["answers"]] or [0]),
            "exact": exact and not est_proxy, "secs": round(time.time() - t0, 2)})
        px.store.db.close()
    except Exception as e:  # noqa: BLE001
        res["exception"] = repr(e)[:500]
    finally:
        eng.stop()
        tmp.cleanup()
    return res


def send(px, h, mt, stream):
    req = {"model": "m", "messages": list(h), "tools": TOOLS}
    if mt:
        req["max_tokens"] = mt
    if stream:
        st, _, _ = px.chat(req, emit=lambda c: None)
    else:
        st, _, _ = px.chat(req)
    return st


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
    return 0 if summ["violations"] == 0 and summ["exceptions"] == 0 else 1


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
         "per_window": {}}
    for w in WINDOWS:
        rs = [r for r in rows if r["window"] == w]
        s["per_window"][w] = {"scenarios": len(rs), "requests": tot("requests", rs), "violations": tot("violations", rs),
                              "client_400": tot("client_400", rs), "finals": tot("finals", rs),
                              "removes": tot("removes", rs), "answer_below_floor": tot("answer_below_floor", rs)}
    return s


if __name__ == "__main__":
    sys.exit(main())
