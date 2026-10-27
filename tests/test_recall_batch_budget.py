# Author: Maurizio Verde — LastReload
"""CPU-only batch allocation regressions with verbatim AgentLongBench 0726 outputs."""
import json
import pathlib
import tempfile
import unittest

from ctxproxy import core, window
from ctxproxy.fake_engine import FakeEngine
from ctxproxy.server import Proxy
from ctxproxy.upstream import Upstream

DATA = pathlib.Path(__file__).parent / 'data'
ROWS = json.loads((DATA / 'recall_batch_0726.json').read_text(encoding='utf-8'))
WRONG, RIGHT, CORRECT_FIRST = 'rd8f4f1664d84', 'r25c664b8d449', 'raeba4acd2c5c'


def run_batch(args, *, filler=26000, rounds=None, proxy_cls=Proxy, ceiling=6000, guard=True,
              max_rounds=4, cpt=1.34):
    """Scripted engine, real archive bodies, deterministic counting, strict request-window checks."""
    with tempfile.TemporaryDirectory() as d:
        received = []
        scripts = [args] + (rounds or [])
        step = [0]

        def policy(body):
            received.append(body)
            i = step[0]
            step[0] += 1
            if i < len(scripts):
                return {'role': 'assistant', 'content': '', 'tool_calls': [
                    {'id': 'call-%d-%d' % (i, j), 'type': 'function', 'function': {
                        'name': core.RECALL_NAME, 'arguments': json.dumps(a)}} for j, a in enumerate(scripts[i])]}
            return {'role': 'assistant', 'content': 'done'}

        eng = FakeEngine(policy, n_ctx=32768)
        store = core.Store(':memory:')
        try:
            cfg = core.Config(recall_struct=True, recall_flex=True, recall_multi=True, window=32768,
                              response_floor=4096, slot_save=False, recall_max_tokens=ceiling,
                              window_guard=guard, max_recall_rounds=max_rounds)
            tc = core.TokenCounter(chars_per_token=cpt)
            px = proxy_cls(cfg, Upstream(eng.start()), store, core.Journal(str(pathlib.Path(d, 'j'))), tc)
            history = [{'role': 'system', 'content': 'You are an assistant.' + ' x' * filler},
                       {'role': 'user', 'content': 'Check rounds 18 and 24 for 0726.'}]
            req = {'model': 'm', 'messages': history, 'max_tokens': 4096}
            prep = px.mgr.prepare(req)
            for r in ROWS:
                store.archive_many([(r['rid'], prep.conv, 0, r['role'], r['name'], r['content'], r['tokens'], 0)])
            eng.counter = px.wguard.count_body
            eng.strict_window = True
            status, response, _ = px.chat(req)
            assert status == 200, response
            assert not eng.violations, eng.violations
            for b in eng.requests:
                assert px.wguard.count_body(b) + b.get('max_tokens', 0) + px.wguard.margin <= 32768
            events = [json.loads(l) for l in pathlib.Path(d, 'j').read_text(encoding='utf-8').splitlines()]
            outputs = [m['content'] for m in eng.requests[-1]['messages'] if m.get('role') == 'tool']
            return outputs, events, received
        finally:
            eng.stop()
            store.db.close()


class TestBatchBudget(unittest.TestCase):
    def test_0726_retains_next_round_and_exact_pages(self):
        outputs, events, requests = run_batch([{'id': WRONG}, {'id': RIGHT}], filler=11000)
        recalls = [e for e in events if e['event'] == 'recall']
        self.assertEqual(len(recalls), 2)
        budgets = [e for e in events if e['event'] == 'recall_batch_budget']
        self.assertEqual(len(budgets), 2)
        self.assertTrue(all(e['future'] > 0 for e in budgets))
        for rec, out, budget in zip(recalls, outputs, budgets):
            self.assertLessEqual(rec['tokens'], budget['cap'])
            m = rec['results'][0]
            text = next(r['content'] for r in ROWS if r['rid'] == m['rid'])
            self.assertIn(text[m['offset']:m['offset'] + m['chars']], out)
            self.assertIn('offset=%d' % (m['offset'] + m['chars']), out)
            self.assertGreaterEqual(rec['tokens'], window.RESULT_R)
        self.assertFalse(any(e.get('reason') == 'space' for e in events))
        self.assertTrue(requests[-1].get('tools'))
        self.assertEqual(requests[-1]['max_tokens'], 4096)

    def test_correction_with_late_query_in_next_round(self):
        _, events, _ = run_batch([{'id': WRONG}, {'id': RIGHT}], filler=11000,
                                rounds=[[{'id': CORRECT_FIRST, 'query': '0726'},
                                         {'id': RIGHT, 'query': '0726'}]])
        recalls = [e for e in events if e['event'] == 'recall']
        self.assertEqual(len(recalls), 4)
        for rec in recalls[-2:]:
            m = rec['results'][0]
            text = next(r['content'] for r in ROWS if r['rid'] == m['rid'])
            self.assertIn('0726', text[m['offset']:m['offset'] + m['chars']])

    def test_single_id_bytes_match_0313(self):
        gold = json.loads((DATA / 'recall_single_0313.json').read_text(encoding='utf-8'))
        for case in gold:
            with self.subTest(args=case['args'], filler=case['filler']):
                outputs, events, _ = run_batch([case['args']], filler=case['filler'])
                self.assertEqual([s.encode() for s in outputs], [s.encode() for s in case['outputs']])
                self.assertFalse(any(e['event'] == 'recall_batch_budget' for e in events))

    def test_many_ids_small_window_budget_and_last_round(self):
        for n in (2, 3, 8, 20):
            for filler in (11000, 11500):
                with self.subTest(n=n, filler=filler):
                    _, events, _ = run_batch([{'id': WRONG if i % 2 else RIGHT} for i in range(n)],
                                             filler=filler, max_rounds=1)
                    budgets = [e for e in events if e['event'] == 'recall_batch_budget']
                    self.assertTrue(budgets)
                    self.assertTrue(all(e['future'] == 0 for e in budgets))
                    self.assertTrue(all(e.get('evidence_lost', 0) == 0 for e in events))

    def test_small_first_result_reuses_unused_share(self):
        _, events, _ = run_batch([{'id': CORRECT_FIRST}, {'id': RIGHT}], filler=11000)
        budgets = [e for e in events if e['event'] == 'recall_batch_budget']
        self.assertGreater(budgets[1]['cap'], budgets[0]['cap'])

    def test_interleaved_query_cannot_consume_later_id_share(self):
        _, events, _ = run_batch([{'id': WRONG}, {'query': '0726'}, {'id': RIGHT}], filler=11000)
        budgets = [e for e in events if e['event'] == 'recall_batch_budget']
        recalls = [e for e in events if e['event'] == 'recall']
        self.assertEqual([e['remaining'] for e in budgets], [3, 2, 1])
        self.assertEqual(len(recalls), 3)
        self.assertEqual(recalls[-1]['results'][0]['rid'], RIGHT)
        self.assertGreaterEqual(recalls[-1]['tokens'], window.RESULT_R)
        self.assertFalse(any(e.get('reason') == 'space' for e in events))

    def test_explicit_result_ceiling_is_respected(self):
        _, events, _ = run_batch([{'id': WRONG}, {'id': RIGHT}], filler=11000, ceiling=512)
        recalls = [e for e in events if e['event'] == 'recall']
        self.assertEqual(len(recalls), 2)
        self.assertTrue(all(e['tokens'] <= 512 for e in recalls))

    def test_reserve_fallback_and_high_generation_estimate(self):
        from unittest.mock import Mock
        # Exercise allocator boundaries without needing an engine generation to be artificially huge.
        px = object.__new__(Proxy)
        px.cfg = core.Config(window=32768)
        px.journal = Mock()
        px._frame_tokens = Mock(return_value=100)
        px._free_search = Mock(return_value=3000)
        st = window.SearchState()
        st.s_est = 10000
        cap = px._batch_result_cap(Mock(conv='c'), [], [], [], {}, 0, 4096, 'q', st, 2)
        self.assertEqual(cap, (3000 - 100) // 2)
        self.assertEqual(px.journal.log.call_args.kwargs['future'], 0)
        px._free_search.return_value = 14000
        cap = px._batch_result_cap(Mock(conv='c'), [], [], [], {}, 0, 4096, 'q', st, 2)
        reserve = 10000 + 100 - 4096
        self.assertEqual(cap, (14000 - 100 - reserve) // 2)
        self.assertEqual(px.journal.log.call_args.kwargs['future'], reserve)

    def test_query_only_and_duplicate_id_rounds_are_unchanged(self):
        for args in ([{'id': RIGHT}, {'id': RIGHT}], [{'query': '0726'}, {'query': '0726'}]):
            _, events, _ = run_batch(args, filler=0)
            self.assertFalse(any(e['event'] == 'recall_batch_budget' for e in events))


if __name__ == '__main__':
    unittest.main()
