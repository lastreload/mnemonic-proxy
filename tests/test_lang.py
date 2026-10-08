"""0.3.1: language of the texts the proxy writes into the prompt.   python3 -m unittest tests.test_lang

- new conversation -> every text inserted by the proxy is English (no Italian word in the physical prompt);
- conversation created by 0.3.0 (no stored language) -> physical prompt identical byte for byte to 0.3.0;
- mixed recognition: Italian markers inside an English conversation and the reverse (receipts, placeholders, guard);
- recall, MCP server and fake_engine work in both languages.
Author: Maurizio Verde — LastReload
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ctxproxy import core  # noqa: E402
from ctxproxy.core import (Config, Journal, Manager, Store, TokenCounter, is_placeholder, recall_tool_def)  # noqa: E402
from ctxproxy.lang import LANG_KEY, norm  # noqa: E402
from ctxproxy.paging import (GUARD_MSG, GUARD_MSG_EN, guard_msg, is_guarded, is_tools_result, receipt_contaminated,  # noqa: E402
                             typed_receipt)
from tests.lang_scenario import prompt_text, run_session  # noqa: E402

# words of the Italian texts of 0.3.0 (BRIEF: "omesso", "conversazione", "gestore del contesto", "nascost", "segmento")
IT_WORDS = ("omess", "conversazione", "gestore del contesto", "nascost", "segmento", "Ricevuta", "Inizio:",
            "messaggio", "archiviat", "richiamo", "ragionament", "esito", "riuscito", "fallito", "punti fermi",
            "Note di passaggio", "definizioni caricate", "uscita", "usa e getta", "prosegue", "risultati", "nessun",
            "Collegamenti", "strumento")

# sha256 of the 26 engine requests of tests/lang_scenario.py produced by ctxproxy 0.3.0 (a139f3b): see
# RESULT.md for how it was computed (same scenario run with PYTHONPATH pointing to a 0.3.0 checkout)
GOLDEN_030 = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "lang_golden_030.sha256")


def digest(reqs: list) -> list[str]:
    return [hashlib.sha256(json.dumps({"c": r["conv"], "m": r["messages"], "t": r["tools"]}, ensure_ascii=False,
                                      sort_keys=True).encode()).hexdigest() for r in reqs]


class TestScenario(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        d = cls.tmp.name
        for name in ("en", "legacy", "it"):
            os.makedirs(os.path.join(d, name))
        cls.en = run_session(os.path.join(d, "en"))
        cls.legacy = run_session(os.path.join(d, "legacy"), legacy_until=1)
        cls.it = run_session(os.path.join(d, "it"), {"prompt_language": "it"})

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_scenario_covers_everything(self):
        t = prompt_text(self.en)
        for k in ("\u27eactx-archive", "Receipt:", "[context manager: automatic recall]", "call NOT executed",
                  "[tools: definitions loaded:", "was NOT executed", "\u00abdisposable\u00bb", "context manager note",
                  "[strata-context: handoff notes request]", "[strata-context: segment 1]",
                  "[strata-context: pinned points]", "[recall: 1 results for"):
            self.assertIn(k, t)

    def test_new_conversation_is_english(self):
        t = prompt_text(self.en)
        found = [w for w in IT_WORDS if w.lower() in t.lower()]
        self.assertEqual(found, [], "Italian words in the prompt of a new conversation")

    def test_old_conversation_byte_identical_to_030(self):
        with open(GOLDEN_030) as f:
            golden = f.read().split()
        self.assertEqual(digest(self.legacy), golden)

    def test_prompt_language_it_is_030(self):
        with open(GOLDEN_030) as f:
            golden = f.read().split()
        self.assertEqual(digest(self.it), golden)

    def test_same_shape_in_both_languages(self):
        self.assertEqual([(r["conv"], r["step"], len(r["messages"])) for r in self.en],
                         [(r["conv"], r["step"], len(r["messages"])) for r in self.legacy])


class Base(unittest.TestCase):
    def mk(self, **kw):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        st = Store(os.path.join(self.tmp.name, "a.sqlite"))
        self.addCleanup(st.db.close)
        return Manager(Config.from_dict({"window": 131072, **kw}), st, TokenCounter(3.5), Journal(None))


class TestLanguageChoice(Base):
    def test_norm(self):
        self.assertEqual([norm(x) for x in ("it", "IT ", "en", "", None, "fr")], ["it", "it", "en", "en", "en", "en"])

    def test_stored_at_first_use_and_kept(self):
        mgr = self.mk()
        self.assertEqual(mgr.prompt_lang("c1", existed=False), "en")
        mgr.cfg.prompt_language = "it"
        self.assertEqual(mgr.prompt_lang("c1", existed=False), "en", "stored choice wins over the config")
        self.assertEqual(mgr.store.kv_get(LANG_KEY + "c1"), "en")
        self.assertEqual(mgr.prompt_lang("c2", existed=False), "it")

    def test_existing_conversation_without_language_is_italian(self):
        mgr = self.mk()
        self.assertEqual(mgr.prompt_lang("old", existed=True), "it")
        self.assertEqual(mgr.store.kv_get(LANG_KEY + "old"), "it")


class TestMixedRecognition(unittest.TestCase):
    INFO = {"name": "read", "args": {"path": "src/a.js"}, "path": "src/a.js", "cmd": ""}
    TEXT = "line\n" * 50

    def receipts(self):
        out = {}
        for lang in ("it", "en"):
            _, r = typed_receipt(self.INFO, self.TEXT, False, "a0123456789ab", "recall", lang)
            out[lang] = (core.OUT_RECEIPT if lang == "it" else core.OUT_RECEIPT_EN).format(
                rid="r0123456789ab", origin=core.origin_text(self.INFO, lang), n=900, receipt=r, rn="recall")
        out["it_placeholder"] = core.OUT_PLACEHOLDER.format(origin="read src/a.js", n=900, head="x", rn="recall",
                                                            rid="r0123456789ab")
        out["en_placeholder"] = core.OUT_PLACEHOLDER_EN.format(origin="read src/a.js", n=900, head="x", rn="recall",
                                                               rid="r0123456789ab")
        out["old_placeholder"] = core.PLACEHOLDER.format(n=900, rid="r0123456789ab")
        return out

    def test_placeholders_recognized_in_both_languages(self):
        for k, v in self.receipts().items():
            self.assertTrue(is_placeholder(v), k)
        self.assertFalse(is_placeholder("plain tool output"))

    def test_guard_catches_both_languages(self):
        for k, v in self.receipts().items():
            if k == "old_placeholder":       # pre-receipt format: never covered by the guard (as in 0.3.0)
                continue
            self.assertTrue(receipt_contaminated("write", {"path": "a.js", "content": "// " + v}), k)
            self.assertTrue(receipt_contaminated("bash", json.dumps({"command": "echo '%s'" % v})), k)
        self.assertFalse(receipt_contaminated("write", {"path": "a.js", "content": "let x = 1;"}))
        self.assertTrue(is_guarded("write"))

    def test_guard_message(self):
        self.assertEqual(guard_msg("it"), GUARD_MSG)
        self.assertEqual(guard_msg("en"), GUARD_MSG_EN)

    def test_markers_both_languages(self):
        self.assertTrue(core.is_notes_request(core.NOTES_MARK + "\nx"))
        self.assertTrue(core.is_notes_request(core.NOTES_MARK_EN + "\nx"))
        self.assertTrue(is_tools_result("[tools: definizioni caricate: todo]\n{}"))
        self.assertTrue(is_tools_result("[tools: definitions loaded: todo]\n{}"))
        self.assertTrue(is_tools_result("[strata_tools: definizioni caricate: todo]\n{}"))

    def test_fake_omissions_stripped_both_languages(self):
        for s in ("[contenuto omesso: 10 token]", "[gestore del contesto: x]", "[content omitted: 10 tokens]",
                  "[context manager: x]"):
            self.assertTrue(core._FAKE_OMIT.search(s), s)


class TestManagerMixed(Base):
    """Markers written in the OTHER language inside a conversation are recognized: a tool result that already is an
    Italian receipt is not masked again in an English conversation, and the reverse."""

    def session(self, foreign: str):
        sysm = {"role": "system", "content": "agent " * 50}
        msgs = [sysm, {"role": "user", "content": "start"}]
        for k in range(14):
            cid = "c%d" % k
            msgs += [{"role": "assistant", "content": "", "tool_calls": [{"id": cid, "type": "function", "function": {
                "name": "bash", "arguments": json.dumps({"command": "cat f%d" % k})}}]},
                {"role": "tool", "tool_call_id": cid, "content": foreign if k == 0 else ("row %d\n" % k) * 600}]
        msgs += [{"role": "assistant", "content": "ok"}, {"role": "user", "content": "next"}]
        return msgs

    def run_one(self, conv_lang, foreign):
        mgr = self.mk(prompt_language=conv_lang, mask_trigger=12000, mask_target=6000, keep_recent_tokens=2000,
                      min_mask_tokens=200, min_batch_tokens=500)
        p = mgr.prepare({"messages": self.session(foreign), "max_tokens": 100})
        self.assertEqual(p.lang, conv_lang)
        first = [m for m in p.messages if m.get("tool_call_id") == "c0"][0]["content"]
        self.assertEqual(first, foreign, "a foreign-language receipt must stay as it is (not masked again)")
        self.assertTrue([m for m in p.messages if m.get("role") == "tool" and m["content"] != foreign
                         and is_placeholder(m["content"])], "other outputs are masked")

    def test_italian_receipt_in_english_conversation(self):
        it = ("\u27eactx-archive id=r0123456789ab \u00b7 uscita di bash `cat f0` \u00b7 questo agente \u00b7 nascosta "
              "per spazio (900 token)\u27eb Ricevuta: x. " + "pad " * 400)
        self.run_one("en", it)

    def test_english_receipt_in_italian_conversation(self):
        en = ("[context manager: output of bash `cat f0` hidden to save space (900 tokens). Start: \u00abx\u00bb. "
              + "pad " * 400)
        self.run_one("it", en)


class TestRecallBothLanguages(Base):
    def setup_conv(self, lang):
        mgr = self.mk(prompt_language=lang)
        msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "go"},
                {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "type": "function", "function": {
                    "name": "bash", "arguments": json.dumps({"command": "cat cfg"})}}]},
                {"role": "tool", "tool_call_id": "c1", "content": "SEED=42\n" + "cfg\n" * 300},
                {"role": "assistant", "content": "ok"}, {"role": "user", "content": "SEED?"}]
        p = mgr.prepare({"messages": msgs, "max_tokens": 100})
        return mgr, p

    def test_recall_texts(self):
        for lang, words in (("en", ("results for", "message")), ("it", ("risultati per", "messaggio"))):
            mgr, p = self.setup_conv(lang)
            out = mgr.recall(p.conv, {"query": "SEED"}, max_idx=6)
            for w in words:
                self.assertIn(w, out, lang)
            miss = mgr.recall(p.conv, {"query": "zzqqy"}, max_idx=6)
            self.assertIn("no results" if lang == "en" else "nessun risultato", miss)

    def test_recall_tool_def(self):
        en, it = recall_tool_def("recall", "en"), recall_tool_def("recall", "it")
        self.assertNotIn("Italian", json.dumps(en))
        self.assertIn("Results and receipts may be in Italian", json.dumps(it))   # 0.2.0-0.3.0 text, unchanged
        self.assertIn("Recupera il testo ORIGINALE", json.dumps(recall_tool_def("strata_recall", "en")))


class TestApiLayerTexts(Base):
    """The API layer writes the 0.3.0 Italian image note / custom-input description (hashed history); English
    conversations get the English text in the physical prompt only."""

    def run_lang(self, lang):
        from ctxproxy.lang import CUSTOM_INPUT_DESC_IT, IMAGE_NOTE_IT
        mgr = self.mk(prompt_language=lang)
        tools = [{"type": "function", "function": {"name": "apply_patch", "description": "patch", "parameters": {
            "type": "object", "properties": {"input": {"type": "string", "description": CUSTOM_INPUT_DESC_IT}}}}}]
        msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "look " + IMAGE_NOTE_IT}]
        p = mgr.prepare({"messages": msgs, "tools": tools, "max_tokens": 100})
        return json.dumps({"m": p.messages, "t": p.tools}, ensure_ascii=False)

    def test_english(self):
        from ctxproxy.lang import CUSTOM_INPUT_DESC_EN, IMAGE_NOTE_EN
        t = self.run_lang("en")
        self.assertIn(IMAGE_NOTE_EN, t)
        self.assertIn(CUSTOM_INPUT_DESC_EN, t)
        self.assertNotIn("immagine", t)

    def test_italian_unchanged(self):
        from ctxproxy.lang import CUSTOM_INPUT_DESC_IT, IMAGE_NOTE_IT
        t = self.run_lang("it")
        self.assertIn(IMAGE_NOTE_IT, t)
        self.assertIn(CUSTOM_INPUT_DESC_IT, t)


if __name__ == "__main__":
    unittest.main()
