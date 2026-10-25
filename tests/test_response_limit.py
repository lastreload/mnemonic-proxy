"""Output ceilings must not cause repeated segment switches (DarkJezter)."""
import copy
from unittest.mock import patch

from tests.test_proxy import Base, TOOLS


class TestResponseLimit(Base):
    def history(self):
        return [{'role': 'system', 'content': 's' * 27000},
                {'role': 'user', 'content': 'Continue.'}]

    def test_high_ceiling_25_turns_and_one_warning(self):
        msgs = self.history()
        with patch('ctxproxy.core.logging.Logger.warning') as warn:
            for i in range(25):
                response, _ = self.send(msgs, max_tokens=120000)
                msgs += [response['choices'][0]['message'], {'role': 'user', 'content': str(i)}]
            self.assertEqual(warn.call_count, 1)
        self.assertFalse(self.events('switch'))
        self.assertFalse(self.events('mask'))
        self.assertEqual(len(self.events('response_clamped')), 25)
        self.assertEqual(len(self.events('new_conversation')), 1)
        for body in self.eng.requests:
            prompt = self.proxy.wguard.count_body(body)
            self.assertLess(body['max_tokens'], 120000)
            self.assertLessEqual(prompt + body['max_tokens'] + self.proxy.wguard.margin,
                                 self.proxy.cfg.window)

    def test_healthy_limit_unchanged(self):
        self.send(self.history(), max_tokens=4096)
        self.assertEqual(self.eng.requests[-1]['max_tokens'], 4096)
        self.assertFalse(self.events('response_clamped'))
        self.assertFalse(self.events('switch'))

    def test_both_fields_follow_engine_precedence_and_input_is_unchanged(self):
        req = {'messages': self.history(), 'tools': TOOLS, 'max_tokens': 4096,
               'max_completion_tokens': 120000}
        original = copy.deepcopy(req)
        status, _, _ = self.proxy.chat(req)
        self.assertEqual(status, 200)
        self.assertEqual(req, original)
        sent = self.eng.requests[-1]
        self.assertEqual(sent['max_tokens'], sent['max_completion_tokens'])
        self.assertLess(sent['max_tokens'], 120000)
        self.assertFalse(self.events('switch'))

    def test_small_completion_limit_wins_over_large_max_tokens(self):
        self.send(self.history(), max_tokens=120000, max_completion_tokens=512)
        self.assertEqual(self.eng.requests[-1]['max_completion_tokens'], 512)
        self.assertFalse(self.events('switch'))

    def test_default_budget_is_explicit(self):
        self.proxy.chat({'messages': self.history(), 'tools': TOOLS})
        self.assertEqual(self.eng.requests[-1]['max_tokens'], self.proxy.cfg.default_response)

    def test_large_prompt_still_attempts_segment_switch(self):
        self.proxy.cfg.mask_enabled = False
        msgs = self.history() + [{'role': 'assistant', 'content': 'a' * 390000},
                                 {'role': 'user', 'content': 'Continue.'}]
        with patch.object(self.proxy.mgr, '_switch', return_value=[]) as switch:
            self.proxy.mgr.prepare({'messages': msgs, 'max_tokens': 120000}, upstream=self.proxy.up)
        switch.assert_called_once()

    def test_floor_never_raises_client_ceiling(self):
        self.proxy.cfg.response_floor = 16384
        self.send(self.history(), max_tokens=128)
        self.assertEqual(self.eng.requests[-1]['max_tokens'], 128)

    def test_clamping_without_segments_and_warning_survives_manager_restart(self):
        from ctxproxy.core import Manager
        self.proxy.cfg.segments_enabled = False
        req = {'messages': self.history(), 'max_completion_tokens': 120000}
        with patch('ctxproxy.core.logging.Logger.warning') as warn:
            prepared = self.proxy.mgr.prepare(req, upstream=self.proxy.up)
            replacement = Manager(self.proxy.cfg, self.proxy.store, self.proxy.tc, self.proxy.journal)
            again = replacement.prepare(req, upstream=self.proxy.up)
            self.assertEqual(warn.call_count, 1)
        self.assertEqual(prepared.conv, again.conv)
        self.assertLess(again.response_limit, 120000)
        self.assertFalse(self.events('switch'))

    def test_overfull_prompt_is_never_sent_without_segments(self):
        self.proxy.cfg.mask_enabled = False
        self.proxy.cfg.segments_enabled = False
        status, _, _ = self.proxy.chat({'messages': [{'role': 'user', 'content': 'x' * 600000}],
                                       'max_tokens': 120000})
        self.assertEqual(status, 400)
        self.assertFalse(self.eng.requests)
