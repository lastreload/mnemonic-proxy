"""Setup friction reported from r/LocalLLM: saved state looked mandatory and the session
folder had to be created by hand.

What this suite pins down:
  - the proxy creates the `slot_dir` itself when it is missing and the engine runs on this
    host; when it cannot, the message carries the `mkdir -p` command;
  - it does NOT create it when the engine is on another host: `check` keeps failing, so a
    wrong folder never hides a `slot_dir` / `--slot-save-path` mismatch;
  - `check` and the startup banner say explicitly that the proxy works without saved state
    (masking, archive, `recall`, segments) and that only the fast resume is lost, plus how
    to turn it on later.

    python3 -m unittest -v tests.test_setup_optional_saved_state
"""
from __future__ import annotations

import io
import json
import os
import socket
import stat
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ctxproxy import check, engines  # noqa: E402
from ctxproxy.fake_engine import FakeEngine  # noqa: E402
from ctxproxy.server import main as server_main  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# a folder the proxy cannot create: needs POSIX permissions and a user that is not root
POSIX_PERMS = os.name != "nt" and getattr(os, "geteuid", lambda: 1)() != 0


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class TestUpstreamIsLocal(unittest.TestCase):
    def test_loopback_and_hostless_urls_are_local(self):
        for url in ("http://127.0.0.1:8095", "http://localhost:8095", "http://[::1]:8095",
                    "http://0.0.0.0:8095", ""):
            self.assertTrue(engines.upstream_is_local(url), url)

    def test_another_host_is_not_local(self):
        for url in ("http://192.168.1.50:8095", "http://strata.lan:8095", "https://api.example.com"):
            self.assertFalse(engines.upstream_is_local(url), url)


class TestEnsureSlotDir(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)

    def test_empty_path_is_nothing_to_do(self):
        self.assertEqual(("", ""), engines.ensure_slot_dir("", "http://127.0.0.1:8095"))

    def test_existing_folder_is_reported_as_exists(self):
        outcome, _ = engines.ensure_slot_dir(self.tmp.name, "http://127.0.0.1:8095")
        self.assertEqual("exists", outcome)

    def test_a_missing_folder_is_created_with_its_parents(self):
        d = os.path.join(self.tmp.name, "strata", "sessions")
        outcome, detail = engines.ensure_slot_dir(d, "http://127.0.0.1:8095")
        self.assertEqual("created", outcome, detail)
        self.assertTrue(os.path.isdir(d))

    def test_a_remote_engine_folder_is_not_created(self):
        d = os.path.join(self.tmp.name, "sessions")
        outcome, _ = engines.ensure_slot_dir(d, "http://192.168.1.50:8095")
        self.assertEqual("remote", outcome)
        self.assertFalse(os.path.exists(d))

    @unittest.skipUnless(POSIX_PERMS, "needs POSIX permissions and a non-root user")
    def test_a_folder_it_cannot_create_is_reported_with_the_error(self):
        parent = os.path.join(self.tmp.name, "ro")
        os.makedirs(parent)
        os.chmod(parent, stat.S_IRUSR | stat.S_IXUSR)
        self.addCleanup(os.chmod, parent, stat.S_IRWXU)
        outcome, detail = engines.ensure_slot_dir(os.path.join(parent, "sessions"),
                                                  "http://127.0.0.1:8095")
        self.assertEqual("failed", outcome)
        self.assertTrue(detail)


class StrataBase(unittest.TestCase):
    """The reporter's setup: Strata (saved state supported by the engine) on this host."""

    flavor = "strata"
    slot_save = True

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.eng = FakeEngine(flavor=self.flavor, slot_save=self.slot_save, n_ctx=131072)
        self.url = self.eng.start()
        self.addCleanup(self.eng.stop)

    def cfg(self, **raw):
        p = os.path.join(self.tmp.name, "cfg.json")
        with open(p, "w", encoding="utf-8") as f:
            json.dump(raw, f)
        return p

    def check(self, *extra, cfg=None, upstream=None):
        argv = ["--upstream", upstream or self.url, "--port", str(free_port()),
                "--data", os.path.join(self.tmp.name, "data"), "--json", *extra]
        if cfg:
            argv += ["--config", cfg]
        out = io.StringIO()
        with redirect_stdout(out), self.assertRaises(SystemExit):
            check.main(argv)
        res = json.loads(out.getvalue())
        return res["exit_code"], {i["check"]: i for i in res["checks"]}

    def serve(self, *extra):
        """server.main without a real socket. -> (exit code or None if it started, stderr)."""
        argv = ["--upstream", self.url, "--port", str(free_port()),
                "--data", os.path.join(self.tmp.name, "d"), *extra]
        out = io.StringIO()
        with mock.patch("ctxproxy.server.ThreadingHTTPServer"), redirect_stderr(out):
            try:
                server_main(argv)
            except SystemExit as e:
                return e.code, out.getvalue()
        return None, out.getvalue()


class TestCheckCreatesTheSessionFolder(StrataBase):
    def test_a_missing_slot_dir_is_created_by_the_proxy(self):
        d = os.path.join(self.tmp.name, "strata-sessions")
        code, items = self.check(cfg=self.cfg(window=131072, slot_save=True, mask_anchor=True,
                                              autosave=True, slot_dir=d))
        item = items["slot_dir"]
        self.assertEqual("ok", item["status"], item)
        self.assertIn("created", item["evidence"])
        self.assertTrue(os.path.isdir(d))
        self.assertNotEqual(1, code, items)

    def test_the_created_folder_still_has_to_match_slot_save_path(self):
        d = os.path.join(self.tmp.name, "strata-sessions")
        _, items = self.check(cfg=self.cfg(window=131072, slot_save=True, slot_dir=d))
        text = items["slot_dir"]["evidence"] + " " + items["slot_dir"]["fix"]
        self.assertIn("--slot-save-path", text)

    def test_a_remote_engine_keeps_the_failure(self):
        # the engine answers (so it is detected), but it is not on this host: the folder must not be
        # created, because a local empty folder is not the engine's --slot-save-path
        d = os.path.join(self.tmp.name, "remote-sessions")
        with mock.patch.object(engines, "upstream_is_local", return_value=False):
            code, items = self.check(cfg=self.cfg(window=131072, slot_save=True, slot_dir=d))
        self.assertEqual("fail", items["slot_dir"]["status"], items["slot_dir"])
        self.assertIn("--slot-save-path", items["slot_dir"]["fix"])
        self.assertIn("not on this host", items["slot_dir"]["evidence"])
        self.assertFalse(os.path.exists(d))
        self.assertEqual(1, code)

    @unittest.skipUnless(POSIX_PERMS, "needs POSIX permissions and a non-root user")
    def test_a_folder_it_cannot_create_fails_with_the_mkdir_command(self):
        parent = os.path.join(self.tmp.name, "ro")
        os.makedirs(parent)
        os.chmod(parent, stat.S_IRUSR | stat.S_IXUSR)
        self.addCleanup(os.chmod, parent, stat.S_IRWXU)
        d = os.path.join(parent, "sessions")
        code, items = self.check(cfg=self.cfg(window=131072, slot_save=True, slot_dir=d))
        item = items["slot_dir"]
        self.assertEqual("fail", item["status"], item)
        self.assertIn("mkdir", item["fix"])
        self.assertIn(d, item["fix"])
        self.assertEqual(1, code)


class TestCheckSaysSavedStateIsOptional(StrataBase):
    def test_supported_but_not_configured_says_the_proxy_works(self):
        cfg = os.path.join(ROOT, "examples", "config.official-strata.json")
        code, items = self.check(cfg=cfg)
        item = items["saved state"]
        self.assertEqual("warn", item["status"], item)
        low = (item["evidence"] + " " + item["fix"]).lower()
        for s in ("masking", "archive", "recall", "fast resume", "optional"):
            self.assertIn(s, low, item)
        # it must not push a llama-server example at a Strata user
        self.assertNotIn("config.llama-server.json", item["fix"])
        self.assertIn("--slot-save-path", item["fix"])
        self.assertIn("slot_dir", item["fix"])
        self.assertEqual(2, code, items)

    def test_no_slot_dir_warning_without_saved_state(self):
        code, items = self.check(cfg=os.path.join(ROOT, "examples", "config.official-strata.json"))
        self.assertNotIn("slot_dir", items, items)


class TestCheckSaysSavedStateIsOptionalOnGenericEngines(StrataBase):
    flavor = "openai"
    slot_save = False

    def test_not_supported_says_what_is_lost(self):
        code, items = self.check(cfg=self.cfg(window=131072))
        item = items["saved state"]
        self.assertEqual("info", item["status"], item)
        low = item["evidence"].lower()
        for s in ("masking", "archive", "recall", "fast resume"):
            self.assertIn(s, low, item)


class TestStartupSaysSavedStateIsOptional(StrataBase):
    def test_banner_says_off_and_how_to_turn_it_on(self):
        code, err = self.serve("--config", os.path.join(ROOT, "examples", "config.official-strata.json"))
        self.assertIsNone(code, err)
        self.assertIn("saved state off", err)
        low = err.lower()
        for s in ("masking", "archive", "recall", "fast resume", "--slot-save-path", "slot_dir"):
            self.assertIn(s, low, err)

    def test_banner_says_yes_when_saved_state_is_configured(self):
        d = os.path.join(self.tmp.name, "sessions")
        code, err = self.serve("--config", self.cfg(window=131072, slot_save=True, mask_anchor=True,
                                                    autosave=True, slot_dir=d))
        self.assertIsNone(code, err)
        self.assertIn("saved state yes", err)
        self.assertNotIn("saved state off", err)

    def test_startup_creates_a_missing_slot_dir(self):
        d = os.path.join(self.tmp.name, "nested", "sessions")
        code, err = self.serve("--config", self.cfg(window=131072, slot_save=True, mask_anchor=True,
                                                    autosave=True, slot_dir=d))
        self.assertIsNone(code, err)
        self.assertTrue(os.path.isdir(d), err)
        self.assertIn("created the session folder", err)

    @unittest.skipUnless(POSIX_PERMS, "needs POSIX permissions and a non-root user")
    def test_startup_warns_with_the_mkdir_command_when_it_cannot_create_it(self):
        parent = os.path.join(self.tmp.name, "ro")
        os.makedirs(parent)
        os.chmod(parent, stat.S_IRUSR | stat.S_IXUSR)
        self.addCleanup(os.chmod, parent, stat.S_IRWXU)
        d = os.path.join(parent, "sessions")
        code, err = self.serve("--config", self.cfg(window=131072, slot_save=True, mask_anchor=True,
                                                    autosave=True, slot_dir=d))
        self.assertIsNone(code, err)
        self.assertIn("mkdir", err)
        self.assertIn(d, err)


class TestReadmeMinimalStrataPath(unittest.TestCase):
    """The README must offer the shortest possible Strata setup, with no saved state."""

    HEADING = "### Strata in two commands"
    OPTIONAL = "**Saved state (optional"

    @classmethod
    def setUpClass(cls):
        with open(os.path.join(ROOT, "README.md"), encoding="utf-8") as f:
            cls.text = f.read()

    def section(self):
        """The minimal-path section, from its heading to the next heading of the same level."""
        self.assertIn(self.HEADING, self.text, "the README has no minimal Strata section")
        start = self.text.index(self.HEADING)
        rest = self.text[start + len(self.HEADING):]
        end = rest.find("\n**Your engine.**")
        self.assertGreater(end, 0, "the minimal section is not followed by the engine table")
        return rest[:end]

    def test_there_is_a_minimal_strata_section(self):
        self.assertTrue(self.section().strip())

    def test_the_minimal_path_is_a_ci_block(self):
        head = self.section().split(self.OPTIONAL)[0]
        self.assertIn("<!-- readme-test: ci -->", head)
        self.assertIn("mnemonic-proxy --upstream", head)

    def test_the_minimal_path_does_not_mention_saved_state_setup(self):
        head = self.section().split(self.OPTIONAL)[0]
        for s in ("--slot-save-path", "slot_dir", "slot_save", "mkdir"):
            self.assertNotIn(s, head, "the minimal path must not mention %r" % s)

    def test_saved_state_comes_after_it_as_an_optional_step(self):
        parts = self.section().split(self.OPTIONAL)
        self.assertEqual(2, len(parts), "saved state must be one optional step after the minimal path")
        later = parts[1]
        self.assertIn("--slot-save-path", later)
        self.assertIn("\"slot_dir\"", later)
        self.assertIn("fast resume", later)

    def test_the_minimal_command_line_really_starts_the_proxy(self):
        """The documented line, taken from the README, run against the Strata fake engine."""
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "readme_blocks_optional", os.path.join(ROOT, "tools", "readme_blocks.py"))
        blocks = importlib.util.module_from_spec(spec)
        sys.modules["readme_blocks_optional"] = blocks
        spec.loader.exec_module(blocks)
        start = self.text.index(self.HEADING)
        block = next(b for b in blocks.load(os.path.join(ROOT, "README.md"))
                     if b.lang == "sh" and b.label == "ci"
                     and "mnemonic-proxy --upstream" in b.text
                     and self.text.index(b.text) > start)
        argv = [ln.split() for ln in block.text.splitlines()
                if ln.startswith("mnemonic-proxy --")][0]
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        eng = FakeEngine(flavor="strata", slot_save=True, n_ctx=131072)
        url = eng.start()
        self.addCleanup(eng.stop)
        # only what a document cannot know is substituted: the engine's address, the port, the data folder
        data = os.path.join(tmp.name, "data")          # not created on purpose: the proxy must do it
        args = argv[1:]
        for flag, value in (("--upstream", url), ("--port", str(free_port())), ("--data", data)):
            args[args.index(flag) + 1] = value
        out = io.StringIO()
        with mock.patch("ctxproxy.server.ThreadingHTTPServer"), redirect_stderr(out):
            server_main(args)
        err = out.getvalue()
        self.assertTrue(os.path.isdir(data), err)
        self.assertIn("saved state off", err)


if __name__ == "__main__":
    unittest.main()
