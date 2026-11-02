# Author: Maurizio Verde — LastReload
"""The example configurations as the user meets them after `pip install` (new-user trial, friction A2).

After `pip install mnemonic-proxy` the repository folder `examples/` does not exist on the user's disk, yet
the README and `check` used to point at `examples/config.llama-server.json`. Two answers are tested here:

  * every message prints the FULL raw URL of the example for the version that is installed (the `vX.Y.Z` tag,
    `main` for a development version);
  * the examples ship inside the wheel, so `mnemonic-proxy init-config --engine ...` writes one locally with
    no network at all.

Standard library only, CPU, a fraction of a second.
"""
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

from ctxproxy import initconfig as examples

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class RefTests(unittest.TestCase):
    def test_a_release_version_points_at_its_own_tag(self):
        self.assertEqual(examples.ref("0.3.22"), "v0.3.22")

    def test_a_development_version_points_at_main(self):
        # 0.3.23.dev0 is `main`: the tag does not exist yet, so a vX.Y.Z URL would be a 404 (friction A4)
        self.assertEqual(examples.ref("0.3.23.dev0"), "main")
        self.assertEqual(examples.ref("0.4.0rc1"), "main")

    def test_the_installed_version_decides_by_default(self):
        from ctxproxy import __version__
        expected = "main" if examples.ref(__version__) == "main" else "v" + __version__
        self.assertEqual(examples.ref(), expected)

    def test_url_is_a_full_raw_github_url(self):
        u = examples.url("config.llama-server.json", version="0.3.22")
        self.assertEqual(u, "https://raw.githubusercontent.com/lastreload/mnemonic-proxy/v0.3.22/"
                            "examples/config.llama-server.json")
        self.assertTrue(examples.url("config.ds4.json", version="0.3.23.dev0").split("/")[-3] == "main")


class CatalogTests(unittest.TestCase):
    def test_every_engine_alias_names_a_file_that_exists_in_the_repo(self):
        for alias, (name, why) in examples.ENGINES.items():
            with self.subTest(alias=alias):
                self.assertTrue(os.path.isfile(os.path.join(ROOT, "examples", name)), name)
                self.assertTrue(why.strip(), alias)

    def test_the_four_aliases_of_the_readme_exist(self):
        for alias in ("strata", "llama-server", "ds4", "openai-32k"):
            self.assertIn(alias, examples.ENGINES)

    def test_read_returns_a_valid_proxy_config(self):
        from ctxproxy.core import Config
        import dataclasses
        names = {f.name for f in dataclasses.fields(Config)}
        for alias in examples.ENGINES:
            with self.subTest(alias=alias):
                raw = json.loads(examples.read(alias))
                self.assertEqual(set(raw) - names, set())
                Config.from_dict(raw)

    def test_an_unknown_alias_is_refused_with_the_list(self):
        with self.assertRaises(KeyError) as cm:
            examples.read("gpt5")
        self.assertIn("llama-server", str(cm.exception))

    def test_the_examples_folder_is_found_from_the_package_or_the_repo(self):
        p = examples.path("config.llama-server.json")
        self.assertTrue(p and os.path.isfile(p), p)


class InitConfigTests(unittest.TestCase):
    def run_cli(self, *argv):
        from ctxproxy.server import main
        out, err, code = io.StringIO(), io.StringIO(), 0
        try:
            with redirect_stdout(out), redirect_stderr(err):
                main(list(argv))
        except SystemExit as e:
            code = e.code if isinstance(e.code, int) else 1
        return code, out.getvalue() + err.getvalue()

    def test_it_writes_the_config_of_the_chosen_engine(self):
        with tempfile.TemporaryDirectory() as d:
            code, out = self.run_cli("init-config", "--engine", "llama-server", "--dir", d)
            self.assertEqual(code, 0, out)
            p = os.path.join(d, "config.json")
            self.assertTrue(os.path.isfile(p), out)
            with open(p, encoding="utf-8") as f:
                raw = json.load(f)
            self.assertTrue(raw["slot_save"])
            self.assertIn("config.json", out)

    def test_every_engine_alias_can_be_written(self):
        for alias in examples.ENGINES:
            with self.subTest(alias=alias), tempfile.TemporaryDirectory() as d:
                code, out = self.run_cli("init-config", "--engine", alias, "--dir", d)
                self.assertEqual(code, 0, out)
                with open(os.path.join(d, "config.json"), encoding="utf-8") as f:
                    json.load(f)

    def test_it_never_overwrites_without_force(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "config.json")
            with open(p, "w", encoding="utf-8") as f:
                f.write('{"window": 65536}')
            code, out = self.run_cli("init-config", "--engine", "ds4", "--dir", d)
            self.assertEqual(code, 1, out)
            self.assertIn("--force", out)
            with open(p, encoding="utf-8") as f:
                self.assertEqual(json.load(f), {"window": 65536})
            code, out = self.run_cli("init-config", "--engine", "ds4", "--dir", d, "--force")
            self.assertEqual(code, 0, out)
            with open(p, encoding="utf-8") as f:
                self.assertEqual(json.load(f)["engine"], "ds4")

    def test_it_creates_the_folder(self):
        with tempfile.TemporaryDirectory() as d:
            sub = os.path.join(d, "new", "deeper")
            code, out = self.run_cli("init-config", "--engine", "strata", "--dir", sub)
            self.assertEqual(code, 0, out)
            self.assertTrue(os.path.isfile(os.path.join(sub, "config.json")))

    def test_an_unknown_engine_lists_the_known_ones_without_a_traceback(self):
        code, out = self.run_cli("init-config", "--engine", "gpt5")
        self.assertEqual(code, 2, out)
        self.assertIn("llama-server", out)
        self.assertNotIn("Traceback", out)

    def test_without_engine_it_lists_what_it_can_write(self):
        code, out = self.run_cli("init-config")
        self.assertEqual(code, 2, out)
        self.assertIn("--engine", out)

    def test_the_installed_command_writes_a_config_in_a_subprocess(self):
        # end to end through the real entry point, as the README tells the user to run it
        with tempfile.TemporaryDirectory() as d:
            p = subprocess.run([sys.executable, "-m", "ctxproxy.server", "init-config",
                                "--engine", "openai-32k", "--dir", d],
                               capture_output=True, text=True, timeout=60, cwd=ROOT)
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
            with open(os.path.join(d, "config.json"), encoding="utf-8") as f:
                self.assertEqual(json.load(f)["window"], 32768)


class PackagingTests(unittest.TestCase):
    def test_the_examples_are_declared_as_package_data(self):
        # the wheel must carry examples/: `init-config` works after `pip install`, with no network
        with open(os.path.join(ROOT, "pyproject.toml"), encoding="utf-8") as f:
            txt = f.read()
        self.assertIn("ctxproxy.examples", txt)
        self.assertIn('"ctxproxy.examples" = "examples"', txt)


if __name__ == "__main__":
    unittest.main()
