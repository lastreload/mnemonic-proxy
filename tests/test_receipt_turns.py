"""Absolute originating turns and immutable published output receipts."""
import copy
import os
import tempfile
import unittest
from unittest.mock import patch

from ctxproxy.core import (Config, Manager, Store, Journal, TokenCounter, chain,
                           output_turns, rid_for, DROP_SUFFIX)
from tests import test_paging


def call(cid, name="read"):
    return {"role": "assistant", "content": "", "tool_calls": [
        {"id": cid, "type": "function", "function": {
            "name": name, "arguments": '{"path":"file.txt"}'}}]}


class ReceiptTurns(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(os.path.join(self.tmp.name, "archive.sqlite"))
        self.addCleanup(self.store.db.close)
        self.mgr = Manager(Config(receipt_turns=True), self.store, TokenCounter(),
                           Journal(os.path.join(self.tmp.name, "journal.jsonl")))
        self.msgs = [{"role": "system", "content": "system"}]
        for n in range(1, 5):
            self.msgs.extend([{"role": "user", "content": "request %d" % n}, call(str(n)),
                              {"role": "tool", "tool_call_id": str(n), "content": "line\n" * 100}])

    def mask(self, msgs, indices):
        hs = chain(msgs, None)
        self.store.add_masks([(hs[i], "conv", i, rid_for(i, msgs[i]), 500, 0, 400) for i in indices])
        return hs

    def test_call_turn_not_result_turn_and_wrappers(self):
        msgs = [call("pre"), {"role": "tool", "tool_call_id": "pre", "content": "x"},
                {"role": "user", "content": "one"}, call("a"),
                {"role": "user", "content": "<tool_response>x</tool_response>"}, call("b"),
                {"role": "user", "content": "two"},
                {"role": "tool", "tool_call_id": "a", "content": "x"},
                {"role": "tool", "tool_call_id": "b", "content": "x"}]
        self.assertEqual(output_turns(msgs), [None, None, 1, 1, 1, 1, 2, 1, 1])

    def test_localized_typed_and_fallback(self):
        for lang in ("it", "en"):
            for typed in (True, False):
                self.mgr.lang = lang
                self.mgr.cfg.typed_receipts = typed
                hs = self.mask(self.msgs, [3, 6, 9, 12])
                # Separate receipt identity for each independent experiment.
                self.store.db.execute("DELETE FROM kv WHERE k LIKE 'output-receipt:%'")
                phys, _ = self.mgr.build(self.msgs, hs, 0, None)
                results = [m["content"] for m in phys if m["role"] == "tool"]
                for n, text in enumerate(results, 1):
                    self.assertIn(("turno %d" if lang == "it" else "turn %d") % n, text)
                self.assertEqual(self.msgs[3]["content"], "line\n" * 100)

    def test_stored_bytes_survive_growth_segments_seal_and_restart(self):
        hs = self.mask(self.msgs, [3, 6, 9, 12])
        first, _ = self.mgr.build(self.msgs, hs, 0, None)
        extended = self.msgs + [{"role": "user", "content": "new request"}]
        # If build recomputes a receipt, fail immediately rather than compare only bytes.
        with patch.object(self.mgr, "out_placeholder", side_effect=AssertionError("recomputed")):
            again, _ = self.mgr.build(extended, chain(extended, None), 0, None)
        self.assertEqual(first, again[:len(first)])
        # A segment beginning at turn 3 retains the absolute number 3, with seal notes.
        self.store.add_segment("conv", 1, 7, hs[6], "sealed handoff", "seal")
        self.assertEqual(self.mgr._segment_for("conv", hs)[1], 7)
        restarted = Manager(Config(receipt_turns=False, typed_receipts=False), self.store,
                            TokenCounter(), self.mgr.journal)
        restarted.lang = "it"
        with patch.object(restarted, "out_placeholder", side_effect=AssertionError("recomputed")):
            tail, _ = restarted.build(self.msgs, hs, 7, "sealed handoff")
        tools = [m for m in tail if m["role"] == "tool"]
        self.assertIn("turn 3", tools[0]["content"])
        self.assertEqual(tools, [m for m in first if m["role"] == "tool"][2:])

    def test_new_segment_first_render_counts_from_start(self):
        hs = self.mask(self.msgs, [9, 12])
        phys, _ = self.mgr.build(self.msgs, hs, 7, "handoff")
        self.assertIn("turn 3", next(m["content"] for m in phys if m["role"] == "tool"))

    def test_disabled_new_receipts_are_legacy_and_stay_frozen(self):
        self.mgr.cfg.receipt_turns = False
        hs = self.mask(self.msgs, [3])
        first, _ = self.mgr.build(self.msgs, hs, 0, None)
        self.assertNotIn("turno 1", first[3]["content"])
        self.mgr.cfg.receipt_turns = True
        again, _ = self.mgr.build(self.msgs, hs, 0, None)
        self.assertEqual(first, again)

    def test_disposable_exchange_localized_and_frozen(self):
        for lang in ("it", "en"):
            self.mgr.lang = lang
            self.store.db.execute("DELETE FROM kv WHERE k LIKE 'output-receipt:%'")
            hs = chain(self.msgs, None)
            self.store.add_masks([(hs[4] + DROP_SUFFIX, "conv", 4, rid_for(4, self.msgs[4]), 500, 0, 400)])
            first, _ = self.mgr.build(self.msgs, hs, 0, None)
            self.assertIn("turno 2" if lang == "it" else "turn 2", first[4]["content"])
            with patch.object(self.mgr, "exchange_receipts", side_effect=AssertionError("recomputed")):
                again, _ = self.mgr.build(self.msgs, hs, 0, None)
            self.assertEqual(first, again)

    def test_recall_keeps_original_body_and_identifier(self):
        # Actual prepare archives the full original; masking changes only physical text.
        helper = test_paging.TestReceiptsInManager()
        self.mgr.cfg = Config.from_dict({"window": 131072, "mask_trigger": 4000,
            "mask_target": 2000, "keep_recent_tokens": 800, "min_mask_tokens": 100,
            "min_batch_tokens": 200, "receipt_turns": True})
        msgs = helper.session()
        original = copy.deepcopy(msgs)
        p = self.mgr.prepare({"messages": msgs, "max_tokens": 100})
        rows = self.store.db.execute("SELECT rid, idx, content FROM archive WHERE role='tool'").fetchall()
        self.assertTrue(rows)
        for rid, idx, body in rows:
            self.assertEqual(rid, rid_for(idx, original[idx]))
            self.assertEqual(body, original[idx]["content"])
        rid, idx, body = rows[0]
        recalled = self.mgr.recall(p.conv, {"id": rid, "mode": "output", "max_tokens": 10000})
        self.assertIn(body, recalled)
        self.assertEqual(msgs, original)


if __name__ == "__main__":
    unittest.main()
