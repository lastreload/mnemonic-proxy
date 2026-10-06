"""Onboarding commands: `mnemonic-proxy demo`, `mnemonic-proxy check`, /health and /v1/engine.

    python3 -m unittest -v tests.test_onboarding
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
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ctxproxy import check, demo  # noqa: E402
from ctxproxy.fake_engine import FakeEngine  # noqa: E402
from ctxproxy.server import build_parser, load_config, main  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class TestDemo(unittest.TestCase):
    def test_demo_passes(self):
        out = io.StringIO()
        self.assertTrue(demo.run(out=out))
        t = out.getvalue()
        self.assertIn("PASS", t)
        self.assertIn(demo.SECRET, t)
        self.assertIn("identical to the original: yes", t)
        self.assertIn("Does not prove", t)

    def test_subcommand_exit_code(self):
        with redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as cm:
            main(["demo"])
        self.assertEqual(cm.exception.code, 0)


class CheckBase(unittest.TestCase):
    flavor, slot_save, n_ctx = "llama", True, 8192

    def setUp(self):
        # 0.3.0: windows < 16K are refused; these tests check other things on an 8K fake engine (development switch)
        p = mock.patch.dict(os.environ, {"MNEMONIC_PROXY_ALLOW_SMALL_WINDOW": "1"})
        p.start()
        self.addCleanup(p.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.eng = FakeEngine(flavor=self.flavor, slot_save=self.slot_save, n_ctx=self.n_ctx)
        self.url = self.eng.start()
        self.addCleanup(self.eng.stop)
        self.slots = os.path.join(self.tmp.name, "slots")
        os.makedirs(self.slots)

    def config(self, **over):
        with open(os.path.join(ROOT, "examples", "config.llama-server.json")) as f:
            raw = json.load(f)
        raw["slot_dir"] = self.slots
        raw.update(over)
        p = os.path.join(self.tmp.name, "cfg.json")
        with open(p, "w") as f:
            json.dump(raw, f)
        return p

    def check(self, *extra, cfg=None):
        argv = ["--upstream", self.url, "--port", str(free_port()), "--data", os.path.join(self.tmp.name, "data"),
                "--json", *extra]
        if cfg:
            argv += ["--config", cfg]
        out = io.StringIO()
        with redirect_stdout(out), self.assertRaises(SystemExit) as cm:
            check.main(argv)
        res = json.loads(out.getvalue())
        self.assertEqual(res["exit_code"], cm.exception.code)
        return res["exit_code"], {(c["check"], c["status"]) for c in res["checks"]}, res["checks"]


class TestCheckLlama(CheckBase):
    def test_ready_with_example_config(self):
        code, st, items = self.check(cfg=self.config())
        self.assertEqual(code, 0, items)
        self.assertIn(("saved state", "ok"), st)
        self.assertIn(("slot_dir", "ok"), st)
        self.assertIn(("context window", "ok"), st)
        self.assertIn(("engine type", "ok"), st)

    def test_live_save_restore_and_slot_dir(self):
        code, st, items = self.check("--live", cfg=self.config())
        self.assertIn(("live: generation", "ok"), st)
        self.assertIn(("live: save/restore", "ok"), st, items)
        # the fake engine does not write files: slot_dir cannot be verified -> fail with the right fix
        self.assertIn(("live: slot_dir", "fail"), st)
        self.assertEqual(code, 1)

    def test_saved_state_not_configured(self):
        code, st, _ = self.check()
        self.assertIn(("saved state", "warn"), st)
        self.assertEqual(code, 2)

    def test_window_too_big(self):
        code, st, _ = self.check(cfg=self.config(window=131072))
        self.assertIn(("context window", "fail"), st)
        self.assertEqual(code, 1)

    def test_missing_slot_dir(self):
        code, st, _ = self.check(cfg=self.config(slot_dir=os.path.join(self.tmp.name, "nope")))
        self.assertIn(("slot_dir", "fail"), st)
        self.assertEqual(code, 1)

    def test_port_busy(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        self.addCleanup(s.close)
        argv = ["--upstream", self.url, "--port", str(s.getsockname()[1]), "--data",
                os.path.join(self.tmp.name, "d"), "--json"]
        out = io.StringIO()
        with redirect_stdout(out), self.assertRaises(SystemExit):
            check.main(argv)
        st = {(c["check"], c["status"]) for c in json.loads(out.getvalue())["checks"]}
        self.assertIn(("port", "fail"), st)

    def test_unknown_field_warns(self):
        code, st, _ = self.check(cfg=self.config(save_enabled=True))
        self.assertIn(("config", "warn"), st)


class TestCheckNoSlotSave(CheckBase):
    slot_save = False

    def test_configured_but_unsupported(self):
        code, st, items = self.check(cfg=self.config())
        warn = [i for i in items if i["check"] == "saved state"][0]
        self.assertEqual(warn["status"], "warn")
        self.assertIn("--slot-save-path", warn["fix"])


class TestCheckDs4(CheckBase):
    """0.3.0: check riconosce ds4-server e prende la finestra da context_length, come il server."""
    flavor = "ds4"

    def test_ds4_example_config(self):
        code, st, items = self.check(cfg=os.path.join(ROOT, "examples", "config.ds4.json"))
        self.assertIn(("engine type", "ok"), st)
        self.assertIn(("saved state", "info"), st)
        win = [i for i in items if i["check"] == "context window"][0]
        self.assertEqual(win["status"], "ok", items)
        self.assertIn("n_ctx=8192", win["evidence"])
        self.assertNotIn(("slot_dir", "fail"), st)
        self.assertNotEqual(code, 1, items)


class TestCheckUnreachable(unittest.TestCase):
    def test_engine_down(self):
        out = io.StringIO()
        with redirect_stdout(out), self.assertRaises(SystemExit) as cm:
            check.main(["--upstream", "http://127.0.0.1:9", "--port", str(free_port()), "--data",
                        tempfile.mkdtemp(), "--json"])
        self.assertEqual(cm.exception.code, 1)
        self.assertIn("engine reachable", out.getvalue())


class TestConfigPaths(unittest.TestCase):
    def test_relative_slot_dir_is_absolute(self):
        a = build_parser().parse_args(["--config", os.path.join(ROOT, "examples", "config.llama-server.json")])
        raw, cfg = load_config(a)
        self.assertEqual(raw["slot_dir"], "./slots")
        self.assertEqual(cfg.slot_dir, os.path.abspath("./slots"))

    def test_examples_use_known_fields(self):
        import dataclasses
        from ctxproxy.core import Config
        names = {f.name for f in dataclasses.fields(Config)}
        for fn in os.listdir(os.path.join(ROOT, "examples")):
            if fn.endswith(".json"):
                with open(os.path.join(ROOT, "examples", fn)) as f:
                    self.assertEqual(set(json.load(f)) - names, set(), fn)

    def test_starter_config_keeps_recall_tool_small(self):
        # first-run 2026-10-05: with recall_struct/recall_multi (recall tool 2.4K chars, 12 params) Qwen3-4B on
        # llama-server b11430 called pi's bash 0/6 times; plain recall 4-6/6. The starter config stays plain.
        for fn in ("config.llama-server.json", "config.llama-server-32k.json"):
            with open(os.path.join(ROOT, "examples", fn)) as f:
                raw = json.load(f)
            for k in ("recall_struct", "recall_multi", "tools_paging"):
                self.assertFalse(raw.get(k, False), (fn, k))

    def test_32k_config_masks_within_32k(self):
        # T1 runs the engine with -c 32768: masking must start well before the window is full.
        a = build_parser().parse_args(["--config", os.path.join(ROOT, "examples", "config.llama-server-32k.json")])
        raw, cfg = load_config(a)
        self.assertLess(cfg.mask_trigger, 32768 // 2)
        self.assertLess(cfg.mask_target, cfg.mask_trigger)
        self.assertTrue(cfg.slot_save and cfg.autosave)

    def test_no_8k_example_config(self):
        # 0.3.0: engine windows below 16K are refused; no example config for them
        self.assertFalse(os.path.exists(os.path.join(ROOT, "examples", "config.llama-server-8k.json")))

    def test_no_args_namespace(self):
        a = SimpleNamespace(config=None, engine=None)
        raw, cfg = load_config(a)
        self.assertEqual(raw, {})


if __name__ == "__main__":
    unittest.main()
