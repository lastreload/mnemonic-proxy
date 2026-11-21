# Author: Maurizio Verde — LastReload
"""Windows console (0.3.24, first Windows user): what the commands PRINT must stay readable on a code page that
is not UTF-8, and what the proxy sends to the engine or writes to disk must not change by a single byte.

The bug, reproduced here with a simulated `cp1252` console: `console_safe` used to switch a non-UTF-8 stream to
`errors="backslashreplace"`, so the receipt brackets `⟪ ⟫` reached the terminal as the literal text `\\u27ea`
`\\u27eb`, and the characters the code page *can* encode (`·`, `—`, `«»`) were written as single bytes that the
terminal then read back with a different code page (`?`).

Covers: `mnemonic-proxy` (startup line), `mnemonic-proxy demo`, `check`, `init-config`, `mnemonic-mcp`.
"""
import io
import json
import os
import sqlite3
import sys
import tempfile
import unittest

from ctxproxy import check, compat, demo
from ctxproxy.core import OUT_RECEIPT, RECEIPT_OPEN


def cp1252(errors="strict"):
    """A stand-in for a Windows console: a text stream whose encoding is not UTF-8."""
    raw = io.BytesIO()
    return io.TextIOWrapper(raw, encoding="cp1252", errors=errors, newline="\n"), raw


class TestAsciiOnly(unittest.TestCase):
    def test_markers_become_readable_ascii(self):
        self.assertEqual(compat.ascii_only("\u27eactx-archive id=r1 \u00b7 x\u27eb"), "<<ctx-archive id=r1 - x>>")
        self.assertEqual(compat.ascii_only("demo \u2014 offline"), "demo -- offline")
        self.assertEqual(compat.ascii_only("Last line: \u00abok\u00bb"), "Last line: <<ok>>")
        self.assertEqual(compat.ascii_only("\u2705 ok"), "[ OK ] ok")

    def test_accents_are_folded_not_mangled(self):
        self.assertEqual(compat.ascii_only("D:\\Projet par d\u00e9faut\\data"), "D:\\Projet par defaut\\data")

    def test_unknown_character_never_crashes(self):
        self.assertEqual(compat.ascii_only("a \u5b57 b"), "a ? b")

    def test_ascii_text_is_returned_unchanged(self):
        s = "plain ascii line\n"
        self.assertIs(compat.ascii_only(s), s)


class TestConsoleSafe(unittest.TestCase):
    def test_cp1252_console_receives_pure_ascii(self):
        s, raw = cp1252()
        w = compat.console_safe(s)
        w.write("\u27eactx-archive id=r1 \u00b7 output of bash\u27eb Last line: \u00ab[0180] ok\u00bb \U0001F4CC\n")
        w.flush()
        out = raw.getvalue()
        self.assertTrue(all(b < 0x80 for b in out), out)
        text = out.decode("ascii")
        self.assertIn("<<ctx-archive id=r1 - output of bash>>", text)
        self.assertIn("Last line: <<[0180] ok>>", text)
        self.assertNotIn("\\u27ea", text)        # the 0.3.23 symptom
        self.assertNotIn("?", text)

    def test_utf8_stream_is_the_same_object_and_the_same_bytes(self):
        raw = io.BytesIO()
        s = io.TextIOWrapper(raw, encoding="utf-8", errors="strict", newline="\n")
        self.assertIs(compat.console_safe(s), s)
        s.write("\u27ea x \u27eb caff\u00e8\n")
        s.flush()
        self.assertEqual(raw.getvalue().decode("utf-8"), "\u27ea x \u27eb caff\u00e8\n")

    def test_no_argument_installs_on_stdout_and_stderr(self):
        out, err = sys.stdout, sys.stderr
        s1, raw1 = cp1252()
        s2, _ = cp1252()
        sys.stdout, sys.stderr = s1, s2
        try:
            compat.console_safe()
            print("\u27ea hi \u27eb")
            sys.stdout.flush()
        finally:
            sys.stdout, sys.stderr = out, err
        self.assertEqual(raw1.getvalue().decode("ascii"), "<< hi >>\n")

    def test_wrapper_delegates_everything_else(self):
        s, _ = cp1252()
        w = compat.console_safe(s)
        self.assertEqual(w.encoding, "cp1252")
        self.assertEqual(w.errors, "replace")     # no UnicodeEncodeError is possible any more
        self.assertIs(w.buffer, s.buffer)
        self.assertFalse(w.isatty())


class TestDemoOnAConsole(unittest.TestCase):
    """`mnemonic-proxy demo`: same run, two consoles. The report must be readable on both, and the archive the
    proxy wrote must be identical byte for byte."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        cls.utf8_dir = os.path.join(cls.tmp.name, "utf8")
        cls.cp_dir = os.path.join(cls.tmp.name, "cp1252")
        u = io.StringIO()
        cls.ok_utf8 = demo.run(out=u, keep=cls.utf8_dir)
        cls.utf8_text = u.getvalue()
        s, raw = cp1252()
        cls.ok_cp = demo.run(out=compat.console_safe(s), keep=cls.cp_dir)
        s.flush()
        cls.cp_bytes = raw.getvalue()

    def test_both_runs_pass(self):
        self.assertTrue(self.ok_utf8)
        self.assertTrue(self.ok_cp)

    def test_cp1252_report_is_pure_ascii_and_readable(self):
        self.assertTrue(all(b < 0x80 for b in self.cp_bytes))
        text = self.cp_bytes.decode("ascii")
        self.assertNotIn("\\u27ea", text)
        self.assertIn("mnemonic-proxy demo -- offline", text)
        self.assertIn("<<ctx-archive id=", text)
        self.assertIn("Last line: <<[0180] compiling module_180.c", text)
        self.assertIn("PASS", text)

    def test_the_two_reports_say_the_same_thing(self):
        self.assertEqual(compat.ascii_only(self.utf8_text).splitlines()[:5],
                         self.cp_bytes.decode("ascii").splitlines()[:5])

    def test_what_went_to_the_engine_and_to_disk_is_identical(self):
        """Identity: the archived text of the two runs matches byte for byte. The console fallback only ever
        touches a stream at write time; `RECEIPT_OPEN` and the placeholder texts are untouched."""
        def archived(d):
            db = sqlite3.connect(os.path.join(d, "archive.sqlite"))
            try:
                rows = db.execute("select rid, role, name, content from archive order by conv, idx").fetchall()
            finally:
                db.close()
            return [tuple(r) for r in rows]

        a, b = archived(self.utf8_dir), archived(self.cp_dir)
        self.assertEqual(len(a), len(b))
        self.assertGreater(len(a), 0)
        for x, y in zip(a, b):
            self.assertEqual(str(x[3]).encode("utf-8"), str(y[3]).encode("utf-8"))
        # the constants that end up in the physical prompt keep their real characters
        self.assertEqual(RECEIPT_OPEN, "\u27eactx-archive")
        self.assertTrue(OUT_RECEIPT.startswith("\u27eactx-archive"))
        self.assertIn("\u00b7", OUT_RECEIPT)

    def test_the_journal_on_disk_stays_utf8(self):
        for d in (self.utf8_dir, self.cp_dir):
            p = os.path.join(d, "journal.jsonl")
            with open(p, "rb") as f:
                blob = f.read()
            blob.decode("utf-8")                       # never cp1252, never backslashreplace
            self.assertTrue(any(json.loads(ln).get("event") == "mask"
                                for ln in blob.decode("utf-8").splitlines() if ln.strip()))


class TestCheckOnAConsole(unittest.TestCase):
    def test_icons_fall_back_and_output_is_ascii(self):
        rep = check.Report()
        rep.add(check.OK, "python", "Python 3.13")
        rep.add(check.OK, "data folder", "D:\\Documents\\Projet par d\u00e9faut\\data is writable")
        rep.add(check.FAIL, "engine reachable", "http://127.0.0.1:8095 does not answer")
        s, raw = cp1252()
        check.render(rep, out=compat.console_safe(s))
        s.flush()
        out = raw.getvalue()
        self.assertTrue(all(b < 0x80 for b in out), out)
        text = out.decode("ascii")
        self.assertIn("[ OK ] python", text)
        self.assertIn("[FAIL] engine reachable", text)
        self.assertIn("Projet par defaut", text)

    def test_the_client_reminder_names_every_documented_client(self):
        rep = check.Report()
        check._reminder(rep)
        line = rep.items[0]["evidence"]
        for piece in ('pi: "compaction": {"enabled": false}', 'opencode: "compaction": {"auto": false}',
                      "Claude Code: DISABLE_AUTO_COMPACT=1", "Hermes: compression.enabled false"):
            self.assertIn(piece, line)


class TestStartupLineIsEnglish(unittest.TestCase):
    def test_token_counter_kind_has_no_italian(self):
        from ctxproxy.core import TokenCounter
        tc = TokenCounter(3.5, tokenizer_path=os.path.join(tempfile.mkdtemp(), "nope"))
        self.assertNotIn("esatto", tc.kind)
        self.assertIn("tokenizer not loaded", tc.kind)

    def test_exact_prefix_is_english(self):
        import ctxproxy.core as core
        src = open(core.__file__, encoding="utf-8").read()
        self.assertIn('self.kind = "exact:"', src)
        self.assertNotIn('"esatto:"', src)


if __name__ == "__main__":
    unittest.main()
