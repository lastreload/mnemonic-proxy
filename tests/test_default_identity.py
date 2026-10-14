# Author: Maurizio Verde — LastReload
"""With the default config the new options (recall_leaf_select, answerfix) change nothing: the recall text and what
reaches the engine are byte-identical to 0.3.2 (7ff7e7f), except for the paging fix of recall by id (a cut page no
longer carries the truncation notice and its offsets are exact; see tests/test_recall_paging.py). The golden file
tests/data/recall_default_golden.json was produced by running this module with --compute on the 7ff7e7f tree
(compute() uses only 0.3.2 APIs) and regenerated with that fix (53 cut-page outputs changed, nothing else).

    python3 -m unittest -v tests.test_default_identity
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
GOLDEN = os.path.join(ROOT, "tests", "data", "recall_default_golden.json")

# recall arguments tried on every format: id (start of the text), id + offset, id + query, query only
ARGS = [{"id": "{rid}"}, {"id": "{rid}", "offset": 200}, {"id": "{rid}", "query": "{needle}"}, {"query": "{needle}"}]
# default config; small cap (the text does not fit); structured recall (recall2) with a small cap
CONFIGS = [{}, {"recall_max_tokens": 300}, {"recall_struct": True, "recall_flex": True, "recall_multi": True,
                                             "recall_max_tokens": 300}]


def compute(inputs: dict) -> dict:
    """Outputs of the recall with the default options, for each format/config/args. Uses only APIs that exist in
    0.3.2 (no recall_leaf_select field), so it runs unchanged on the 7ff7e7f tree to produce the golden file."""
    from ctxproxy import core
    out = {}
    for fmt, (text, needle) in sorted(inputs["formats"].items()):
        rid = "r0123456789ab"
        rows = [("u000000000001", "c", 0, "user", "", "show the file", 3, 0),
                ("r0123456789ab", "c", 1, "tool", "bash", text, max(1, len(text) // 3), 0)]
        for ci, cfg_over in enumerate(CONFIGS):
            cfg = core.Config(**cfg_over)
            st = core.Store(":memory:")
            st.archive_many(rows)
            m = core.Manager(cfg, st, core.TokenCounter(cfg.chars_per_token, None), core.Journal(None))
            m.question = "what is %s in the file?" % needle      # set by the server when the option is on
            for ai, a in enumerate(ARGS):
                args = {k: v.format(rid=rid, needle=needle) if isinstance(v, str) else v for k, v in a.items()}
                out["%s|cfg%d|args%d" % (fmt, ci, ai)] = m.recall("c", args, max_idx=5)
    out["server"] = server_round(inputs["formats"]["yaml"][0], {"window_guard": False})
    out["server"].pop("_guard", None)
    return out


def server_round(text: str, over: dict | None = None, n_ctx: int = 12288) -> dict:
    """One full proxy turn with the default config: the model asks recall(id) of a big output in a small window
    (12K: the server's budget check cuts the result). Returns what the engine received in the round after.
    over: extra config (0.3.3: the golden is the 0.3.2 path, window_guard off; see test_guard_on_small_window)."""
    from ctxproxy.core import RECALL_NAME, Config, Journal, Store, TokenCounter
    from ctxproxy.server import Proxy
    from ctxproxy.upstream import Upstream
    from tests.fake_engine import FakeEngine
    from tests.test_proxy import TOOLS
    got, state = {}, {"k": None}

    def policy(body):
        msgs = body["messages"]
        last = msgs[-1]
        if last.get("role") == "user" and "port" in str(last.get("content")):
            state["k"] = 0
        if last.get("role") == "tool" and state["k"] == 1:
            got["tool"] = str(last.get("content"))
            got["keys"] = sorted(k for k in body if k not in ("messages", "tools"))
            got["max_tokens"] = body.get("max_tokens")
            state["k"] = 2
        if state["k"] == 0:
            import re
            ids = re.findall(r"r[0-9a-f]{12}", json.dumps(msgs))
            if not ids:                            # big window: nothing archived, the output is in the prompt
                state["k"] = 2
                return {"role": "assistant", "content": "ok"}
            rid = ids[0]
            state["k"] = 1
            return {"role": "assistant", "content": "", "tool_calls": [
                {"type": "function", "function": {"name": RECALL_NAME, "arguments": json.dumps({"id": rid})}}]}
        return {"role": "assistant", "content": "ok"}

    tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
    eng = FakeEngine(policy, n_ctx=n_ctx)
    url = eng.start()
    try:
        cfg = Config.from_dict({"window": n_ctx, "allow_small_window": True, **(over or {})})
        got["_guard"] = None
        proxy = Proxy(cfg, Upstream(url), Store(os.path.join(tmp.name, "a.sqlite")),
                      Journal(os.path.join(tmp.name, "j.jsonl")), TokenCounter(3.5))
        got["_guard"] = proxy.wguard
        h = [{"role": "system", "content": "you are a coding agent"},
             {"role": "user", "content": "Run: cat services.yaml"},
             {"role": "assistant", "content": "", "tool_calls": [{"id": "call_cat", "type": "function", "function": {
                 "name": "bash", "arguments": json.dumps({"command": "cat services.yaml"})}}]},
             {"role": "tool", "tool_call_id": "call_cat", "content": text * 4},
             {"role": "assistant", "content": "Read."}]
        for k in range(6):
            h += [{"role": "user", "content": "step %d" % k},
                  {"role": "assistant", "content": "", "tool_calls": [{"id": "call_s%d" % k, "type": "function",
                                                                       "function": {"name": "bash", "arguments":
                                                                                    json.dumps({"command": "ls"})}}]},
                  {"role": "tool", "tool_call_id": "call_s%d" % k, "content": "auth 1\nbilling 1\n" * 40},
                  {"role": "assistant", "content": "Done %d." % k}]
            proxy.chat({"model": "m", "messages": h + [{"role": "user", "content": "ok"}], "tools": TOOLS,
                        "max_tokens": 1024})
        h.append({"role": "user", "content": "Without re-running: what is the port of the payments replica?"})
        st, r, _ = proxy.chat({"model": "m", "messages": h, "tools": TOOLS, "max_tokens": 1024})
        got["status"] = st
        got["answer"] = r["choices"][0]["message"].get("content") if st == 200 else r
        if n_ctx != 12288:                         # 0.3.3 identity test: every request the engine received
            got["_requests"] = json.dumps(eng.requests, sort_keys=True)
        proxy.store.db.close()
    finally:
        eng.stop()
        tmp.cleanup()
    return got


class TestDefaultIdentity(unittest.TestCase):
    def test_options_are_off(self):
        from ctxproxy import core
        c = core.Config()
        self.assertFalse(c.recall_leaf_select)
        self.assertFalse(c.answer_thinking_after_recall)
        self.assertFalse(c.truncation_guard)

    def test_recall_and_server_byte_identical_to_0_3_2(self):
        with open(GOLDEN, encoding="utf-8") as f:
            g = json.load(f)
        got = compute(g["inputs"])
        self.assertEqual(set(got), set(g["expected"]))
        self.assertGreaterEqual(len({k.split("|")[0] for k in got if "|" in k}), 12)   # every format
        for k in sorted(g["expected"]):
            self.assertEqual(got[k], g["expected"][k], k)
        self.assertEqual(got["server"]["status"], 200)
        self.assertIn("tool", got["server"])
        self.assertRegex(got["server"]["tool"][-200:], "truncated")   # the server-side cut is exercised too

    def test_guard_on_small_window(self):
        """0.3.3, window guard on (default): the recall result is no longer cut by the old double reserve (0.3.2 left
        it ~0 tokens in 12K); it gets what fits with the answer floor, and the next request respects INV-1."""
        with open(GOLDEN, encoding="utf-8") as f:
            g = json.load(f)
        got = server_round(g["inputs"]["formats"]["yaml"][0])
        wg = got.pop("_guard")
        self.assertEqual(got["status"], 200)
        self.assertGreater(len(got["tool"]), len(g["expected"]["server"]["tool"]))
        self.assertEqual((wg.violations, wg.refused), (0, 0))

    def test_guard_identity_at_128k(self):
        """0.3.3, test 4: where the room abounds (128K) the guard changes nothing: same requests, byte for byte."""
        with open(GOLDEN, encoding="utf-8") as f:
            g = json.load(f)
        text = g["inputs"]["formats"]["yaml"][0]
        on = server_round(text, n_ctx=131072)
        off = server_round(text, {"window_guard": False}, n_ctx=131072)
        wg = on.pop("_guard")
        off.pop("_guard")
        self.assertEqual(on, off)
        self.assertEqual((wg.violations, wg.refused), (0, 0))


if __name__ == "__main__":
    if sys.argv[1:2] == ["--compute"]:          # used to produce the golden file on the 0.3.2 tree
        inp = json.load(open(sys.argv[2], encoding="utf-8"))
        json.dump(compute(inp), sys.stdout, ensure_ascii=False)
    else:
        unittest.main()
