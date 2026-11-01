"""Emergency masking of oversized current tool results, without model inference.

The archived HTTP-26 fixture uses the calibrated counters from the diagnosis replay. It is not a tokenizer-exact test:
24074 is the recorded framed size of the current tool result and 32854 is the recorded prepared prompt estimate.
"""
from __future__ import annotations

import json
import unittest
from pathlib import Path

from ctxproxy.core import Config, Journal, Manager, Store, TokenCounter, chain, content_text, rid_for
from ctxproxy.window import WindowExceeded


FIXTURE = json.loads((Path(__file__).parent / "data" / "oversized_current_tool.json").read_text(encoding="utf-8"))
CONFIG = {
    "window": 32768,
    "mask_trigger": 22000,
    "mask_target": 13000,
    "keep_recent_tokens": 5000,
    "min_batch_tokens": 4000,
    "reserve": 4096,
    "default_response": 8192,
    "tail_max": 8000,
    "notes_max_tokens": 4096,
    "mask_reasoning": True,
    "mask_tool_args": True,
}


class FixtureCounter:
    """Recorded counts for the archived fixture; deterministic length counts elsewhere."""

    exact = False
    tk = None

    def __init__(self, request):
        self.tools_json = json.dumps(request.get("tools") or [], ensure_ascii=False)
        self.system_text = content_text(request["messages"][0].get("content"))
        self.first_user_text = content_text(request["messages"][1].get("content"))
        self.large_tool_text = content_text(request["messages"][44].get("content"))
        self.spec_tool_text = content_text(request["messages"][3].get("content"))
        function = request["messages"][43]["tool_calls"][0]["function"]
        self.current_call_text = (function.get("name") or "") + function["arguments"]
        self.history_delta = 0
        self.assistant_delta = 0

    def count(self, text):
        if not isinstance(text, str) or not text:
            return 0
        if text == self.tools_json or (text.startswith('[{"type": "function"') and '"name": "recall"' in text):
            return 5038  # Recorded system+tools aggregate, assigned to tools by the replay counter.
        if text == self.system_text:
            return 0
        if text == self.large_tool_text:
            return 24068  # Plus msg_overhead=6: the recorded 24074.
        if text == self.spec_tool_text:
            return 200
        if text == self.first_user_text:
            return int(len(text) / 3.5 + 0.5) + self.history_delta
        if text == self.current_call_text:
            return int(len(text) / 3.5 + 0.5) + self.assistant_delta
        return int(len(text) / 3.5 + 0.5)

    def raw(self, text):
        return self.count(text)


class NoInference:
    def __init__(self):
        self.calls = []

    def chat(self, body):
        self.calls.append(body)
        raise AssertionError("The emergency-mask path must not request segment notes or model output")


class TestCurrentToolEmergency(unittest.TestCase):
    def setUp(self):
        self.stores = []

    def tearDown(self):
        for store in self.stores:
            store.db.close()

    def prepare_fixture(self, cfg_over=None, req_over=None):
        cfg = Config.from_dict({**CONFIG, **(cfg_over or {})})
        store = Store(":memory:")
        self.stores.append(store)
        counter = FixtureCounter(FIXTURE)
        mgr = Manager(cfg, store, counter, Journal(None))
        baseline_history = sum(mgr.msg_tokens(m, True) for m in FIXTURE["messages"][1:43])
        counter.history_delta = 3635 - baseline_history
        baseline_assistant = mgr.msg_tokens(FIXTURE["messages"][43], True)
        counter.assistant_delta = 98 - baseline_assistant
        upstream = NoInference()
        request = {**FIXTURE, **(req_over or {})}
        prepared = mgr.prepare(request, upstream=upstream)
        return prepared, mgr, store, upstream

    @staticmethod
    def emergency_events(prepared):
        return [e for e in prepared.events if e.get("event") == "mask" and e.get("emergency") == "current_tool"]

    def test_archived_oversized_tool_keeps_question_budget_and_recall(self):
        p, mgr, store, upstream = self.prepare_fixture()
        messages = FIXTURE["messages"]
        question = content_text(messages[42].get("content"))
        original = content_text(messages[44].get("content"))
        rid = rid_for(44, messages[44])

        self.assertEqual(p.est_tokens, 8885)  # Sanitized fixture result, not tokenizer-exact equivalence.
        self.assertEqual(p.response_limit, 8192)
        self.assertLessEqual(p.est_tokens + p.response_limit + mgr.cfg.reserve + 8, mgr.cfg.window)
        self.assertIn(question, [content_text(m.get("content")) for m in p.messages])
        self.assertEqual(p.messages[-1].get("role"), "tool")
        self.assertEqual(p.messages[-1].get("tool_call_id"), messages[44].get("tool_call_id"))
        self.assertIn(f"{p.recall_name} id={rid}", content_text(p.messages[-1].get("content")))
        self.assertNotEqual(content_text(p.messages[-1].get("content")), original)
        self.assertEqual([e["event"] for e in p.events].count("switch"), 0)
        self.assertFalse([e for e in p.events if e.get("event") in ("response_clamped", "response_no_fit")])
        self.assertEqual(len(self.emergency_events(p)), 1)
        self.assertFalse(upstream.calls)

        archived = store.get(rid, p.conv)
        self.assertIsNotNone(archived)
        self.assertEqual(archived[5], original)
        recalled = mgr.recall(p.conv, {"id": rid, "query": "ORD-2649722c99ed"})
        self.assertIn("ORD-2649722c99ed", recalled)

    def test_recent_tool_at_exact_budget_boundary_is_unchanged(self):
        # 32854 + 8192 + 4096 + 8 == 45150: no emergency is necessary at the exact boundary.
        p, _, _, upstream = self.prepare_fixture({"window": 45150})
        self.assertEqual(p.est_tokens, 32854)
        self.assertEqual(p.response_limit, 8192)
        self.assertEqual(p.messages[-1], FIXTURE["messages"][44])
        self.assertFalse(self.emergency_events(p))
        self.assertFalse(upstream.calls)

    def test_explicit_max_tokens_is_preserved(self):
        p, _, _, _ = self.prepare_fixture(req_over={"max_tokens": 4096})
        event = self.emergency_events(p)[0]
        self.assertEqual(event["response_budget"], 4096)
        self.assertEqual(p.response_limit, 4096)
        self.assertFalse([e for e in p.events if e.get("event") == "response_clamped"])

    def test_residual_no_fit_is_explicit_and_does_not_hide_current_tool(self):
        # The planner reserve can be exhausted even when the guard's smaller margin would allow output.
        cfg = Config.from_dict({**CONFIG, "window": 12000, "segments_enabled": False, "window_guard": False})
        store = Store(":memory:")
        self.stores.append(store)
        mgr = Manager(cfg, store, FixtureCounter(FIXTURE), Journal(None))
        with self.assertRaises(WindowExceeded) as refused:
            mgr.prepare(FIXTURE)
        self.assertEqual(refused.exception.status, 400)
        self.assertFalse(store.masks_for(chain(FIXTURE['messages'], FIXTURE.get('tools'))))

    def test_disabled_recall_does_not_hide_current_evidence(self):
        cfg = Config.from_dict({**CONFIG, "inject_recall": False, "segments_enabled": False})
        store = Store(":memory:")
        self.stores.append(store)
        mgr = Manager(cfg, store, FixtureCounter(FIXTURE), Journal(None))
        with self.assertRaises(WindowExceeded):
            mgr.prepare(FIXTURE)
        self.assertFalse(store.masks_for(chain(FIXTURE['messages'], FIXTURE.get('tools'))))

    def test_parallel_results_mask_only_the_largest_necessary(self):
        calls = [{"id": "a", "type": "function", "function": {"name": "read", "arguments": "{}"}},
                 {"id": "b", "type": "function", "function": {"name": "read", "arguments": "{}"}}]
        messages = [{"role": "system", "content": "s"},
                    {"role": "user", "content": "Keep this literal question."},
                    {"role": "assistant", "content": "", "tool_calls": calls},
                    {"role": "tool", "tool_call_id": "a", "content": "A" * 2400},
                    {"role": "tool", "tool_call_id": "b", "content": "B" * 1200}]
        cfg = Config.from_dict({"window": 3800, "reserve": 100, "default_response": 200,
                                "min_mask_tokens": 20, "mask_trigger": 1000000,
                                "segments_enabled": False, "inject_recall": True, "typed_receipts": False,
                                "prompt_language": "en"})
        store = Store(":memory:")
        self.stores.append(store)
        mgr = Manager(cfg, store, TokenCounter(1), Journal(None))
        p = mgr.prepare({"messages": messages})

        events = self.emergency_events(p)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["count"], 1)
        self.assertEqual(events[0]["ids"], [rid_for(3, messages[3])])
        self.assertIn(rid_for(3, messages[3]), p.messages[-2]["content"])
        self.assertEqual(p.messages[-1], messages[4])
        self.assertEqual(p.messages[1]["content"], "Keep this literal question.")
        self.assertEqual([m["role"] for m in p.messages], ["system", "user", "assistant", "tool", "tool"])
        self.assertEqual(p.response_limit, 200)
        self.assertLessEqual(p.est_tokens + p.response_limit + cfg.reserve + 8, cfg.window)


if __name__ == "__main__":
    unittest.main()
