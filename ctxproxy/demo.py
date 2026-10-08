# Author: Maurizio Verde — LastReload
"""`mnemonic-proxy demo`: see the proxy work in under a minute, offline, with no model and no GPU.

A scripted fake model (ctxproxy.fake_engine) runs behind the real proxy code in a temporary folder:

  1. an agent session reads a long build log that contains one identifier;
  2. the session keeps growing until the proxy hides old tool outputs (masking);
  3. we show what the engine actually receives in place of the log (a one-line receipt);
  4. the user asks for the identifier: the model calls `recall`, the proxy resolves it internally, the client only
     sees the final answer;
  5. the archived text returned by `recall` is compared byte for byte with the original output.

What it proves: archive, masking, recall (the proxy's own logic). What it does not prove: the quality of a real
model, tool calling of a real model, saved engine state (KV) — that needs a real engine (README, Tier 1).
Exit code 0 = PASS, 1 = FAIL.
"""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import time

from .core import RECEIPT_OPEN, OUT_PREFIX, Config, Journal, Store, TokenCounter
from .fake_engine import FakeEngine
from .server import Proxy
from .upstream import Upstream

SECRET = "BUILD-7f3a9c-ORCHID-42"
TOOLS = [{"type": "function", "function": {
    "name": "bash", "description": "run a shell command",
    "parameters": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}}}]
SYSTEM = {"role": "system", "content": "You are a coding agent. Use the tools to inspect the project."}
QUESTION = "What was the build id printed in build.log?"


def build_log() -> str:
    lines = []
    for i in range(1, 181):
        lines.append("[%04d] compiling module_%03d.c ... ok (%d ms)" % (i, i, 40 + (i * 37) % 300))
        if i == 97:
            lines.append("[%04d] build id: %s  (keep this for the release notes)" % (i, SECRET))
    return "\n".join(lines) + "\n"


def filler(k: int) -> str:
    return "".join("test_%02d_%03d ... passed in %d.%02ds\n" % (k, j, j % 3, (j * 7) % 100) for j in range(150))


def call(cid: str, cmd: str) -> dict:
    return {"role": "assistant", "content": "", "tool_calls": [
        {"id": cid, "type": "function", "function": {"name": "bash", "arguments": json.dumps({"command": cmd})}}]}


class ScriptedModel:
    """The fake model. It never sees anything the proxy did not send it."""

    def __init__(self):
        self.recall_args = None

    def __call__(self, body: dict) -> dict:
        msgs = body.get("messages") or []
        last = msgs[-1] if msgs else {}
        if last.get("role") == "tool" and str(last.get("content", "")).startswith("[recall"):
            m = re.search(r"build id: (\S+)", last["content"])
            ans = ("The build id was %s (found in the archived build.log)." % m.group(1)) if m else \
                "I could not find it."
            return {"role": "assistant", "content": ans}
        if last.get("role") == "user" and last.get("content") == QUESTION:
            # the log is no longer in the prompt: only the receipt with its id is. Ask the proxy for the exact text.
            for m in msgs:
                c = str(m.get("content") or "")
                if m.get("role") == "tool" and (c.startswith(RECEIPT_OPEN) or c.startswith(OUT_PREFIX)) \
                        and "cat build.log" in c:
                    rid = re.search(r"id=(\w+)", c)
                    if rid is None:
                        continue
                    self.recall_args = {"id": rid.group(1)}
                    return {"role": "assistant", "content": "", "tool_calls": [
                        {"id": "call_recall", "type": "function",
                         "function": {"name": "recall", "arguments": json.dumps(self.recall_args)}}]}
            if any(SECRET in str(m.get("content")) for m in msgs):
                return {"role": "assistant", "content": "The build id was %s (still in my prompt)." % SECRET}
            return {"role": "assistant", "content": "I do not know."}
        return {"role": "assistant", "content": "ok"}


def _p(out, s=""):
    print(s, file=out, flush=True)


def run(out=None, keep: str | None = None) -> bool:
    out = out or sys.stdout
    t0 = time.time()
    checks: list[tuple[str, bool]] = []
    _p(out, "mnemonic-proxy demo — offline, scripted fake model, temporary folder\n")
    _p(out, "Proves: verbatim archive, masking (receipts), recall resolved inside the proxy.")
    _p(out, "Does not prove: real model quality or tool calling, saved engine state (KV). See README, Tier 1.\n")
    tmp = tempfile.TemporaryDirectory(prefix="mnemonic-demo-")
    data = keep or tmp.name
    os.makedirs(data, exist_ok=True)
    model = ScriptedModel()
    eng = FakeEngine(model, flavor="openai")
    url = eng.start(0)
    store = None
    try:
        cfg = Config.from_dict({"window": 16384, "mask_trigger": 6000, "mask_target": 3000,
                                "keep_recent_tokens": 1500, "min_batch_tokens": 1000, "min_age_turns": 2,
                                "reserve": 512, "default_response": 1024, "tail_max": 3000,
                                "notes_max_tokens": 1024, "recall_max_tokens": 6000})
        store = Store(os.path.join(data, "archive.sqlite"))
        proxy = Proxy(cfg, Upstream(url), store, Journal(os.path.join(data, "journal.jsonl")), TokenCounter(3.5))
        log = build_log()
        h = [SYSTEM, {"role": "user", "content": "The release build failed. Check build.log, then run the tests."},
             call("call_log", "cat build.log"), {"role": "tool", "tool_call_id": "call_log", "content": log}]

        def send(msgs):
            st, r, _ = proxy.chat({"model": "local-model", "messages": msgs, "tools": TOOLS, "max_tokens": 512})
            if st != 200:
                raise RuntimeError("proxy returned HTTP %d: %s" % (st, r))
            return r

        _p(out, "1. The agent reads build.log: %d lines, %d chars; line 98 holds the build id %s."
           % (log.count("\n"), len(log), SECRET))
        masked_at = None
        for k in range(1, 13):
            r = send(h)
            ctx = r["strata_context"]
            if ctx["masked"] and masked_at is None:
                masked_at = k
                _p(out, "2. Turn %d: the session holds ~%d tokens; the proxy hid %d old output(s) (~%d tokens)."
                   % (k, ctx["virtual_tokens"], ctx["masked"], ctx["masked_tokens"]))
                break
            h += [call("call_t%d" % k, "pytest -q tests/part%d" % k),
                  {"role": "tool", "tool_call_id": "call_t%d" % k, "content": filler(k)}]
        checks.append(("masking happened", masked_at is not None))
        sent = eng.requests[-1]["messages"]
        seen = next((m for m in sent if m.get("role") == "tool" and m.get("tool_call_id") == "call_log"), None)
        hidden = seen is not None and SECRET not in str(seen.get("content")) and len(str(seen.get("content"))) < 600
        checks.append(("the engine no longer receives build.log", hidden))
        _p(out, "3. What the engine receives in place of build.log (%d chars instead of %d):"
           % (len(str((seen or {}).get("content"))), len(log)))
        _p(out, "     " + str((seen or {}).get("content"))[:400].replace("\n", " "))
        _p(out, "   (receipts are in English for new conversations; conversations born before 0.3.1 keep Italian)")
        n0 = len(eng.requests)
        h += [{"role": "assistant", "content": "Tests pass."}, {"role": "user", "content": QUESTION}]
        r = send(h)
        answer = r["choices"][0]["message"].get("content") or ""
        internal = len(eng.requests) - n0
        _p(out, "4. The user asks: %r" % QUESTION)
        _p(out, "   The model called recall(%s); the proxy answered it itself (%d engine rounds, the client saw one"
           " reply):" % (json.dumps(model.recall_args), internal))
        _p(out, "     " + answer)
        checks.append(("recall resolved inside the proxy", model.recall_args is not None and internal == 2
                       and not r["choices"][0]["message"].get("tool_calls")))
        checks.append(("answer contains the build id", SECRET in answer))
        conv = r["strata_context"]["conversation_id"]
        got = proxy.mgr.recall(conv, model.recall_args or {})
        body = got.split("\n", 1)[1] if "\n" in got else ""
        same = body == log
        checks.append(("recall returns the original output byte for byte", same))
        _p(out, "5. recall(%s) returns %d chars; identical to the original: %s"
           % (json.dumps(model.recall_args), len(body), "yes" if same else "NO"))
    except Exception as e:  # noqa: BLE001
        checks.append(("demo ran without errors: %r" % e, False))
    finally:
        eng.stop()
        if store is not None:
            store.db.close()
        tmp.cleanup()
    ok = bool(checks) and all(c for _, c in checks)
    _p(out)
    for name, c in checks:
        _p(out, "  [%s] %s" % ("ok" if c else "FAIL", name))
    _p(out, "\n%s in %.1f s" % ("PASS" if ok else "FAIL", time.time() - t0))
    if ok:
        _p(out, "Next: a real model on CPU — README, Tier 1 (`mnemonic-proxy check` verifies the setup).")
    return ok


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(prog="mnemonic-proxy demo",
                                 description="Offline demo: archive, masking and recall with a scripted fake model.")
    ap.add_argument("--keep", metavar="DIR", help="keep the demo archive in DIR instead of a temporary folder")
    a = ap.parse_args(argv)
    sys.exit(0 if run(keep=a.keep) else 1)


if __name__ == "__main__":
    main()
