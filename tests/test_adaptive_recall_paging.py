# Author: Maurizio Verde — LastReload
"""CPU-only regression tests, including verbatim outputs from three benchmark failures/pages."""
import json
import pathlib
import tempfile
import unittest

from ctxproxy import core, engines
from ctxproxy.server import Proxy
from ctxproxy.upstream import Upstream
from tests.fake_engine import FakeEngine

CASES = json.loads((pathlib.Path(__file__).parent / 'data' / 'recall_paging_benchmark.json').read_text())
RID = 'r0123456789ab'


class TestAdaptivePaging(unittest.TestCase):
    def setUp(self):
        self.store = core.Store(':memory:')
        self.cfg = core.Config(recall_struct=True, recall_flex=True, recall_multi=True)
        self.tc = core.TokenCounter()
        self.mgr = core.Manager(self.cfg, self.store, self.tc, core.Journal(None))

    def tearDown(self):
        self.store.db.close()

    def put(self, text, conv='c', idx=1, role='tool'):
        self.store.archive_many([(RID, conv, idx, role, 'bash', text, self.tc.count(text), 0)])

    def read(self, args=None, cap=6000):
        meta = []
        out = self.mgr.recall('c', {'id': RID, **(args or {})}, max_idx=9, meta=meta, max_tokens=cap)
        self.assertLessEqual(self.tc.count(out), min(cap, self.cfg.recall_max_tokens))
        if meta:
            m = meta[0]
            text = self.store.get(RID, 'c')[5]
            self.assertIn(text[m['offset']:m['offset'] + m['chars']], out)
        return out, meta

    def test_whole_remainder_and_explicit_cap(self):
        text = CASES[0]['text']
        self.put(text)
        out, meta = self.read()
        self.assertIn(text, out)
        self.assertEqual((meta[0]['offset'], meta[0]['chars']), (0, len(text)))
        out, meta = self.read({'offset': 1973})
        self.assertIn(text[1973:], out)
        self.assertEqual(meta[0]['chars'], len(text) - 1973)
        self.cfg.recall_max_tokens = 300
        out, meta = self.read()
        self.assertLess(meta[0]['chars'], len(text))
        self.assertIn('offset=%d' % meta[0]['chars'], out)

    def test_late_query_centered_in_one_line_json(self):
        text = CASES[1]['text']
        self.put(text)
        out, meta = self.read({'query': '0191'}, cap=350)
        m = meta[0]
        pos = text.index('0191')
        self.assertTrue(m['offset'] <= pos < m['offset'] + m['chars'])
        self.assertGreater(m['offset'], 3933)
        self.assertIn('0191', out)
        self.assertIn(str(len(text)), out)
        self.assertIn('offset=%d' % (m['offset'] + m['chars']), out)

    def test_output_query_reads_output_not_link(self):
        self.put(CASES[2]['text'])
        out, meta = self.read({'query': '0567', 'mode': 'output'}, cap=350)
        self.assertIn('0567', out)
        self.assertTrue(meta)

    def test_output_query_on_arguments_explains_instead_of_ignoring(self):
        self.put('{}', role='assistant-tool-args')
        out, meta = self.read({'query': '0567', 'mode': 'output'})
        self.assertIn('query/offset', out)
        self.assertFalse(meta)

    def test_unicode_offsets_char_cap_and_no_match(self):
        self.cfg.recall_max_chars = 120
        text = '\u0130\r\n' * 100 + 'target-value' + '\n\u00e8' * 500
        self.put(text)
        out, meta = self.read({'query': 'target-value'}, cap=200)
        self.assertLessEqual(meta[0]['chars'], 120)
        self.assertIn('target-value', out)
        out, meta = self.read({'query': 'MISSING'}, cap=200)
        self.assertIn('MISSING', out)
        self.assertFalse(meta)

    def test_branch_and_conversation_boundaries(self):
        self.put('SECRET', conv='other')
        out, meta = self.read()
        self.assertNotIn('SECRET', out)
        self.assertFalse(meta)
        self.store.db.execute('delete from archive')
        self.put('FUTURE', idx=10)
        out, meta = self.read()
        self.assertNotIn('FUTURE', out)
        self.assertFalse(meta)

    def test_engine_keeps_result_ceiling_not_window_fraction(self):
        engines.apply(self.cfg, engines.Engine(kind='strata', n_ctx=32768), explicit=set(), log=None)
        self.assertEqual(self.cfg.recall_max_tokens, 6000)
        self.assertEqual(self.cfg.recall_struct_max_tokens, 1024)


class TestFakeEnginePaging(unittest.TestCase):
    def run_case(self, case, args, window=32768, filler=0):
        received = []
        sent = [False]

        def policy(body):
            if not sent[0]:
                sent[0] = True
                return {'role': 'assistant', 'content': '', 'tool_calls': [
                    {'id': 'recall-test', 'type': 'function', 'function': {
                        'name': core.RECALL_NAME, 'arguments': json.dumps({'id': RID, **args})}}]}
            received.extend(m['content'] for m in body['messages'] if m.get('tool_call_id') == 'recall-test')
            return {'role': 'assistant', 'content': 'done'}

        with tempfile.TemporaryDirectory() as d:
            eng = FakeEngine(policy, flavor='strata', n_ctx=window)
            url = eng.start()
            store = core.Store(':memory:')
            try:
                cfg = core.Config(recall_struct=True, recall_flex=True, recall_multi=True,
                                  window=window, reserve=2048, response_floor=4096, slot_save=False)
                # Match the high token density of the actual numeric JSON without an optional tokenizer dependency.
                tc = core.TokenCounter(chars_per_token=len(case['text']) / case['tokens'])
                proxy = Proxy(cfg, Upstream(url), store, core.Journal(str(pathlib.Path(d) / 'j.jsonl')), tc)
                history = [{'role': 'system', 'content': 'You are an assistant.' + ' x' * filler},
                           {'role': 'user', 'content': 'Read the archived output.'}]
                # Archive under the conversation produced by prepare, without changing its physical prefix.
                p = proxy.mgr.prepare({'messages': history, 'max_tokens': 4096})
                store.archive_many([(RID, p.conv, 0, 'tool', 'bash', case['text'], case['tokens'], 0)])
                eng.counter = proxy.wguard.count_body
                status, result, _ = proxy.chat({'model': 'm', 'messages': history, 'max_tokens': 4096})
                if not received:
                    self.fail(pathlib.Path(d, 'j.jsonl').read_text()[-6000:])
                self.assertEqual(status, 200, result)
                self.assertTrue(received)
                prefix = eng.requests[0]['messages']
                self.assertEqual(eng.requests[-1]['messages'][:len(prefix)], prefix)
                self.assertFalse(eng.violations)
                for req in eng.requests:
                    self.assertLessEqual(proxy._wcount(req['messages'], req.get('tools', []), req)
                                         + req.get('max_tokens', 0) + proxy.wguard.margin, window)
                return received[-1]
            finally:
                eng.stop()
                store.db.close()

    def test_real_first_output_in_one_response(self):
        self.assertIn(CASES[0]['text'], self.run_case(CASES[0], {}))

    def test_real_late_value_in_budget_centered_page(self):
        # Leave less than the whole result, but room for a useful page. The guard must still fit every request.
        out = self.run_case(CASES[1], {'query': '0191'}, filler=15000)
        self.assertIn('0191', out)
        self.assertNotIn(CASES[1]['text'], out)
        self.assertIn('offset=', out)

    def test_real_output_mode_query(self):
        self.assertIn('0567', self.run_case(CASES[2], {'query': '0567', 'mode': 'output'}))
