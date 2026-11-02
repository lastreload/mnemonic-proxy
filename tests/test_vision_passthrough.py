"""Vision passthrough: pictures travel to the engine, their cost is an upper bound.

No network and no model: a fixture template replicates the one branch of a Qwen chat template that matters,
the vision marker. The real pack template is exercised the same way (tests/test_pack_template.py proves the
template != vocabulary property); here we prove what must hold for images:

  - a request with an image is NOT refused and NOT counted as text-only;
  - the picture's bytes never enter the prompt the engine tokenizes;
  - text-only prompts keep the byte-identical count they had before;
  - nothing drops a picture silently: a removed one is named by its hash;
  - a video is refused (fail closed), because no engine reads it and its marker has no budget;
  - neither is a picture the engine would not read: a system message's, one with no readable source, one hidden
    under a type the adapter ignores. An empty source and a lost turn are both worse than a 400;
  - the markers the template wrote have to be the pictures being priced - a missing marker is an error, never a
    count that quietly stops counting;
  - a picture cannot be priced at image_tokens <= 0: the guard refuses and the config refuses at load;
  - a saved-state identity exists only for pictures whose bytes the proxy can name (data: URIs), so an url whose
    content changed cannot be restored under the old image's embeddings.
"""
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from ctxproxy.autosave import prompt_text
from ctxproxy.core import Config, Journal, Manager, Store, TokenCounter
from ctxproxy.render import count_images, image_hash, image_markers, redact_images
from ctxproxy.window import WindowGuard, WindowExceeded, remove_piece

try:
    import jinja2                      # noqa: F401
    from tokenizers import Tokenizer, models, pre_tokenizers
    EXACT = True
except ImportError:
    EXACT = False

# The image branch of the real Qwen template (chat_template.jinja), reduced to what the guard must see.
QWEN = ("{%- macro rc(content) %}{% for item in content %}"
        "{%- if 'image' in item or 'image_url' in item or item.type == 'image' %}"
        "{{- '<|vision_start|><|image_pad|><|vision_end|>' }}"
        "{%- elif item.type == 'text' %}{{- item.text }}"
        "{%- else %}{{- raise_exception('Unexpected item type in content.') }}"
        "{%- endif %}{% endfor %}{% endmacro -%}"
        "{% for m in messages %}{{ m.role }} "
        "{% if m.content is string %}{{ m.content }}{% else %}{{ rc(m.content) }}{% endif %} "
        "{% endfor %}assistant ")

MARKER = "<|vision_start|><|image_pad|><|vision_end|>"   # la scrittura che una persona può citare nel testo
PNG = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUg"
PNG2 = "data:image/png;base64,AAAAanotherpicture"
SYSTEM = {"role": "system", "content": "assistente"}


def parts(*items):
    return list(items)


def text(t):
    return {"type": "text", "text": t}


def img(url):
    return {"type": "image_url", "image_url": {"url": url}}


def exact_fixture(tmp: Path, template: str = QWEN, window: int = 4000, margin: int = 64,
                  image_tokens: int = 320) -> dict:
    """A pack-shaped tokenizer, a chat template carrying the Qwen vision branch, and the guard in front of them.

    The tokenizer is a fixture (words, not BPE): what the guard does with markers and pictures does not depend on
    the vocabulary. The real pack template is exercised the same way in tests/test_vision_real_template.py."""
    tk = Tokenizer(models.WordLevel({'[UNK]': 0, 'hello': 1, 'user': 2, 'assistant': 3}, unk_token='[UNK]'))
    tk.pre_tokenizer = pre_tokenizers.WhitespaceSplit()
    tk.save(str(tmp / 'tokenizer.json'))
    (tmp / 'chat_template.jinja').write_text(template)
    cfg = Config(window=window, window_margin=margin, image_tokens=image_tokens)
    journal, store = Journal(None), Store()
    mgr = Manager(cfg, store, TokenCounter(tokenizer_path=str(tmp / 'tokenizer.json')), journal)
    return {'cfg': cfg, 'journal': journal, 'store': store, 'mgr': mgr, 'guard': WindowGuard(cfg, mgr, journal)}


@unittest.skipUnless(EXACT, 'install mnemonic-proxy[exact]')
class VisionGuardTests(unittest.TestCase):
    """Pack template + tokenizer: the guard is the place that used to refuse every picture."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        fx = exact_fixture(Path(self.tmp.name))
        self.tc, self.store = fx['mgr'].tc, fx['store']
        self.cfg, self.journal, self.mgr, self.g = fx['cfg'], fx['journal'], fx['mgr'], fx['guard']
        self.addCleanup(self.store.db.close)

    def body(self, content):
        return {'messages': [{'role': 'user', 'content': content}], 'max_tokens': 200}

    def test_image_is_not_refused(self):
        body = self.g.enforce(self.body(parts(text('hello'), img(PNG))))
        self.assertEqual(body['max_tokens'], 200)        # room is left: the request goes through
        self.assertEqual(self.g.count_errors, 0)

    def test_image_cost_is_the_configured_bound(self):
        plain = self.g.count([{'role': 'user', 'content': 'hello'}], None)
        with_img = self.g.count([{'role': 'user', 'content': parts(text('hello'), img(PNG))}], None)
        self.assertEqual(self.g.images, 1)
        self.assertEqual(self.g.image_allowance, self.cfg.image_tokens)
        self.assertEqual(with_img - plain, self.cfg.image_tokens)   # the bound, not zero and not the base64 size

    def test_several_images(self):
        msgs = [{'role': 'user', 'content': parts(img(PNG), text('e questa?'), img(PNG2))}]
        self.g.count(msgs, None)
        self.assertEqual(self.g.images, 2)
        self.assertEqual(self.g.image_allowance, 2 * self.cfg.image_tokens)

    def test_bytes_never_reach_the_prompt(self):
        rendered = self.tc.prompt_template.render([{'role': 'user', 'content': parts(text('ciao'), img(PNG))}],
                                                 None, {})
        self.assertIn('<|image_pad|>', rendered)
        self.assertNotIn('iVBORw0KGgo', rendered)
        self.assertNotIn('base64', rendered)

    def test_text_only_count_is_unchanged(self):
        msgs = [{'role': 'user', 'content': 'hello'}]
        self.assertEqual(self.g.count(msgs, None), self.tc.raw(self.tc.prompt_template.render(msgs, None, {})))
        self.assertEqual(self.g.images, 0)

    def test_guarantee_is_honest_in_the_journal(self):
        self.g.enforce(self.body(parts(img(PNG))))
        rec = [r for r in self.journal.mem if r['event'] == 'window_guard'][-1]
        self.assertEqual(rec['guarantee'], 'image_bounded')    # never "exact" for a prompt carrying a picture
        self.assertEqual(rec['images'], 1)
        self.assertEqual(rec['image_allowance'], self.cfg.image_tokens)
        self.g.enforce(self.body('hello'))
        rec = [r for r in self.journal.mem if r['event'] == 'window_guard'][-1]
        self.assertNotIn('images', rec)                        # text-only lines stay byte-identical to before
        self.assertIn(rec['guarantee'], ('exact', 'tokenizer_bound'))

    def test_a_system_message_picture_is_refused(self):
        """openai_to_messages reads a system message's text parts and the pack template raises on its pictures: the
        bytes would never reach the model, so the request is refused instead of quietly counted as text."""
        msgs = [{'role': 'system', 'content': parts(text('base'), img(PNG))}, {'role': 'user', 'content': 'hello'}]
        with self.assertRaises(WindowExceeded) as ctx:
            self.g.enforce({'messages': msgs, 'max_tokens': 50})
        self.assertIn('system message cannot carry a picture', str(ctx.exception))
        self.assertEqual(self.g.count_errors, 1)

    def test_a_picture_the_adapter_would_drop_is_refused(self):
        """A picture under a key/shape the engine does not read must not become an empty source or a lost turn."""
        for part, why in (({'type': 'input_image', 'file_id': 'file-7'}, 'no readable source'),
                          ({'type': 'image_url', 'image_url': {'url': '   '}}, 'no readable source'),
                          ({'type': 'text', 'text': 'x', 'image_url': img(PNG)}, 'does not read as an image')):
            with self.subTest(part=part.get('type')):
                with self.assertRaises(WindowExceeded) as ctx:
                    self.g.enforce({'messages': [{'role': 'user', 'content': [part]}], 'max_tokens': 50})
                self.assertIn(why, str(ctx.exception))

    def test_the_markers_and_the_pictures_have_to_be_the_same_number(self):
        """A template that writes no marker for a picture (a fine-tune without a vision branch) cannot be priced:
        the guard refuses rather than budgeting a marker that is not in the prompt."""
        from ctxproxy.chattemplate import PackTemplate
        no_vision = Path(self.tmp.name) / 'no_vision.jinja'
        no_vision.write_text("{% for m in messages %}{{ m.role }}: {{ m.content }}{% endfor %}")
        tpl = PackTemplate(no_vision)
        with self.assertRaises(ValueError) as ctx:
            tpl.render([{'role': 'user', 'content': parts(text('hello'), img(PNG))}], None, {}, with_counts=True)
        self.assertIn('vision marker', str(ctx.exception))

    def test_video_fails_closed(self):
        msgs = [{'role': 'user', 'content': [{'type': 'video_url', 'video_url': {'url': 'x'}}]}]
        with self.assertRaises(WindowExceeded):
            self.g.enforce({'messages': msgs, 'max_tokens': 50})
        self.assertEqual(self.g.count_errors, 1)

    def test_planning_budgets_images_too(self):
        msgs = [{'role': 'user', 'content': parts(text('hello'), img(PNG))}]
        total_img, _ = self.mgr.estimate(msgs, None)
        total_txt, _ = self.mgr.estimate([{'role': 'user', 'content': 'hello'}], None)
        self.assertEqual(total_img - total_txt, self.cfg.image_tokens)


@unittest.skipUnless(EXACT, 'install mnemonic-proxy[exact]')
class EngineStateIdentityTests(unittest.TestCase):
    """The saved-state identity must change when the picture changes: vision markers are identical between
    different pictures, so without a stamp a restore could hand back a KV holding another image's embeddings."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        tk = Tokenizer(models.WordLevel({'[UNK]': 0, 'hello': 1}, unk_token='[UNK]'))
        tk.pre_tokenizer = pre_tokenizers.WhitespaceSplit()
        path = Path(self.tmp.name) / 'tokenizer.json'
        tk.save(str(path))
        self.tc = TokenCounter(tokenizer_path=str(path))   # no template: the built-in Qwen renderer

    def body(self, url):
        return {"messages": [{"role": "user", "content": parts(text('hello'), img(url))}]}

    def test_different_pictures_different_identity(self):
        a, b = prompt_text(self.body(PNG)), prompt_text(self.body(PNG2))
        self.assertIsNotNone(a)
        self.assertNotEqual(a, b)

    def test_same_picture_same_identity(self):
        self.assertEqual(prompt_text(self.body(PNG)), prompt_text(self.body(PNG)))

    def test_text_only_identity_unchanged(self):
        body = {"messages": [{"role": "user", "content": "hello"}]}
        self.assertEqual(prompt_text(body), prompt_text(body))
        self.assertNotIn('<img:', prompt_text(body))

    # ---- where the picture comes from decides whether an identity exists at all ----

    def test_a_data_uri_picture_keeps_its_content_identity(self):
        """A data: URI carries its bytes, so its hash names the picture: same URI -> same state, another URI ->
        another state. That is what makes a restore safe under the same marker."""
        self.assertIsNotNone(prompt_text(self.body(PNG)))
        self.assertEqual(prompt_text(self.body(PNG)), prompt_text(self.body(PNG)))
        self.assertNotEqual(prompt_text(self.body(PNG)), prompt_text(self.body(PNG2)))

    def test_an_external_picture_gives_no_state_identity(self):
        """An url or a path can hold different bytes on the next turn while naming the same picture, so a state
        saved under that identity could be restored with the previous image's embeddings: no identity, no save,
        no restore (the conservative answer, the same one an edited history gets)."""
        for url in ("https://example.com/shot.png", "http://intranet/shot.png",
                    "/var/tmp/shot.png", "file:///tmp/shot.png", "C:\\shots\\a.png"):
            with self.subTest(url=url):
                self.assertIsNone(prompt_text(self.body(url)))

    def test_an_external_picture_anywhere_in_the_prompt_gives_no_identity(self):
        body = {"messages": [{"role": "user", "content": parts(text('il grafico'), img(PNG))},
                             {"role": "assistant", "content": "guarda"},
                             {"role": "user", "content": parts(text('e questo?'),
                                                                img("https://example.com/other.png"))}]}
        self.assertIsNone(prompt_text(body))


class _AnchorReached(Exception):
    """The anchor request was about to go out: the prefix was anchored, not skipped."""


class AnchorStateTests(unittest.TestCase):
    """Masking anchors reuse engine state for a physical prefix: with a picture whose bytes live outside the
    prompt, the prefix hash would not notice the bytes changing, so no anchor is sealed or restored."""

    def setUp(self):
        self.store = Store()
        self.addCleanup(self.store.db.close)
        self.journal = Journal(None)
        self.mgr = Manager(Config(window=8000), self.store, TokenCounter(3.5), self.journal)

    class _Upstream:
        """slot() records; chat() says out loud that the anchor request was reached (the anchor path catches errors
        into an anchor_error event, so the message is what the test can see)."""

        def __init__(self):
            self.calls = []

        def slot(self, *a, **kw):
            self.calls.append(a)

        def chat(self, body):
            raise _AnchorReached('anchor request reached')

    def _anchor(self, phys):
        up = self._Upstream()
        events = self.mgr._anchor('conv1', 3, {'model': 'm', 'messages': phys}, phys, [0, 1, 2],
                                  [20000, 20000, 20000], None, {'frontier_index': 2}, up)
        return events, up

    def test_an_external_picture_skips_the_anchor(self):
        phys = [{'role': 'user', 'content': 'ciao'},
                {'role': 'user', 'content': parts(text('vedi?'), img("https://example.com/a.png"))},
                {'role': 'assistant', 'content': 'sì'}]
        events, up = self._anchor(phys)
        self.assertEqual([(e['event'], e.get('reason')) for e in events], [('anchor_skip', 'volatile_picture')])
        self.assertEqual(up.calls, [])                          # no save/restore request was even attempted
        self.assertIsNone(self.store.anchor('conv1', 3))         # and nothing was recorded as an anchor

    def test_a_data_uri_picture_still_anchors(self):
        """The conservative rule is about urls, not about pictures: a data: prefix is anchored exactly as before."""
        phys = [{'role': 'user', 'content': 'ciao'},
                {'role': 'user', 'content': parts(text('vedi?'), img(PNG))},
                {'role': 'assistant', 'content': 'sì'}]
        events, up = self._anchor(phys)
        self.assertEqual([e['event'] for e in events], ['anchor_error'])
        self.assertIn('anchor request reached', events[0]['error'])   # the prefix went to the engine, as it used to


class LiteralSlackPictureBytesTests(unittest.TestCase):
    """The literal special-token reserve (#28) must skip a picture's bytes wherever they hide in the payload,
    and keep counting them everywhere else: skipping them is a budget decision, not a way to stop looking."""

    class _Decoder:
        def __init__(self, *tags):
            self._m = {i: SimpleNamespace(special=True, content=t) for i, t in enumerate(tags)}

        def get_added_tokens_decoder(self):
            return self._m

    def setUp(self):
        self.mgr = SimpleNamespace(tc=SimpleNamespace(exact=True, tk=self._Decoder('<|im_start|>', '<|im_end|>')),
                                   piece_tokens=lambda text: 0, estimate=lambda *a: (0, []))
        self.g = WindowGuard(Config(window=8000), self.mgr, Journal(None))

    def test_nested_base64_is_not_scanned(self):
        """{"image_url": {"url": ...}} hides the bytes one level below the key that names them: the skip has to
        follow the picture down, or a megabyte of base64 is priced as prompt text."""
        blob = "data:image/png;base64," + ("<|im_start|>" * 200)
        msgs = [{'role': 'user', 'content': [{'type': 'image_url', 'image_url': {'url': blob}}]}]
        self.assertEqual(self.g.literal_slack(msgs, None), 0)

    def test_anthropic_nested_source_is_not_scanned(self):
        msgs = [{'role': 'user', 'content': [{'type': 'image', 'source':
                                              {'type': 'base64', 'media_type': 'image/png',
                                               'data': "<|im_end|>" * 500}}]}]
        self.assertEqual(self.g.literal_slack(msgs, None), 0)

    def test_a_plain_image_url_string_is_not_scanned(self):
        msgs = [{'role': 'user', 'content': [{'type': 'image_url', 'image_url': "data:image/png;base64,<|im_start|>"}]}]
        self.assertEqual(self.g.literal_slack(msgs, None), 0)

    def test_the_same_text_outside_a_picture_is_still_counted(self):
        tag = "<|im_start|>"                                    # 12 utf-8 bytes, reserved at 1 token each
        self.assertEqual(self.g.literal_slack([{'role': 'user', 'content': tag * 3}], None), 36)
        self.assertEqual(self.g.literal_slack(
            [{'role': 'user', 'content': [{'type': 'text', 'text': tag * 2}, {'type': 'image_url',
                                                                               'image_url': {'url': 'data:,<|im_start|>'}}]}],
            None), 24)

    def test_big_payload_stays_cheap(self):
        """One megabyte of base64 next to a real tag: the reserve is about the tag, not about the picture."""
        big = "A" * (1024 * 1024)
        msgs = [{'role': 'user', 'content': [{'type': 'image_url', 'image_url': {'url': "data:image/png;base64," + big}},
                                             {'type': 'text', 'text': 'nota <|im_end|>'}]}]
        self.assertEqual(self.g.literal_slack(msgs, None), 10)   # only the tag in the text part


class EstimatedPathTests(unittest.TestCase):
    """Without a tokenizer the guard estimates, but what the engine will read is not an estimation question: the
    structural refusals apply on that path too, and text-only prompts keep the estimate they always produced."""

    def setUp(self):
        self.journal = Journal(None)
        self.cfg = Config(window=8000)
        self.g = WindowGuard(self.cfg, Manager(self.cfg, Store(), TokenCounter(), self.journal), self.journal)

    def body(self, c):
        return {'messages': [{'role': 'user', 'content': c}], 'max_tokens': 50}

    def test_the_bound_is_added_to_the_estimated_budget(self):
        """`images` and `image_allowance` are set next to the number, not by it: a guard that recorded them but never
        added the reserve to the estimate would still look correct here, and would send a prompt over the window. So
        the test compares two budgets and the room the client is given.

        Measured shape (window 8000, image_tokens 1024): one picture costs the bound **plus the fixed per-message
        overhead**, and a bigger payload costs nothing more - the estimate prices the picture by the bound, never by
        the size of its base64."""
        plain = self.g.count([{'role': 'user', 'content': parts(text('guarda'))}], None)
        one = self.g.count([{'role': 'user', 'content': parts(text('guarda'), img(PNG))}], None)
        two = self.g.count([{'role': 'user', 'content': parts(text('guarda'), img(PNG), img(PNG2))}], None)
        per_picture = one - plain
        self.assertGreaterEqual(per_picture, self.cfg.image_tokens)   # the bound is in the number, not only in the field
        self.assertLess(per_picture, self.cfg.image_tokens + 64)      # the rest is message overhead, not payload bytes
        self.assertEqual(two - one, per_picture)                      # strictly linear in the pictures
        self.assertEqual((self.g.images, self.g.image_allowance), (2, 2 * self.cfg.image_tokens))
        huge = self.g.count([{'role': 'user', 'content': parts(text('guarda'),
                                                                img("data:image/png;base64," + "A" * 200000))}], None)
        self.assertEqual(huge, one)                                   # the bytes do not size the estimate
        # and the reserve is real in the decision too: the room shrinks by exactly what the pictures cost
        self.assertEqual(self.g.room(plain) - self.g.room(one), per_picture)
        self.assertEqual(self.g.room(plain) - self.g.room(two), 2 * per_picture)

    def test_a_picture_is_forwarded_and_priced(self):
        self.g.enforce(self.body(parts(text('patrz'), img(PNG))))
        self.assertEqual(self.g.images, 1)
        self.assertEqual(self.g.image_allowance, self.cfg.image_tokens)
        rec = [r for r in self.journal.mem if r['event'] == 'window_guard'][-1]
        self.assertEqual(rec['guarantee'], 'estimated')          # the count is still an estimate, honestly labelled
        self.assertEqual(rec['images'], 1)

    def test_a_picture_costs_the_bound_and_not_its_base64(self):
        # `images` alone would pass even if the estimate never added the bound: compare the budgets themselves.
        plain = self.g.count([{'role': 'user', 'content': 'patrz'}], None)
        small = self.g.count([{'role': 'user', 'content': parts(text('patrz'), img(PNG))}], None)
        huge = self.g.count([{'role': 'user', 'content': parts(text('patrz'), img(PNG + 'A' * 60000))}], None)
        self.assertGreaterEqual(small - plain, self.cfg.image_tokens)   # the bound really is in the number
        self.assertEqual(huge, small)                                   # and the bytes never change it

    def test_a_quoted_marker_is_not_a_picture_here_either(self):
        # the estimate counts pictures by their parts, so a marker quoted in text must not buy a bound
        plain = self.g.count([{'role': 'user', 'content': 'patrz'}], None)
        quoted = self.g.count([{'role': 'user', 'content': 'patrz ' + MARKER}], None)
        self.assertEqual(self.g.count_errors, 0)
        self.assertEqual((self.g.images, self.g.image_allowance), (0, 0))
        self.assertLess(quoted - plain, self.cfg.image_tokens)

    def test_what_the_engine_would_not_read_is_refused_here_too(self):
        cases = (([{'role': 'system', 'content': parts(text('instrukcje'), img(PNG))},
                   {'role': 'user', 'content': 'ciao'}], 'system message cannot carry a picture'),
                 ([{'role': 'user', 'content': [{'type': 'input_image', 'file_id': 'file-1'}]}],
                  'no readable source'),
                 ([{'role': 'user', 'content': [text('x'), {'type': 'video_url', 'video_url': {'url': 'v'}}]}],
                  'video content is not supported'))
        for msgs, why in cases:
            with self.subTest(why=why):
                before = self.g.count_errors
                with self.assertRaises(WindowExceeded) as ctx:
                    self.g.enforce({'messages': msgs, 'max_tokens': 50})
                self.assertIn(why, str(ctx.exception))
                self.assertEqual(self.g.count_errors, before + 1)     # one refusal, one counted error

    def test_a_picture_under_a_zero_bound_is_refused(self):
        guard = WindowGuard(Config(window=8000, image_tokens=0),
                           Manager(self.cfg, Store(), TokenCounter(), self.journal), Journal(None))
        with self.assertRaises(WindowExceeded) as ctx:
            guard.enforce(self.body(parts(text('patrz'), img(PNG))))
        self.assertIn('image_tokens=0', str(ctx.exception))

    def test_text_only_prompts_keep_the_estimate_they_always_had(self):
        import math
        msgs = [{'role': 'system', 'content': 'sistema'}, {'role': 'user', "content": 'hello'}]
        est, _ = self.g.mgr.estimate(msgs, None)
        slack = float(getattr(self.cfg, 'window_estimate_slack', 0.0) or 0.0)
        self.assertEqual(self.g.count(msgs, None), int(math.ceil(est * (1.0 + slack))))
        self.assertEqual(self.g.images, 0)
        self.g.enforce({'messages': msgs, 'max_tokens': 50})
        self.assertNotIn('images', [r for r in self.journal.mem if r['event'] == 'window_guard'][-1])


@unittest.skipUnless(EXACT, 'install mnemonic-proxy[exact]')
class LiteralMarkerTests(unittest.TestCase):
    """Someone quoting `<|vision_start|><|image_pad|><|vision_end|>` is writing about the template, not attaching a
    picture. The template prints the quote verbatim, so the rendered prompt holds a real spelling of the marker that
    no picture produced. Counting it as one would price a few characters as a whole picture and - because the guard
    requires the markers to *be* the pictures - refuse a text-only question outright. Strata shields literal
    spellings (#28), so the engine will not read a picture there either, and the autosave stamp must not label one:
    a stamp absorbed by a quotation leaves a real picture named by another picture's hash."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)
        fx = exact_fixture(self.path)
        self.cfg, self.journal, self.store = fx['cfg'], fx['journal'], fx['store']
        self.mgr, self.g = fx['mgr'], fx['guard']
        self.addCleanup(self.store.db.close)

    def count(self, content):
        return self.g.count([{'role': 'user', 'content': content}], None)

    def test_a_quoted_marker_is_not_a_picture(self):
        plain = self.count('hello')
        quoted = self.count('hello ' + MARKER)
        self.assertEqual(self.g.count_errors, 0)        # markers == pictures still holds: the quote is not one
        self.assertEqual((self.g.images, self.g.image_allowance), (0, 0))
        self.assertLess(quoted - plain, self.cfg.image_tokens)   # priced as the text it is, not as a picture

    def test_a_quoted_marker_does_not_hide_a_real_picture(self):
        self.count(parts(text('hello ' + MARKER), img(PNG)))
        self.assertEqual(self.g.count_errors, 0)
        self.assertEqual((self.g.images, self.g.image_allowance), (1, self.cfg.image_tokens))

    def test_quoted_markers_do_not_shift_the_stamps_of_real_pictures(self):
        body = {"messages": [{"role": "user", "content": parts(text('a ' + MARKER + ' b'), img(PNG), img(PNG2))}]}
        t = prompt_text(body)
        self.assertIsNotNone(t)
        self.assertEqual(t.count('<img:'), 2)                # one stamp per picture: the quote absorbed none
        self.assertIn('<img:%s>' % image_hash(img(PNG))[:16], t)    # each picture named by its own hash, in order
        self.assertIn('<img:%s>' % image_hash(img(PNG2))[:16], t)
        self.assertIn(MARKER, t)                             # the quotation is still there, as text

    def test_a_quoted_marker_still_changes_the_identity(self):
        a = {"messages": [{"role": "user", "content": 'hello'}]}
        b = {"messages": [{"role": "user", "content": 'hello ' + MARKER}]}
        self.assertNotEqual(prompt_text(a), prompt_text(b))


class PictureBudgetConfigTests(unittest.TestCase):
    """image_tokens is the operator's promise about the engine's clamp: a value that cannot bound anything is a
    typo, and it is refused where the configuration is assembled."""

    def test_negative_and_non_integer_bounds_are_refused(self):
        for bad in (-1, -1024, "1024", 1.5, True, None):
            with self.subTest(image_tokens=bad):
                with self.assertRaises(ValueError) as ctx:
                    Config.from_dict({"window": 8000, "image_tokens": bad})
                self.assertIn('image_tokens', str(ctx.exception))

    def test_zero_is_allowed_means_pictures_refused(self):
        cfg = Config.from_dict({"window": 8000, "image_tokens": 0})
        self.assertEqual(cfg.image_tokens, 0)                   # legal: it says 'no pictures', the guard enforces it
        self.assertEqual(Config.from_dict({"window": 8000, "image_tokens": 302}).image_tokens, 302)
        self.assertEqual(Config().image_tokens, 1024)           # the default still bounds the engine's GPU clamp

    def test_the_default_config_bounds_a_picture(self):
        self.assertGreater(Config().image_tokens, 0)


class NoPictureIsSilentTests(unittest.TestCase):
    """The hard rule: a picture is never removed without saying so."""

    def test_receipt_names_the_removed_picture(self):
        loop = [{'role': 'tool', 'tool_call_id': 'c1', 'content': parts(text('output'), img(PNG))}]
        remove_piece(loop, {'i': 0, 'kind': 'result', 'text': 'output'}, 'recall', 'en', lambda t: 500)
        self.assertIn('<immagine:', loop[0]['content'])
        self.assertNotIn('iVBORw0KGgo', loop[0]['content'])

    def test_markers_match_the_archive_placeholder(self):
        from ctxproxy.core import content_text
        c = parts(text('x'), img(PNG), img(PNG2))
        # Same hashes, same order as the row the archive keeps: a receipt and a recall say the same thing.
        self.assertEqual(content_text(c).replace('\n', '').removeprefix('x'), image_markers(c))

    def test_the_archive_names_every_shape_of_picture_the_same_way(self):
        """One extraction for both sides, whatever shape the client sent: an archive row and a receipt must not
        name two different pictures for one part - and must not name the hash of an empty source."""
        from ctxproxy.core import content_text
        from ctxproxy.render import image_hashes
        for part in (img(PNG),
                     {"type": "input_image", "image_url": PNG},
                     {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AAAA"}},
                     {"type": "image", "source": {"type": "url", "url": "https://example.com/a.png"}}):
            with self.subTest(part=part.get("type")):
                c = [text('x'), part]
                self.assertEqual(content_text(c).replace('\n', '').removeprefix('x'), image_markers(c))
                empty = image_hashes([{"type": "image_url", "image_url": {"url": ""}}])[0]
                self.assertNotEqual(image_hashes([part])[0], empty)     # the content is named, not nothing

    def test_redaction_keeps_text_and_drops_bytes(self):
        msgs = [{'role': 'user', 'content': parts(text('guarda'), img(PNG))}, {'role': 'user', 'content': 'ciao'}]
        red = redact_images(msgs)
        self.assertNotIn('iVBORw0KGgo', str(red))
        self.assertIn('<immagine:', str(red))
        self.assertIn('guarda', str(red))
        self.assertEqual(count_images(msgs), 1)
        self.assertEqual(count_images([{'role': 'user', 'content': 'solo testo'}]), 0)


class ProxyPassesPicturesTests(unittest.TestCase):
    """End to end with the fake engine: the proxy manages memory around a picture, it never eats it.

    Runs without a tokenizer on purpose, so it is not skipped in an environment without the [exact] extra."""

    def setUp(self):
        from ctxproxy.core import Config, Journal, Store, TokenCounter
        from ctxproxy.fake_engine import FakeEngine
        from ctxproxy.server import Proxy
        from ctxproxy.upstream import Upstream
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.jpath = os.path.join(self.tmp.name, "journal.jsonl")
        self.eng = FakeEngine(lambda body: {"role": "assistant", "content": "vedo un grafico"})
        self.proxy = Proxy(Config.from_dict({"window": 60000}), Upstream(self.eng.start()),
                           Store(os.path.join(self.tmp.name, "a.sqlite")), Journal(self.jpath), TokenCounter(3.5))

    def tearDown(self):
        self.eng.stop()
        self.proxy.store.db.close()
        self.tmp.cleanup()

    def _events(self, name):
        if not os.path.exists(self.jpath):
            return []
        with open(self.jpath, encoding="utf-8") as f:
            return [e for e in (json.loads(line) for line in f) if e["event"] == name]

    def test_picture_reaches_the_engine_untouched(self):
        msgs = [SYSTEM, {"role": "user", "content": parts(text("co vedi?"), img(PNG))}]
        st, resp, _ = self.proxy.chat({"model": "m", "messages": msgs, "max_tokens": 64})
        self.assertEqual(st, 200, resp)
        sent = self.eng.requests[-1]["messages"][-1]["content"]
        self.assertIsInstance(sent, list)                                   # not flattened to a string
        self.assertEqual([p.get("type") for p in sent], ["text", "image_url"])
        self.assertEqual(sent[1]["image_url"]["url"], PNG)                   # bytes kept, not a placeholder

    def test_request_line_reports_the_pictures_it_sent(self):
        msgs = [SYSTEM, {"role": "user", "content": parts(text("co vedi?"), img(PNG))}]
        self.proxy.chat({"model": "m", "messages": msgs, "max_tokens": 64})
        req = [e for e in self._events("request") if e.get("round") == 0][-1]
        self.assertEqual(req["images"], 1)

    def test_text_only_request_is_not_marked_as_multimodal(self):
        self.proxy.chat({"model": "m", "messages": [SYSTEM, {"role": "user", "content": "ciao"}], "max_tokens": 64})
        req = [e for e in self._events("request") if e.get("round") == 0][-1]
        self.assertNotIn("images", req)


if __name__ == '__main__':
    unittest.main()
