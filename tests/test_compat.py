# Author: Maurizio Verde — LastReload
"""Portability helpers (ctxproxy/compat.py): lock, console encoding, read-only sqlite URI, file names.
Runs on Linux and on the windows-latest CI runner (same assertions, OS-specific branches)."""
import io
import os
import sqlite3
import sys
import tempfile
import threading
import time
import unittest

from ctxproxy import compat


class TestLock(unittest.TestCase):
    def test_exclusive_between_threads_on_separate_fds(self):
        d = tempfile.mkdtemp()
        p = os.path.join(d, ".lock")
        order = []

        def worker(tag):
            fd = os.open(p, os.O_RDWR | os.O_CREAT | compat.O_BINARY, 0o600)
            try:
                compat.lock_fd(fd)
                order.append(tag + "-in")
                time.sleep(0.2)
                order.append(tag + "-out")
                compat.unlock_fd(fd)
            finally:
                os.close(fd)

        a = threading.Thread(target=worker, args=("a",))
        a.start()
        time.sleep(0.05)
        b = threading.Thread(target=worker, args=("b",))
        b.start()
        a.join()
        b.join()
        # never interleaved: each holder leaves before the other enters
        self.assertEqual(order, ["a-in", "a-out", "b-in", "b-out"])

    def test_kvarchive_imports_without_fcntl(self):
        import ctxproxy.kvarchive as k
        self.assertFalse(hasattr(k, "fcntl"))


class TestConsole(unittest.TestCase):
    def test_cp1252_stream_gets_ascii_stand_ins(self):
        raw = io.BytesIO()
        s = io.TextIOWrapper(raw, encoding="cp1252", errors="strict")
        w = compat.console_safe(s)
        w.write("pin \U0001F4CC drop \U0001F5D1 receipt \u27ea x \u27eb caff\u00e8\n")
        w.flush()
        out = raw.getvalue()
        self.assertTrue(all(b < 0x80 for b in out), out)       # 0.3.24: nothing above ASCII reaches the console
        text = out.decode("ascii")
        self.assertEqual(text, "pin [pin] drop [del] receipt << x >> caffe\n")
        self.assertNotIn("\\U0001f4cc", text)                  # the 0.3.23 backslashreplace output

    def test_utf8_stream_untouched(self):
        s = io.TextIOWrapper(io.BytesIO(), encoding="utf-8", errors="strict")
        self.assertIs(compat.console_safe(s), s)
        self.assertEqual(s.errors, "strict")


class TestSqliteUri(unittest.TestCase):
    def test_readonly_uri_opens_and_refuses_writes(self):
        d = tempfile.mkdtemp(prefix="a b#c%")
        p = os.path.join(d, "archive.sqlite")
        db = sqlite3.connect(p)
        db.execute("create table t(x)")
        db.execute("insert into t values (1)")
        db.commit()
        db.close()
        ro = sqlite3.connect(compat.sqlite_ro_uri(p), uri=True)
        self.assertEqual(ro.execute("select x from t").fetchall(), [(1,)])
        with self.assertRaises(sqlite3.OperationalError):
            ro.execute("insert into t values (2)")
        ro.close()

    def test_posix_form_unchanged(self):
        if compat.WINDOWS:
            self.skipTest("POSIX form")
        self.assertEqual(compat.sqlite_ro_uri("/x/a?b#c.sqlite"), "file:/x/a%3fb%23c.sqlite?mode=ro")
        # "%" followed by hex digits must stay literal (mkdtemp's random suffix made this flaky)
        self.assertEqual(compat.sqlite_ro_uri("/x/a%2fb.sqlite"), "file:/x/a%252fb.sqlite?mode=ro")

    def test_percent_hex_in_path_opens(self):
        d = tempfile.mkdtemp(prefix="p%2f")
        p = os.path.join(d, "archive.sqlite")
        db = sqlite3.connect(p)
        db.execute("create table t(x)")
        db.commit()
        db.close()
        ro = sqlite3.connect(compat.sqlite_ro_uri(p), uri=True)
        self.assertEqual(ro.execute("select count(*) from t").fetchall(), [(0,)])
        ro.close()


class TestNames(unittest.TestCase):
    def test_bad_names(self):
        self.assertTrue(compat.bad_name("a/b.bin"))
        self.assertTrue(compat.bad_name(".hidden"))
        self.assertFalse(compat.bad_name("c-0123abcd-seg0-A.bin"))
        self.assertFalse(compat.bad_name("c0123abcd-seg2-auto-ab12cd34ef.bin"))
        self.assertEqual(compat.bad_name("a:b.bin"), compat.WINDOWS)
        self.assertEqual(compat.bad_name("a\\b.bin"), compat.WINDOWS)


class TestReplace(unittest.TestCase):
    def test_replace_overwrites(self):
        d = tempfile.mkdtemp()
        a, b = os.path.join(d, "a"), os.path.join(d, "b")
        with open(b, "w", encoding="utf-8") as f:
            f.write("old")
        with open(a, "w", encoding="utf-8") as f:
            f.write("new")
        compat.replace(a, b)
        with open(b, encoding="utf-8") as f:
            self.assertEqual(f.read(), "new")
        self.assertFalse(os.path.exists(a))

    @unittest.skipUnless(compat.WINDOWS, "Windows sharing semantics")
    def test_replace_waits_for_a_reader(self):  # pragma: no cover - Windows only
        d = tempfile.mkdtemp()
        a, b = os.path.join(d, "a"), os.path.join(d, "b")
        for p, t in ((a, "new"), (b, "old")):
            with open(p, "w", encoding="utf-8") as f:
                f.write(t)
        h = open(b, encoding="utf-8")
        threading.Timer(0.3, h.close).start()
        compat.replace(a, b)
        with open(b, encoding="utf-8") as f:
            self.assertEqual(f.read(), "new")


if __name__ == "__main__":
    unittest.main()
