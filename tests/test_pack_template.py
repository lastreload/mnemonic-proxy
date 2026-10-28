"""No network/model: synthetic fine-tune template proves tokenizer != prompt template."""
import json
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace

from ctxproxy.core import Config, Journal, Manager, Store, TokenCounter
from ctxproxy.window import WindowGuard, WindowExceeded, labeled_chat

try:
    import jinja2
    from tokenizers import Tokenizer, models, pre_tokenizers
    EXACT = True
except ImportError:
    EXACT = False

# An independently authored stand-in for a fine-tune's extra instructions.
# The real pinned Tiel/PolyStrata replay is documented separately; this is NOT its template.
SOURCE = ("{{ 'extra ' * 300 }}"
          "{% for m in messages %}{{ m.role }} {{ m.content }} "
          "{% for c in m.tool_calls|default([]) %}{{ c.function.arguments | tojson }} {% endfor %}"
          "{% endfor %}assistant ")


@unittest.skipUnless(EXACT, 'install mnemonic-proxy[exact]')
class PackTemplateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)
        tk = Tokenizer(models.WordLevel({'[UNK]':0, 'extra':1, 'user':2, 'assistant':3}, unk_token='[UNK]'))
        tk.pre_tokenizer = pre_tokenizers.WhitespaceSplit()
        tk.save(str(self.path/'tokenizer.json'))
        (self.path/'chat_template.jinja').write_text(SOURCE)
        self.tc = TokenCounter(tokenizer_path=str(self.path/'tokenizer.json'))
        self.store = Store()
        self.addCleanup(self.store.db.close)
        self.cfg = Config(window=400, window_margin=64)
        self.mgr = Manager(self.cfg, self.store, self.tc, Journal(None))
        self.g = WindowGuard(self.cfg, self.mgr, self.mgr.journal)
        self.body = {'messages':[{'role':'user','content':'hello'}], 'max_tokens':200}

    def test_own_template_in_guard_and_planning(self):
        self.assertIsNotNone(self.tc.prompt_template)
        self.assertEqual(self.g.count_body(self.body), 303)
        self.assertEqual(self.mgr.estimate(self.body['messages'], None)[0], 303)
        self.assertEqual(self.g.enforce(self.body)['max_tokens'], 33)
        self.tc.prompt_template = None
        # A correct vocabulary with the wrong rendering still undercounts.
        self.assertLess(self.g.count_body(self.body), 303)

    def test_template_overflow_is_refused(self):
        self.cfg.window = 300
        with self.assertRaises(WindowExceeded):
            self.g.enforce(self.body)

    def test_invalid_template_is_not_silently_ignored(self):
        (self.path/'chat_template.jinja').write_text('{% invalid %}')
        with self.assertRaisesRegex(ValueError, 'cannot load pack chat template'):
            TokenCounter(tokenizer_path=str(self.path/'tokenizer.json'))

    def test_render_failure_is_fail_closed(self):
        (self.path/'chat_template.jinja').write_text("{{ raise_exception('invalid conversation') }}")
        self.tc.prompt_template = TokenCounter(tokenizer_path=str(self.path/'tokenizer.json')).prompt_template
        with self.assertRaises(WindowExceeded):
            self.g.enforce(self.body)
        self.assertEqual(self.g.count_errors, 1)

    def test_file_tokenizer_also_discovers_adjacent_template(self):
        tc = TokenCounter(tokenizer_path=str(self.path/'tokenizer.json'))
        self.assertIsNotNone(tc.prompt_template)

    def test_no_template_keeps_legacy_counter(self):
        (self.path/'chat_template.jinja').unlink()
        self.assertIsNone(TokenCounter(tokenizer_path=str(self.path/'tokenizer.json')).prompt_template)

    def test_tool_arguments_decoded_before_template(self):
        body = {'messages':[{'role':'assistant','content':'', 'tool_calls':[{
            'function':{'name':'tool','arguments':'{"x":"hello"}'}}]}]}
        rendered = self.tc.prompt_template.render(body['messages'], None, {})
        self.assertIn('{"x": "hello"}', rendered)

    def test_images_fail_closed_in_pack_template_mode(self):
        body = {'messages':[{'role':'user','content':[{'type':'image_url','image_url':{'url':'x'}}]}]}
        with self.assertRaises(WindowExceeded):
            self.g.enforce(body)


class LabelTests(unittest.TestCase):
    def test_label_restored_on_failure_through_wrapper(self):
        guard = SimpleNamespace(where='answer')
        def fail(body):
            self.assertEqual(guard.where, 'notes')
            raise ValueError('test')
        wrapped = SimpleNamespace(up=SimpleNamespace(window_guard=guard), chat=fail)
        with self.assertRaises(ValueError):
            labeled_chat(wrapped, {}, 'notes')
        self.assertEqual(guard.where, 'answer')


if __name__ == '__main__':
    unittest.main()
