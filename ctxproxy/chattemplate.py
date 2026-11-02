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

from .render import (VISION, UnsupportedPicture, _args, _has_image, _text_of, literal_marker_count, parts_of,
                     picture_contract)


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

    def render(self, messages, tools, kwargs, with_counts=False):
        """The prompt the engine will tokenize, as rendered by the pack's own template.

        Pictures pass through as parts (render.parts_of), the items openai_to_messages hands the template: it writes
        one vision marker per picture and never the bytes. One marker costs the engine a dynamic number of tokens
        (its vision tower expands the pad, capped by the engine's own limit) and no engine endpoint reports that
        number beforehand, so the caller budgets markers with cfg.image_tokens: pass with_counts=True and count them
        through Manager.piece_tokens, which already applies that allowance.

        The allowance is honest only while the markers *are* the pictures the engine will read, so the contract is
        checked from both ends and a mismatch is a refusal, never an approximation: render.picture_contract rejects
        what the adapter would not read at all (a leading system message's picture - the pack template raises on
        those; a part with no readable source; a picture under a type the engine drops), and the markers the template
        wrote have to come out of that same number.
        """
        expected = picture_contract(messages)
        normalized = []
        for i, msg in enumerate(messages):
            role = msg.get('role')
            if role == 'developer':
                role = 'system'
            if role == 'system' and i > 0:
                role = 'user'
            content = msg.get('content')
            if _has_image(content):
                # vetted by picture_contract: a leading system message got there, no source is empty
                body = parts_of(content, 'messages[%d].content' % i)
            else:
                # a picture-free content goes to the template as its text, exactly as _parts_of flattens it
                body = _text_of(content)
            out = {'role': role, 'content': body}
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
            # PolyStrata ignores completed empty assistant turns. A message with parts is never treated as empty:
            # a picture is content, and dropping the turn would silently drop the engine's promise of seeing it.
            if (i != len(messages) - 1 and role == 'assistant' and isinstance(out['content'], str)
                    and not out['content'].strip() and not out.get('tool_calls')):
                continue
            normalized.append(out)
        tdefs = [t.get('function', t) if t.get('type') == 'function' else t for t in tools or []] or None
        try:
            rendered = self.template.render(messages=normalized, tools=tdefs, add_generation_prompt=True,
                                            **{'enable_thinking': True, **kwargs})
        except Exception as error:
            raise ValueError('pack chat template could not render request: %s' % error) from error
        # Markers counted on the rendered text: what the template actually wrote is what the guard budgets, and it
        # has to be the pictures the request carries. Fewer markers means a picture the engine will not read (the
        # guard would have priced a token that is not in the prompt), more means a picture nobody priced: either way
        # the counted prompt is not the prompt the engine will build, so this fails closed - a missing marker is
        # never tolerated and never re-estimated.
        markers = rendered.count(VISION)
        # A marker can be *quoted text* too - someone pasting the spelling into a message is not attaching a
        # picture, and the template prints the quote verbatim. It must neither break the equality below (which
        # compares the pictures being sent) nor be priced as a picture: the guard subtracts it from the budget.
        quoted = literal_marker_count(messages, tools)
        if markers - quoted != expected:
            raise UnsupportedPicture(
                'pack chat template wrote %d vision marker(s) for the %d picture(s) the request carries%s: cannot '
                'budget a prompt whose pictures the template did not write'
                % (markers - quoted, expected,
                   "" if not quoted else " (besides %d marker(s) quoted as text)" % quoted))
        return (rendered, markers) if with_counts else rendered


def discover(tokenizer_path):
    path = Path(tokenizer_path)
    directory = path if path.is_dir() else path.parent
    template = directory / 'chat_template.jinja'
    return PackTemplate(template) if template.is_file() else None
