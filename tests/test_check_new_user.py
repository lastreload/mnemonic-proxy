"""`check` after the 0.3.23 "new user" trial: a missing --config, the example URLs, the engine-aware hints.

Four frictions of `/home/mverde/projects/.../readme-newuser/RESULT.md`, each with its own class:

  A1  `check --config config.json` on a file that does not exist used to stop at the first line and advise
      "fix the JSON file". It must say what to do (create it, or drop --config) AND go on to check the engine.
  A2  `examples/config.*.json` does not exist after `pip install`: every hint prints the full raw URL of the
      file for the version that is installed, or the `init-config` command.
  A8  on a data folder that already has conversations the one-line summary stayed readable: N choices not
      applied, with the reasons left to `--explain-config`.
  A9  the configuration hint follows the engine that was really detected (no llama-server advice on Strata).

Fake engine, no model, CPU, a couple of seconds.
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

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ctxproxy import check, initconfig  # noqa: E402
from ctxproxy.fake_engine import FakeEngine  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class CheckRun(unittest.TestCase):
    flavor, n_ctx, slot_save = "llama", 32768, True

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.eng = FakeEngine(flavor=self.flavor, slot_save=self.slot_save, n_ctx=self.n_ctx)
        self.url = self.eng.start()
        self.addCleanup(self.eng.stop)

    def run_check(self, *extra, cfg=None, data=None):
        argv = ["--upstream", self.url, "--port", str(free_port()),
                "--data", data or os.path.join(self.tmp.name, "data"), "--json", *extra]
        if cfg is not None:
            argv += ["--config", cfg]
        out = io.StringIO()
        with redirect_stdout(out), self.assertRaises(SystemExit) as cm:
            check.main(argv)
        res = json.loads(out.getvalue())
        self.assertEqual(res["exit_code"], cm.exception.code)
        return res

    def item(self, res, name):
        for c in res["checks"]:
            if c["check"] == name:
                return c
        self.fail("no %r line in %s" % (name, [c["check"] for c in res["checks"]]))

    def text(self, *args, **kw):
        """the same run, rendered as the user sees it."""
        argv = ["--upstream", self.url, "--port", str(free_port()),
                "--data", kw.pop("data", None) or os.path.join(self.tmp.name, "data"), *args]
        if kw.get("cfg") is not None:
            argv += ["--config", kw["cfg"]]
        out = io.StringIO()
        with redirect_stdout(out), self.assertRaises(SystemExit):
            check.main(argv)
        return out.getvalue()


class MissingConfigTests(CheckRun):
    """A1: a --config that is not there."""

    def test_the_other_checks_still_run(self):
        missing = os.path.join(self.tmp.name, "config.json")
        res = self.run_check(cfg=missing)
        names = [c["check"] for c in res["checks"]]
        self.assertIn("config", names)
        # the whole report, not just the first line: the engine is what the user came to check
        for expected in ("python", "data folder", "port", "engine reachable", "engine type", "context window"):
            self.assertIn(expected, names, names)
        self.assertEqual(res["exit_code"], 1)

    def test_the_fix_is_something_the_user_can_actually_do(self):
        missing = os.path.join(self.tmp.name, "config.json")
        c = self.item(self.run_check(cfg=missing), "config")
        self.assertEqual(c["status"], "fail")
        self.assertIn("does not exist", c["evidence"])
        self.assertIn("init-config", c["fix"])
        self.assertIn("drop --config", c["fix"])
        self.assertNotIn("fix the JSON file", c["fix"])

    def test_a_broken_json_file_says_so_and_keeps_going(self):
        p = os.path.join(self.tmp.name, "broken.json")
        with open(p, "w", encoding="utf-8") as f:
            f.write("{ not json")
        res = self.run_check(cfg=p)
        c = self.item(res, "config")
        self.assertEqual(c["status"], "fail")
        self.assertIn("fix the JSON file", c["fix"])
        self.assertIn("engine type", [x["check"] for x in res["checks"]])

    def test_a_json_file_that_is_not_an_object(self):
        p = os.path.join(self.tmp.name, "list.json")
        with open(p, "w", encoding="utf-8") as f:
            f.write("[1, 2]")
        res = self.run_check(cfg=p)
        self.assertEqual(self.item(res, "config")["status"], "fail")
        self.assertIn("engine type", [x["check"] for x in res["checks"]])

    def test_the_rendered_report_has_no_traceback(self):
        t = self.text(cfg=os.path.join(self.tmp.name, "config.json"))
        self.assertNotIn("Traceback", t)
        self.assertNotIn("Errno 2", t)
        self.assertIn("NOT READY", t)

    def test_the_proxy_itself_prints_one_line_and_exits(self):
        from ctxproxy.server import main as server_main
        with self.assertRaises(SystemExit) as cm:
            server_main(["--upstream", self.url, "--port", str(free_port()),
                         "--data", os.path.join(self.tmp.name, "data"),
                         "--config", os.path.join(self.tmp.name, "nope.json")])
        msg = str(cm.exception.code)
        self.assertEqual(len(msg.splitlines()), 1, msg)
        self.assertIn("does not exist", msg)
        self.assertIn("init-config", msg)
        self.assertIn("drop --config", msg)


class ExampleUrlTests(CheckRun):
    """A2: no hint may name a path that `pip install` does not create."""

    def test_no_check_line_points_at_a_bare_examples_path(self):
        for argv, cfg in (((), None), ((), os.path.join(self.tmp.name, "missing.json"))):
            res = self.run_check(*argv, cfg=cfg)
            for c in res["checks"]:
                for field in ("evidence", "fix"):
                    txt = c[field]
                    for bad in ("examples/config.", "examples\\config."):
                        if bad in txt:
                            self.assertIn("https://", txt, (c["check"], field, txt))

    def test_the_hint_without_a_config_carries_a_usable_command_or_url(self):
        c = self.item(self.run_check(), "config")
        self.assertEqual(c["status"], "info")
        self.assertTrue("init-config" in c["fix"] or "https://" in c["fix"], c["fix"])

    def test_the_url_matches_the_installed_version(self):
        from ctxproxy import __version__
        u = initconfig.url_for("llama-server")
        self.assertIn("raw.githubusercontent.com/lastreload/mnemonic-proxy/", u)
        self.assertIn("/main/" if "dev" in __version__ else "/v%s/" % __version__, u)


class EngineAwareHintTests(CheckRun):
    """A9: the hint follows the engine that was detected."""
    flavor, n_ctx, slot_save = "strata", 131072, True

    def test_strata_is_not_told_about_llama_server(self):
        c = self.item(self.run_check(), "config")
        self.assertNotIn("llama-server", c["fix"], c["fix"])
        self.assertIn("slot_dir", c["fix"])
        self.assertIn("strata", c["fix"].lower())

    def test_llama_server_is(self):
        self.eng.stop()
        self.eng = FakeEngine(flavor="llama", slot_save=True, n_ctx=32768)
        self.url = self.eng.start()
        c = self.item(self.run_check(), "config")
        self.assertIn("llama-server", c["fix"])

    def test_an_unreachable_engine_keeps_a_generic_hint(self):
        # the config line is printed before the engine answers: it must still be true when it never does
        port = free_port()
        out = io.StringIO()
        argv = ["--upstream", "http://127.0.0.1:%d" % port, "--port", str(free_port()),
                "--data", os.path.join(self.tmp.name, "data"), "--json"]
        with redirect_stdout(out), self.assertRaises(SystemExit):
            check.main(argv)
        res = json.loads(out.getvalue())
        c = self.item(res, "config")
        self.assertNotIn("llama-server use examples/", c["fix"])
        self.assertTrue("init-config" in c["fix"] or "https://" in c["fix"], c["fix"])


class AutoConfigSummaryTests(CheckRun):
    """A8: the summary line stays a line, on an existing data folder."""
    flavor, n_ctx, slot_save = "strata", 131072, True

    def existing_data(self):
        """a data folder that already holds a conversation, so every automatic choice is `kept`."""
        import sqlite3
        d = os.path.join(self.tmp.name, "old")
        os.makedirs(d, exist_ok=True)
        from ctxproxy.core import Store
        st = Store(os.path.join(d, "archive.sqlite"))
        try:
            st.db.execute("INSERT INTO archive (rid, conv, idx, role, name, content, tokens, created) "
                          "VALUES ('r1', 'c', 1, 'tool', 'bash', 'x', 1, 0)")
            st.db.commit()
        except sqlite3.Error as e:          # schema drift must fail loudly, not silently skip the test
            self.fail("could not seed the archive: %s" % e)
        finally:
            st.db.close()
        from ctxproxy.autoconfig import existing_install
        self.assertTrue(existing_install(d))
        return d

    def test_the_summary_is_one_short_line_and_counts_the_kept_choices(self):
        d = self.existing_data()
        c = self.item(self.run_check(data=d), "auto config")
        self.assertLess(len(c["evidence"]), 200, c["evidence"])
        self.assertEqual(c["evidence"].count("already has conversations"), 1, c["evidence"])
        self.assertRegex(c["evidence"], r"^\d+ choices? not applied")
        self.assertIn("--explain-config", c["fix"])

    def test_the_reasons_are_still_there_in_full(self):
        d = self.existing_data()
        res = self.run_check(data=d)
        lines = "\n".join(res["auto_config"]["lines"])
        self.assertIn("kept ", lines)
        self.assertIn("already has conversations", lines)
        # and in the rendered --explain-config block, where the user is sent
        t = self.text("--explain-config", data=d)
        self.assertIn("automatic configuration", t)
        self.assertIn("already has conversations", t)

    def test_a_fresh_folder_still_lists_what_was_chosen(self):
        c = self.item(self.run_check(), "auto config")
        self.assertIn("chosen for this engine and this machine", c["evidence"])


class NoRegressionTests(CheckRun):
    def test_a_good_config_is_still_reported_as_good(self):
        p = os.path.join(self.tmp.name, "cfg.json")
        with open(p, "w", encoding="utf-8") as f:
            json.dump({"mask_tool_args": True}, f)
        c = self.item(self.run_check(cfg=p), "config")
        self.assertEqual(c["status"], "ok")
        self.assertIn("1 fields, all known", c["evidence"])

    def test_unknown_fields_are_still_a_warning(self):
        p = os.path.join(self.tmp.name, "cfg.json")
        with open(p, "w", encoding="utf-8") as f:
            json.dump({"nonsense_field": 1}, f)
        c = self.item(self.run_check(cfg=p), "config")
        self.assertEqual(c["status"], "warn")
        self.assertIn("nonsense_field", c["evidence"])


class ExitCodeDocTests(unittest.TestCase):
    """A6: the three outcomes `check` really has."""

    def test_the_three_words(self):
        from ctxproxy.check import Report, render
        for status, word, code in ((check.OK, "READY", 0), (check.WARN, "READY with warnings", 2),
                                   (check.FAIL, "NOT READY", 1)):
            rep = Report()
            rep.add(status, "x", "y")
            self.assertEqual(rep.exit_code, code)
            out = io.StringIO()
            render(rep, out=out)
            self.assertIn("%s (exit %d)" % (word, code), out.getvalue())

    def test_the_readme_documents_all_three(self):
        """The three outcomes must be documented where `check` is introduced.

        0.3.25: that is `docs/install.md` now, not the landing page; the test reads every
        documentation page so it keeps working wherever the Install section lives.
        """
        import glob
        pages = [os.path.join(ROOT, "README.md")] + sorted(glob.glob(os.path.join(ROOT, "docs", "*.md")))
        texts = []
        for path in pages:
            with open(path, encoding="utf-8") as f:
                texts.append(f.read())
        txt = "\n".join(texts)
        for word in ("READY` (exit 0)", "READY with warnings` (exit 2", "NOT READY` (exit 1"):
            self.assertIn(word, txt, "the documentation does not document %r" % word)


if __name__ == "__main__":
    unittest.main()
