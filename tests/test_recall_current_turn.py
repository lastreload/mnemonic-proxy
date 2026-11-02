"""Issue: recall refused the ids of blocks masked AFTER the last user message (same agentic turn).

A single user prompt followed by many tool calls is the normal shape of `opencode run`, of a Hermes
worker and of any headless agent. The server passes `max_idx=last_user` to every recall, and the
archive filter is `idx < max_idx`, so every block the proxy masked during THAT turn -- normal masking
and `emergency=current_tool` alike -- had an id the model could read in the receipt but could not
resolve until a NEW user message arrived. Sibling branches (edits, regenerations) must stay excluded.

No GPU, no inference: fake engine only.   python3 -m unittest -v tests.test_recall_current_turn
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ctxproxy.core import (Config, Journal, Manager, Store, TokenCounter, aid_for, content_text,  # noqa: E402
                           is_human_user, rid_for)

SYSTEM = {"role": "system", "content": "You are a coding agent. " * 20}
PROMPT = ("Read src/core.py and src/server.py completely with several read calls and then write "
          "docs/01-architecture.md naming the exact functions.")
TOOLS = [{"type": "function", "function": {"name": "read", "description": "read a file",
                                           "parameters": {"type": "object",
                                                          "properties": {"path": {"type": "string"},
                                                                         "offset": {"type": "integer"}}}}}]
NEEDLE = "def out_placeholder(self, info, orig, n_orig, rid, lang):"


def read_round(k: int, size: int, path: str = "src/core.py", needle: str = ""):
    """One assistant read call plus its (large) result, as a client sends them back."""
    cid = "call_%d" % k
    body = ("line %d of %s\n" % (k, path)) * (size // 24)
    if needle:
        body += needle + "\n"
    return [{"role": "assistant", "content": "", "reasoning_content": "step %d" % k,
             "tool_calls": [{"id": cid, "type": "function",
                             "function": {"name": "read",
                                          "arguments": json.dumps({"path": path, "offset": k * 4000})}}]},
            {"role": "tool", "tool_call_id": cid, "content": body}]


def single_turn(n_rounds: int, size: int = 3500):
    """ONE human message, then n_rounds of tool traffic: the shape of a headless agent run."""
    msgs = [SYSTEM, {"role": "user", "content": PROMPT}]
    for k in range(n_rounds):
        msgs += read_round(k, size, needle=NEEDLE if k == 2 else "")
    return msgs


class Base(unittest.TestCase):
    cfg_over: dict = {}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        cfg = Config.from_dict({"window": 131072, "mask_trigger": 12000, "mask_target": 6000,
                                "keep_recent_tokens": 3000, "min_mask_tokens": 200,
                                "prompt_language": "en", **self.cfg_over})
        self.store = Store(os.path.join(self.tmp.name, "a.sqlite"))
        self.mgr = Manager(cfg, self.store, TokenCounter(3.5), Journal(None))

    def tearDown(self):
        self.store.db.close()
        self.tmp.cleanup()

    @staticmethod
    def last_user(msgs):
        """Exactly what server.py:_chat_managed computes and passes as max_idx."""
        return max((i for i, m in enumerate(msgs) if is_human_user(m)), default=None)

    def prepared(self, msgs):
        p = self.mgr.prepare({"messages": msgs, "tools": TOOLS})
        return p, self.last_user(msgs)


class TestIdOfSameTurn(Base):
    def test_masked_output_id_resolves_inside_the_same_turn(self):
        msgs = single_turn(35)
        p, lu = self.prepared(msgs)
        self.assertEqual(lu, 1)                       # a single human message: every block is "after" it
        self.assertTrue([e for e in p.events if e.get("event") == "mask"], "the fixture must mask")
        rid = rid_for(3, msgs[3])                     # the first masked tool output of this same turn
        self.assertIsNotNone(self.store.get(rid, p.conv), "the text must be in the archive")
        out = self.mgr.recall(p.conv, {"id": rid}, max_idx=lu, max_tokens=4000)
        self.assertNotIn("not found", out)
        self.assertIn("line 0 of src/core.py", out)

    def test_receipt_id_in_the_prompt_is_the_id_that_resolves(self):
        """The id the model actually reads: whatever receipt the proxy wrote into the prompt."""
        msgs = single_turn(35)
        p, lu = self.prepared(msgs)
        ids = set()
        for m in p.messages:
            for tok in content_text(m.get("content")).replace("=", " ").split():
                if len(tok) == 13 and tok[0] in "rta" and all(c in "0123456789abcdef" for c in tok[1:]):
                    ids.add(tok)
        self.assertTrue(ids, "no archive id in the prepared prompt")
        unresolved = [i for i in sorted(ids)
                      if "not found" in self.mgr.recall(p.conv, {"id": i}, max_idx=lu, max_tokens=4000)]
        self.assertEqual(unresolved, [], "receipt ids the model cannot resolve in its own turn")

    def test_arguments_id_and_output_id_behave_the_same(self):
        msgs = single_turn(35)
        p, lu = self.prepared(msgs)
        aid, rid = aid_for(2, msgs[2]), rid_for(3, msgs[3])
        a_out = self.mgr.recall(p.conv, {"id": aid}, max_idx=lu, max_tokens=4000)
        r_out = self.mgr.recall(p.conv, {"id": rid}, max_idx=lu, max_tokens=4000)
        self.assertEqual("not found" in a_out, "not found" in r_out,
                         "arguments ids and output ids must be reachable under the same rule")
        self.assertNotIn("not found", a_out)

    def test_current_user_message_stays_excluded(self):
        """The question is in the prompt in full: its own id must not become a recall target."""
        msgs = single_turn(35)
        p, lu = self.prepared(msgs)
        out = self.mgr.recall(p.conv, {"id": rid_for(1, msgs[1])}, max_idx=lu, max_tokens=4000)
        self.assertIn("not found", out)


class TestEmergencyMaskOfSameTurn(Base):
    # mask_trigger out of reach: only _mask_current_tools (emergency=current_tool) can act here.
    cfg_over = {"window": 24000, "mask_trigger": 10 ** 9, "keep_recent_tokens": 20000, "reserve": 1000,
                "default_response": 4000, "segments_enabled": False, "window_guard": False}

    def test_emergency_masked_current_tool_is_recallable(self):
        """emergency=current_tool masks the LAST message: idx >= last_user by construction."""
        huge = read_round(99, 60000, path="build.log")
        msgs = single_turn(4) + huge
        p, lu = self.prepared(msgs)
        emergency = [e for e in p.events if e.get("event") == "mask" and e.get("emergency") == "current_tool"]
        self.assertTrue(emergency, "the fixture must trigger the emergency mask")
        rid = rid_for(len(msgs) - 1, msgs[-1])
        self.assertIn(rid, emergency[-1]["ids"])
        out = self.mgr.recall(p.conv, {"id": rid}, max_idx=lu, max_tokens=6000)
        self.assertNotIn("not found", out)
        self.assertIn("line 99 of build.log", out)


class TestQueryAndPathOfSameTurn(Base):
    def test_query_finds_text_masked_in_the_same_turn(self):
        msgs = single_turn(35)
        p, lu = self.prepared(msgs)
        out = self.mgr.recall(p.conv, {"query": NEEDLE}, max_idx=lu, max_tokens=4000)
        # The "no match" notice echoes the query, so assert on text only the archived block carries.
        self.assertIn("line 2 of src/core.py", out)

    def test_path_timeline_lists_operations_of_the_same_turn(self):
        msgs = single_turn(35)
        p, lu = self.prepared(msgs)
        ops = self.store.fileops(p.conv, "src/core.py", max_idx=lu)
        self.assertTrue(ops, "the file operations of this turn must be visible to recall path=...")
        out = self.mgr.recall(p.conv, {"path": "src/core.py"}, max_idx=lu, max_tokens=4000)
        self.assertIn("src/core.py", out)


class TestStructuredRecallOfSameTurn(Base):
    cfg_over = {"recall_struct": True, "recall_multi": True, "recall_flex": True}

    def test_struct_id_and_queries_reach_the_same_turn(self):
        msgs = single_turn(35)
        p, lu = self.prepared(msgs)
        rid = rid_for(3, msgs[3])
        out = self.mgr.recall(p.conv, {"id": rid}, max_idx=lu, max_tokens=4000)
        self.assertNotIn("not found", out)
        self.assertIn("line 0 of src/core.py", out)
        many = self.mgr.recall(p.conv, {"queries": [NEEDLE, "line 4 of src/core.py"]}, max_idx=lu, max_tokens=4000)
        self.assertIn("line 4 of src/core.py", many)

    def test_mode_output_treats_both_id_kinds_alike(self):
        """`around` applied no chain filter at all: that is why a... answered and r... did not."""
        msgs = single_turn(35)
        p, lu = self.prepared(msgs)
        aid, rid = aid_for(2, msgs[2]), rid_for(3, msgs[3])
        for i in (aid, rid):
            self.assertNotIn("not found", self.mgr.recall(p.conv, {"id": i, "mode": "output"},
                                                          max_idx=lu, max_tokens=4000))
        # and an id of another conversation stays unknown on this path too
        self.assertIn("not found", self.mgr.recall(p.conv, {"id": "r000000000000", "mode": "output"},
                                                   max_idx=lu, max_tokens=4000))


class TestSiblingBranchStaysExcluded(Base):
    def test_abandoned_branch_block_is_not_reachable(self):
        """A regenerated turn writes different text at the same indices: the old text is a sibling branch."""
        first = single_turn(35)
        p1, _ = self.prepared(first)
        dead = rid_for(5, first[5])
        self.assertIsNotNone(self.store.get(dead, p1.conv))

        other = list(first[:4])
        for k in range(4, 35):
            other += read_round(k, 3500, path="src/other.py")
        p2, lu2 = self.prepared(other)
        self.assertEqual(p1.conv, p2.conv, "the regeneration must stay in the same conversation")
        live = rid_for(5, other[5])
        self.assertNotIn("not found", self.mgr.recall(p2.conv, {"id": live}, max_idx=lu2, max_tokens=4000))
        self.assertIn("not found", self.mgr.recall(p2.conv, {"id": dead}, max_idx=lu2, max_tokens=4000),
                      "a block of an abandoned sibling branch must stay out of reach")
        # The query echo repeats the words, so assert on the id: no hit may come from the abandoned branch
        # (indices 0-3 are shared by both branches and legitimately reachable).
        hits = []
        self.mgr.recall(p2.conv, {"query": "line 5 of src/core.py"}, max_idx=lu2, meta=hits, max_tokens=4000)
        self.assertNotIn(dead, [h.get("rid") for h in hits])


class TestSiblingBranchOnTheStructuredPath(Base):
    cfg_over = {"recall_struct": True}

    def test_mode_output_does_not_bypass_the_chain(self):
        """`around` applied no chain filter: an abandoned-branch id answered there while the same id was
        refused on the paging path. The two paths must agree."""
        first = single_turn(35)
        p1, _ = self.prepared(first)
        dead = aid_for(4, first[4])
        self.assertIsNotNone(self.store.get(dead, p1.conv))

        other = list(first[:4])
        for k in range(4, 35):
            other += read_round(k, 3500, path="src/other.py")
        p2, lu2 = self.prepared(other)
        self.assertEqual(p1.conv, p2.conv)
        self.assertNotIn("not found", self.mgr.recall(p2.conv, {"id": aid_for(4, other[4]), "mode": "output"},
                                                      max_idx=lu2, max_tokens=4000))
        self.assertIn("not found", self.mgr.recall(p2.conv, {"id": dead, "mode": "output"},
                                                   max_idx=lu2, max_tokens=4000))


class TestThroughTheServer(Base):
    """The whole round trip: the model calls recall on a receipt id in the MIDDLE of its own turn
    (the last message of the request is a tool result, no new human message), and the proxy must
    answer with the archived text. This is the path that passes max_idx=last_user in server.py."""

    def setUp(self):
        super().setUp()
        from ctxproxy.fake_engine import FakeEngine
        from ctxproxy.server import Proxy
        from ctxproxy.upstream import Upstream
        self.asked: list[str] = []
        self.eng = FakeEngine(self.policy)
        url = self.eng.start()
        cfg = Config.from_dict({"window": 131072, "mask_trigger": 12000, "mask_target": 6000,
                                "keep_recent_tokens": 3000, "min_mask_tokens": 200, "prompt_language": "en"})
        self.jpath = os.path.join(self.tmp.name, "j.jsonl")
        self.proxy = Proxy(cfg, Upstream(url), self.store, Journal(self.jpath), TokenCounter(3.5))

    def tearDown(self):
        self.eng.stop()
        super().tearDown()

    def policy(self, body):
        """First the archive id of a masked output, taken from the prompt the proxy just built; then the answer."""
        msgs = body["messages"]
        last = msgs[-1]
        txt = content_text(last.get("content"))
        if last.get("role") == "tool" and txt.startswith("[recall"):
            return {"role": "assistant", "content": "recalled: " + txt.split("\n", 1)[-1][:40]}
        for m in msgs:
            for tok in content_text(m.get("content")).replace("=", " ").split():
                if len(tok) == 13 and tok[0] == "r" and all(c in "0123456789abcdef" for c in tok[1:]):
                    self.asked.append(tok)
                    return {"role": "assistant", "content": "",
                            "tool_calls": [{"id": "rc1", "type": "function",
                                            "function": {"name": "recall",
                                                         "arguments": json.dumps({"id": tok})}}]}
        return {"role": "assistant", "content": "no id in the prompt"}

    def test_recall_in_the_middle_of_the_turn_returns_the_text(self):
        msgs = single_turn(35)
        st, r, _ = self.proxy.chat({"model": "m", "messages": msgs, "tools": TOOLS, "max_tokens": 1024})
        self.assertEqual(st, 200, r)
        self.assertTrue(self.asked, "the fake model found no archive id in the prompt")
        with open(self.jpath, encoding="utf-8") as f:
            ev = [json.loads(x) for x in f]
        self.assertTrue([e for e in ev if e["event"] == "mask"], "the fixture must mask")
        rec = [e for e in ev if e["event"] == "recall"]
        self.assertEqual(len(rec), 1, rec)
        self.assertTrue(rec[0]["results"], "the recall of the current turn came back empty: %s" % rec[0])
        answer = r["choices"][0]["message"]["content"]
        self.assertTrue(answer.startswith("recalled: line "), answer)


if __name__ == "__main__":
    unittest.main()
