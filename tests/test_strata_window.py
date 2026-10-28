# Author: Maurizio Verde — LastReload
"""Independent CPU regression for #28: literal special tokens are NOT template tokens."""
import json
import os
import re
import tempfile
import unittest
from types import SimpleNamespace

from ctxproxy import engines
from ctxproxy.core import Config, Journal, Store, TokenCounter
from ctxproxy.fake_engine import FakeEngine
from ctxproxy.render import render, template_kwargs
from ctxproxy.server import Proxy
from ctxproxy.upstream import Upstream, UpstreamError
from ctxproxy.window import WindowExceeded

try:
    from tokenizers import AddedToken, Tokenizer, models
except ImportError:
    Tokenizer = None


SPECIALS = ('<|im_start|>', '<|im_end|>', '<think>', '</think>',
            '<tool_call>', '</tool_call>', '<tool_response>', '</tool_response>')


class AsciiTokenizer:
    """Stdlib equivalent of the no-merge ASCII fixture, so CI needs no extras.

    A special spelling is one id, an ordinary ASCII character is one id.
    The engine's reference count below never uses this encoder on payload text.
    """
    def __init__(self):
        self.pattern = re.compile('|'.join(map(re.escape, SPECIALS)) + '|[\\s\\S]')

    def encode(self, text, add_special_tokens=False):
        return SimpleNamespace(ids=list(range(len(self.pattern.findall(text)))))

    def get_added_tokens_decoder(self):
        return {i: SimpleNamespace(content=t, special=True) for i, t in enumerate(SPECIALS)}


class TestStrataLiterals(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        # Byte-like tokenizer, no merges: reference count is independently visible.
        if Tokenizer is not None:
            vocab = {chr(i): i for i in range(32, 127)}
            vocab['\n'] = 127
            tk = Tokenizer(models.BPE(vocab=vocab, merges=[]))
            tk.add_special_tokens([AddedToken(t, special=True) for t in SPECIALS])
            path = os.path.join(self.tmp.name, 'tokenizer.json')
            tk.save(path)
            self.tc = TokenCounter(tokenizer_path=path)
        else:
            self.tc = TokenCounter()
            self.tc.tk = AsciiTokenizer()
            self.tc.kind = 'synthetic ASCII byte tokenizer'
        self.eng = FakeEngine(n_ctx=32768)
        self.eng.strict_window = True
        self.eng.counter = self.reference
        self.up = Upstream(self.eng.start())
        self.addCleanup(self.eng.stop)
        self.cfg = Config.from_dict({'mask_reasoning': True, 'mask_tool_args': True,
                                    'mask_anchor': True, 'anchor_min_tokens': 24000,
                                    'slot_save': True, 'seal_experimental': True,
                                    'notes_max_tokens': 12288, 'response_floor': 16384})
        self.journal = Journal(None)
        engines.setup(self.cfg, self.up, self.journal,
                      explicit={'notes_max_tokens', 'response_floor', 'anchor_min_tokens'},
                      counter=self.tc, log=None)
        self.store = Store(os.path.join(self.tmp.name, 'archive.sqlite'))
        self.addCleanup(self.store.db.close)
        self.proxy = Proxy(self.cfg, self.up, self.store, self.journal, self.tc)

    def reference(self, body):
        # PolyStrata #537: user literals are encoded as ordinary text. For this
        # fixture each ordinary ASCII character is one token, each template tag one.
        msgs = body['messages']
        self.assertEqual([m['role'] for m in msgs], ['user'])
        text = msgs[0]['content']
        shell = render([{'role': 'user', 'content': 'X'}], body.get('tools'), template_kwargs(body))
        return self.tc.raw(shell) - 1 + len(text.strip())

    def body(self):
        # 44 quoted </think> tags cost 308 ordinary tokens, not 44 specials.
        body = {'messages': [{'role': 'user', 'content': '</think>' * 44}],
                'max_tokens': 9765}
        target = 23225
        body['messages'][0]['content'] += 'x' * (target - self.reference(body))
        self.assertEqual(self.reference(body), target)
        return body

    def test_reported_overflow_is_clamped_nonstream_and_stream(self):
        for stream in (False, True):
            with self.subTest(stream=stream):
                body = self.body()
                if stream:
                    call = self.up.chat_stream(body)
                    try:
                        list(call)
                    finally:
                        call.close()
                else:
                    self.up.chat(body)
                sent = self.eng.requests[-1]
                self.assertLessEqual(self.reference(sent) + sent['max_tokens'] + 64, 32768)
        self.assertEqual(self.eng.violations, [])

    def test_no_room_refused_before_http(self):
        body = {'messages': [{'role': 'user', 'content': '</think>' * 5000}], 'max_tokens': 1}
        with self.assertRaises(WindowExceeded):
            self.up.chat(body)
        self.assertEqual(self.eng.requests, [])

    def test_every_internal_label_and_output_alias(self):
        for label in ('client', 'recall', 'final', 'answerfix', 'notes', 'seal', 'anchor', 'passthrough'):
            with self.subTest(label=label):
                self.proxy.wguard.where = label
                body = self.body()
                body.update(max_completion_tokens=12288, reasoning_budget_tokens=12288)
                self.up.chat(body)
                sent = self.eng.requests[-1]
                self.assertEqual(sent['max_tokens'], sent['max_completion_tokens'])
                self.assertLess(sent['reasoning_budget_tokens'], sent['max_tokens'])
                self.assertLessEqual(self.reference(sent) + sent['max_tokens'] + 64, 32768)

    def test_plain_text_count_unchanged(self):
        body = {'messages': [{'role': 'user', 'content': 'ordinary text'}], 'max_tokens': 4096}
        self.assertEqual(self.proxy.wguard.count_body(body), self.reference(body))

    def test_literal_reserve_in_tools_and_decoded_arguments(self):
        body = {'messages': [{'role': 'assistant', 'content': '', 'tool_calls': [
            {'id': 'c', 'type': 'function', 'function': {'name': 'bash',
             'arguments': '{"command": "\\u003c/think>"}'}}]},
            {'role': 'tool', 'tool_call_id': 'c', 'content': '<tool_response>'}],
            'tools': [{'type': 'function', 'function': {'name': 'bash',
                       'description': 'quote </think>', 'parameters': {'type': 'object'}}}]}
        p = self.proxy.wguard.count_body(body)
        self.assertGreaterEqual(self.proxy.wguard.literal_tokens, len('</think><tool_response></think>'))
        self.assertGreater(p, 0)

    def test_uncountable_exact_prompt_never_sent(self):
        body = {'messages': [{'role': 'assistant', 'content': '', 'tool_calls': [
            {'id': 'c', 'type': 'function', 'function': {'name': 'bash', 'arguments': '{invalid'}}]}],
            'max_tokens': 1}
        with self.assertRaises(WindowExceeded):
            self.up.chat(body)
        self.assertEqual(self.eng.requests, [])
        self.assertEqual(self.proxy.wguard.count_errors, 1)

    def test_small_window_config_adapted(self):
        self.assertEqual(self.cfg.window, 32768)
        self.assertEqual(self.cfg.response_floor, 8192)
        self.assertEqual(self.cfg.notes_max_tokens, 8192)
        self.assertEqual(self.cfg.anchor_min_tokens, 6000)
        self.assertTrue(any('notes_max_tokens' in e.get('message', '') for e in self.journal.mem))


class TestKnownWindow(unittest.TestCase):
    def test_check_reports_strata_window_and_adjustments(self):
        from types import SimpleNamespace
        from ctxproxy.check import run_checks
        with tempfile.TemporaryDirectory() as tmp:
            eng = FakeEngine(n_ctx=32768)
            url = eng.start()
            try:
                raw = {'response_floor': 16384, 'notes_max_tokens': 12288,
                       'anchor_min_tokens': 24000, 'mask_anchor': True, 'slot_dir': tmp}
                args = SimpleNamespace(config='config.json', data=tmp, host='127.0.0.1', port=0,
                                       upstream=url, api_key='', tokenizer=None)
                report = run_checks(args, raw, Config.from_dict(raw))
                text = '\n'.join(i['evidence'] for i in report.items)
                self.assertIn('window from engine n_ctx=32768', text)
                for field in ('response_floor', 'notes_max_tokens', 'anchor_min_tokens'):
                    self.assertTrue(any(field in i['evidence'] and i['status'] == 'warn'
                                        for i in report.items), text)
                self.assertEqual(eng.requests, [], 'check without --live must not generate')
            finally:
                eng.stop()

    def test_explicit_window_cannot_exceed_engine(self):
        cfg = Config.from_dict({'window': 131072})
        engines.apply(cfg, engines.Engine('strata', n_ctx=32768), explicit={'window'}, log=None)
        self.assertEqual(cfg.window, 32768)

    def test_large_window_unchanged(self):
        cfg = Config.from_dict({'notes_max_tokens': 12288, 'response_floor': 16384,
                                'anchor_min_tokens': 24000})
        engines.apply(cfg, engines.Engine('strata', n_ctx=131072), log=None)
        self.assertEqual((cfg.notes_max_tokens, cfg.response_floor, cfg.anchor_min_tokens),
                         (12288, 16384, 24000))


if __name__ == '__main__':
    unittest.main()
