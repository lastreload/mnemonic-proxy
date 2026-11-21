# Author: Maurizio Verde — LastReload
"""`init-config` writes a MINIMAL file, and a one-click launcher (0.3.24).

Reported by the first Windows user (u/derbidrd): `init-config --engine strata` used to write
`examples/config.example.json`, sixteen explicit fields — `window` 131072, a `/path/to/strata/sessions`
placeholder, `slot_save`/`mask_anchor`/`autosave` true, `notes_max_tokens` 12288, `response_floor` 16384,
`live_dump` true, `seal_experimental` true. A field written in the config always wins over the automatic
configuration, so that file *switched the automatic configuration off*: on a 115K engine, or on a machine
without the RAM for a session file, it handed the user a wrong setup — from the very command `check` suggests.

What is tested here:

  * the minimal file holds only what nothing can work out: `slot_dir` (only when asked for), `window` for a
    generic engine that does not report its context, the explicit `false` of base mode;
  * no field the proxy chooses by itself, no `live_dump`, no placeholder that turns saved state on;
  * `"auto_config"` is never written (see the module docstring of `ctxproxy.initconfig` for why);
  * `--full` still writes the complete documented example;
  * `--launcher` writes `start-mnemonic-proxy.bat` on Windows, `.sh` elsewhere, with the real paths, the
    engine checked before the proxy starts, and `pause` at the end on Windows.

Standard library only, CPU, a fraction of a second.
"""
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

from ctxproxy import initconfig as examples

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# every field the automatic configuration picks by itself (autoconfig.AUTO_FIELDS), plus the ones that come
# from the engine or that write the whole prompt to disk: none of them may appear in a minimal file
NEVER_MINIMAL = ("slot_save", "mask_anchor", "autosave", "engine_tokenize", "response_floor",
                 "notes_max_tokens", "live_dump", "seal_experimental", "auto_config",
                 "autosave_idle_s", "autosave_keep", "autosave_max_gb", "autosave_min_free_gb")


def run_cli(*argv):
    from ctxproxy.server import main
    out, err, code = io.StringIO(), io.StringIO(), 0
    try:
        with redirect_stdout(out), redirect_stderr(err):
            main(list(argv))
    except SystemExit as e:
        code = e.code if isinstance(e.code, int) else 1
    return code, out.getvalue() + err.getvalue()


class MinimalContentTests(unittest.TestCase):
    def test_strata_is_empty_on_purpose(self):
        # Strata reports its window on /v1/status and autoconfig decides the saved-state options: nothing
        # is left for the file to say (README, "Strata in two commands")
        self.assertEqual(examples.minimal("strata"), {})

    def test_strata_with_slot_dir_holds_only_slot_dir(self):
        self.assertEqual(examples.minimal("strata", slot_dir="/srv/sessions"),
                         {"slot_dir": "/srv/sessions"})

    def test_no_minimal_file_writes_a_field_the_proxy_chooses_itself(self):
        for alias in examples.ENGINES:
            with self.subTest(alias=alias):
                raw = examples.minimal(alias, slot_dir="/srv/sessions"
                                       if examples.accepts_slot_dir(alias) else None)
                for field in NEVER_MINIMAL:
                    if alias == "official-strata" and field in ("slot_save", "autosave"):
                        continue        # base mode IS the explicit "no" to saved state: see below
                    self.assertNotIn(field, raw, "%s wrote %s" % (alias, field))

    def test_no_minimal_file_ever_writes_auto_config(self):
        # writing "auto_config": true would disable the protection of autoconfig.decide() on a data folder
        # that already holds conversations: an upgrade would silently change how they run
        for alias in examples.ENGINES:
            with self.subTest(alias=alias):
                self.assertNotIn("auto_config", examples.minimal(alias))
                self.assertNotIn("auto_config", examples.minimal_text(alias))

    def test_no_minimal_file_carries_a_path_placeholder(self):
        for alias in examples.ENGINES:
            with self.subTest(alias=alias):
                self.assertNotIn("/path/to", examples.minimal_text(alias))

    def test_base_mode_strata_says_no_to_saved_state_and_nothing_else(self):
        # the engine *can* save state, so only the user can say "base mode": that is the whole file
        self.assertEqual(examples.minimal("official-strata"), {"slot_save": False, "autosave": False})

    def test_a_generic_engine_needs_its_window_written(self):
        # the proxy cannot read the context of a plain OpenAI-compatible engine, so `window` is the one
        # thing it has to be told; every threshold then scales from it
        self.assertEqual(examples.minimal("openai-32k"), {"window": 32768})

    def test_ds4_and_llama_server_need_nothing(self):
        for alias in ("ds4", "llama-server", "llama-server-32k", "llama-server-router"):
            with self.subTest(alias=alias):
                self.assertEqual(examples.minimal(alias), {})

    def test_every_minimal_file_is_a_valid_proxy_config(self):
        from ctxproxy.core import Config
        import dataclasses
        names = {f.name for f in dataclasses.fields(Config)}
        for alias in examples.ENGINES:
            with self.subTest(alias=alias):
                raw = json.loads(examples.minimal_text(alias))
                self.assertEqual(set(raw) - names, set())
                Config.from_dict(raw)

    def test_slot_dir_is_refused_where_it_means_nothing(self):
        for alias in ("ds4", "openai-32k", "official-strata"):
            with self.subTest(alias=alias):
                self.assertFalse(examples.accepts_slot_dir(alias))
                with self.assertRaises(ValueError):
                    examples.minimal(alias, slot_dir="/srv/sessions")

    def test_the_note_names_what_was_left_out(self):
        for alias in examples.ENGINES:
            with self.subTest(alias=alias):
                note = examples.minimal_note(alias)
                self.assertIn("chosen by the proxy", note)


class CliMinimalTests(unittest.TestCase):
    def test_strata_writes_an_empty_object(self):
        with tempfile.TemporaryDirectory() as d:
            code, out = run_cli("init-config", "--engine", "strata", "--dir", d)
            self.assertEqual(code, 0, out)
            with open(os.path.join(d, "config.json"), encoding="utf-8") as f:
                self.assertEqual(json.load(f), {})
            self.assertIn("--full", out)

    def test_slot_dir_lands_in_the_file(self):
        with tempfile.TemporaryDirectory() as d:
            code, out = run_cli("init-config", "--engine", "strata", "--dir", d,
                                "--slot-dir", os.path.join(d, "sessions"))
            self.assertEqual(code, 0, out)
            with open(os.path.join(d, "config.json"), encoding="utf-8") as f:
                self.assertEqual(json.load(f), {"slot_dir": os.path.join(d, "sessions")})

    def test_slot_dir_on_an_engine_that_has_none_fails_without_a_traceback(self):
        with tempfile.TemporaryDirectory() as d:
            code, out = run_cli("init-config", "--engine", "openai-32k", "--dir", d,
                                "--slot-dir", "/srv/sessions")
            self.assertEqual(code, 1, out)
            self.assertNotIn("Traceback", out)
            self.assertIn("strata", out)
            self.assertFalse(os.path.exists(os.path.join(d, "config.json")))

    def test_full_still_writes_the_documented_example(self):
        with tempfile.TemporaryDirectory() as d:
            code, out = run_cli("init-config", "--engine", "strata", "--dir", d, "--full")
            self.assertEqual(code, 0, out)
            with open(os.path.join(d, "config.json"), encoding="utf-8") as f:
                raw = json.load(f)
            self.assertEqual(raw, json.loads(examples.read("strata")))
            self.assertIn("config.example.json", out)

    def test_full_substitutes_the_slot_dir_placeholder(self):
        with tempfile.TemporaryDirectory() as d:
            code, out = run_cli("init-config", "--engine", "strata", "--dir", d, "--full",
                                "--slot-dir", "/srv/sessions")
            self.assertEqual(code, 0, out)
            with open(os.path.join(d, "config.json"), encoding="utf-8") as f:
                self.assertEqual(json.load(f)["slot_dir"], "/srv/sessions")

    def test_full_without_slot_dir_warns_about_the_placeholder(self):
        with tempfile.TemporaryDirectory() as d:
            code, out = run_cli("init-config", "--engine", "strata", "--dir", d, "--full")
            self.assertEqual(code, 0, out)
            self.assertIn("slot_dir", out)

    def test_every_alias_writes_a_minimal_file_that_reads_back(self):
        for alias in examples.ENGINES:
            with self.subTest(alias=alias), tempfile.TemporaryDirectory() as d:
                code, out = run_cli("init-config", "--engine", alias, "--dir", d)
                self.assertEqual(code, 0, out)
                with open(os.path.join(d, "config.json"), encoding="utf-8") as f:
                    json.load(f)

    def test_the_installed_command_writes_the_minimal_file_in_a_subprocess(self):
        with tempfile.TemporaryDirectory() as d:
            p = subprocess.run([sys.executable, "-m", "ctxproxy.server", "init-config",
                                "--engine", "openai-32k", "--dir", d],
                               capture_output=True, text=True, timeout=60, cwd=ROOT)
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
            with open(os.path.join(d, "config.json"), encoding="utf-8") as f:
                self.assertEqual(json.load(f), {"window": 32768})


class LauncherTests(unittest.TestCase):
    def test_the_windows_launcher_is_a_bat_and_the_unix_one_a_sh(self):
        self.assertEqual(examples.launcher_name("Windows"), "start-mnemonic-proxy.bat")
        self.assertEqual(examples.launcher_name("Linux"), "start-mnemonic-proxy.sh")
        self.assertEqual(examples.launcher_name("Darwin"), "start-mnemonic-proxy.sh")

    def test_the_bat_carries_the_real_values_and_pauses_at_the_end(self):
        txt = examples.launcher_text("Windows", upstream="http://127.0.0.1:8080", port=8096,
                                     data="C:\\mn\\data", config="C:\\mn\\config.json",
                                     tokenizer="C:\\packs\\gyro-gyro-m\\tokenizer")
        self.assertTrue(txt.startswith("@echo off"))
        self.assertIn("http://127.0.0.1:8080", txt)
        self.assertIn("8096", txt)
        self.assertIn("C:\\mn\\data", txt)
        self.assertIn("C:\\mn\\config.json", txt)
        self.assertIn("C:\\packs\\gyro-gyro-m\\tokenizer", txt)
        self.assertIn("pause", txt)
        self.assertIn("title", txt)
        self.assertTrue(txt.endswith("pause\r\n"), repr(txt[-40:]))

    def test_the_bat_uses_crlf_everywhere(self):
        txt = examples.launcher_text("Windows", upstream="http://127.0.0.1:8095", port=8096,
                                     data=".\\data", config=".\\config.json")
        self.assertEqual(txt.count("\n"), txt.count("\r\n"), "a bare LF in a .bat")
        self.assertNotIn("\r\r", txt)

    def test_the_bat_declares_its_variables_at_the_top_and_has_no_personal_path(self):
        txt = examples.launcher_text("Windows", upstream="http://127.0.0.1:8095", port=8096,
                                     data=".\\data", config=".\\config.json")
        head = txt.split("cd /d")[0]
        for var in ("UPSTREAM", "PORT", "DATA", "CONFIG", "VENV"):
            self.assertIn("set %s=" % var, head, var)
        self.assertNotIn("derbidrd", txt)
        self.assertNotIn("mverde", txt)

    def test_without_a_tokenizer_the_line_is_present_but_commented_out(self):
        txt = examples.launcher_text("Windows", upstream="http://127.0.0.1:8095", port=8096,
                                     data=".\\data", config=".\\config.json")
        self.assertIn("rem if not defined TOKENIZER", txt)
        for line in txt.split("\r\n"):
            if "TOKENIZER=" in line and not line.strip().startswith("rem"):
                self.fail("the tokenizer is set for real without --tokenizer: %r" % line)

    def test_the_launcher_checks_the_engine_before_starting_the_proxy(self):
        starts = {"Windows": '"%PROXY%" --upstream', "Linux": 'exec "$PROXY"'}
        for system, start in starts.items():
            with self.subTest(system=system):
                txt = examples.launcher_text(system, upstream="http://127.0.0.1:8095", port=8096,
                                             data="./data", config="./config.json")
                self.assertLess(txt.index("does not answer"), txt.index(start),
                                "the engine is checked after the proxy starts")
                self.assertIn(" check ", txt)

    def test_the_launcher_says_what_to_do_when_the_port_is_taken(self):
        for system in ("Windows", "Linux"):
            with self.subTest(system=system):
                txt = examples.launcher_text(system, upstream="http://127.0.0.1:8095", port=8096,
                                             data="./data", config="./config.json")
                self.assertIn("8096", txt)
                low = txt.lower()
                self.assertTrue("port" in low and "taken" in low, txt)

    def test_the_sh_launcher_is_posix_and_execs_the_proxy(self):
        txt = examples.launcher_text("Linux", upstream="http://127.0.0.1:8095", port=8096,
                                     data="./data", config="./config.json")
        self.assertTrue(txt.startswith("#!/bin/sh"))
        self.assertNotIn("\r", txt)
        self.assertIn("exec ", txt)

    def test_the_generated_sh_passes_shellcheck_or_sh_n(self):
        import shutil as _sh
        txt = examples.launcher_text("Linux", upstream="http://127.0.0.1:8095", port=8096,
                                     data="./data", config="./config.json")
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "l.sh")
            with open(p, "w", encoding="utf-8") as f:
                f.write(txt)
            sh = _sh.which("sh") or _sh.which("dash") or _sh.which("bash")
            if not sh:
                self.skipTest("no POSIX shell to parse with")
            r = subprocess.run([sh, "-n", p], capture_output=True, text=True, timeout=30)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_the_cli_writes_the_launcher_next_to_the_config(self):
        with tempfile.TemporaryDirectory() as d:
            code, out = run_cli("init-config", "--engine", "strata", "--dir", d, "--launcher",
                                "--upstream", "http://127.0.0.1:8080", "--port", "8096",
                                "--data", os.path.join(d, "data"))
            self.assertEqual(code, 0, out)
            name = examples.launcher_name()
            p = os.path.join(d, name)
            self.assertTrue(os.path.isfile(p), out)
            self.assertIn(name, out)
            with open(p, encoding="utf-8") as f:
                txt = f.read()
            self.assertIn("http://127.0.0.1:8080", txt)
            self.assertIn(os.path.join(d, "data"), txt)
            self.assertIn(os.path.join(d, "config.json"), txt)
            self.assertTrue(os.path.isfile(os.path.join(d, "config.json")))

    def test_the_generated_launcher_is_executable_off_windows(self):
        if os.name == "nt":
            self.skipTest("POSIX permissions only")
        with tempfile.TemporaryDirectory() as d:
            code, out = run_cli("init-config", "--engine", "strata", "--dir", d, "--launcher")
            self.assertEqual(code, 0, out)
            mode = os.stat(os.path.join(d, examples.launcher_name())).st_mode
            self.assertTrue(mode & stat.S_IXUSR, oct(mode))

    def test_the_launcher_is_never_overwritten_without_force(self):
        with tempfile.TemporaryDirectory() as d:
            code, out = run_cli("init-config", "--engine", "strata", "--dir", d, "--launcher")
            self.assertEqual(code, 0, out)
            p = os.path.join(d, examples.launcher_name())
            with open(p, "w", encoding="utf-8") as f:
                f.write("mine\n")
            code, out = run_cli("init-config", "--engine", "strata", "--dir", d, "--launcher", "--force")
            self.assertEqual(code, 0, out)
            with open(p, encoding="utf-8") as f:
                self.assertNotEqual(f.read(), "mine\n")

    def test_an_existing_launcher_alone_is_reported_and_kept(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, examples.launcher_name())
            with open(p, "w", encoding="utf-8") as f:
                f.write("mine\n")
            code, out = run_cli("init-config", "--engine", "strata", "--dir", d, "--launcher")
            self.assertEqual(code, 1, out)
            self.assertIn("--force", out)
            with open(p, encoding="utf-8") as f:
                self.assertEqual(f.read(), "mine\n")


class GenericWindowScalingTests(unittest.TestCase):
    """`{"window": 32768}` alone has to be a complete 32K configuration, or the minimal file is a lie."""

    def _apply(self, raw):
        from ctxproxy.core import Config
        from ctxproxy import engines
        cfg = Config.from_dict(raw)
        eng = engines.Engine(kind="openai", upstream="http://127.0.0.1:8095")
        engines.apply(cfg, eng, None, set(raw), log=None)
        return cfg

    def test_a_written_window_scales_the_window_sized_thresholds(self):
        cfg = self._apply({"window": 32768})
        full = json.loads(examples.read("openai-32k"))
        for field in ("mask_trigger", "mask_target", "keep_recent_tokens", "min_batch_tokens",
                      "tail_max", "reserve", "default_response", "pins_max_tokens",
                      "recall_struct_max_tokens", "auto_recall_max_tokens"):
            with self.subTest(field=field):
                self.assertEqual(getattr(cfg, field), full[field],
                                 "%s: minimal gives %s, the documented 32K example says %s"
                                 % (field, getattr(cfg, field), full[field]))

    def test_a_threshold_written_by_the_user_still_wins(self):
        cfg = self._apply({"window": 32768, "mask_trigger": 12345})
        self.assertEqual(cfg.mask_trigger, 12345)

    def test_the_default_window_is_left_alone(self):
        from ctxproxy.core import Config
        cfg = self._apply({})
        self.assertEqual(cfg.mask_trigger, Config().mask_trigger)


if __name__ == "__main__":
    unittest.main()
