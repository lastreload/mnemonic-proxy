# Author: Maurizio Verde — LastReload
"""A closed search must not accept an undeclared internal call serialized as text."""
import json
import unittest
from pathlib import Path

from tests.test_final_recall_retry import Stubborn, names_of
from ctxproxy.core import RECALL_NAME
from ctxproxy.server import Proxy, internal_call_text
from types import SimpleNamespace
from typing import cast

FIXTURE = json.loads((Path(__file__).parent / 'fixtures' / 'closed-search-recall.json').read_text())
RAW = FIXTURE['message']['content']


class TextualRecall(Stubborn):
    def __init__(self, text=RAW, **kw):
        self.text = text
        super().__init__(script=[[{'args': {'query': 'db-s3-007'}}]] * 5,
                         extra={'max_recall_rounds': 4}, reasoning_chars=600, window_=65536, **kw)

    def policy(self, body):
        if self.k is not None and RECALL_NAME not in names_of(body):
            self.final_bodies.append(body)
            return {'role': 'assistant', 'content': self.text}
        return super().policy(body)


class TestTextualRecall(unittest.TestCase):
    def test_classifier_is_narrow_and_accepts_internal_aliases(self):
        for name in ('recall', 'strata_recall', 'tools'):
            text = RAW.replace('function=recall', 'function=' + name)
            self.assertTrue(internal_call_text(' \n' + text + '\n ', {name}))
            self.assertFalse(internal_call_text(text, {'unrelated'}))
        for text in (RAW + RAW, RAW + ' explanation', '```' + RAW + '```',
                     RAW.replace('</tool_call>', ''), 'ordinary answer'):
            self.assertFalse(internal_call_text(text, {'recall'}))

    def test_stream_holds_raw_call_at_every_split_but_not_prose(self):
        def run(parts, final: bool | None = True):
            chunks = [{'choices': [{'delta': {'content': t}, 'finish_reason': None}]} for t in parts]
            def stream(_):
                yield from chunks
            seen = []
            p = SimpleNamespace(up=SimpleNamespace(chat_stream=stream), cfg=SimpleNamespace(receipt_guard=False),
                                _emit_delta=lambda emit, state, meta, delta: seen.append(delta))
            state = None if final is None else {'final': final, 'internal': {'recall'}}
            r = Proxy._round(cast(Proxy, p), {}, lambda _: None, state)
            return ''.join(d.get('content', '') for d in seen), r
        for i in range(len(RAW) + 1):
            out, r = run([RAW[:i], RAW[i:]])
            self.assertEqual(out, '')
            self.assertEqual(r['choices'][0]['message']['content'], RAW)
        self.assertEqual(run(list(RAW))[0], '')
        self.assertEqual(run(list('ordinary answer'))[0], 'ordinary answer')
        self.assertEqual(run(list(RAW), final=False)[0], RAW)
        self.assertEqual(run(list(RAW), final=None)[0], RAW)

    def test_real_fixture_fails_explicitly_after_four_recalls(self):
        for stream in (False, True):
            with self.subTest(stream=stream):
                s = TextualRecall(stream=stream)
                try:
                    st, r = s.run()[-1]
                    self.assertEqual(st, 200)
                    self.assertEqual(len(s.events('recall')), 4)
                    self.assertEqual(len(s.final_bodies), 1)
                    msg = r['choices'][0]['message']
                    self.assertTrue(r['strata_context']['final_answer_missing'])
                    self.assertIn('did not produce a final text answer', msg['content'])
                    self.assertNotIn('<tool_call>', msg['content'])
                    self.assertNotIn('tool_calls', msg)
                    self.assertEqual(s.events('final_answer_missing')[-1]['reason'], 'internal_call_as_text')
                    self.assertEqual(s.eng.violations, [])
                    if stream:
                        text = ''.join(c['choices'][0]['delta'].get('content') or '' for c in s.chunks
                                       if c.get('choices'))
                        self.assertEqual(text, msg['content'])
                finally:
                    s.close()

    def test_quotes_explanations_and_client_tools_are_not_reclassified(self):
        for text in ('```xml\n' + RAW + '\n```', 'Example:\n' + RAW,
                     RAW.replace('function=recall', 'function=bash'), 'answer: yes',
                     RAW + '\nThis is an example, not a call.'):
            for stream in (False, True):
                with self.subTest(text=text, stream=stream):
                    s = TextualRecall(text=text, stream=stream)
                    try:
                        _, r = s.run()[-1]
                        self.assertEqual(r['choices'][0]['message']['content'], text)
                        self.assertFalse(r['strata_context']['final_answer_missing'])
                        if stream:
                            out = ''.join(c['choices'][0]['delta'].get('content') or '' for c in s.chunks
                                          if c.get('choices'))
                            self.assertEqual(out, text)
                    finally:
                        s.close()


if __name__ == '__main__':
    unittest.main()
