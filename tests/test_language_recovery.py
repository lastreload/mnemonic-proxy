"""Interrupted first preparation must not turn a new conversation into legacy Italian.

The engine is a local HTTP fake: assertions cover actual request payloads, not
language choices of a real model. Run: python -m unittest tests.test_language_recovery
"""
import json
import os
import tempfile
import unittest
from unittest.mock import patch

from ctxproxy.core import Config, Journal, Store, TokenCounter, chain
from ctxproxy.fake_engine import FakeEngine
from ctxproxy.lang import LANG_KEY
from ctxproxy.server import Proxy
from ctxproxy.upstream import Upstream


class TestLanguageRecovery(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.engine = FakeEngine(lambda body: {"role": "assistant", "content": "ok"})
        self.url = self.engine.start()
        self.addCleanup(self.engine.stop)
        self.db = os.path.join(self.tmp.name, "archive.sqlite")
        self.req = {"model": "fake", "max_tokens": 128, "messages": [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "Please explain the test results in English."}]}

    def proxy(self, **kwargs):
        store = Store(self.db)
        self.addCleanup(store.db.close)
        return Proxy(Config.from_dict({"window": 32768, **kwargs}), Upstream(self.url),
                     store, Journal(None), TokenCounter(3.5))

    def test_interrupted_first_prepare_keeps_english_payload_after_restart(self):
        proxy = self.proxy()
        with patch.object(proxy.store, "archive_many", side_effect=OSError("injected archive interruption")):
            with self.assertRaisesRegex(OSError, "injected archive interruption"):
                proxy.chat(self.req)
        conv, _ = proxy.store.find_conv(chain(self.req["messages"], None))
        assert conv is not None
        self.assertTrue(proxy.store.has_conv(conv))
        self.assertEqual(self.engine.requests, [], "no payload was sent before the interruption")
        proxy.store.db.close()
        resumed = self.proxy()
        code, _, _ = resumed.chat(self.req)
        self.assertEqual(code, 200)
        payload = self.engine.requests[-1]
        # Optional evidence dump for the offline RED/GREEN reproduction.
        if os.environ.get("LANGUAGE_PAYLOAD_DUMP"):
            with open(os.environ["LANGUAGE_PAYLOAD_DUMP"], "w") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
        text = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn("Recupera il testo ORIGINALE", text)
        self.assertEqual(resumed.store.kv_get(LANG_KEY + conv), "en")
        self.assertIn('"name": "recall"', text)
        self.assertEqual(payload["messages"], self.req["messages"])


    def test_failure_boundaries_keep_all_defaults_with_and_without_hint(self):
        from ctxproxy.core import NAMES_KEY, RECEIPTS_KEY
        for method in ("add_chain", "archive_many", "set_chain", "_scan_marks", "_scan_fileops"):
            for hint in (None, "explicit-conversation"):
                for lang in ("en", "it"):
                    with self.subTest(method=method, hint=hint, lang=lang):
                        self.db = os.path.join(self.tmp.name, "%s-%s-%s.sqlite" % (method, hint, lang))
                        proxy = self.proxy(prompt_language=lang, rich_receipts=True)
                        target = proxy.mgr if method.startswith("_scan") else proxy.store
                        original = getattr(target, method)

                        def interrupted(*args, **kwargs):
                            original(*args, **kwargs)
                            raise OSError("injected interruption")

                        with patch.object(target, method, side_effect=interrupted):
                            with self.assertRaisesRegex(OSError, "injected interruption"):
                                proxy.chat(self.req, hint=hint)
                        conv, _ = proxy.store.find_conv(chain(self.req["messages"], None))
                        assert conv is not None
                        self.assertEqual(proxy.store.kv_get(LANG_KEY + conv), lang)
                        stored_names = proxy.store.kv_get(NAMES_KEY + conv)
                        assert isinstance(stored_names, str)
                        self.assertEqual(json.loads(stored_names)["recall"], "recall")
                        self.assertEqual(proxy.store.kv_get(RECEIPTS_KEY + conv), "rich")
                        proxy.store.db.close()
                        resumed = self.proxy(prompt_language="it" if lang == "en" else "en")
                        self.assertEqual(resumed.chat(self.req, hint=hint)[0], 200)
                        self.assertEqual(resumed.mgr.lang, lang)
                        first = self.engine.requests[-1]
                        self.assertEqual(resumed.chat(self.req, hint=hint)[0], 200)
                        self.assertEqual(self.engine.requests[-1], first)

    def test_shared_manager_preserves_user_languages_and_stored_choice(self):
        proxy = self.proxy()
        for hint, config, expected, user in (
            ("english", "en", "en", "Please answer in English."),
            ("italian", "it", "it", "Rispondi in italiano, per favore."),
            ("french", "en", "en", "Réponds en français. Citation: buongiorno."),
            ("english", "it", "en", "Please answer in English."),
        ):
            with self.subTest(hint=hint, config=config):
                proxy.cfg.prompt_language = config
                req = {**self.req, "messages": [{"role": "user", "content": user}]}
                self.assertEqual(proxy.chat(req, hint=hint)[0], 200)
                self.assertEqual(proxy.mgr.lang, expected)
                self.assertEqual(self.engine.requests[-1]["messages"], req["messages"])

    def test_recovered_session_segment_notes_pins_and_recall(self):
        from ctxproxy.core import is_notes_request, rid_for
        from tests.lang_scenario import tool_round
        for status, notes in (
            ("clean", {"content": "Keep the user's chosen language. Citation: buongiorno."}),
            ("recovered", {"content": "", "reasoning_content": "Répondre en français; dato originale."}),
            ("fallback", {"content": ""}),
        ):
            with self.subTest(status=status):
                self.db = os.path.join(self.tmp.name, status + ".sqlite")
                cfg = {"window": 40000, "mask_enabled": False, "tail_max": 12000,
                       "reserve": 2000, "default_response": 2000, "slot_save": False}
                proxy = self.proxy(**cfg)
                with patch.object(proxy.store, "archive_many", side_effect=OSError("interrupted")):
                    with self.assertRaises(OSError):
                        proxy.chat(self.req)
                proxy.store.db.close()
                proxy = self.proxy(**cfg)
                self.engine.policy = lambda body: ({"role": "assistant", **notes}
                    if is_notes_request(str(body["messages"][-1].get("content", "")))
                    else {"role": "assistant", "content": "ok"})
                h = self.req["messages"] + [
                    {"role": "assistant", "content": "ok"},
                    {"role": "user", "content": "📌 Answer in the user's requested language. Quote: buongiorno.\nContinue."}]
                frames, physical = [], {}
                for k in range(1, 14):
                    h += tool_round(k, 24000)
                    self.assertEqual(proxy.chat({**self.req, "messages": h})[0], 200)
                    physical = self.engine.requests[-1]
                    frames = [m["content"] for m in physical["messages"]
                              if isinstance(m.get("content"), str)
                              and m["content"].startswith("[strata-context: segment ")]
                    if frames:
                        break
                self.assertTrue(frames, "must exercise a real segment switch")
                frame = frames[0]
                self.assertIn("## Handoff notes", frame)
                self.assertNotIn("Note di passaggio", frame)
                if status == "clean":
                    self.assertIn(notes["content"], frame)
                elif status == "recovered":
                    self.assertIn(notes["reasoning_content"], frame)
                    self.assertIn("Reasoning at the segment switch:", frame)
                else:
                    self.assertIn("notes not available", frame)
                self.assertIn("[strata-context: pinned points]", json.dumps(physical))
                # The stored segment is replayed exactly, even after restart/config change.
                proxy.store.db.close()
                proxy = self.proxy(**cfg, prompt_language="it")
                self.assertEqual(proxy.chat({**self.req, "messages": h})[0], 200)
                self.assertEqual(self.engine.requests[-1]["messages"], physical["messages"])
                self.assertEqual(self.engine.requests[-1]["tools"], physical["tools"])
                conv, _ = proxy.store.find_conv(chain(h, None))
                # Return an intentional foreign-language quote verbatim through actual recall.
                target = rid_for(3, h[3])
                def recall_policy(body):
                    if body["messages"][-1]["role"] == "tool":
                        return {"role": "assistant", "content": "ok"}
                    from tests.lang_scenario import call
                    return {"role": "assistant", "content": "", "tool_calls": [call("r1", "recall", id=target)]}
                self.engine.policy = recall_policy
                self.assertEqual(proxy.chat({**self.req, "messages": h + [
                    {"role": "assistant", "content": "ok"}, {"role": "user", "content": "Recall the pinned instruction."}]})[0], 200)
                recall = [m["content"] for m in self.engine.requests[-1]["messages"]
                          if m.get("tool_call_id") == "r1"]
                self.assertTrue(recall)
                self.assertIn(h[3]["content"], recall[0])
                self.assertIn("message", recall[0])
                self.assertEqual(proxy.store.get(target, conv)[5], h[3]["content"])


if __name__ == "__main__":
    unittest.main()
