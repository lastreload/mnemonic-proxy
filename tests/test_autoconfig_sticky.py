# Author: Maurizio Verde — LastReload
"""0.3.24: the automatic choices must not change between two starts of the same installation.

Reported by the first Windows user (0.3.23). First start on an empty data folder, window 115000: the proxy
applies `response_floor = 14375` and `notes_max_tokens = 10781`. One hour later, `check` on the SAME folder
(which now holds conversations):

    [INFO] auto config 2 choices not applied: this data folder already has conversations
    kept response_floor = 0 (automatic choice would be 14375)
    kept notes_max_tokens = 7187 (automatic choice would be 10781)

so the next restart would run with different values than the first one. The rule that keeps an upgrade from
changing a running setup (PolyStrata before poly.7, see test_autoconfig.TestExistingInstall) must only protect
folders that predate the automatic configuration, not folders the automatic configuration created itself.

These tests pin the provenance of the data folder, recorded in the folder and kept apart from the presence of
conversations:
  * a folder created by this version keeps getting the same choices after it has conversations;
  * a folder from an earlier setup (no mark) stays protected, even when its archive is empty;
  * an unreadable mark, or an old journal that happens to carry an `auto_config` event, are not a consent;
  * `check` reads the provenance and never creates it.
"""
import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request

from ctxproxy import autoconfig, check
from ctxproxy.core import Config
from tests.fake_engine import FakeEngine
from tests.test_autoconfig import BIG, local_engine, make_archive

WINDOW = 115000                      # the window of the report
FLOOR = WINDOW // 8                  # 14375
NOTES = WINDOW * 3 // 32             # 10781


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def run_check(data_dir, config_path, url, port=None):
    """`mnemonic-proxy check --json` in this process, as the user would run it. -> the report."""
    out = io.StringIO()
    old = sys.stdout
    sys.stdout = out
    try:
        check.main(["--json", "--upstream", url, "--port", str(port or free_port()),
                    "--data", data_dir, "--config", config_path])
    except SystemExit:
        pass
    finally:
        sys.stdout = old
    return json.loads(out.getvalue())


def auto_line(report):
    return [i for i in report["checks"] if i["check"] == "auto config"][0]


def folder_line(report):
    """The one line that says which of the three cases this data folder is."""
    return [i for i in report["checks"] if i["check"] == "data folder origin"][0]


def write_journal(data_dir, *records):
    """A journal.jsonl as an earlier version left it (append-only, oldest first)."""
    os.makedirs(data_dir, exist_ok=True)
    with open(os.path.join(data_dir, "journal.jsonl"), "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


APPLIED_0323 = {"event": "auto_config", "machine": {}, "decisions": [{"field": "response_floor", "value": FLOOR,
                                                                      "changed": True}]}
START_0323 = {"event": "startup", "version": "0.3.23", "window": WINDOW}


class TestOrigin(unittest.TestCase):
    """`autoconfig.origin`: where this data folder comes from, read-only."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.data = os.path.join(self.tmp, "data")

    def test_a_folder_that_is_not_there_yet_is_new(self):
        self.assertEqual(autoconfig.origin(self.data)[0], autoconfig.ORIGIN_FRESH)
        os.makedirs(self.data)
        self.assertEqual(autoconfig.origin(self.data)[0], autoconfig.ORIGIN_FRESH, "an empty folder is new")

    def test_an_earlier_folder_without_the_mark_is_protected_even_with_an_empty_archive(self):
        """SOL §6: an old folder does not become safe because its archive happens to be empty."""
        make_archive(self.data, rows=0)
        where, why = autoconfig.origin(self.data)
        self.assertEqual(where, autoconfig.ORIGIN_EARLIER)
        self.assertIn("archive.sqlite", why)

    def test_a_folder_with_conversations_and_no_mark_is_protected(self):
        make_archive(self.data, rows=1)
        self.assertEqual(autoconfig.origin(self.data)[0], autoconfig.ORIGIN_EARLIER)

    def test_the_mark_makes_the_folder_automatic(self):
        os.makedirs(self.data)
        self.assertTrue(autoconfig.write_mark(self.data, Config(window=WINDOW), []))
        where, why = autoconfig.origin(self.data)
        self.assertEqual(where, autoconfig.ORIGIN_AUTO)
        self.assertIn(autoconfig.MARK_FILE, why)
        make_archive(self.data, rows=3)
        self.assertEqual(autoconfig.origin(self.data)[0], autoconfig.ORIGIN_AUTO,
                         "conversations do not change where the folder comes from")

    def test_an_unreadable_mark_is_treated_as_an_earlier_folder(self):
        os.makedirs(self.data)
        with open(os.path.join(self.data, autoconfig.MARK_FILE), "w", encoding="utf-8") as f:
            f.write("{ not json")
        where, why = autoconfig.origin(self.data)
        self.assertEqual(where, autoconfig.ORIGIN_EARLIER)
        self.assertIn("cannot be read", why)

    def test_an_old_auto_config_journal_event_is_not_a_consent(self):
        os.makedirs(self.data)
        with open(os.path.join(self.data, "journal.jsonl"), "w", encoding="utf-8") as f:
            f.write(json.dumps({"event": "auto_config", "decisions": [{"field": "slot_save"}]}) + "\n")
        self.assertEqual(autoconfig.origin(self.data)[0], autoconfig.ORIGIN_EARLIER)

    def test_a_folder_created_by_0_3_23_with_the_choices_applied_is_automatic(self):
        """0.3.23 applied the choices to new folders but wrote no mark: its journal is the record. The first start
        in the (append-only) journal is a 0.3.23 start whose auto_config event applied a choice."""
        make_archive(self.data, rows=2)
        write_journal(self.data, APPLIED_0323, START_0323, {"event": "request"},
                      {"event": "auto_config", "decisions": []}, START_0323)
        where, why = autoconfig.origin(self.data)
        self.assertEqual(where, autoconfig.ORIGIN_AUTO)
        self.assertIn("0.3.23", why)

    def test_a_folder_whose_first_start_predates_0_3_23_stays_earlier(self):
        make_archive(self.data, rows=2)
        write_journal(self.data, {"event": "startup", "version": "0.3.22"}, APPLIED_0323, START_0323)
        self.assertEqual(autoconfig.origin(self.data)[0], autoconfig.ORIGIN_EARLIER)

    def test_a_first_0_3_23_start_that_applied_nothing_stays_earlier(self):
        """A pre-0.3.23 folder whose journal was deleted: 0.3.23 saw conversations and applied nothing."""
        make_archive(self.data, rows=2)
        write_journal(self.data, {"event": "auto_config", "decisions": []}, START_0323)
        self.assertEqual(autoconfig.origin(self.data)[0], autoconfig.ORIGIN_EARLIER)

    def test_an_unreadable_journal_line_before_the_first_start_stays_earlier(self):
        make_archive(self.data, rows=2)
        write_journal(self.data, APPLIED_0323)
        with open(os.path.join(self.data, "journal.jsonl"), "a", encoding="utf-8") as f:
            f.write("{ not json\n" + json.dumps(START_0323) + "\n")
        self.assertEqual(autoconfig.origin(self.data)[0], autoconfig.ORIGIN_EARLIER)

    def test_the_mark_records_the_version_and_the_applied_choices(self):
        os.makedirs(self.data)
        cfg = Config(window=WINDOW, slot_dir=self.tmp)
        decisions = autoconfig.decide(cfg, local_engine(), set(), BIG)
        autoconfig.write_mark(self.data, cfg, decisions)
        mark = autoconfig.read_mark(self.data)
        self.assertEqual(mark["version"], autoconfig.MARK_VERSION)
        self.assertEqual(mark["window"], WINDOW)
        self.assertEqual(mark["applied"]["response_floor"], FLOOR)
        self.assertTrue(mark["proxy_version"])

    def test_the_mark_never_appears_by_itself_on_an_earlier_folder(self):
        """A pre-0.3.23 folder must never become "automatic" on its own (PolyStrata < poly.7)."""
        make_archive(self.data, rows=1)
        cfg = Config(window=WINDOW, slot_dir=self.tmp)
        autoconfig.decide(cfg, local_engine(), set(), BIG, existing=True)
        self.assertFalse(os.path.exists(os.path.join(self.data, autoconfig.MARK_FILE)))
        self.assertFalse(cfg.slot_save, "saved state is never switched on by itself on an earlier folder")


class TestCheckReadsTheProvenance(unittest.TestCase):
    """`check` says which of the three cases the folder is, and writes nothing."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.data = os.path.join(self.tmp, "data")
        self.fake = FakeEngine(flavor="strata", n_ctx=131072)
        self.url = self.fake.start()
        self.addCleanup(self.fake.stop)
        self.config = os.path.join(self.tmp, "config.json")
        with open(self.config, "w", encoding="utf-8") as f:
            json.dump({"window": WINDOW, "slot_dir": os.path.join(self.tmp, "slots")}, f)

    def test_new_folder_is_named_and_not_created(self):
        rep = run_check(self.data, self.config, self.url)
        self.assertIn("new data folder", folder_line(rep)["evidence"])
        # `check` does create the folder to prove it is writable (check.py, "data folder"), but the provenance
        # mark is the server's to write: otherwise a `check` run before the first start would make an earlier
        # folder look automatic.
        self.assertFalse(os.path.exists(os.path.join(self.data, autoconfig.MARK_FILE)),
                         "check must not create the provenance mark")

    def test_an_automatic_folder_with_conversations_still_gets_the_choices(self):
        os.makedirs(self.data)
        autoconfig.write_mark(self.data, Config(window=WINDOW), [])
        make_archive(self.data, rows=2)
        rep = run_check(self.data, self.config, self.url)
        chosen = {d["field"]: d for d in rep["auto_config"]["decisions"] if d["changed"]}
        self.assertEqual(chosen["response_floor"]["value"], FLOOR)
        self.assertEqual(chosen["notes_max_tokens"]["value"], NOTES)
        self.assertIn("automatic configuration", folder_line(rep)["evidence"])
        self.assertIn("response_floor=%d" % FLOOR, auto_line(rep)["evidence"])

    def test_an_earlier_folder_is_still_protected(self):
        make_archive(self.data, rows=2)
        rep = run_check(self.data, self.config, self.url)
        kept = {d["field"]: d for d in rep["auto_config"]["decisions"] if d.get("kept_existing")}
        self.assertIn("response_floor", kept)
        self.assertIn("earlier", folder_line(rep)["evidence"])
        self.assertIn("not applied", auto_line(rep)["evidence"])

    def test_check_never_writes_the_mark_on_an_earlier_folder(self):
        make_archive(self.data, rows=2)
        run_check(self.data, self.config, self.url)
        self.assertFalse(os.path.exists(os.path.join(self.data, autoconfig.MARK_FILE)))


class TestTheReportedSequence(unittest.TestCase):
    """The report itself: empty folder -> choices applied -> a conversation archived -> a second start and
    `check` keep the very same values."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.data = os.path.join(self.tmp, "data")
        self.fake = FakeEngine(flavor="strata", n_ctx=131072)
        self.url = self.fake.start()
        self.addCleanup(self.fake.stop)
        self.config = os.path.join(self.tmp, "config.json")
        with open(self.config, "w", encoding="utf-8") as f:
            json.dump({"window": WINDOW, "slot_dir": os.path.join(self.tmp, "slots")}, f)

    def start_proxy(self):
        """One real start of the proxy on self.data. -> the startup log."""
        port = free_port()
        log = os.path.join(self.tmp, "server-%d.log" % port)
        with open(log, "w", encoding="utf-8") as f:
            p = subprocess.Popen([sys.executable, "-m", "ctxproxy.server", "--data", self.data,
                                  "--upstream", self.url, "--port", str(port), "--config", self.config],
                                 stdout=f, stderr=f)
        try:
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                if p.poll() is not None:
                    break
                try:
                    with urllib.request.urlopen("http://127.0.0.1:%d/health" % port, timeout=1):
                        break
                except OSError:
                    time.sleep(.05)
        finally:
            p.terminate()
            try:
                p.wait(timeout=20)
            except subprocess.TimeoutExpired:      # pragma: no cover
                p.kill()
                p.wait()
        with open(log, encoding="utf-8", errors="replace") as f:
            return f.read()

    def test_the_choices_survive_the_first_conversation(self):
        first = self.start_proxy()
        self.assertIn("auto config: response_floor = %d" % FLOOR, first, first)
        self.assertIn("auto config: notes_max_tokens = %d" % NOTES, first, first)
        self.assertIn("new data folder", first, first)
        self.assertTrue(os.path.exists(os.path.join(self.data, autoconfig.MARK_FILE)),
                        "the applied choices must be recorded in the data folder")

        make_archive(self.data, rows=4)            # an hour of use

        second = self.start_proxy()
        self.assertIn("auto config: response_floor = %d" % FLOOR, second, second)
        self.assertIn("auto config: notes_max_tokens = %d" % NOTES, second, second)
        self.assertIn("automatic configuration", second, second)

        rep = run_check(self.data, self.config, self.url)
        chosen = {d["field"]: d["value"] for d in rep["auto_config"]["decisions"] if d["changed"]}
        self.assertEqual(chosen.get("response_floor"), FLOOR, rep["auto_config"]["lines"])
        self.assertEqual(chosen.get("notes_max_tokens"), NOTES, rep["auto_config"]["lines"])
        self.assertFalse([d for d in rep["auto_config"]["decisions"] if d.get("kept_existing")],
                         "nothing is 'kept' on a folder this version created")

    def test_a_folder_created_by_0_3_23_gets_its_choices_back_and_the_mark(self):
        """The reporter's own folder: created by 0.3.23 with the choices applied, then conversations. After the
        upgrade the first start applies the choices again and records the mark, so it never depends on the journal
        again."""
        make_archive(self.data, rows=4)
        write_journal(self.data, APPLIED_0323, START_0323)
        log = self.start_proxy()
        self.assertIn("auto config: response_floor = %d" % FLOOR, log, log)
        self.assertIn("auto config: notes_max_tokens = %d" % NOTES, log, log)
        self.assertIn("0.3.23", log, log)
        self.assertTrue(os.path.exists(os.path.join(self.data, autoconfig.MARK_FILE)), log)


if __name__ == "__main__":
    unittest.main()
