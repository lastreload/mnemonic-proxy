# Author: Maurizio Verde — LastReload
"""Compact receipts (0.3.24): the id written once, the fixed instruction in the tool description.

Every placeholder of a hidden tool output used to carry two copies of the same facts: the archive id, once in
the frame (`⟪ctx-archive id=…⟫`) and once in the line the model is supposed to copy (`recall id=…`), and the
instruction "For the CURRENT state re-read the file or re-run the command", identical in every block. Measured
on real sessions that is 48 tokens of a 135-token receipt with no information of their own.

`compact_receipts` writes the id once, in the `recall id=<id>` form the tool description already points at, and
moves the instruction once into the description of the conversation's `recall` tool. Nothing else is removed:
kind, origin, outcome, line range, sha256 and the visible quote stay. Off by default until an A/B decides, and
chosen per conversation like `rich_receipts`, so a conversation that already has an archive keeps the exact
bytes it was masked with.
"""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ctxproxy.core import (COMPACT_KEY, Config, Journal, LEGACY_RECALL_NAME, Manager, OUT_PREFIX,  # noqa: E402
                           OUT_PREFIX_EN, RECALL_NAME, RECEIPT_OPEN, Store, TokenCounter, is_placeholder,
                           recall_tool_def)
from ctxproxy.paging import receipt_contaminated  # noqa: E402

READ = {"name": "read", "cmd": None, "path": "src/render.js", "args": {"path": "src/render.js", "offset": 700}}
BASH = {"name": "bash", "cmd": "npm test", "args": {"command": "npm test"}, "path": None}
QUERY = {"name": "query", "cmd": None, "path": None, "args": {"q": "x"}}

READ_OUT = "\n".join("line %d of src/render.js" % i for i in range(700, 751)) + "\n"
BASH_OUT = "test ok\n" * 38 + "exit code 0\n"
OTHER_OUT = "\n".join("doc %d: lorem ipsum dolor" % i for i in range(60))
RID = "raae01ebb620f"

# 0.3.23 texts, byte for byte: the option off must not change a single character.
BEFORE = {
    ("it", "read"): "\u27eactx-archive id=raae01ebb620f \u00b7 uscita di read src/render.js \u00b7 questo agente \u00b7 "
                    "nascosta per spazio (693 token)\u27eb Ricevuta: lettura: righe 700\u2013fine, 51 righe "
                    "restituite, sha256 30dd8957544d del testo letto. Testo esatto: recall id=raae01ebb620f. "
                    "Per lo stato ATTUALE rileggi il file o riesegui il comando.",
    ("it", "bash"): "\u27eactx-archive id=raae01ebb620f \u00b7 uscita di bash `npm test` \u00b7 questo agente \u00b7 "
                    "nascosta per spazio (693 token)\u27eb Ricevuta: test: codice d'uscita 0. Testo esatto: "
                    "recall id=raae01ebb620f. Per lo stato ATTUALE rileggi il file o riesegui il comando.",
    ("en", "read"): "\u27eactx-archive id=raae01ebb620f \u00b7 output of read src/render.js \u00b7 this agent \u00b7 "
                    "hidden to save space (693 tokens)\u27eb Receipt: read: lines 700\u2013end, 51 lines returned, "
                    "sha256 30dd8957544d of the text read. Exact text: recall id=raae01ebb620f. For the CURRENT "
                    "state re-read the file or re-run the command.",
    ("en", "bash"): "\u27eactx-archive id=raae01ebb620f \u00b7 output of bash `npm test` \u00b7 this agent \u00b7 "
                    "hidden to save space (693 tokens)\u27eb Receipt: test: exit code 0. Exact text: "
                    "recall id=raae01ebb620f. For the CURRENT state re-read the file or re-run the command.",
}

# The compact form: the frame without the id, and no final instruction. Everything else identical.
AFTER = {
    ("it", "read"): "\u27eactx-archive \u00b7 uscita di read src/render.js \u00b7 questo agente \u00b7 nascosta per "
                    "spazio (693 token)\u27eb Ricevuta: lettura: righe 700\u2013fine, 51 righe restituite, "
                    "sha256 30dd8957544d del testo letto. Testo esatto: recall id=raae01ebb620f.",
    ("it", "bash"): "\u27eactx-archive \u00b7 uscita di bash `npm test` \u00b7 questo agente \u00b7 nascosta per "
                    "spazio (693 token)\u27eb Ricevuta: test: codice d'uscita 0. Testo esatto: "
                    "recall id=raae01ebb620f.",
    ("en", "read"): "\u27eactx-archive \u00b7 output of read src/render.js \u00b7 this agent \u00b7 hidden to save "
                    "space (693 tokens)\u27eb Receipt: read: lines 700\u2013end, 51 lines returned, "
                    "sha256 30dd8957544d of the text read. Exact text: recall id=raae01ebb620f.",
    ("en", "bash"): "\u27eactx-archive \u00b7 output of bash `npm test` \u00b7 this agent \u00b7 hidden to save "
                    "space (693 tokens)\u27eb Receipt: test: exit code 0. Exact text: recall id=raae01ebb620f.",
}

CASES = {"read": (READ, READ_OUT), "bash": (BASH, BASH_OUT), "other": (QUERY, OTHER_OUT)}


def manager(db: str, lang: str = "it", **kw) -> Manager:
    store = Store(db)
    m = Manager(Config.from_dict({"window": 131072, **kw}), store, TokenCounter(3.5), Journal(None))
    m.lang, m.rn = lang, RECALL_NAME
    m.compact = bool(kw.get("compact_receipts"))
    return m


class TestTheOption(unittest.TestCase):
    def test_off_by_default(self):
        self.assertFalse(Config().compact_receipts)


class TestPlaceholderText(unittest.TestCase):
    """The texts, before and after, byte for byte."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, "a.sqlite")

    def ph(self, lang: str, case: str, compact: bool) -> str:
        m = manager(self.db, lang, compact_receipts=compact)
        self.addCleanup(m.store.db.close)
        info, out = CASES[case]
        return m.out_placeholder(info, out, 693, RID)

    def test_option_off_is_byte_identical_to_0_3_23(self):
        for (lang, case), text in BEFORE.items():
            self.assertEqual(self.ph(lang, case, False), text, (lang, case))

    def test_option_on_gives_the_compact_text(self):
        for (lang, case), text in AFTER.items():
            self.assertEqual(self.ph(lang, case, True), text, (lang, case))

    def test_the_id_appears_exactly_once(self):
        for lang in ("it", "en"):
            for case in CASES:
                compact = self.ph(lang, case, True)
                self.assertEqual(compact.count(RID), 1, (lang, case, compact))
                self.assertIn("%s id=%s" % (RECALL_NAME, RID), compact)
                # ...and twice in the old form, which is what this option is about
                self.assertEqual(self.ph(lang, case, False).count(RID), 2 if case != "other" else 1, (lang, case))

    def test_the_fixed_instruction_is_gone_from_every_placeholder(self):
        for lang in ("it", "en"):
            for case in CASES:
                compact = self.ph(lang, case, True)
                for fixed in ("Per lo stato ATTUALE", "For the CURRENT state",
                              "rileggi il file o riesegui il comando", "re-read the file or re-run the command"):
                    self.assertNotIn(fixed, compact, (lang, case, fixed))

    def test_nothing_else_is_removed(self):
        """Kind, origin, outcome, line range, sha256 and the visible quote stay."""
        read_it, read_en = self.ph("it", "read", True), self.ph("en", "read", True)
        for part in ("src/render.js", "sha256 30dd8957544d", "700", "51", "693"):
            self.assertIn(part, read_it, part)
            self.assertIn(part, read_en, part)
        self.assertIn("lettura: righe 700\u2013fine", read_it)
        self.assertIn("read: lines 700\u2013end", read_en)
        for lang in ("it", "en"):
            other = self.ph(lang, "other", True)
            self.assertIn("doc 0: lorem ipsum dolor", other)        # the visible quote
            self.assertIn("\u00ab", other)
            self.assertIn("query", other)                           # the origin
            self.assertIn("693", other)

    def test_the_compact_placeholder_is_still_recognised_as_one(self):
        for lang in ("it", "en"):
            for case in CASES:
                self.assertTrue(is_placeholder(self.ph(lang, case, True)), (lang, case))

    def test_the_outgoing_guard_still_catches_a_copied_compact_receipt(self):
        for lang in ("it", "en"):
            for case in CASES:
                args = {"path": "a.js", "content": "// " + self.ph(lang, case, True)}
                self.assertTrue(receipt_contaminated("write", json.dumps(args)), (lang, case))

    def test_the_text_is_a_pure_function_of_the_piece(self):
        for lang in ("it", "en"):
            for case in CASES:
                self.assertEqual(self.ph(lang, case, True), self.ph(lang, case, True))

    def test_the_compact_placeholder_is_shorter(self):
        for lang in ("it", "en"):
            for case in CASES:
                self.assertLess(len(self.ph(lang, case, True)), len(self.ph(lang, case, False)), (lang, case))


class TestToolDescription(unittest.TestCase):
    """The instruction leaves the placeholders and enters the tool description, once."""

    def test_the_description_is_unchanged_with_the_option_off(self):
        for lang in ("it", "en"):
            self.assertEqual(recall_tool_def(RECALL_NAME, lang, False), recall_tool_def(RECALL_NAME, lang))

    def test_the_description_carries_the_instruction_once_with_the_option_on(self):
        for lang, needle in (("it", "Per lo stato ATTUALE"), ("en", "For the CURRENT state")):
            d = recall_tool_def(RECALL_NAME, lang, True)["function"]["description"]
            self.assertEqual(d.count(needle), 1, (lang, d))
            self.assertIn("rileggi il file o riesegui il comando" if lang == "it"
                          else "re-read the file or re-run the command", d)
            # everything the description said before is still there
            base = recall_tool_def(RECALL_NAME, lang, False)["function"]["description"]
            self.assertTrue(d.startswith(base), (lang, d))

    def test_the_description_still_points_at_the_form_the_placeholder_uses(self):
        d = recall_tool_def(RECALL_NAME, "en", True)["function"]["description"]
        self.assertIn("'%s id=<id>'" % RECALL_NAME, d)

    def test_the_legacy_tool_is_untouched(self):
        for lang in ("it", "en"):
            for compact in (False, True):
                self.assertEqual(recall_tool_def(LEGACY_RECALL_NAME, lang, compact),
                                 recall_tool_def(LEGACY_RECALL_NAME, lang))
        d = recall_tool_def(LEGACY_RECALL_NAME, "it", True)["function"]["description"]
        self.assertNotIn("Per lo stato ATTUALE", d)

    def test_the_added_cost_is_one_sentence(self):
        for lang in ("it", "en"):
            base = recall_tool_def(RECALL_NAME, lang, False)["function"]["description"]
            on = recall_tool_def(RECALL_NAME, lang, True)["function"]["description"]
            self.assertLess(len(on) - len(base), 200, lang)


class TestPerConversationChoice(unittest.TestCase):
    """Chosen once per conversation and remembered, exactly like rich_receipts in 0.3.22."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, "a.sqlite")

    def mgr(self, **kw):
        m = manager(self.db, **kw)
        self.addCleanup(m.store.db.close)
        return m

    def test_default_is_the_full_form(self):
        m = self.mgr()
        self.assertFalse(m.receipt_compact("c0", existed=False))
        self.assertEqual(m.store.kv_get(COMPACT_KEY + "c0"), "full")

    def test_a_new_conversation_gets_the_compact_form(self):
        m = self.mgr(compact_receipts=True)
        self.assertTrue(m.receipt_compact("c1", existed=False))
        self.assertEqual(m.store.kv_get(COMPACT_KEY + "c1"), "compact")

    def test_a_conversation_with_an_archive_keeps_the_full_form(self):
        m = self.mgr(compact_receipts=True)
        self.assertFalse(m.receipt_compact("old", existed=True))
        self.assertEqual(m.store.kv_get(COMPACT_KEY + "old"), "full")

    def test_the_stored_choice_wins_over_the_configuration(self):
        self.mgr(compact_receipts=True).receipt_compact("c2", existed=False)
        self.assertTrue(self.mgr(compact_receipts=False).receipt_compact("c2", existed=False))
        self.assertFalse(self.mgr(compact_receipts=False).receipt_compact("c3", existed=False))
        self.mgr(compact_receipts=False).receipt_compact("c4", existed=False)
        self.assertFalse(self.mgr(compact_receipts=True).receipt_compact("c4", existed=False))


class TestWholeSession(unittest.TestCase):
    """A real session through the proxy and the fake engine: compact placeholders, recall by id still works."""

    @classmethod
    def setUpClass(cls):
        from tests.lang_scenario import run_session
        cls.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        for name, over in (("off", {}), ("on", {"compact_receipts": True}),
                           ("on_it", {"compact_receipts": True, "prompt_language": "it"})):
            d = os.path.join(cls.tmp.name, name)
            os.makedirs(d)
            setattr(cls, name, run_session(d, over))

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def placeholders(self, reqs):
        """Only the placeholders of hidden outputs: the receipt guard's own message is recognised by
        is_placeholder too (it quotes the delimiter) but is not the placeholder of an archived block."""
        out = []
        for r in reqs:
            for m in r["messages"]:
                c = m.get("content")
                if m.get("role") == "tool" and isinstance(c, str) \
                        and c.startswith((RECEIPT_OPEN, OUT_PREFIX, OUT_PREFIX_EN)):
                    out.append(c)
        return out

    def test_the_session_produced_placeholders_in_both_arms(self):
        self.assertTrue(self.placeholders(self.off))
        self.assertTrue(self.placeholders(self.on))
        self.assertTrue(self.placeholders(self.on_it))

    def test_the_compact_session_never_repeats_the_fixed_instruction(self):
        for reqs, fixed in ((self.on, "For the CURRENT state"), (self.on_it, "Per lo stato ATTUALE")):
            for c in self.placeholders(reqs):
                self.assertNotIn(fixed, c, c)

    def test_the_full_session_still_carries_it(self):
        self.assertTrue(any("For the CURRENT state" in c for c in self.placeholders(self.off)))

    def test_the_id_is_written_once_in_every_compact_placeholder(self):
        import re
        for reqs in (self.on, self.on_it):
            for c in self.placeholders(reqs):
                ids = re.findall(r"\b[rta][0-9a-f]{12}\b", c)
                self.assertEqual(len(ids), len(set(ids)), c)
                self.assertEqual(len(ids), 1, c)

    def test_the_instruction_is_in_the_tool_description_of_the_compact_session(self):
        for reqs, needle in ((self.on, "For the CURRENT state"), (self.on_it, "Per lo stato ATTUALE")):
            descs = [t["function"]["description"] for r in reqs for t in (r.get("tools") or [])
                     if t["function"]["name"] == RECALL_NAME]
            self.assertTrue(descs)
            self.assertTrue(all(d.count(needle) == 1 for d in descs), descs[:1])
        descs = [t["function"]["description"] for r in self.off for t in (r.get("tools") or [])
                 if t["function"]["name"] == RECALL_NAME]
        self.assertTrue(descs)
        self.assertTrue(all("For the CURRENT state" not in d for d in descs))

    def test_a_placeholder_already_written_never_changes(self):
        for reqs in (self.off, self.on, self.on_it):
            seen = {}
            import re
            for c in self.placeholders(reqs):
                m = re.search(r"\b([rta][0-9a-f]{12})\b", c)
                if not m:
                    continue
                prev = seen.setdefault(m.group(1), c)
                self.assertEqual(prev, c, m.group(1))

    def test_recall_by_id_works_from_the_compact_form(self):
        """The id read in a compact placeholder resolves to the original text."""
        import re
        for name in ("on", "on_it"):
            reqs = getattr(self, name)
            managers = []
            for db in ("a.sqlite", "b.sqlite"):
                path = os.path.join(self.tmp.name, name, db)
                self.assertTrue(os.path.exists(path), path)
                store = Store(path)
                self.addCleanup(store.db.close)
                managers.append(Manager(Config.from_dict({"window": 131072}), store,
                                        TokenCounter(3.5), Journal(None)))
            convs = [(mm, c[0]) for mm in managers for c in mm.store.db.execute("SELECT DISTINCT conv FROM archive")]
            tried = 0
            for c in self.placeholders(reqs):
                rid = re.search(r"\b([rta][0-9a-f]{12})\b", c).group(1)
                texts = [mm.recall(conv, {"id": rid}, max_idx=99) for mm, conv in convs]
                found = [t for t in texts if t and "not found" not in t and "non trovato" not in t]
                self.assertTrue(found, (name, rid, texts))
                tried += 1
            self.assertGreater(tried, 0)


if __name__ == "__main__":
    unittest.main()
