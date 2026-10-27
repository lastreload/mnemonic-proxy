"""Issue #17: live checks on fresh Strata 0.2.x sessions (CPU-only HTTP fake)."""
import io
import json
import os
import socket
import tempfile
import unittest
from contextlib import redirect_stdout
import urllib.error
import urllib.request
from unittest import mock

from ctxproxy import check, engines
from ctxproxy.core import Config
from ctxproxy.fake_engine import FakeEngine
from ctxproxy.upstream import Upstream


def policy(body):
    if body.get("tools"):
        return {"role": "assistant", "tool_calls": [{"id": "call_time", "type": "function",
                "function": {"name": "get_time", "arguments": '{"city":"Rome"}'}}]}
    return {"role": "assistant", "content": "ready"}


class TestEmptySlotLive(unittest.TestCase):
    def setUp(self):
        self.fake = FakeEngine(policy=policy, reject_empty_save=True)
        self.up = Upstream(self.fake.start())
        self.addCleanup(self.fake.stop)
        self.eng = engines.detect(self.up, "auto", 0)
        self.cfg = Config(slot_save=True)

    def live(self):
        rep = check.Report()
        check._live(rep, self.up, self.eng, self.cfg)
        return rep

    def assert_passed(self, rep):
        self.assertEqual(rep.exit_code, 0, rep.items)
        self.assertIn(("live: save/restore", "ok"), {(i["check"], i["status"]) for i in rep.items})

    def test_fake_matches_strata_empty_session_response(self):
        with urllib.request.urlopen(self.up.base + "/slots") as r:
            self.assertEqual(json.load(r), [{"id": 0, "n_ctx": 131072, "is_processing": False,
                                            "n_prompt_tokens": 0}])
        req = urllib.request.Request(self.up.base + "/slots/0?action=save", data=b'{"filename":"empty.bin"}',
                                     headers={"Content-Type": "application/json"})
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(req)
        self.assertEqual(cm.exception.code, 400)
        self.assertEqual(json.loads(cm.exception.read()), {"error": {"code": 400,
                         "message": "no complete session to save", "type": "invalid_request_error", "kind": "invalid"}})
        self.assertFalse(self.fake.files)

    def test_check_live_cli_is_ready_on_fresh_strata(self):
        with tempfile.TemporaryDirectory() as tmp, socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
            sock.close()
            cfg = os.path.join(tmp, "config.json")
            with open(cfg, "w") as f:
                json.dump({"slot_save": True}, f)
            out = io.StringIO()
            with redirect_stdout(out), self.assertRaises(SystemExit) as cm:
                check.main(["--live", "--json", "--config", cfg, "--upstream", self.up.base,
                            "--data", os.path.join(tmp, "data"), "--port", str(port)])
            report = json.loads(out.getvalue())
        self.assertTrue(report["ready"], report)
        self.assertEqual(cm.exception.code, 2, report)  # slot_dir deliberately unset: fake has no disk files
        self.assertFalse(any(i["status"] == "fail" for i in report["checks"]))
        self.assertTrue(any(i["check"] == "live: save/restore" and i["status"] == "ok"
                            for i in report["checks"]))

    def test_empty_slot_skips_backup_and_final_restore(self):
        rep = self.live()
        self.assert_passed(rep)
        self.assertEqual([a for a, _, _ in self.fake.slots], ["save", "restore"])
        self.assertFalse(any("backup" in fn for _, fn, _ in self.fake.slots))
        self.assertTrue(any(i["check"] == "live: backup" and i["status"] == "info" and
                            "empty" in i["evidence"] and "restore" in i["evidence"] for i in rep.items))
        self.assertFalse(self.fake.requests[0]["chat_template_kwargs"]["enable_thinking"])

    def test_empty_session_error_fallback_without_slots_metadata(self):
        get = engines._get_json
        with mock.patch.object(engines, "_get_json", side_effect=lambda up, path:
                               None if path == "/slots" else get(up, path)):
            rep = self.live()
        self.assert_passed(rep)
        self.assertEqual([a for a, _, _ in self.fake.slots], ["save", "save", "restore"])
        self.assertTrue(self.fake.slots[0][1].endswith("-backup.bin"))
        self.assertFalse(any(a == "restore" and fn.endswith("-backup.bin") for a, fn, _ in self.fake.slots))

    def test_nonempty_slot_is_backed_up_and_restored(self):
        self.up.chat({"messages": [{"role": "user", "content": "Existing conversation"}]})
        before = self.fake.live
        self.assert_passed(self.live())
        self.assertEqual(self.fake.live, before)
        self.assertEqual([a for a, _, _ in self.fake.slots], ["save", "save", "restore", "restore"])
        self.assertEqual(self.fake.slots[0][1], self.fake.slots[-1][1])

    def test_other_backup_errors_still_fail_without_generation(self):
        self.fake.live = "Existing conversation"
        with mock.patch.object(self.up, "slot", side_effect=RuntimeError("upstream HTTP 500: disk full")):
            rep = self.live()
        self.assertEqual(rep.exit_code, 1)
        self.assertEqual(rep.items[0]["check"], "live: backup")
        self.assertIn("disk full", rep.items[0]["evidence"])
        self.assertFalse(self.fake.requests)

    def test_busy_slot_is_not_touched(self):
        self.fake.busy = True
        self.assertEqual(self.live().exit_code, 1)
        self.assertFalse(self.fake.slots)
        self.assertFalse(self.fake.requests)

    def test_old_strata_still_preserves_its_slot(self):
        self.fake.reject_empty_save = False  # old Strata has no /slots metadata
        self.assert_passed(self.live())
        self.assertEqual(self.fake.live, "")
        self.assertEqual([a for a, _, _ in self.fake.slots], ["save", "save", "restore", "restore"])

    def test_llama_nonempty_slot_still_preserved(self):
        self.fake.flavor = "llama"
        self.eng = engines.detect(self.up, "auto", 0)
        self.fake.live = "Existing llama conversation"
        before = self.fake.live
        self.assert_passed(self.live())
        self.assertEqual(self.fake.live, before)
        self.assertEqual([a for a, _, _ in self.fake.slots], ["save", "save", "restore", "restore"])
        self.assertNotIn("chat_template_kwargs", self.fake.requests[0])


if __name__ == "__main__":
    unittest.main()
