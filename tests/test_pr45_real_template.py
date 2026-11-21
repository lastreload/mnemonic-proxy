"""Real backend 6c5afc86 fixtures: no template rewriting or weakened equality.

A templates/requests/prompts/IDs captured by t_ce1fd4c0 on PC4070TI.
Templates extracted from the existing GGUF; tokenizer provenance in offline report.
"""
import copy
import json
from pathlib import Path
import tempfile
import unittest

from ctxproxy.chattemplate import PackTemplate
from ctxproxy.core import Config, TokenCounter
from ctxproxy.render import template_kwargs

F = Path(__file__).parent / 'fixtures' / 'pr45_real'
RAW = json.loads((F / 'backend-A.json').read_text())


class RealTemplateTests(unittest.TestCase):
    def test_openai_real_backend_integral_prompt(self):
        template = PackTemplate(F / 'A.jinja', tools_format='openai')
        for name, capture in RAW.items():
            with self.subTest(case=name):
                b = capture['request']
                self.assertEqual(template.render(b['messages'], b.get('tools'), template_kwargs(b)),
                                 capture['response']['prompt'])

    def test_default_strata_keeps_old_catalogue(self):
        b = RAW['tools']['request']
        out = PackTemplate(F / 'A.jinja').render(b['messages'], b['tools'], {})
        self.assertEqual(len(out), 4948)
        self.assertNotEqual(out, RAW['tools']['response']['prompt'])

    def test_openai_catalogue_is_not_changed(self):
        tools = [{'type': 'function', 'function': {'name': 'x', 'parameters': {'type': 'object'}},
                  'strict': True}, {'name': 'already_unwrapped'}]
        before = copy.deepcopy(tools)
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'chat_template.jinja'
            p.write_text('{{ tools | tojson }}')
            t = PackTemplate(p, tools_format='openai')
            self.assertEqual(json.loads(t.render([{'role': 'user', 'content': 'x'}], tools, {})), tools)
            self.assertEqual(t.render([{'role': 'user', 'content': 'x'}], None, {}), 'null')
        self.assertEqual(tools, before)

    def test_invalid_format_fails_even_without_tokenizer(self):
        with self.assertRaisesRegex(ValueError, 'template_tools_format'):
            TokenCounter(template_tools_format='guess')

    def test_profiles_propagate_and_override_format(self):
        from ctxproxy.profiles import load_profiles
        from unittest.mock import patch
        raw = {'template_tools_format': 'openai', 'profiles': {
            'A': {}, 'B': {'template_tools_format': 'strata'}}}
        with patch('ctxproxy.profiles.TokenCounter', wraps=TokenCounter) as counter:
            specs, _ = load_profiles(raw, Config.from_dict(raw))
        self.assertEqual([c.kwargs['template_tools_format'] for c in counter.call_args_list],
                         ['openai', 'strata'])
        self.assertEqual(specs[0].new_counter().prompt_template, None)

    def test_server_main_propagates_format_without_profiles(self):
        from unittest.mock import patch
        from tokenizers import Tokenizer, models
        from ctxproxy import server
        class Observed(Exception):
            pass
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)
            Tokenizer(models.WordLevel({'[UNK]': 0}, unk_token='[UNK]')).save(str(p/'tokenizer.json'))
            (p/'chat_template.jinja').write_text('{{ tools | tojson }}')
            cfg = {'template_tools_format': 'openai', 'auto_config': False,
                   'autosave': False, 'slot_save': False, 'mask_anchor': False, 'kv_archive': False}
            (p/'cfg.json').write_text(json.dumps(cfg))
            def setup(*args, **kwargs):
                self.assertEqual(kwargs['counter'].prompt_template.tools_format, 'openai')
                raise Observed()
            with patch('ctxproxy.engines.setup', side_effect=setup), self.assertRaises(Observed):
                server.main(['--data', str(p/'data'), '--config', str(p/'cfg.json'),
                             '--tokenizer', str(p/'tokenizer.json')])

    def test_granite_self_unwrapping_preserves_render(self):
        b = RAW['tools']['request']
        old = PackTemplate(F / 'B.jinja').render(b['messages'], b['tools'], {})
        new = PackTemplate(F / 'B.jinja', tools_format='openai').render(b['messages'], b['tools'], {})
        self.assertEqual(new, old)
