# Author: Maurizio Verde — LastReload
"""0.3.23 automatic configuration (ctxproxy/autoconfig.py).

The complaint behind it (r/LocalLLM): "why is the default config not the recommended config ... it would be so
nice if it could detect the engine version, context size, vram + ram and apply the best settings by itself".

These tests pin the three promises:
  * only engine/machine-dependent options are chosen, and an explicit field always wins;
  * nothing is chosen in silence: every decision carries a reason and the way to force it otherwise;
  * `check --explain-config` shows the whole picture without starting the proxy.
"""
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest

from ctxproxy import autoconfig, check, engines
from ctxproxy.core import Config
from tests.fake_engine import FakeEngine
from ctxproxy.upstream import Upstream


def local_engine(**kw):
    """An engine as `detect` would return it for a local engine with saved state."""
    kw.setdefault("kind", "strata")
    kw.setdefault("slot_save", True)
    kw.setdefault("upstream", "http://127.0.0.1:8095")
    kw.setdefault("n_ctx", 131072)
    return engines.Engine(**kw)


BIG = autoconfig.Machine(ram_total_mb=32768, ram_free_mb=20000, sources={"ram": "test"})
TINY = autoconfig.Machine(ram_total_mb=4096, ram_free_mb=1024, sources={"ram": "test"})


class TestMachine(unittest.TestCase):
    def test_ram_is_read_on_this_platform(self):
        total, free, source = autoconfig.read_ram()
        if sys.platform.startswith(("linux", "darwin")) or os.name == "nt":
            self.assertTrue(total and total > 0, "total RAM should be readable here: %r" % ((total, free, source),))
            self.assertTrue(source, "the source of the figure must be reported")
        if total and free:
            self.assertLessEqual(free, total + 1)

    def test_vram_is_optional_and_never_fails(self):
        m = autoconfig.Machine.read(engine=None)
        self.assertIsInstance(m.summary(), dict)
        self.assertIn("RAM", m.line())
        self.assertIn("VRAM", m.line())

    def test_vram_from_the_engine_needs_no_tool(self):
        eng = local_engine(machine={"gpu": {"name": "RTX 4070", "total_mib": 12288, "used_mib": 4288}})
        m = autoconfig.Machine.read(eng)
        self.assertEqual(m.vram_total_mb, 12288)
        self.assertEqual(m.vram_free_mb, 8000)
        self.assertEqual(m.sources.get("vram"), "engine /v1/status machine.gpu")

    def test_no_external_tool_is_required(self):
        """A missing helper is not an error: _run returns "" and the figure stays unknown."""
        self.assertEqual(autoconfig._run(["mnemonic-proxy-no-such-tool-xyz", "--help"]), "")


class TestStateEstimate(unittest.TestCase):
    def test_kb_per_token(self):
        # 131072 tokens * 19.5 KB = about 2.44 GiB of engine state for a full window
        self.assertAlmostEqual(autoconfig.state_file_gib(131072, 19.5), 2.4375, places=3)
        self.assertAlmostEqual(autoconfig.state_file_gib(32768, 19.5), 0.6094, places=3)
        self.assertEqual(autoconfig.state_file_gib(0, 19.5), 0.0)

    def test_the_rate_is_a_config_field_with_a_source(self):
        self.assertEqual(Config().state_kb_per_token, autoconfig.STATE_KB_DEFAULT)
        self.assertIn("autosave.py", autoconfig.STATE_KB_SOURCE)
        self.assertIn("ROUTER-MODE-RESULT.md", autoconfig.STATE_KB_SOURCE)

    def test_the_default_matches_the_figure_autosave_already_uses(self):
        """autosave.py sizes its own disk check at 20000 bytes/token: the two must not disagree."""
        src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(autoconfig.__file__))),
                                "ctxproxy", "autosave.py"), encoding="utf-8").read()
        self.assertIn("per_tok = 20000", src)
        self.assertAlmostEqual(autoconfig.STATE_KB_DEFAULT, 20000 / 1024.0, places=1)

    def test_a_broken_rate_falls_back_to_the_default(self):
        self.assertEqual(autoconfig.state_kb(Config(state_kb_per_token=0)), autoconfig.STATE_KB_DEFAULT)
        self.assertEqual(autoconfig.state_kb(Config(state_kb_per_token=-3)), autoconfig.STATE_KB_DEFAULT)
        self.assertEqual(autoconfig.state_kb(Config(state_kb_per_token=40.0)), 40.0)

    def test_version_tuple(self):
        self.assertEqual(autoconfig.version_tuple("0.1.51"), (0, 1, 51))
        self.assertEqual(autoconfig.version_tuple("v0.1.40"), (0, 1, 40))
        self.assertEqual(autoconfig.version_tuple("6123 (abc1234)"), (6123,))
        self.assertEqual(autoconfig.version_tuple(None), ())
        self.assertEqual(autoconfig.version_tuple("unknown"), ())


class TestSavedStateRules(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def cfg(self, **kw):
        kw.setdefault("slot_dir", self.tmp)
        return Config(**kw)

    def test_turned_on_when_engine_machine_and_folder_all_agree(self):
        cfg = self.cfg()
        d = autoconfig.decide(cfg, local_engine(), set(), BIG)
        self.assertTrue(cfg.slot_save and cfg.mask_anchor and cfg.autosave)
        picked = {x.field: x for x in d if x.changed}
        self.assertEqual(set(autoconfig.AUTO_SAVED_STATE) - set(picked), set())
        self.assertIn("KB/token", picked["slot_save"].why)
        self.assertTrue(picked["slot_save"].override, "a choice must say how to force it otherwise")

    def test_explicit_false_always_wins(self):
        cfg = self.cfg(slot_save=False, mask_anchor=False, autosave=False)
        explicit = {"slot_save", "mask_anchor", "autosave"}
        d = autoconfig.decide(cfg, local_engine(), explicit, BIG)
        self.assertFalse(cfg.slot_save or cfg.mask_anchor or cfg.autosave)
        self.assertTrue(all(not x.changed for x in d if x.field in explicit))
        self.assertEqual([x.why for x in d if x.field == "slot_save"], ["written in the config"])

    def test_engine_without_saved_state(self):
        cfg = self.cfg()
        autoconfig.decide(cfg, local_engine(kind="openai", slot_save=False), set(), BIG)
        self.assertFalse(cfg.slot_save or cfg.mask_anchor or cfg.autosave)
        ok, why, fix = autoconfig.saved_state_gate(cfg, local_engine(kind="openai", slot_save=False), BIG)
        self.assertFalse(ok)
        self.assertIn("does not offer saved state", why)
        self.assertTrue(fix)

    def test_remote_engine_is_not_assumed_to_share_the_folder(self):
        cfg = self.cfg()
        eng = local_engine(upstream="http://10.0.0.9:8095")
        autoconfig.decide(cfg, eng, set(), BIG)
        self.assertFalse(cfg.slot_save)
        ok, why, _ = autoconfig.saved_state_gate(cfg, eng, BIG)
        self.assertFalse(ok)
        self.assertIn("another host", why)

    def test_no_slot_dir_no_saved_state_but_the_fix_is_one_field(self):
        cfg = Config()
        ok, why, fix = autoconfig.saved_state_gate(cfg, local_engine(), BIG)
        self.assertFalse(ok)
        self.assertIn("slot_dir", why)
        self.assertIn("slot_dir", fix)

    def test_not_enough_free_ram_for_the_state_file(self):
        cfg = self.cfg()
        ok, why, _ = autoconfig.saved_state_gate(cfg, local_engine(), TINY)
        self.assertFalse(ok)
        self.assertIn("2.4 GiB", why)
        self.assertIn("1.0 GiB of RAM", why)
        autoconfig.decide(cfg, local_engine(), set(), TINY)
        self.assertFalse(cfg.slot_save)

    def test_a_smaller_window_fits_the_same_small_machine(self):
        cfg = self.cfg(window=16384)
        ok, _, _ = autoconfig.saved_state_gate(cfg, local_engine(n_ctx=16384), TINY)
        self.assertTrue(ok, "16384 tokens * 19.5 KB = 0.3 GiB: it fits in 1 GiB")

    def test_unknown_ram_does_not_block(self):
        cfg = self.cfg()
        ok, why, _ = autoconfig.saved_state_gate(cfg, local_engine(), autoconfig.Machine())
        self.assertTrue(ok)
        self.assertIn("RAM unknown", why)

    def test_strata_older_than_the_session_files(self):
        cfg = self.cfg()
        ok, why, _ = autoconfig.saved_state_gate(cfg, local_engine(version="0.1.39"), BIG)
        self.assertFalse(ok)
        self.assertIn("0.1.39", why)
        self.assertTrue(autoconfig.saved_state_gate(cfg, local_engine(version="0.1.51"), BIG)[0])

    def test_strata_with_parallel_slots_refuses_save_restore(self):
        # verified in Strata's serve/server.py slot_action(): HTTP 501 before the engine is touched
        cfg = self.cfg()
        ok, why, _ = autoconfig.saved_state_gate(cfg, local_engine(parallel=4), BIG)
        self.assertFalse(ok)
        self.assertIn("parallel", why)

    def test_llama_server_with_parallel_slots_keeps_saved_state(self):
        cfg = self.cfg()
        eng = local_engine(kind="llama.cpp", parallel=4, version="6123 (abc)")
        self.assertTrue(autoconfig.saved_state_gate(cfg, eng, BIG)[0])

    def test_autosave_needs_the_disk_too(self):
        cfg = self.cfg(autosave_min_free_gb=10 ** 6)       # more free disk than any machine has
        d = autoconfig.decide(cfg, local_engine(), set(), BIG)
        self.assertTrue(cfg.slot_save, "slot_save only needs the RAM")
        self.assertFalse(cfg.autosave, "autosave keeps several files: it needs the disk")
        why = [x.why for x in d if x.field == "autosave"][0]
        self.assertIn("free on the slot_dir disk", why)


class TestOtherRules(unittest.TestCase):
    def test_engine_tokenize_when_the_engine_counts_exactly(self):
        cfg = Config()
        autoconfig.decide(cfg, local_engine(kind="llama.cpp", tokenize=True), set(), BIG)
        self.assertTrue(cfg.engine_tokenize)

    def test_no_engine_tokenize_with_a_local_exact_tokenizer(self):
        cfg = Config()
        autoconfig.decide(cfg, local_engine(kind="llama.cpp", tokenize=True), set(), BIG, exact=True)
        self.assertFalse(cfg.engine_tokenize)

    def test_window_sized_budgets_match_the_readme_at_128k(self):
        cfg = Config(window=131072)
        autoconfig.decide(cfg, local_engine(), set(), BIG)
        self.assertEqual(cfg.response_floor, 16384)
        self.assertEqual(cfg.notes_max_tokens, 12288)

    def test_window_sized_budgets_scale_down(self):
        cfg = Config(window=32768)
        autoconfig.decide(cfg, local_engine(n_ctx=32768), set(), BIG)
        self.assertEqual(cfg.response_floor, 4096, "the value this README suggests for 32K")
        self.assertEqual(cfg.notes_max_tokens, 3072)
        self.assertLessEqual(cfg.response_floor, cfg.window // 4, "never over the startup cap")
        self.assertLessEqual(cfg.notes_max_tokens, cfg.window // 4)

    def test_nothing_is_decided_below_the_minimum_window(self):
        """Under 16384 the proxy already refuses to start without an opt-in: the scaled defaults stay."""
        cfg = Config(window=8192, notes_max_tokens=512)
        d = autoconfig.decide(cfg, local_engine(n_ctx=8192), set(), BIG)
        self.assertEqual((cfg.response_floor, cfg.notes_max_tokens), (0, 512))
        self.assertEqual([x.field for x in d if x.changed and x.field in ("response_floor",
                                                                          "notes_max_tokens")], [])

    def test_explicit_budgets_are_untouched(self):
        cfg = Config(window=131072, response_floor=1000, notes_max_tokens=2000)
        autoconfig.decide(cfg, local_engine(), {"response_floor", "notes_max_tokens"}, BIG)
        self.assertEqual((cfg.response_floor, cfg.notes_max_tokens), (1000, 2000))

    def test_auto_config_false_decides_nothing(self):
        cfg = Config(auto_config=False, slot_dir="/tmp", window=131072)
        self.assertEqual(autoconfig.decide(cfg, local_engine(), set(), BIG), [])
        self.assertFalse(cfg.slot_save)
        self.assertEqual(cfg.response_floor, 0)

    def test_nothing_that_changes_the_prompt_is_touched(self):
        """An old conversation must be replayed byte for byte: the prompt-shaping options keep their default."""
        cfg = Config(slot_dir="/tmp")
        before = {k: getattr(cfg, k) for k in ("mask_reasoning", "mask_tool_args", "recall_struct", "recall_flex",
                                               "recall_multi", "tools_paging", "typed_receipts", "rich_receipts",
                                               "prompt_language", "auto_recall", "recall_leaf_select",
                                               "seal_experimental", "mask_trigger", "mask_target",
                                               "keep_recent_tokens", "recall_tool_name")}
        autoconfig.decide(cfg, local_engine(), set(), BIG)
        self.assertEqual({k: getattr(cfg, k) for k in before}, before)

    def test_every_decision_has_a_reason(self):
        cfg = Config(slot_dir="/tmp", window=131072)
        for d in autoconfig.decide(cfg, local_engine(), set(), BIG):
            self.assertTrue(d.why, d)
            self.assertIn(d.field, {f.name for f in __import__("dataclasses").fields(Config)})
            self.assertTrue(d.line().startswith(("auto ", "kept ")))


class TestEngineDetection(unittest.TestCase):
    """Version, parallelism and GPU come from the endpoints the proxy already calls."""

    def engine(self, **kw):
        eng = FakeEngine(**kw)
        url = eng.start()
        self.addCleanup(eng.stop)
        return eng, engines.detect(Upstream(url))

    def test_strata_version_and_window_and_gpu(self):
        fake, eng = self.engine(flavor="strata", n_ctx=131072)
        self.assertEqual(eng.kind, "strata")
        self.assertEqual(eng.version, fake.version)
        self.assertEqual(eng.n_ctx, 131072)
        self.assertEqual(eng.parallel, 1)
        self.assertEqual((eng.machine.get("gpu") or {}).get("total_mib"), 12288)
        self.assertTrue(eng.upstream.startswith("http://"))
        self.assertEqual(eng.summary().get("version"), fake.version)

    def test_llama_build_info(self):
        fake, eng = self.engine(flavor="llama", n_ctx=32768)
        self.assertEqual(eng.kind, "llama.cpp")
        self.assertEqual(eng.version, fake.version)
        self.assertEqual(eng.machine, {})

    def test_a_generic_engine_reports_no_version(self):
        _, eng = self.engine(flavor="openai")
        self.assertIsNone(eng.version)
        self.assertNotIn("version", eng.summary())


class TestCheckExplainConfig(unittest.TestCase):
    """`check --explain-config`: the choices, without starting the proxy."""

    def run_check(self, flavor, config=None, extra=()):
        fake = FakeEngine(flavor=flavor, n_ctx=131072)
        url = fake.start()
        self.addCleanup(fake.stop)
        tmp = tempfile.mkdtemp()
        argv = ["--json", "--upstream", url, "--port", "18996", "--data", os.path.join(tmp, "data")]
        if config is not None:
            path = os.path.join(tmp, "config.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(config, f)
            argv += ["--config", path]
        argv += list(extra)
        out = io.StringIO()
        old = sys.stdout
        sys.stdout = out
        try:
            check.main(argv)
        except SystemExit:
            pass
        finally:
            sys.stdout = old
        return json.loads(out.getvalue()), tmp

    def test_strata_with_a_slot_dir_gets_the_recommended_setup(self):
        tmp = tempfile.mkdtemp()
        data, _ = self.run_check("strata", {"slot_dir": os.path.join(tmp, "slots")})
        auto = data["auto_config"]
        chosen = {d["field"]: d for d in auto["decisions"] if d["changed"]}
        for field in ("slot_save", "mask_anchor", "autosave", "response_floor", "notes_max_tokens"):
            self.assertIn(field, chosen, auto["decisions"])
            self.assertTrue(chosen[field]["why"])
        self.assertEqual(data["exit_code"], 0, "a recommended setup must be READY without warnings")
        line = [i for i in data["checks"] if i["check"] == "auto config"][0]
        self.assertIn("slot_save=true", line["evidence"])
        self.assertIn("--explain-config", line["fix"])
        # the saved-state check now sees what the server would really run with
        saved = [i for i in data["checks"] if i["check"] == "saved state"][0]
        self.assertEqual(saved["status"], check.OK)
        self.assertIn("slot_save", saved["evidence"])

    def test_without_a_slot_dir_the_reason_is_printed_and_nothing_is_on(self):
        data, _ = self.run_check("strata")
        auto = data["auto_config"]
        kept = {d["field"]: d for d in auto["decisions"] if not d["changed"]}
        self.assertIn("slot_dir", kept["slot_save"]["why"])
        self.assertIn("slot_dir", kept["slot_save"]["override"])
        self.assertTrue(any("slot_save = false" in line for line in auto["lines"]))

    def test_auto_config_false_is_reported(self):
        data, _ = self.run_check("strata", {"auto_config": False, "slot_dir": "/tmp"})
        self.assertEqual(data["auto_config"]["decisions"], [])
        line = [i for i in data["checks"] if i["check"] == "auto config"][0]
        self.assertIn("off", line["evidence"])

    def test_machine_facts_are_in_the_report(self):
        data, _ = self.run_check("strata")
        machine = data["auto_config"]["machine"]
        self.assertIn("sources", machine)
        self.assertTrue(any("engine: strata" in line for line in data["auto_config"]["lines"]))

    def test_explain_config_renders_without_json(self):
        fake = FakeEngine(flavor="llama", n_ctx=32768)
        url = fake.start()
        self.addCleanup(fake.stop)
        tmp = tempfile.mkdtemp()
        out = subprocess.run([sys.executable, "-m", "ctxproxy.check", "--explain-config", "--upstream", url,
                              "--port", "18997", "--data", os.path.join(tmp, "data")],
                             capture_output=True, text=True, timeout=120,
                             cwd=os.path.dirname(os.path.dirname(os.path.abspath(check.__file__))))
        self.assertIn("automatic configuration", out.stdout)
        self.assertIn("engine: llama.cpp", out.stdout)
        self.assertIn("a field written in the config always wins", out.stdout)


if __name__ == "__main__":
    unittest.main()
