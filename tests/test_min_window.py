"""0.3.0 — minimum engine window: below 16K the proxy refuses to start and `check` says NOT READY.

Decision 2026-10-06 (card t_12ae7948): minimum 16384, recommended >= 32768; 8K stays excluded until it has been
proven on more runs. Only MNEMONIC_PROXY_ALLOW_SMALL_WINDOW=1 (development) lets a smaller window through.

    python3 -m unittest -v tests.test_min_window
"""
from __future__ import annotations

import io
import json
import os
import socket
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ctxproxy import check, engines  # noqa: E402
from ctxproxy.fake_engine import FakeEngine  # noqa: E402
from ctxproxy.server import main  # noqa: E402

ENV = engines.ALLOW_SMALL_WINDOW_ENV


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class Base(unittest.TestCase):
    flavor = "llama"

    def start(self, n_ctx):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.eng = FakeEngine(flavor=self.flavor, slot_save=False, n_ctx=n_ctx)
        self.url = self.eng.start()
        self.addCleanup(self.eng.stop)
        env = {k: v for k, v in os.environ.items() if k != ENV}
        p = mock.patch.dict(os.environ, env, clear=True)
        p.start()
        self.addCleanup(p.stop)

    def cfg(self, **raw):
        p = os.path.join(self.tmp.name, "cfg.json")
        with open(p, "w") as f:
            json.dump(raw, f)
        return p

    def serve(self, *extra):
        """Runs server.main without opening a socket. -> (exit code or None if it started, stdout)."""
        argv = ["--upstream", self.url, "--port", str(free_port()), "--data", os.path.join(self.tmp.name, "d"),
                *extra]
        out = io.StringIO()
        with mock.patch("ctxproxy.server.ThreadingHTTPServer"), redirect_stdout(out):
            try:
                main(argv)
            except SystemExit as e:
                return e.code, out.getvalue() + str(e.code)
        return None, out.getvalue()

    def check(self, *extra):
        argv = ["--upstream", self.url, "--port", str(free_port()), "--data", os.path.join(self.tmp.name, "d"),
                "--json", *extra]
        out = io.StringIO()
        with redirect_stdout(out), self.assertRaises(SystemExit):
            check.main(argv)
        res = json.loads(out.getvalue())
        return res["exit_code"], res["checks"]


class TestServerRefuses(Base):
    def test_8k_engine_refused_with_clear_message(self):
        self.start(8192)
        code, out = self.serve()
        self.assertIsNotNone(code, "the proxy started on an 8K window")
        self.assertNotEqual(code, 0)
        for s in ("8192", "16384", "32768", "llama-server", "-c 32768", "ds4-server", "--ctx 32768"):
            self.assertIn(s, out)

    def test_16k_engine_starts(self):
        self.start(16384)
        code, out = self.serve()
        self.assertIsNone(code, out)
        self.assertIn("window=16384", out)

    def test_32k_engine_starts(self):
        self.start(32768)
        code, out = self.serve()
        self.assertIsNone(code, out)
        self.assertIn("window=32768", out)

    def test_config_window_8k_refused(self):
        self.start(32768)
        code, out = self.serve("--config", self.cfg(window=8192))
        self.assertIsNotNone(code, "the proxy started with window 8192 in the config")
        self.assertNotEqual(code, 0)
        self.assertIn("8192", out)
        self.assertIn("16384", out)

    def test_env_lets_small_window_through(self):
        self.start(8192)
        os.environ[ENV] = "1"
        code, out = self.serve()
        self.assertIsNone(code, out)
        self.assertIn("window=8192", out)


class TestServerRefusesDs4(Base):
    flavor = "ds4"

    def test_ds4_8k_refused(self):
        self.start(8192)
        code, out = self.serve()
        self.assertIsNotNone(code)
        self.assertIn("--ctx 32768", out)

    def test_ds4_32k_starts(self):
        self.start(32768)
        code, out = self.serve()
        self.assertIsNone(code, out)
        self.assertIn("window=32768", out)


class TestCheck(Base):
    def fails(self, items):
        return [i for i in items if i["check"] == "context window" and i["status"] == "fail"]

    def test_check_8k_not_ready(self):
        self.start(8192)
        code, items = self.check()
        self.assertEqual(code, 1, items)
        f = self.fails(items)
        self.assertTrue(f, items)
        self.assertIn("16384", f[0]["evidence"])
        self.assertIn("-c 32768", f[0]["fix"])

    def test_check_live_8k_not_ready(self):
        self.start(8192)
        code, items = self.check("--live")
        self.assertEqual(code, 1, items)
        self.assertTrue(self.fails(items), items)

    def test_check_32k_ready(self):
        self.start(32768)
        code, items = self.check()
        self.assertFalse(self.fails(items), items)
        self.assertNotEqual(code, 1, items)

    def test_check_config_window_8k_not_ready(self):
        self.start(32768)
        code, items = self.check("--config", self.cfg(window=8192))
        self.assertEqual(code, 1, items)
        self.assertTrue(self.fails(items), items)

    def test_check_env(self):
        self.start(8192)
        os.environ[ENV] = "1"
        code, items = self.check()
        self.assertFalse(self.fails(items), items)


if __name__ == "__main__":
    unittest.main()
