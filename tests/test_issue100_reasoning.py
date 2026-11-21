"""Issue #100: real Spark Sharp v2.0.0 template from the issue #95 GGUF extraction.

Fixture SHA256: 0b6025e3f98cc6a48b7ffa84f9ec39f50399fbc4d128d54952554f3cf9febd53.
The WordLevel vocabulary is synthetic: no model/inference, totals are not Spark model counts.
"""
import json
from pathlib import Path
import tempfile
import unittest

from ctxproxy.core import Config, Journal, Manager, Store, TokenCounter
from ctxproxy.window import WindowGuard

try:
    import jinja2  # noqa: F401
    from tokenizers import Tokenizer, models, pre_tokenizers
    EXACT = True
except ImportError:
    EXACT = False


@unittest.skipUnless(EXACT, 'install mnemonic-proxy[exact]')
class SparkReasoningTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name)
        tk = Tokenizer(models.WordLevel({'[UNK]': 0, 'PONG': 1}, unk_token='[UNK]'))
        tk.pre_tokenizer = pre_tokenizers.WhitespaceSplit()
        tk.save(str(self.path / 'tokenizer.json'))
        source = Path(__file__).parent / 'fixtures' / 'spark_chat_template.jinja'
        (self.path / 'chat_template.jinja').write_bytes(source.read_bytes())
        self.tc = TokenCounter(tokenizer_path=str(self.path))
        self.assertTrue(self.tc.exact)
        self.assertIsNotNone(self.tc.prompt_template)
        self.store = Store()
        self.addCleanup(self.store.db.close)
        self.cfg = Config(window=65536, window_margin=256)
        self.journal = Journal(str(self.path / 'journal.jsonl'))
        self.mgr = Manager(self.cfg, self.store, self.tc, self.journal)
        self.guard = WindowGuard(self.cfg, self.mgr, self.journal)
        self.body = {'model': 'spark-fixture', 'max_tokens': 64,
                     'messages': [{'role': 'user', 'content': 'Reply with exactly: PONG'}]}

    def test_high_survives_real_template_and_count(self):
        body = {**self.body, 'reasoning_effort': 'high'}
        # Direct rendering is the control: the same template accepts native high.
        direct = self.tc.prompt_template.render(body['messages'], None, {'reasoning_effort': 'high'})
        self.assertIn('thinking_effort=high', direct)
        sent = self.guard.enforce(body)
        self.assertEqual(sent['reasoning_effort'], 'high')
        self.assertEqual(self.guard.count_errors, 0)
        self.assertLess(self.guard.last_count, self.cfg.window - 320)

    def test_render_error_is_not_context_overflow(self):
        from ctxproxy.upstream import UpstreamError
        with self.assertRaises(UpstreamError) as ctx:
            self.guard.enforce({**self.body, 'reasoning_effort': 'medium'})
        error = json.loads(ctx.exception.body)['error']
        self.assertEqual(ctx.exception.status, 400)
        self.assertEqual(error['type'], 'prompt_count_error')
        self.assertEqual(error['code'], 'prompt_count_failed')
        self.assertIn('Unexpected reasoning effort medium', error['message'])
        self.assertNotIn('leaves no room', error['message'])
        self.assertEqual(self.guard.refused, 1)
        self.assertEqual(self.guard.count_errors, 1)

    def test_spark_effort_matrix_and_nested_form(self):
        from ctxproxy.upstream import UpstreamError
        for field in ('reasoning_effort', 'reasoning'):
            for effort in (None, 'off', 'low', 'medium', 'high', 'xhigh', 'banana', 'maximum'):
                with self.subTest(field=field, effort=effort):
                    extra = {} if effort is None else {field: {'effort': effort} if field == 'reasoning' else effort}
                    body = {**self.body, **extra}
                    if effort in (None, 'off', 'low', 'high'):
                        self.assertEqual(self.guard.enforce(body)['max_tokens'], 64)
                    else:
                        with self.assertRaises(UpstreamError) as ctx:
                            self.guard.enforce(body)
                        error = json.loads(ctx.exception.body)['error']
                        self.assertEqual(error['code'], 'prompt_count_failed')
                        self.assertIn('Unexpected reasoning effort ' + effort, error['message'])
                        self.assertNotIn('leaves no room', error['message'])

    def test_explicit_override_and_thinking_off(self):
        from ctxproxy.render import template_kwargs
        for ctk in ({'reasoning_effort': 'low'}, {'enable_thinking': False},
                    {'enable_thinking': False, 'reasoning_effort': 'banana'},
                    {'reasoning_effort': 'banana', 'enable_thinking': False}):
            with self.subTest(ctk=ctk):
                body = {**self.body, 'reasoning_effort': 'high', 'chat_template_kwargs': ctk}
                self.assertEqual(self.guard.enforce(body)['max_tokens'], 64)
                kw = template_kwargs(body)
                self.assertEqual(kw, {'enable_thinking': False} if ctk.get('enable_thinking') is False
                                 else {'reasoning_effort': 'low'})

    def test_positive_template_values_are_not_normalized(self):
        from ctxproxy.render import template_kwargs
        for effort in ('high', 'xhigh', 'medium', 'banana', 'max', 'maximum', ' HIGH '):
            self.assertEqual(template_kwargs({'reasoning_effort': effort}), {'reasoning_effort': effort})

    def test_false_is_an_explicit_top_level_switch(self):
        from ctxproxy.render import template_kwargs
        body = {**self.body, 'reasoning_effort': False, 'reasoning': {'effort': 'high'}}
        self.assertEqual(template_kwargs(body), {'enable_thinking': False})

    def test_legacy_qwen_rendering_keeps_its_vocabulary(self):
        from ctxproxy.render import EFFORT_TEXT, render, template_kwargs
        for effort, resolved in ((None, 'xhigh'), ('high', 'xhigh'), ('xhigh', 'xhigh'),
                                 ('medium', 'medium'), ('low', 'low'), ('banana', 'xhigh'),
                                 ('max', 'xhigh'), ('maximum', 'xhigh'), (' HIGH ', 'xhigh')):
            with self.subTest(effort=effort):
                body = {} if effort is None else {'reasoning_effort': effort}
                prompt = render(self.body['messages'], None, template_kwargs(body))
                self.assertEqual(prompt, render(self.body['messages'], None, {'reasoning_effort': resolved}))
                if EFFORT_TEXT[resolved]:
                    self.assertIn(EFFORT_TEXT[resolved], prompt)
        for off in ('off', 'none', 'minimal', 'disabled', 'false', False):
            prompt = render(self.body['messages'], None, template_kwargs({'reasoning_effort': off}))
            self.assertNotIn('Reasoning effort is set to', prompt)
            self.assertTrue(prompt.endswith('<think>\n\n</think>\n\n'))

    def test_real_qwen_template_still_owns_high_alias(self):
        from ctxproxy.chattemplate import PackTemplate
        from ctxproxy.render import render, template_kwargs
        template = PackTemplate(Path(__file__).parent / 'fixtures' / 'pack_chat_template.jinja')
        for effort in (None, 'high', 'medium', 'low', 'xhigh', 'off'):
            body = {} if effort is None else {'reasoning_effort': effort}
            kw = template_kwargs(body)
            self.assertEqual(template.render(self.body['messages'], None, kw),
                             render(self.body['messages'], None, kw))

    def test_actual_overflow_and_clamp_are_unchanged(self):
        from ctxproxy.window import WindowExceeded
        count = self.guard.count_body(self.body)
        self.cfg.window = count + self.cfg.window_margin + 10
        self.assertEqual(self.guard.enforce(self.body)['max_tokens'], 10)
        self.cfg.window = count + self.cfg.window_margin
        with self.assertRaises(WindowExceeded) as ctx:
            self.guard.enforce(self.body)
        error = json.loads(ctx.exception.body)['error']
        self.assertEqual(error['type'], 'context_window_exceeded')
        self.assertEqual(error['code'], 'window_guard')
        self.assertEqual(ctx.exception.prompt, count)
        self.assertIn('leaves no room', error['message'])


if __name__ == '__main__':
    unittest.main()
