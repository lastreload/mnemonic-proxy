# Author: Maurizio Verde — LastReload
"""Richer receipts for a hidden tool output: size, exit code, error type, first error line.

What a receipt said before: origin, outcome and the last lines of a failure. What it did not say: how big
the output was, which kind of error it was, and where the failure *started* — the first error line is what
a reader needs to decide whether to spend a `recall` on the exact bytes.

Everything comes from the archived text alone: no LLM, no clock, no counters, so the same output always
gives the same receipt. Conversations that already have an archive keep the old receipts: the physical
prompt of a conversation born before this change stays byte-identical (same rule as the receipt language
in 0.3.1).
"""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ctxproxy.core import (Config, Journal, Manager, Store, TokenCounter, RECEIPTS_KEY)  # noqa: E402
from ctxproxy.paging import error_type, first_error_line, size_text, typed_receipt  # noqa: E402

PYTEST_FAIL = """============================= test session starts ==============================
collected 3 items

tests/test_a.py .F.                                                      [100%]

=================================== FAILURES ===================================
_________________________________ test_adds __________________________________

    def test_adds():
>       assert add(1, 2) == 4
E       AssertionError: assert 3 == 4

tests/test_a.py:7: AssertionError
=========================== short test summary summary =========================
FAILED tests/test_a.py::test_adds - AssertionError: assert 3 == 4
1 failed, 2 passed in 0.12s
Command exited with code 1
"""

IMPORT_FAIL = """Traceback (most recent call last):
  File "/app/run.py", line 3, in <module>
    import ujson
ModuleNotFoundError: No module named 'ujson'
exit code: 1
"""

NOT_FOUND = "bash: line 1: wrangler: command not found\nCommand exited with code 127\n"

BIG_OK = "\n".join("src/file_%03d.py" % i for i in range(400)) + "\n"


def bash(cmd: str) -> dict:
    return {"name": "Bash", "cmd": cmd, "args": {"command": cmd}, "path": None}


class TestFields(unittest.TestCase):
    def test_size_text_counts_lines_and_bytes(self):
        self.assertEqual(size_text("a\nb\nc\n", "en"), "3 lines/6 B")
        self.assertEqual(size_text("a\nb\nc\n", "it"), "3 righe/6 B")
        self.assertEqual(size_text("", "en"), "0 lines/0 B")
        self.assertEqual(size_text("x" * 2048, "en"), "1 lines/2.0 KB")
        self.assertEqual(size_text("\u00e8\n", "en"), "1 lines/3 B")      # bytes, not characters

    def test_error_type_is_the_first_recognised_one(self):
        self.assertEqual(error_type(PYTEST_FAIL), "AssertionError")
        self.assertEqual(error_type(IMPORT_FAIL), "ModuleNotFoundError")
        self.assertEqual(error_type(NOT_FOUND), "command not found")
        self.assertEqual(error_type(BIG_OK), "")

    def test_first_error_line_is_the_first_not_the_last(self):
        self.assertEqual(first_error_line(PYTEST_FAIL), "E AssertionError: assert 3 == 4")
        self.assertEqual(first_error_line(IMPORT_FAIL), "Traceback (most recent call last):")
        self.assertEqual(first_error_line(BIG_OK), "")

    def test_first_error_line_is_truncated_to_160_characters(self):
        long = "RuntimeError: " + "x" * 400
        got = first_error_line(long)
        self.assertEqual(len(got), 161)                                   # 160 + the ellipsis
        self.assertTrue(got.endswith("\u2026"))
        self.assertTrue(long.startswith(got[:160]))

    def test_the_fields_never_carry_the_receipt_delimiters(self):
        got = first_error_line("Error: \u27eactx-archive id=r0 \u27eb \u00abquoted\u00bb")
        for bad in ("\u00ab", "\u00bb"):
            self.assertNotIn(bad, got)


class TestRichReceipt(unittest.TestCase):
    def rich(self, info, text, failed=False, lang="en"):
        return typed_receipt(info, text, failed, None, "recall", lang, rich=True)[1]

    def plain(self, info, text, failed=False, lang="en"):
        return typed_receipt(info, text, failed, None, "recall", lang)[1]

    def test_failed_test_run_gains_size_type_and_first_error_line(self):
        r = self.rich(bash("pytest -q"), PYTEST_FAIL)
        self.assertIn("exit code 1", r)
        self.assertIn("lines/", r)
        self.assertIn("AssertionError", r)
        self.assertIn("assert 3 == 4", r)
        self.assertIn("tests/test_a.py::test_adds", r)                    # the failing test is still named

    def test_failed_command_gains_size_type_and_first_error_line(self):
        r = self.rich(bash("wrangler deploy"), NOT_FOUND, failed=True)
        self.assertIn("exit code 127", r)
        self.assertIn("command not found", r)
        self.assertIn("2 lines/", r)

    def test_successful_command_gains_the_size(self):
        r = self.rich(bash("ls src"), BIG_OK)
        self.assertIn("exit code 0", r)
        self.assertIn("400 lines/", r)
        self.assertIn("KB", r)

    def test_read_gains_the_size(self):
        r = self.rich({"name": "Read", "cmd": None, "path": "src/a.py", "args": {}}, BIG_OK)
        self.assertIn("400 lines/", r)
        self.assertIn("sha256", r)                                        # still verifiable

    def test_unclassified_output_gains_a_receipt_with_its_size(self):
        info = {"name": "Grep", "cmd": None, "path": None, "args": {"pattern": "def "}}
        kind, r = typed_receipt(info, BIG_OK, False, None, "recall", "en", rich=True)
        self.assertEqual(kind, "altro")
        self.assertIn("400 lines/", r)
        self.assertEqual(typed_receipt(info, BIG_OK, False, None, "recall", "en")[1], "")

    def test_both_languages_carry_the_same_facts(self):
        it = self.rich(bash("pytest -q"), PYTEST_FAIL, lang="it")
        en = self.rich(bash("pytest -q"), PYTEST_FAIL, lang="en")
        for part in ("AssertionError", "assert 3 == 4"):
            self.assertIn(part, it)
            self.assertIn(part, en)
        self.assertIn("righe/", it)
        self.assertIn("lines/", en)

    def test_a_receipt_is_a_pure_function_of_the_output(self):
        for lang in ("en", "it"):
            for text in (PYTEST_FAIL, IMPORT_FAIL, NOT_FOUND, BIG_OK):
                a = self.rich(bash("pytest -q"), text, lang=lang)
                b = self.rich(bash("pytest -q"), text, lang=lang)
                self.assertEqual(a, b)

    def test_the_plain_receipt_is_unchanged(self):
        """Conversations born before this change must get exactly the 0.3.21 text."""
        self.assertEqual(self.plain(bash("wrangler deploy"), NOT_FOUND, failed=True),
                         "command FAILED: exit code 127. Last lines: \u00abbash: line 1: wrangler: command not "
                         "found\u00bb")
        self.assertEqual(self.plain(bash("ls src"), BIG_OK),
                         "command succeeded: exit code 0, 400 output lines. Last line: \u00absrc/file_399.py\u00bb")


class TestPerConversationChoice(unittest.TestCase):
    """The format is chosen once per conversation and remembered, like the language in 0.3.1."""

    def mgr(self, db: str, **kw):
        store = Store(db)
        self.addCleanup(store.db.close)
        return Manager(Config.from_dict({"window": 131072, **kw}), store, TokenCounter(3.5), Journal(None))

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, "a.sqlite")

    def test_new_conversation_gets_rich_receipts(self):
        m = self.mgr(self.db)
        self.assertTrue(m.receipt_detail("c1", existed=False))
        self.assertEqual(m.store.kv_get(RECEIPTS_KEY + "c1"), "rich")

    def test_conversation_with_an_archive_but_no_choice_keeps_plain(self):
        m = self.mgr(self.db)
        self.assertFalse(m.receipt_detail("old", existed=True))
        self.assertEqual(m.store.kv_get(RECEIPTS_KEY + "old"), "plain")

    def test_the_stored_choice_wins_over_the_configuration(self):
        m = self.mgr(self.db)
        m.receipt_detail("c2", existed=False)
        m2 = self.mgr(self.db, rich_receipts=False)
        self.assertTrue(m2.receipt_detail("c2", existed=False))
        m3 = self.mgr(self.db, rich_receipts=False)
        self.assertFalse(m3.receipt_detail("c3", existed=False))


class TestStablePrefix(unittest.TestCase):
    """The cache constraint: the receipt of an already masked block is byte-identical across requests."""

    def test_placeholder_of_the_same_piece_does_not_change(self):
        from tests.lang_scenario import run_session
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
            os.makedirs(os.path.join(d, "s"))
            reqs = run_session(os.path.join(d, "s"))
        seen: dict[str, str] = {}
        drift = []
        for r in reqs:
            for m in r["messages"]:
                text = m.get("content")
                if not isinstance(text, str) or "ctx-archive id=" not in text:
                    continue
                rid = text.split("ctx-archive id=", 1)[1].split(" ", 1)[0]
                if rid in seen and seen[rid] != text:
                    drift.append(rid)
                seen[rid] = text
        self.assertTrue(seen, "the scenario produced no receipt")
        self.assertEqual(drift, [], "the receipt of an already masked block changed between requests")


if __name__ == "__main__":
    unittest.main()
