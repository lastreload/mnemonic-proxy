"""An empty engine session must not turn optional runtime saves into request failures."""
from unittest import mock

from tests.test_proxy import Base, history, tool_round


class TestEmptySessionRuntime(Base):
    def setUp(self):
        super().setUp()
        self.eng.reject_empty_save = True

    def lose_session_before_save(self, action, *args, **kwargs):
        if action == "save":
            # Simulate session loss after prefill but before the HTTP save.
            self.eng.live = ""
        return self.original_slot(action, *args, **kwargs)

    def test_anchor_save_error_keeps_request_working(self):
        cfg = self.proxy.cfg
        cfg.mask_anchor, cfg.slot_save, cfg.segments_enabled = True, True, False
        cfg.anchor_min_tokens, cfg.min_batch_tokens = 500, 3000
        self.original_slot = self.proxy.up.slot
        h = history(1)
        with mock.patch.object(self.proxy.up, "slot", side_effect=self.lose_session_before_save):
            for k in range(1, 40):
                h += tool_round(k, 3500)
                self.send(h)
                if self.events("anchor_error"):
                    break
        errors = self.events("anchor_error")
        self.assertTrue(errors)
        self.assertEqual(errors[-1]["step"], "save")
        self.assertIn("no complete session to save", errors[-1]["error"])
        self.assertFalse(self.events("anchor_save"))
        req = self.events("request")[-1]
        self.assertIsNone(self.proxy.store.anchor(req["conv"], 0))
        self.assertTrue(any(b.get("max_tokens") == 1 for b in self.eng.requests))
        self.send(h)

    def test_segment_save_error_keeps_switch_working(self):
        cfg = self.proxy.cfg
        cfg.window, cfg.mask_enabled, cfg.tail_max = 40000, False, 12000
        cfg.reserve, cfg.default_response, cfg.slot_save = 2000, 2000, True
        self.original_slot = self.proxy.up.slot
        h = history(1) + [{"role": "assistant", "content": "done"}, {"role": "user", "content": "continue"}]
        with mock.patch.object(self.proxy.up, "slot", side_effect=self.lose_session_before_save):
            for k in range(1, 14):
                h += tool_round(k, 12000)
                self.send(h, max_tokens=1000)
                if self.events("switch"):
                    break
        self.assertTrue(self.events("switch"))
        self.assertIn("no complete session to save", self.events("save_error")[-1]["error"])
        self.assertFalse(self.events("save"))
        self.send(h, max_tokens=1000)
