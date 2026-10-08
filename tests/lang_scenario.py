"""Long fake_engine session used by the 0.3.1 language tests and by tools/lang_e2e.py.

It goes through: pinned point, disposable exchange, masking (outputs, reasoning, call arguments), typed receipts,
automatic recall, a recall call by the model, the receipt guard, tools paging, and (second conversation) a segment
switch with handoff notes and the pinned-points block. Only public 0.3.0 APIs are used, so the same script runs on
0.3.0 (golden prefixes) and on 0.3.1.

    run_session(db_dir, cfg_over={}, legacy_until=0) -> list of {"step", "messages", "tools"} sent to the engine

legacy_until=N: the first N steps of each conversation run with prompt_language="it" and the stored language is then
deleted, i.e. the conversation looks exactly like one created by 0.3.0 and continued by 0.3.1.
Author: Maurizio Verde — LastReload
"""
from __future__ import annotations

import json
import os

from ctxproxy.core import Config, Journal, Store, TokenCounter
from ctxproxy.server import Proxy
from ctxproxy.upstream import Upstream
from ctxproxy.fake_engine import FakeEngine

AUTO_HEADS = ("[gestore del contesto: richiamo automatico]", "[context manager: automatic recall]")

SYSTEM = {"role": "system", "content": "You are a coding agent. " * 20}


def fn(name, desc, **props):
    return {"type": "function", "function": {"name": name, "description": desc, "parameters": {
        "type": "object", "properties": {k: {"type": v} for k, v in props.items()}}}}


TOOLS = [fn("read", "read a file", path="string"), fn("bash", "run a command", command="string"),
         fn("edit", "edit a file", path="string", oldText="string", newText="string"),
         fn("write", "write a file", path="string", content="string"),
         fn("todo", "manage the todo list", action="string"), fn("bg_run", "run in background", command="string")]


def call(cid, name, **args):
    return {"id": cid, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


def tool_round(k, size=3500):
    cid = "c%d" % k
    if k % 3 == 1:
        return [{"role": "assistant", "content": "", "reasoning_content": ("thinking about step %d. " % k) * 60,
                 "tool_calls": [call(cid, "write", path="src/f%d.js" % k, content=("let v%d = %d;\n" % (k, k)) * 120)]},
                {"role": "tool", "tool_call_id": cid, "content": "Successfully wrote %d bytes to src/f%d.js" % (1500, k)}]
    if k % 3 == 2:
        return [{"role": "assistant", "content": "", "reasoning_content": ("reading step %d. " % k) * 60,
                 "tool_calls": [call(cid, "read", path="src/f%d.js" % (k - 1))]},
                {"role": "tool", "tool_call_id": cid, "content": ("line %d of src/f%d.js\n" % (k, k - 1)) * (size // 20)}]
    return [{"role": "assistant", "content": "", "reasoning_content": ("testing step %d. " % k) * 60,
             "tool_calls": [call(cid, "bash", command="npm test")]},
            {"role": "tool", "tool_call_id": cid, "content": ("test %d ok\n" % k) * (size // 10) +
             "Tests: %d passed, 1 failed (crash%d)\nexit code 1" % (k, k)}]


def _tool_names(body):
    return [t["function"]["name"] for t in body.get("tools") or []]


def policy(body):
    msgs = body["messages"]
    last = msgs[-1]
    if last.get("role") == "user" and str(last.get("content") or "").startswith(AUTO_HEADS) and len(msgs) > 1:
        last = msgs[-2]                  # automatic recall in the tail: the request is the user message before it
    txt = str(last.get("content") or "")
    names = _tool_names(body)
    rn = next((n for n in names if n in ("recall", "strata_recall")), "recall")
    tn = next((n for n in names if n in ("tools", "strata_tools")), "tools")
    if last.get("role") == "user" and txt.startswith("[strata-context:"):
        return {"role": "assistant", "content": "## Goal\nsnake game\n## Current state\nfiles f1..f9 written"}
    if last.get("role") == "tool":
        cid = last.get("tool_call_id") or ""
        if cid == "g1":
            return {"role": "assistant", "content": "", "tool_calls": [call("g2", "read", path="src/f1.js")]}
        if cid == "p1":
            return {"role": "assistant", "content": "", "tool_calls": [call("p2", "todo", action="list")]}
        if cid == "d1":
            return {"role": "assistant", "content": "", "tool_calls": [call("d2", "bg_run", command="sleep 1")]}
        return {"role": "assistant", "content": "done: " + txt[:40]}
    if last.get("role") != "user":
        return {"role": "assistant", "content": "ok"}
    if "SEARCH" in txt:
        return {"role": "assistant", "content": "", "tool_calls": [call("r1", rn, query="SEED_MAGIC")]}
    if "COPY" in txt:
        rec = next((str(m.get("content")) for m in msgs if m.get("role") == "tool" and
                    "\u27eactx-archive" in str(m.get("content"))), "")
        return {"role": "assistant", "content": "", "tool_calls": [call("g1", "write", path="a.js", content="// " + rec)]}
    if "LIST" in txt:
        return {"role": "assistant", "content": "", "tool_calls": [call("p1", tn, query="todo list")]}
    if "BACKGROUND" in txt:
        return {"role": "assistant", "content": "", "tool_calls": [call("d1", "bg_run", command="sleep 1")]}
    return {"role": "assistant", "content": "ok"}


CFG_A = {"window": 131072, "auto_recall": True, "auto_recall_min_score": 0.5, "auto_recall_min_terms": 1,
         "mask_trigger": 20000, "mask_target": 9000, "keep_recent_tokens": 3000, "min_batch_tokens": 1000,
         "min_mask_tokens": 200, "mask_reasoning": True, "mask_tool_args": True, "tools_paging": True,
         "tools_core": ["read", "bash", "edit", "write"]}
CFG_B = {"window": 40000, "mask_enabled": False, "tail_max": 12000, "reserve": 2000, "default_response": 2000,
         "slot_save": True, "seal_experimental": True, "pins_max_tokens": 8192}


def _client_reply(r, h):
    m = r["choices"][0]["message"]
    out = {"role": "assistant", "content": m.get("content") or ""}
    if m.get("tool_calls"):
        out["tool_calls"] = m["tool_calls"]
    h.append(out)
    for c in m.get("tool_calls") or []:
        h.append({"role": "tool", "tool_call_id": c["id"], "content": "client ran %s" % c["function"]["name"]})


def steps_a(send):
    """Conversation A: pinned point, disposable exchange, masking, receipts, auto recall, recall, guard, paging."""
    h = [SYSTEM, {"role": "user", "content": "\U0001F4CC always use port 8123\ncreate the snake game"}]
    send(h)
    h += [{"role": "assistant", "content": "ok"}, {"role": "user", "content": "~ start the test server"},
          {"role": "assistant", "content": "", "reasoning_content": "starting " * 300,
           "tool_calls": [call("s1", "bash", command="node tools/serve.mjs 8123")]},
          {"role": "tool", "tool_call_id": "s1", "content": "listening on 8123\n" + "log\n" * 900},
          {"role": "assistant", "content": "server started"}, {"role": "user", "content": "go on"},
          {"role": "assistant", "content": "", "tool_calls": [call("c0", "bash", command="cat config.txt")]},
          {"role": "tool", "tool_call_id": "c0", "content": "SEED_MAGIC=27013482\n" + "config line\n" * 150}]
    for k in range(1, 30):
        h += tool_round(k)
    h += [{"role": "assistant", "content": "done"}, {"role": "user", "content": "what was SEED_MAGIC? SEARCH"}]
    for ask in ("now COPY it into a.js", "show me the LIST of tasks", "run it in BACKGROUND", "thanks"):
        _client_reply(send(h), h)
        h.append({"role": "user", "content": ask})
    send(h)


def steps_b(send):
    """Conversation B: pinned point + growth until the segment switch, then more turns on the new segment."""
    h = [SYSTEM, {"role": "user", "content": "\U0001F4CC the board is 20x20\ncreate the snake game"}]
    _client_reply(send(h), h)
    h.append({"role": "user", "content": "go on"})
    for k in range(1, 14):
        h += tool_round(k, 24000)
        send(h)


def run_conv(proxy_factory, steps, legacy_until, eng):
    """Runs one conversation; returns the engine requests of every step."""
    out = []
    st = {"proxy": proxy_factory("it" if legacy_until else None), "n": 0}

    def send(msgs):
        if legacy_until and st["n"] == legacy_until:
            # from here on the conversation looks like one created by 0.3.0: no stored language
            st["proxy"].store.db.execute("DELETE FROM kv WHERE k LIKE 'prompt_language:%'")
            st["proxy"].store.db.close()
            st["proxy"] = proxy_factory(None)
        k0 = len(eng.requests)
        code, r, _ = st["proxy"].chat({"model": "m", "messages": list(msgs), "tools": TOOLS, "max_tokens": 1024})
        assert code == 200, r
        st["n"] += 1
        out.extend({"step": st["n"], "messages": q["messages"], "tools": q.get("tools")} for q in eng.requests[k0:])
        return r

    steps(send)
    st["proxy"].store.db.close()
    return out


def run_session(db_dir: str, cfg_over: dict | None = None, legacy_until: int = 0) -> list:
    eng = FakeEngine(policy)
    url = eng.start()
    res = []
    try:
        for name, base, steps in (("a", CFG_A, steps_a), ("b", CFG_B, steps_b)):
            db = os.path.join(db_dir, name + ".sqlite")

            def factory(lang, base=base, db=db):
                d = {**base, **(cfg_over or {})}
                if lang:
                    d["prompt_language"] = lang
                return Proxy(Config.from_dict(d), Upstream(url), Store(db),
                             Journal(os.path.join(db_dir, name + ".jsonl")), TokenCounter(3.5))
            for r in run_conv(factory, steps, legacy_until, eng):
                res.append({"conv": name, **r})
    finally:
        eng.stop()
    return res


def prompt_text(reqs: list) -> str:
    """All text the engine received (messages + tools), for word checks."""
    return "\n".join(json.dumps({"m": r["messages"], "t": r["tools"]}, ensure_ascii=False) for r in reqs)
