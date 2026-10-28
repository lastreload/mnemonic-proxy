"""Render a pack's own Qwen-family chat template for the final window check.

The tokenizer vocabulary does not specify the prompt: fine-tunes such as Tiel
ship a different template, including additional instructions and tool framing.
Planning still uses attributed Qwen pieces; the last guard counts the full
pack-rendered prompt before any chat request is sent.
"""
from __future__ import annotations

import json
from pathlib import Path
import re

from .render import _args, _text_of, _has_image


class PackTemplate:
    def __init__(self, path):
        try:
            from jinja2.sandbox import ImmutableSandboxedEnvironment
            env = ImmutableSandboxedEnvironment(trim_blocks=True, lstrip_blocks=True,
                                                 extensions=['jinja2.ext.loopcontrols'])
            env.filters['tojson'] = lambda value, **kw: json.dumps(value, **{'ensure_ascii': False, **kw})
            env.filters['from_json'] = json.loads

            def fail(message):
                raise ValueError(str(message))

            env.globals['raise_exception'] = fail
            self.path = str(path)
            source = Path(path).read_text(encoding='utf-8')
            source = re.sub(r'({%-?\s*)endgeneration(\s*-?%})', r'\1endif\2', source)
            source = re.sub(r'({%-?\s*)generation(\s*-?%})', r'\1if true\2', source)
            self.template = env.from_string(source)
        except Exception as error:
            # Loading a discovered template must not silently restore the old counter.
            raise ValueError('cannot load pack chat template %s: %s; install mnemonic-proxy[exact]'
                             % (path, error)) from error

    def render(self, messages, tools, kwargs):
        normalized = []
        for i, msg in enumerate(messages):
            role = msg.get('role')
            if role == 'developer':
                role = 'system'
            if role == 'system' and i > 0:
                role = 'user'
            content = msg.get('content')
            if _has_image(content):
                # A tokenizer/template cannot know dynamic image patch counts.
                # Existing image planning remains available without a pack template;
                # this stronger guard refuses rather than claiming an exact bound.
                raise ValueError('pack-template window guard requires text-only messages')
            out = {'role': role, 'content': _text_of(content)}
            if msg.get('reasoning_content'):
                out['reasoning_content'] = msg['reasoning_content']
            if msg.get('tool_call_id'):
                out['tool_call_id'] = msg['tool_call_id']
            if msg.get('tool_calls'):
                out['tool_calls'] = []
                for call in msg['tool_calls']:
                    fn = call.get('function', call)
                    out['tool_calls'].append({'function': {'name': fn.get('name'),
                                                           'arguments': _args(fn.get('arguments'))},
                                              **({'id': call['id']} if call.get('id') else {})})
            # PolyStrata ignores completed empty assistant turns.
            if (i != len(messages) - 1 and role == 'assistant' and not out['content'].strip()
                    and not out.get('tool_calls')):
                continue
            normalized.append(out)
        tdefs = [t.get('function', t) if t.get('type') == 'function' else t for t in tools or []] or None
        try:
            return self.template.render(messages=normalized, tools=tdefs, add_generation_prompt=True,
                                        **{'enable_thinking': True, **kwargs})
        except Exception as error:
            raise ValueError('pack chat template could not render request: %s' % error) from error


def discover(tokenizer_path):
    path = Path(tokenizer_path)
    directory = path if path.is_dir() else path.parent
    template = directory / 'chat_template.jinja'
    return PackTemplate(template) if template.is_file() else None
