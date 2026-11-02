"""The pack's REAL chat template, without a GPU, a model or the network.

`tests/fixtures/pack_chat_template.jinja` is the template the pinned pack ships
(<Strata-data>/packs/<pack>/tokenizer/chat_template.jinja, sha256
12827f24b742ea4e80cdc12dbcf9622227056b9f797252a3149263d4f9aaadce, LF). It is the file Strata hands Jinja - including
the branches a reduced fixture would hide: the merged system block that raises on pictures, the `Picture N:`
counter, the `<tool_response>` framing and the reasoning instructions - not another hand-written Qwen template.
`tests/test_vision_passthrough.py` keeps the small template for the unit-level questions; this file asks what the
real one does, end to end, with a synthetic tokenizer (the assertions are about markers, allowances and bytes,
never about a real vocabulary).

Point `MNEMONIC_TEST_PACK_TEMPLATE` at a template to run the same tests against the live pack file: the drift test
then checks the committed copy still matches it.
"""
import os
import tempfile
import unittest
from pathlib import Path

from ctxproxy.core import Config, Journal, Manager, Store, TokenCounter
from ctxproxy.render import VISION
from ctxproxy.window import WindowGuard, WindowExceeded

try:
    import jinja2                      # noqa: F401
    from tokenizers import Tokenizer, models, pre_tokenizers
    EXACT = True
except ImportError:
    EXACT = False

FIXTURE = Path(__file__).parent / "fixtures" / "pack_chat_template.jinja"
FIXTURE_SHA = "12827f24b742ea4e80cdc12dbcf9622227056b9f797252a3149263d4f9aaadce"
LIVE = os.environ.get("MNEMONIC_TEST_PACK_TEMPLATE")          # the real pack file, when the runner has one

PNG = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUg"
PNG2 = "data:image/png;base64,AAAAanotherpicture"
SYSTEM = {"role": "system", "content": "assistente"}


def text(t):
    return {"type": "text", "text": t}


def img(url):
    return {"type": "image_url", "image_url": {"url": url}}


def parts(*items):
    return list(items)


def template_bytes() -> bytes:
    """The bytes of the template under test: the live pack file when one is pointed at, the committed copy.

    Normalized to LF: that is what the pack ships and what the engine's Jinja reads, while a Windows working tree can
    hand the copy over with CRLF (`git apply` with core.autocrlf=true rewrites a new file). Reading the bytes the
    engine reads is what makes the counts here the engine's counts; `.gitattributes` keeps the committed copy
    byte-exact so this is only about transport."""
    return (Path(LIVE).read_bytes() if LIVE else FIXTURE.read_bytes()).replace(b"\r\n", b"\n")


@unittest.skipUnless(EXACT, 'install mnemonic-proxy[exact]')
class RealTemplateTests(unittest.TestCase):
    """Guard + pack tokenizer + the pack's own template: what a picture costs and what never reaches the prompt."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        d = Path(self.tmp.name)
        tk = Tokenizer(models.WordLevel({'[UNK]': 0, 'hello': 1, 'user': 2, 'assistant': 3}, unk_token='[UNK]'))
        tk.pre_tokenizer = pre_tokenizers.WhitespaceSplit()
        tk.save(str(d / 'tokenizer.json'))
        (d / 'chat_template.jinja').write_bytes(template_bytes())
        self.tc = TokenCounter(tokenizer_path=str(d / 'tokenizer.json'))
        self.assertIsNotNone(self.tc.prompt_template)              # the real template is the one loaded
        self.store = Store()
        self.addCleanup(self.store.db.close)
        self.cfg = Config(window=4000, window_margin=64, image_tokens=320)
        self.journal = Journal(None)
        self.mgr = Manager(self.cfg, self.store, self.tc, self.journal)
        self.g = WindowGuard(self.cfg, self.mgr, self.journal)

    def body(self, content, mt=200):
        return {'messages': [{'role': 'user', 'content': content}], 'max_tokens': mt}

    # ---- the picture is priced, once each ----

    def test_one_picture_writes_one_marker(self):
        rendered, markers = self.tc.prompt_template.render(
            [{'role': 'user', 'content': parts(text('hello'), img(PNG))}], None, {}, with_counts=True)
        self.assertEqual(markers, 1)
        self.assertEqual(rendered.count(VISION), 1)
        self.assertEqual(rendered.count('<|image_pad|>'), 1)

    def test_the_allowance_is_actually_charged(self):
        plain = self.g.count([{'role': 'user', 'content': 'hello'}], None)
        with_img = self.g.count([{'role': 'user', 'content': parts(text('hello'), img(PNG))}], None)
        self.assertEqual(self.g.images, 1)
        self.assertEqual(self.g.image_allowance, self.cfg.image_tokens)
        self.assertEqual(with_img - plain, self.cfg.image_tokens)   # the bound, not zero and not the base64 size

    def test_two_pictures_two_markers_two_allowances(self):
        msgs = [{'role': 'user', 'content': parts(img(PNG), text('e questa?'), img(PNG2))}]
        self.g.count(msgs, None)
        self.assertEqual(self.g.images, 2)
        self.assertEqual(self.g.image_allowance, 2 * self.cfg.image_tokens)

    def test_request_with_a_picture_is_not_refused(self):
        body = self.g.enforce(self.body(parts(text('hello'), img(PNG))))
        self.assertEqual(body['max_tokens'], 200)                   # room left: the request goes through
        self.assertEqual(self.g.count_errors, 0)

    # ---- parts stay parts, bytes stay out of the prompt ----

    def test_text_and_picture_stay_separate_parts(self):
        from ctxproxy.fake_engine import FakeEngine
        from ctxproxy.server import Proxy
        from ctxproxy.upstream import Upstream
        jpath = os.path.join(self.tmp.name, "journal.jsonl")
        eng = FakeEngine(lambda body: {"role": "assistant", "content": "vedo un grafico"})
        self.addCleanup(eng.stop)
        proxy = Proxy(Config.from_dict({"window": 60000}), Upstream(eng.start()),
                      Store(os.path.join(self.tmp.name, "a.sqlite")), Journal(jpath), self.tc)
        self.addCleanup(proxy.store.db.close)
        st, resp, _ = proxy.chat({"model": "m", "max_tokens": 64,
                                  "messages": [SYSTEM, {"role": "user",
                                                        "content": parts(text("cosa vedi?"), img(PNG))}]})
        self.assertEqual(st, 200, resp)
        sent = eng.requests[-1]["messages"][-1]["content"]
        self.assertIsInstance(sent, list)
        self.assertEqual([p.get("type") for p in sent], ["text", "image_url"])
        self.assertEqual(sent[1]["image_url"]["url"], PNG)           # bytes kept: the engine decodes the picture

    def test_the_bytes_never_enter_the_prompt(self):
        rendered = self.tc.prompt_template.render(
            [{'role': 'user', 'content': parts(text('guarda'), img(PNG))}], None, {})
        self.assertIn('<|image_pad|>', rendered)
        self.assertNotIn('iVBORw0KGgo', rendered)
        self.assertNotIn('base64', rendered)
        self.assertLess(len(rendered), 1200)                        # a prompt, not a picture in base64

    # ---- the other branches of the real template ----

    def test_pictures_in_assistant_and_tool_turns_are_written(self):
        """The template renders content for every role past the system block, so a picture in a tool result is a
        marker the engine will expand - and a marker the guard has to price."""
        msgs = [{'role': 'user', 'content': 'leggi lo screenshot'},
                {'role': 'assistant', 'content': 'ok', 'tool_calls':
                    [{'id': 'c1', 'type': 'function', 'function': {'name': 'shot', 'arguments': {}}}]},
                {'role': 'tool', 'tool_call_id': 'c1', 'content': parts(text('fatto'), img(PNG))},
                {'role': 'assistant', 'content': parts(text('ecco'), img(PNG2))},
                {'role': 'user', 'content': 'grazie'}]
        rendered, markers = self.tc.prompt_template.render(msgs, None, {}, with_counts=True)
        self.assertEqual(markers, 2)
        self.assertIn('<tool_response>', rendered)
        self.g.count(msgs, None)
        self.assertEqual(self.g.images, 2)
        self.assertEqual(self.g.image_allowance, 2 * self.cfg.image_tokens)

    def test_a_late_system_message_becomes_a_user_turn_and_keeps_its_picture(self):
        msgs = [{'role': 'user', 'content': 'ciao'}, {'role': 'system', 'content': parts(text('nota'), img(PNG))}]
        rendered, markers = self.tc.prompt_template.render(msgs, None, {}, with_counts=True)
        self.assertEqual(markers, 1)                                # as _late_system_to_user does on the engine
        self.g.count(msgs, None)
        self.assertEqual(self.g.images, 1)

    # ---- nothing is lost quietly ----

    def test_a_system_message_picture_is_refused(self):
        """The engine reads a system message's text parts only and the pack template raises on its pictures: the
        bytes would never reach the model, so the request is refused instead of counted."""
        msgs = [dict(SYSTEM, content=parts(text('base'), img(PNG))), {'role': 'user', 'content': 'hello'}]
        with self.assertRaises(WindowExceeded) as ctx:
            self.g.enforce({'messages': msgs, 'max_tokens': 50})
        self.assertIn('system message cannot carry a picture', str(ctx.exception))
        self.assertEqual(self.g.count_errors, 1)

    def test_an_unreadable_picture_is_refused_not_emptied(self):
        for part, why in (({'type': 'input_image', 'file_id': 'file-1'}, 'no readable source'),
                          ({'type': 'image_url', 'image_url': {'url': ''}}, 'no readable source'),
                          ({'type': 'text', 'text': 'x', 'image_url': img(PNG)}, 'does not read as an image')):
            msgs = [{'role': 'user', 'content': [part]}]
            with self.subTest(part=part.get('type')):
                with self.assertRaises(WindowExceeded) as ctx:
                    self.g.enforce({'messages': msgs, 'max_tokens': 50})
                self.assertIn(why, str(ctx.exception))
                self.assertIn('not sent', str(ctx.exception))
        self.assertEqual(self.g.count_errors, 3)

    def test_video_next_to_a_picture_is_refused(self):
        msgs = [{'role': 'user', 'content': parts(img(PNG), {'type': 'video_url', 'video_url': {'url': 'x'}})}]
        with self.assertRaises(WindowExceeded):
            self.g.enforce({'messages': msgs, 'max_tokens': 50})
        self.assertEqual(self.g.count_errors, 1)

    def test_a_picture_needs_a_positive_bound(self):
        """image_tokens <= 0 prices a picture at nothing: the prompt would be counted smaller than the engine will
        read it, so a request carrying a picture is refused until the bound is a number that bounds."""
        self.cfg.image_tokens = 0
        with self.assertRaises(WindowExceeded) as ctx:
            self.g.enforce(self.body(parts(text('hello'), img(PNG))))
        self.assertIn('image_tokens=0', str(ctx.exception))
        self.assertEqual(self.g.count_errors, 1)
        # a text-only prompt is untouched by the broken bound: the guard has nothing to over-promise
        self.assertEqual(self.g.enforce(self.body('hello'))['max_tokens'], 200)
        self.assertEqual(self.g.count_errors, 1)

    # ---- text stays exactly what it was ----

    def test_text_only_prompt_is_unaffected(self):
        msgs = [SYSTEM, {'role': 'user', 'content': 'hello'}]
        prompt = self.g.count(msgs, None)
        self.assertEqual(self.g.images, 0)
        self.assertEqual(prompt, self.mgr.piece_tokens(self.tc.prompt_template.render(msgs, None, {})))
        self.g.enforce({'messages': msgs, 'max_tokens': 100})
        rec = [r for r in self.journal.mem if r['event'] == 'window_guard'][-1]
        self.assertNotIn('images', rec)                              # text-only journal lines stay byte-identical
        self.assertNotIn('image_allowance', rec)

    def test_the_journal_says_bounded_not_exact(self):
        self.g.enforce(self.body(parts(img(PNG))))
        rec = [r for r in self.journal.mem if r['event'] == 'window_guard'][-1]
        self.assertEqual(rec['guarantee'], 'image_bounded')
        self.assertEqual(rec['images'], 1)
        self.assertEqual(rec['image_allowance'], self.cfg.image_tokens)


class TemplateCopyTests(unittest.TestCase):
    """The committed copy is the pack's file, and says so: a drifted copy would test a template nobody runs."""

    def test_the_copy_is_here_and_not_empty(self):
        self.assertTrue(FIXTURE.is_file(), "tests/fixtures/pack_chat_template.jinja is missing")
        raw = FIXTURE.read_bytes()
        self.assertIn(b'<|vision_start|><|image_pad|><|vision_end|>', raw)
        self.assertIn(b'System message cannot contain images.', raw)     # the branch the guard mirrors

    def test_the_copy_is_the_template_it_claims(self):
        import hashlib
        # The copy IS the artifact under test: if its content changed, the tests below render a template no engine
        # loads. Line endings are normalized at load (see template_bytes), so this catches content drift only.
        self.assertEqual(hashlib.sha256(template_bytes()).hexdigest(), FIXTURE_SHA,
                         "tests/fixtures/pack_chat_template.jinja is not the pack's template bytes any more")

    def test_the_copy_arrived_without_rewritten_line_endings(self):
        if FIXTURE.read_bytes() == template_bytes():
            return
        self.skipTest("the copy reached this checkout with CRLF (git apply with core.autocrlf=true rewrites new "
                      "files); the bytes are normalized at load, so the tests above still read the pack's template")

    def test_the_copy_matches_the_live_pack_template(self):
        if not LIVE:
            self.skipTest('set MNEMONIC_TEST_PACK_TEMPLATE to the pack template to check for drift')
        import hashlib
        self.assertEqual(hashlib.sha256(template_bytes()).hexdigest(), FIXTURE_SHA,
                         "the pack template changed: re-copy it into tests/fixtures and re-check the guard's "
                         "contract against the new branches")


if __name__ == '__main__':
    unittest.main()
