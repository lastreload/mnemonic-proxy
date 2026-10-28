# Author: Maurizio Verde — LastReload
"""Scalar diagnostic fixture, NOT a prompt replay or an independent tokenizer measurement."""
import json
from pathlib import Path
from types import SimpleNamespace
import unittest

from ctxproxy import core
from ctxproxy.window import WindowGuard

FIXTURE = json.loads((Path(__file__).parent / 'data/budget_gate_0938.json').read_text(encoding='utf-8'))


def diagnostic():
    f = FIXTURE
    obs = f['call']
    final = f['guard_final']
    cfg = core.Config(window=final['window'], window_margin=final['margin'])
    # Inject the recorded estimate only, not fabricated messages or an engine response.
    mgr = SimpleNamespace(tc=core.TokenCounter(), estimate=lambda *_: (final['prompt'], 0))
    guard = WindowGuard(cfg, mgr, core.Journal(None))
    body = {'messages': [], 'max_tokens': obs['max_tokens']}
    estimated = guard.enforce(body)['max_tokens']
    # Counterfactual count supplied from the observed engine prompt length, not a tokenizer.
    mgr.estimate = lambda *_: (obs['prompt_tokens'], 0)
    observed_count_cap = guard.enforce(body)['max_tokens']
    return {'estimated_prompt': final['prompt'], 'observed_prompt': obs['prompt_tokens'],
            'estimated_max_tokens': estimated, 'observed_count_max_tokens': observed_count_cap,
            'over_budget': obs['prompt_tokens'] + estimated + final['margin'] - final['window'],
            'estimate_error': obs['prompt_tokens'] - final['prompt'],
            'fits_with_estimate': final['prompt'] + estimated + final['margin'] <= final['window'],
            'native_output_total': sum(s['generated'] for s in f['native_segments']) + f['wrap_tokens_derived']}


class TestBudgetGateFixture(unittest.TestCase):
    def test_estimate_can_pass_without_any_batch_code(self):
        d = diagnostic()
        self.assertTrue(d['fits_with_estimate'])
        self.assertEqual(d['estimated_max_tokens'], 4096)
        self.assertEqual(d['observed_count_max_tokens'], 3199)
        self.assertEqual(d['over_budget'], 897)
        self.assertEqual(d['estimate_error'], 2484)

    def test_continuation_cache_is_not_initial_prompt_count(self):
        first, last = FIXTURE['native_segments']
        self.assertEqual(first['prompt'], FIXTURE['call']['usage']['prompt_tokens'])
        self.assertEqual(last['prompt'], first['prompt'] + first['generated'] + FIXTURE['wrap_tokens_derived'])
        self.assertEqual(last['cached'] + last['read'], last['prompt'])
        self.assertGreater(last['cached'], first['prompt'])
        self.assertEqual(diagnostic()['native_output_total'], FIXTURE['call']['completion_tokens'])
        self.assertEqual(len(FIXTURE['batch_events']), 4)


if __name__ == '__main__':
    print(json.dumps(diagnostic(), indent=2))
    unittest.main()
