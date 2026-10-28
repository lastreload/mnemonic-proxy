"""Tests for setup.sh and run-strata-proxy.sh (the one-click layer, without touching the engine).

Covers the review points:
  1. the flow works without Python 3.14: a created config is born with kv_archive OFF
     (opt-in via KV_ARCHIVE=1), and a personal config asking for kv_archive without
     support makes the script fail with a clear message without being overwritten;
  2. the engine check accepts only 2xx and tells connection/timeout apart from HTTP 4xx/5xx;
  3. no rm -rf on an arbitrary path: VENV_MAIN / VENV_KV outside the project, with "..",
     a symlink, or a folder that is not a venv, are refused before any removal;
  4. privileges: without Python the default is to print the command of the detected
     package manager and stop (no sudo); --install-deps runs it — directly as root,
     via sudo otherwise;
  5. the tests do not depend on the host Python version: the stubs intercept the
     `import compression.zstd` probe, and real venvs control it via a private startup hook.

The tests use real venvs created by `python -m venv` (fast) and a stub
`mnemonic-proxy` that does not start a server: what is tested is the shell, not the proxy.
"""
import glob
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def run(cmd, cwd, env=None, timeout=180):
    e = dict(os.environ)
    e.update(env or {})
    p = subprocess.run(cmd, cwd=cwd, env=e, capture_output=True, text=True, timeout=timeout)
    return p.returncode, p.stdout + p.stderr


def make_tree():
    """copy the tracked baseline (pyproject, ctxproxy, examples) + the current scripts."""
    tmp = tempfile.mkdtemp(prefix="mp-shell-")
    r = subprocess.run(["git", "archive", "HEAD"], cwd=ROOT, capture_output=True)
    if r.returncode != 0:
        raise RuntimeError("git archive failed: " + r.stderr.decode()[:200])
    subprocess.run(["tar", "-x", "-C", tmp], input=r.stdout, check=True)
    for name in ("setup.sh", "run-strata-proxy.sh"):
        shutil.copy(os.path.join(ROOT, name), os.path.join(tmp, name))
    return tmp


def stub_venv(tree, name, kv=False):
    """venv with bin/python and bin/mnemonic-proxy (a stub that starts no server).

    The python is a wrapper around the real one that intercepts ONLY the `import
    compression.zstd` probe: kv=True passes, kv=False fails — so the test does not
    depend on the host Python version (a 3.14 host does not turn kv on "for free").
    """
    d = os.path.join(tree, name)
    binp = os.path.join(d, "bin")
    os.makedirs(binp, exist_ok=True)
    real = sys.executable
    verdict = "0" if kv else "1"
    wrap = os.path.join(binp, "python")
    with open(wrap, "w", encoding="utf-8") as fh:
        fh.write("#!/bin/sh\n"
                 "if [ \"$1\" = \"-c\" ]; then case \"$2\" in *compression.zstd*) exit " + verdict + ";; esac; fi\n"
                 "exec " + real + " \"$@\"\n")
    os.chmod(wrap, 0o755)
    stub = os.path.join(binp, "mnemonic-proxy")
    with open(stub, "w", encoding="utf-8") as fh:
        fh.write("#!/bin/sh\necho \"[stub mnemonic-proxy] $*\"\n")
    os.chmod(stub, 0o755)
    return name


def snapshot_tree(root):
    """Snapshot every path under `root` with size and mtime: two equal snapshots
    prove that a run created, removed or modified nothing (--check is read-only).

    Byte-compiled caches (__pycache__, *.pyc) are skipped: importing Python code
    during the run rewrites them, and they are not part of what --check must keep
    untouched (the venv, the config, the project files)."""
    snap = {}
    for base, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d != "__pycache__")
        for name in sorted(dirs):
            # directories: only their existence matters (their size/mtime change
            # whenever any child is written, which is not what --check must preserve)
            snap[os.path.relpath(os.path.join(base, name), root)] = ("dir",)
        for name in sorted(files):
            if name.endswith((".pyc", ".pyo")):
                continue
            p = os.path.join(base, name)
            try:
                st = os.lstat(p)
            except OSError:
                continue
            snap[os.path.relpath(p, root)] = (st.st_size, st.st_mtime_ns, st.st_mode)
    return snap


def stub_packet(venv):
    """Make a REAL venv look installed for setup.sh: bin/mnemonic-proxy + an
    importable ctxproxy package in site-packages. Used by the --check tests that
    need a *complete* environment, without running pip."""
    binp = os.path.join(venv, "bin")
    with open(os.path.join(binp, "mnemonic-proxy"), "w", encoding="utf-8") as fh:
        fh.write("#!/bin/sh\nprintf 'stub\\n'\n")
    os.chmod(os.path.join(binp, "mnemonic-proxy"), 0o755)
    sp = glob.glob(os.path.join(venv, "lib", "python*", "site-packages"))
    if sp:
        site = sp[0]
    else:                              # a stub venv has no lib/: create one
        site = os.path.join(venv, "lib", "python%d.%d" % sys.version_info[:2], "site-packages")
        os.makedirs(site, exist_ok=True)
    pkg = os.path.join(site, "ctxproxy")
    os.makedirs(pkg, exist_ok=True)
    with open(os.path.join(pkg, "__init__.py"), "w", encoding="utf-8") as fh:
        fh.write("# stub package for the setup tests\n")
    return venv


def _venv_python(venv):
    return os.path.join(venv, "Scripts" if os.name == "nt" else "bin", "python")


def _venv_site_packages(venv):
    code = "import sysconfig; print(sysconfig.get_path('purelib'))"
    return subprocess.check_output([_venv_python(venv), "-c", code],
                                   text=True, timeout=60).strip()


def show_zstd(venv):
    """Make `import compression.zstd` succeed inside a REAL venv via a private
    module loaded by a .pth file, on any host Python. The venv's python stays
    untouched, so pip, venv creation and the package probes keep working: only
    the kv_archive probe is faked."""
    sp = _venv_site_packages(venv)
    with open(os.path.join(sp, "_zstd_fixture.py"), "w", encoding="utf-8") as fh:
        fh.write("import sys, types\n"
                 "if 'compression.zstd' not in sys.modules:\n"
                 "    pkg = types.ModuleType('compression')\n"
                 "    pkg.__path__ = []\n"
                 "    mod = types.ModuleType('compression.zstd')\n"
                 "    pkg.zstd = mod\n"
                 "    sys.modules['compression'] = pkg\n"
                 "    sys.modules['compression.zstd'] = mod\n")
    with open(os.path.join(sp, "zz_zstd_fixture.pth"), "w", encoding="utf-8") as fh:
        fh.write("import _zstd_fixture\n")


def hide_zstd(venv):
    """Block `import compression.zstd` inside a REAL venv via a private module
    loaded by a .pth file.

    The venv's bin/python stays untouched (pip and venv keep working correctly);
    only the kv_archive probe sees a pre-3.14 Python, on any machine.
    """
    sp = _venv_site_packages(venv)
    with open(os.path.join(sp, "_zstd_fixture.py"), "w", encoding="utf-8") as fh:
        fh.write("import sys\n"
                 "class _BlockZstd:\n"
                 "    def find_spec(self, name, path=None, target=None):\n"
                 "        if name == 'compression.zstd':\n"
                 "            raise ImportError('blocked by test')\n"
                 "        return None\n"
                 "sys.meta_path.insert(0, _BlockZstd())\n")
    with open(os.path.join(sp, "zz_zstd_fixture.pth"), "w", encoding="utf-8") as fh:
        fh.write("import _zstd_fixture\n")


class ZstdFixtureTests(unittest.TestCase):
    """The real-venv zstd hooks must load even when sitecustomize is shadowed."""

    def setUp(self):
        self.tree = tempfile.mkdtemp(prefix="mp-zstd-fixture-")
        self.venv = os.path.join(self.tree, "venv")
        subprocess.run([sys.executable, "-m", "venv", self.venv],
                       capture_output=True, text=True, timeout=300, check=True)

    def tearDown(self):
        shutil.rmtree(self.tree, ignore_errors=True)

    def probe(self, fixture, collision):
        fixture(self.venv)
        env = dict(os.environ)
        env.pop("PYTHONPATH", None)
        shadow = os.path.join(self.tree, "shadow")
        if collision:
            os.makedirs(shadow, exist_ok=True)
            with open(os.path.join(shadow, "sitecustomize.py"), "w", encoding="utf-8") as fh:
                fh.write("# Simulates a system sitecustomize ahead of the venv.\n")
            env["PYTHONPATH"] = shadow
        code = (
            "import json, sys\n"
            "try:\n"
            "    import compression.zstd\n"
            "except ImportError:\n"
            "    available = False\n"
            "else:\n"
            "    available = True\n"
            "hook = sys.modules.get('_zstd_fixture')\n"
            "print(json.dumps({'available': available, 'hook_loaded': hook is not None, "
            "'sitecustomize': getattr(sys.modules.get('sitecustomize'), '__file__', None)}))\n"
        )
        rc, out = run([_venv_python(self.venv), "-c", code], cwd=self.tree, env=env)
        self.assertEqual(rc, 0, out)
        result = json.loads(out.strip().splitlines()[-1])
        self.assertTrue(result["hook_loaded"], result)
        if collision:
            self.assertEqual(os.path.realpath(result["sitecustomize"]),
                             os.path.realpath(os.path.join(shadow, "sitecustomize.py")))
        return result

    def test_show_zstd_loads_despite_sitecustomize_collision(self):
        for collision in (False, True):
            with self.subTest(collision=collision):
                self.assertTrue(self.probe(show_zstd, collision)["available"])

    def test_hide_zstd_loads_despite_sitecustomize_collision(self):
        for collision in (False, True):
            with self.subTest(collision=collision):
                self.assertFalse(self.probe(hide_zstd, collision)["available"])


def fake_py314(path, valid=True):
    """Create an executable that mimics the Python >= 3.14 interpreter probe.

    setup.sh asks the interpreter a single question:
        `python -c 'import sys, venv, ensurepip, compression.zstd; ...'`
    valid=True makes that probe succeed (exit 0); valid=False makes it fail. Any
    other invocation still delegates to the host interpreter, so this never depends
    on a real Python 3.14 being installed. `path` may contain spaces."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    verdict = "0" if valid else "1"
    body = ("if [ \"$1\" = \"-c\" ]; then\n"
            "  case \"$2\" in *compression.zstd*) exit " + verdict + ";; esac\n"
            "fi\n")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("#!/bin/sh\n" + body + "exec " + sys.executable + " \"$@\"\n")
    os.chmod(path, 0o755)
    return path


def tmp_abs(tree, name):
    return os.path.join(tree, name)


def strata_dir(tree, slot_path):
    """a 'Strata' folder with one model JSON declaring slot_save_path."""
    d = os.path.join(tree, "strata")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "modelo.json"), "w", encoding="utf-8") as fh:
        json.dump({"slot_save_path": slot_path}, fh)
    return d


def strata_configs(tree, name, items):
    """a 'Strata' folder with several engine JSONs: items = [(model, tokenizer, port, slot)].

    Simulates a multi-pack install: each config points to its own pack's tokenizer
    and slot dir, and the launcher must pick the file of the engine in use, not
    the first one in alphabetical order.
    """
    d = os.path.join(tree, name)
    os.makedirs(d, exist_ok=True)
    for model, tok, port, slot in items:
        os.makedirs(tok, exist_ok=True)
        with open(os.path.join(d, "strata-%s.json" % model), "w", encoding="utf-8") as fh:
            json.dump({"model_name": model, "port": port,
                       "slot_save_path": slot, "tokenizer": tok}, fh)
    return d


class _Handler(BaseHTTPRequestHandler):
    code, sleep = 200, 0.0
    body = b'{"status":"ok","context":{"native":131072}}'

    def do_GET(self):
        if self.sleep:
            import time
            time.sleep(self.sleep)
        body = self.body
        self.send_response(self.code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass                      # the client already hung up (timeout): expected noise

    def log_message(self, *a):
        pass


def serve(code=200, sleep=0.0, body=None):
    h = type("H", (_Handler,),
             {"code": code, "sleep": sleep, "body": body or _Handler.body})
    srv = HTTPServer(("127.0.0.1", 0), h)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return "http://127.0.0.1:%d" % srv.server_address[1], srv



@unittest.skipIf(os.name == "nt", "bash scripts, Linux only")
class RunScriptTests(unittest.TestCase):
    """run-strata-proxy.sh: venv chosen by support, coherent created config, engine check."""

    def setUp(self):
        self.tree = make_tree()
        self.strata = strata_dir(self.tree, os.path.join(self.tree, "sessions"))
        self.cfg = os.path.join(self.tree, "config.json")

    def tearDown(self):
        shutil.rmtree(self.tree, ignore_errors=True)

    def write_config(self, cfg):
        with open(self.cfg, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh, indent=2)

    def read_config(self):
        with open(self.cfg, encoding="utf-8") as fh:
            return json.load(fh)

    def env(self, url, extra=None):
        e = {"SKIP_SETUP": "1", "STRATA_URL": url, "STRATA_DIR": self.strata,
             "TOKENIZER": os.path.join(self.tree, "tok"), "CONFIG": "./config.json",
             "DATA": "./data-test", "PROBE_TIMEOUT": "2"}
        e.update(extra or {})
        return e

    def boot(self, code=200, sleep=0.0, extra=None, body=None):
        url, srv = serve(code, sleep, body)
        rc, out = run(["./run-strata-proxy.sh"], cwd=self.tree, env=self.env(url, extra))
        srv.shutdown()
        srv.server_close()
        return rc, out

    def test_tokenizer_deduced_from_engine_config(self):
        """TOKENIZER is not mandatory: the launcher reads it from the "tokenizer" key
        of the engine JSON, as it already does with slot_save_path. No personal path
        in the launcher: whoever has the engine in any folder finds it automatically."""
        stub_venv(self.tree, ".venv")
        tok = os.path.join(self.tree, "tok_auto")
        os.makedirs(tok, exist_ok=True)
        with open(os.path.join(self.strata, "modelo.json"), "w", encoding="utf-8") as fh:
            json.dump({"slot_save_path": os.path.join(self.tree, "sessions"),
                       "tokenizer": tok}, fh)
        rc, out = self.boot(extra={"TOKENIZER": ""})
        self.assertEqual(rc, 0, out)
        self.assertIn("tokenizer=" + tok, out)
        self.assertIn("--tokenizer " + tok, out)

    def test_without_tokenizer_the_proxy_starts_without_the_flag(self):
        """No tokenizer in the engine config: the launcher must not pass --tokenizer.
        The proxy counts chars / chars_per_token, which is the documented estimate —
        not an error, and no empty path is passed."""
        stub_venv(self.tree, ".venv")
        rc, out = self.boot(extra={"TOKENIZER": ""})
        self.assertEqual(rc, 0, out)
        self.assertNotIn("--tokenizer", out)
        self.assertIn("chars/3.5", out)

    def test_two_configs_without_identification_choose_nothing(self):
        """Two packs in the engine folder, no identifier of the engine in use: the
        launcher passes no --tokenizer and deduces no slot_dir. A file from another
        pack would give wrong numbers and sessions in the wrong place; the chars/3.5
        estimate and the Strata folder's sessions are more honest."""
        stub_venv(self.tree, ".venv")
        d = strata_configs(self.tree, "multi",
                           [("coder", os.path.join(self.tree, "tokA"), 8081,
                             os.path.join(self.tree, "sessA")),
                            ("swift", os.path.join(self.tree, "tokB"), 8080,
                             os.path.join(self.tree, "sessB"))])
        rc, out = self.boot(extra={"TOKENIZER": "", "STRATA_DIR": d})
        self.assertEqual(rc, 0, out)
        # the proxy line is the one that counts: the warning mentions --tokenizer,
        # the call does not.
        stub = [l for l in out.splitlines() if l.startswith("[stub mnemonic-proxy]")]
        self.assertEqual(len(stub), 1, out)
        self.assertNotIn("--tokenizer", stub[0])
        self.assertNotIn("tokA", out)
        self.assertNotIn("tokB", out)
        self.assertNotIn("sessA", out)
        self.assertNotIn("sessB", out)
        self.assertIn("chars/3.5", out)
        self.assertIn("not identified", out)
        self.assertEqual(self.read_config()["slot_dir"], os.path.join(d, "sessions"))

    def test_two_configs_with_same_model_name_choose_nothing(self):
        """Two files declaring the SAME model_name, each with its own tokenizer and
        slot: the engine's 'model' does not break the tie (it matches both) and the
        URL port matches none. Nothing is chosen — two candidates, an honest launcher."""
        stub_venv(self.tree, ".venv")
        d = strata_configs(self.tree, "multi",
                           [("swift", os.path.join(self.tree, "tokB"), 8080,
                             os.path.join(self.tree, "sessB")),
                            ("coder", os.path.join(self.tree, "tokA"), 8081,
                             os.path.join(self.tree, "sessA"))])
        with open(os.path.join(d, "strata-swift-copy.json"), "w", encoding="utf-8") as fh:
            json.dump({"model_name": "swift", "port": 9999,
                       "tokenizer": os.path.join(self.tree, "tokC"),
                       "slot_save_path": os.path.join(self.tree, "sessC")}, fh)
        body = b'{"status":"ok","model":"swift","context":{"native":131072}}'
        rc, out = self.boot(body=body, extra={"TOKENIZER": "", "STRATA_DIR": d})
        self.assertEqual(rc, 0, out)
        stub = [l for l in out.splitlines() if l.startswith("[stub mnemonic-proxy]")]
        self.assertEqual(len(stub), 1, out)
        self.assertNotIn("--tokenizer", stub[0])
        self.assertNotIn("tokB", out)
        self.assertNotIn("tokC", out)
        self.assertIn("not identified", out)
        self.assertEqual(self.read_config()["slot_dir"], os.path.join(d, "sessions"))

    def test_engine_model_picks_tokenizer_and_slot_from_the_same_pack(self):
        """With two configs, the 'model' from /v1/status decides the file: tokenizer and
        slot_dir come from the SAME pack (the one of the engine in use), not from the
        first in alphabetical order (coder sorts before swift, exactly what we don't want)."""
        stub_venv(self.tree, ".venv")
        d = strata_configs(self.tree, "multi",
                           [("coder", os.path.join(self.tree, "tokA"), 8081,
                             os.path.join(self.tree, "sessA")),
                            ("swift", os.path.join(self.tree, "tokB"), 8080,
                             os.path.join(self.tree, "sessB"))])
        body = b'{"status":"ok","model":"swift","context":{"native":131072}}'
        rc, out = self.boot(body=body, extra={"TOKENIZER": "", "STRATA_DIR": d})
        self.assertEqual(rc, 0, out)
        self.assertIn("--tokenizer " + os.path.join(self.tree, "tokB"), out)
        self.assertNotIn("tokA", out)
        self.assertEqual(self.read_config()["slot_dir"], os.path.join(self.tree, "sessB"))

    def test_url_port_picks_tokenizer_and_slot_from_the_same_pack(self):
        """Without 'model' in /v1/status, the URL port identifies the file: tokenizer
        and slot_dir come from the SAME pack, the one whose port matches STRATA_URL."""
        stub_venv(self.tree, ".venv")
        url, srv = serve(200)
        try:
            port = int(url.rsplit(":", 1)[1])
            d = strata_configs(self.tree, "multi",
                               [("coder", os.path.join(self.tree, "tokA"), 8081,
                                 os.path.join(self.tree, "sessA")),
                                ("swift", os.path.join(self.tree, "tokB"), port,
                                 os.path.join(self.tree, "sessB"))])
            rc, out = run(["./run-strata-proxy.sh"], cwd=self.tree,
                          env=self.env(url, {"TOKENIZER": "", "STRATA_DIR": d}))
        finally:
            srv.shutdown()
            srv.server_close()
        self.assertEqual(rc, 0, out)
        self.assertIn("--tokenizer " + os.path.join(self.tree, "tokB"), out)
        self.assertNotIn("tokA", out)
        self.assertEqual(self.read_config()["slot_dir"], os.path.join(self.tree, "sessB"))

    def test_single_file_in_the_folder_keeps_the_behaviour(self):
        """A single JSON in the engine folder: tokenizer and slot_dir from that file,
        exactly as before the identification rule."""
        stub_venv(self.tree, ".venv")
        tok = os.path.join(self.tree, "tok_uno")
        slot = os.path.join(self.tree, "sess_uno")
        os.makedirs(tok, exist_ok=True)
        with open(os.path.join(self.strata, "modelo.json"), "w", encoding="utf-8") as fh:
            json.dump({"model_name": "uno", "port": 8080, "tokenizer": tok,
                       "slot_save_path": slot}, fh)
        rc, out = self.boot(extra={"TOKENIZER": ""})
        self.assertEqual(rc, 0, out)
        self.assertIn("--tokenizer " + tok, out)
        self.assertEqual(self.read_config()["slot_dir"], slot)

    def test_without_slot_save_path_sessions_go_to_the_strata_folder(self):
        """A JSON without slot_save_path: nothing is invented. The slot falls back to
        the $STRATA_DIR/sessions convention and the tokenizer still comes from the same file."""
        stub_venv(self.tree, ".venv")
        tok = os.path.join(self.tree, "tok_noslot")
        os.makedirs(tok, exist_ok=True)
        with open(os.path.join(self.strata, "modelo.json"), "w", encoding="utf-8") as fh:
            json.dump({"model_name": "noslot", "tokenizer": tok}, fh)
        rc, out = self.boot(extra={"TOKENIZER": ""})
        self.assertEqual(rc, 0, out)
        self.assertIn("--tokenizer " + tok, out)
        self.assertEqual(self.read_config()["slot_dir"], os.path.join(self.strata, "sessions"))

    def test_explicit_tokenizer_and_slot_dir_take_precedence(self):
        """TOKENIZER and SLOT_DIR given by the caller win, even with two configs and
        an engine that does not identify itself: the discovery does not overwrite them."""
        stub_venv(self.tree, ".venv")
        d = strata_configs(self.tree, "multi",
                           [("coder", os.path.join(self.tree, "tokA"), 8081,
                             os.path.join(self.tree, "sessA")),
                            ("swift", os.path.join(self.tree, "tokB"), 8080,
                             os.path.join(self.tree, "sessB"))])
        rc, out = self.boot(extra={"TOKENIZER": os.path.join(self.tree, "tok_escolhido"),
                                   "SLOT_DIR": os.path.join(self.tree, "sess_escolhida"),
                                   "STRATA_DIR": d})
        self.assertEqual(rc, 0, out)
        self.assertIn("--tokenizer " + os.path.join(self.tree, "tok_escolhido"), out)
        self.assertNotIn("tokA", out)
        self.assertNotIn("tokB", out)
        self.assertNotIn("sessA", out)
        self.assertNotIn("sessB", out)
        self.assertEqual(self.read_config()["slot_dir"],
                         os.path.join(self.tree, "sess_escolhida"))

    def test_without_python_314_starts_and_creates_config_without_kv(self):
        stub_venv(self.tree, ".venv")                      # python without compression.zstd
        rc, out = self.boot()
        self.assertEqual(rc, 0, out)
        cfg = self.read_config()
        self.assertIs(cfg["kv_archive"], False, "a created config must not ask for kv without support")
        self.assertEqual(cfg["slot_dir"], os.path.join(self.tree, "sessions"))
        self.assertIn("kv_archive=off", out)

    def test_with_support_new_config_is_born_with_kv_off_and_says_how_to_enable(self):
        """kv_archive is opt-in (experimental): even with Python 3.14 available the
        created config is born with kv_archive=false, and the output shows how to enable it."""
        stub_venv(self.tree, ".venv314", kv=True)
        rc, out = self.boot()
        self.assertEqual(rc, 0, out)
        self.assertIs(self.read_config()["kv_archive"], False)
        self.assertIn("kv_archive=off", out)
        self.assertIn("KV_ARCHIVE=1", out)

    def test_kv_opt_in_with_support_creates_config_with_kv(self):
        stub_venv(self.tree, ".venv314", kv=True)
        rc, out = self.boot(extra={"KV_ARCHIVE": "1"})
        self.assertEqual(rc, 0, out)
        self.assertIs(self.read_config()["kv_archive"], True)
        self.assertIn("kv_archive=on", out)

    def test_kv_opt_in_without_support_creates_config_without_kv_and_warns(self):
        """KV_ARCHIVE=1 without Python 3.14: the config is born without kv and the reason is printed."""
        stub_venv(self.tree, ".venv")                        # zstd probe blocked by the stub
        rc, out = self.boot(extra={"KV_ARCHIVE": "1"})
        self.assertEqual(rc, 0, out)
        self.assertIs(self.read_config()["kv_archive"], False)
        self.assertIn("KV_ARCHIVE=1 ignored", out)
        self.assertIn("kv_archive=off", out)

    def test_picks_the_venv_that_has_support(self):
        stub_venv(self.tree, ".venv")
        stub_venv(self.tree, ".venv314", kv=True)
        rc, out = self.boot(extra={"KV_ARCHIVE": "1"})
        self.assertEqual(rc, 0, out)
        self.assertIn("venv=.venv314", out)
        self.assertIn("kv_archive=on", out)

    def test_missing_slot_dir_warns_without_failing(self):
        """The effective slot_dir is printed; if the folder does not exist, a warning
        with the mkdir command (saved state has nowhere to write), but the proxy starts."""
        stub_venv(self.tree, ".venv")
        rc, out = self.boot()
        self.assertEqual(rc, 0, out)
        slot = os.path.join(self.tree, "sessions")
        self.assertIn("slot_dir=%s does not exist" % slot, out)
        self.assertIn('mkdir -p "%s"' % slot, out)
        self.assertIn("slot_dir=" + slot, out)

    def test_present_slot_dir_does_not_warn(self):
        stub_venv(self.tree, ".venv")
        os.makedirs(os.path.join(self.tree, "sessions"))
        rc, out = self.boot()
        self.assertEqual(rc, 0, out)
        self.assertNotIn("does not exist", out)

    def test_personal_config_with_kv_without_support_fails_without_touching_the_config(self):
        stub_venv(self.tree, ".venv")
        self.write_config({"slot_dir": "/my/slot", "kv_archive": True, "response_floor": 16384})
        before = open(self.cfg, "rb").read()
        rc, out = self.boot()
        self.assertNotEqual(rc, 0)
        self.assertIn("compression.zstd", out)
        self.assertIn("was not modified", out)
        self.assertEqual(open(self.cfg, "rb").read(), before, "a personal config must not be modified")
        self.assertEqual(self.read_config()["slot_dir"], "/my/slot")

    def test_personal_config_without_kv_starts_without_314(self):
        stub_venv(self.tree, ".venv")
        self.write_config({"slot_dir": "/my/slot", "kv_archive": False})
        rc, out = self.boot()
        self.assertEqual(rc, 0, out)
        self.assertIn("kv_archive=off", out)
        self.assertIn("--config ./config.json", out)

    def test_personal_config_without_kv_key_is_treated_as_without_kv(self):
        stub_venv(self.tree, ".venv")
        self.write_config({"slot_dir": "/my/slot"})
        rc, out = self.boot()
        self.assertEqual(rc, 0, out)
        self.assertIn("kv_archive=off", out)

    def test_invalid_config_json_fails(self):
        stub_venv(self.tree, ".venv")
        with open(self.cfg, "w", encoding="utf-8") as fh:
            fh.write("{ this is not json")
        rc, out = self.boot()
        self.assertNotEqual(rc, 0)
        self.assertIn("invalid JSON", out)

    def test_engine_http_503_is_an_error(self):
        stub_venv(self.tree, ".venv")
        rc, out = self.boot(code=503)
        self.assertNotEqual(rc, 0)
        self.assertIn("HTTP 503", out)

    def test_http_404_from_another_service_is_an_error(self):
        stub_venv(self.tree, ".venv")
        rc, out = self.boot(code=404)
        self.assertNotEqual(rc, 0)
        self.assertIn("HTTP 404", out)

    def test_refused_connection_is_an_error(self):
        stub_venv(self.tree, ".venv")
        rc, out = self.boot(extra={"STRATA_URL": "http://127.0.0.1:1"})
        self.assertNotEqual(rc, 0)
        self.assertIn("connection", out)

    def test_timeout_is_an_error(self):
        stub_venv(self.tree, ".venv")
        rc, out = self.boot(sleep=3.0, extra={"PROBE_TIMEOUT": "1"})
        self.assertNotEqual(rc, 0)
        self.assertIn("connection", out)

    def test_invalid_timeout_without_curl_does_not_invent_an_http_code(self):
        """Without curl the probe falls back to python: a non-numeric PROBE_TIMEOUT must
        not produce an empty HTTP code, which would be read as 'the port answered'."""
        stub_venv(self.tree, ".venv")
        self.write_config({"slot_dir": "/my/slot", "kv_archive": False})
        nobin = os.path.join(self.tree, "nobin")
        os.makedirs(nobin, exist_ok=True)
        os.symlink(shutil.which("dirname"), os.path.join(nobin, "dirname"))
        rc, out = self.boot(extra={"STRATA_URL": "http://127.0.0.1:1",
                                   "PROBE_TIMEOUT": "abc", "PATH": nobin})
        self.assertNotEqual(rc, 0, out)
        self.assertIn("connection", out)
        self.assertNotIn("answered HTTP", out)

    def test_without_venv_does_not_start(self):
        rc, out = self.boot()
        self.assertNotEqual(rc, 0)


def python_without_pip(path):
    """a python that fails on `-m pip`: what an interrupted venv looks like to setup.sh."""
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("#!/bin/sh\n"
                 "if [ \"$1\" = \"-m\" ] && [ \"$2\" = \"pip\" ]; then exit 1; fi\n"
                 "exec " + sys.executable + " \"$@\"\n")
    os.chmod(path, 0o755)


@unittest.skipIf(os.name == "nt", "bash scripts, Linux only")
class SetupScriptTests(unittest.TestCase):
    """setup.sh: checking without Python 3.14 and watched paths before any rm -rf."""

    def setUp(self):
        self.tree = make_tree()

    def tearDown(self):
        shutil.rmtree(self.tree, ignore_errors=True)

    def real_venv(self, name, with_bin=True):
        subprocess.run([sys.executable, "-m", "venv", name], cwd=self.tree,
                       capture_output=True, text=True, timeout=300, check=True)
        if with_bin:
            stub = os.path.join(self.tree, name, "bin", "mnemonic-proxy")
            with open(stub, "w", encoding="utf-8") as fh:
                fh.write("#!/bin/sh\nprintf 'stub\\n'\n")
            os.chmod(stub, 0o755)
        return os.path.join(self.tree, name)

    def setup(self, args=(), env=None):
        return run(["./setup.sh"] + list(args), cwd=self.tree, env=env or {}, timeout=600)

    def test_posix_and_bash_syntax(self):
        for name in ("setup.sh", "run-strata-proxy.sh"):
            for shell in ("sh", "bash"):
                if shutil.which(shell) is None:
                    continue
                rc, out = run([shell, "-n", name], cwd=self.tree)
                self.assertEqual(rc, 0, "%s -n %s: %s" % (shell, name, out))

    def test_check_passes_without_python_314(self):
        """A valid environment (venv + installed package) passes --check with exit 0,
        even without Python 3.14: the missing kv_archive is opt-in, not a failure."""
        venv = self.real_venv(".venv")
        stub_packet(venv)        # make bin/mnemonic-proxy + the importable package
        hide_zstd(venv)          # independent of the host Python (review point 5)
        rc, out = self.setup(["--check"])
        self.assertEqual(rc, 0, out)
        self.assertIn("kv_archive (zstd)", out)
        self.assertIn(": no", out)

    def test_check_fails_without_venv(self):
        rc, out = self.setup(["--check"])
        self.assertEqual(rc, 1, out)
        self.assertIn("MISSING", out)

# ---- --check is read-only (no removal, no creation, no install) ----------
    def test_check_does_not_remove_an_incomplete_venv(self):
        """A venv whose python has no pip (interrupted creation): --check reports it
        and exits non-zero, leaving the directory exactly as it was — no rm -rf, no
        recreation (that is what a plain ./setup.sh does)."""
        binp = os.path.join(self.tree, ".venv", "bin")
        os.makedirs(binp)
        with open(os.path.join(self.tree, ".venv", "pyvenv.cfg"), "w", encoding="utf-8") as fh:
            fh.write("home = /usr/bin\n")
        marker = os.path.join(self.tree, ".venv", "marker.txt")
        with open(marker, "w", encoding="utf-8") as fh:
            fh.write("untouched")
        python_without_pip(os.path.join(binp, "python"))

        rc, out = self.setup(["--check"])

        self.assertEqual(rc, 1, out)
        self.assertIn("incomplete", out)
        self.assertTrue(os.path.isdir(os.path.join(self.tree, ".venv")),
                        "--check must not remove the venv")
        self.assertTrue(os.path.exists(marker), "--check must not touch the venv contents")
        self.assertTrue(os.path.exists(os.path.join(binp, "python")),
                        "--check must not recreate the venv")

    def test_check_does_not_install_a_missing_package(self):
        """A venv with a working python+pip but without the package: --check reports
        the missing package, exits non-zero and installs nothing — no setup/egg-info
        is created, so the environment is untouched."""
        venv = self.real_venv(".venv", with_bin=False)   # no mnemonic-proxy stub on purpose

        rc, out = self.setup(["--check"])

        self.assertEqual(rc, 1, out)
        self.assertIn("package is not installed", out)
        self.assertFalse(glob.glob(os.path.join(venv, "lib", "python*", "site-packages",
                                                "mnemonic_proxy*")),
                         "--check must not install the package")
        self.assertFalse(os.path.exists(os.path.join(self.tree, "mnemonic_proxy.egg-info")),
                         "--check must not leave install artifacts")

    def test_check_ok_with_a_valid_env_and_kv_available(self):
        """Full positive case: main venv valid with the package installed, and the KV
        venv with compression.zstd. --check exits 0, reports kv_archive (zstd) yes,
        and touches nothing: no file or directory of the two venvs is created,
        removed or modified.

        The KV support is simulated (stub wrapper) so the test does not depend on a
        real Python >= 3.14 being installed."""
        main = self.real_venv(".venv")
        stub_packet(main)                 # bin/mnemonic-proxy + importable ctxproxy package
        hide_zstd(main)                   # force KV to come from .venv314, even on a 3.14 host
        # KV venv: a REAL venv (so the package probe works) marked as installed, with
        # zstd support faked via a .pth startup hook -> setup.sh reuses it (it requires both
        # the package and compression.zstd). No real Python >= 3.14 involved.
        kv = self.real_venv(".venv314")
        stub_packet(kv)
        show_zstd(kv)

        before = snapshot_tree(self.tree)

        rc, out = self.setup(["--check"])

        self.assertEqual(rc, 0, out)
        self.assertIn("package yes", out)                 # main venv has the package
        self.assertIn("kv_archive (zstd)   : yes", out)    # KV venv has compression.zstd
        self.assertIn(" in %s" % os.path.join(self.tree, kv), out)
        self.assertEqual(snapshot_tree(self.tree), before,
                         "--check must not create, remove or modify any file")

    def test_no_kv_does_not_try_uv_and_exits_zero(self):
        venv = self.real_venv(".venv")
        hide_zstd(venv)
        rc, out = self.setup(["--no-sudo", "--no-kv"])
        self.assertEqual(rc, 0, out)
        self.assertIn("kv_archive (zstd)", out)
        self.assertIn(": no", out)
        self.assertNotIn("creating %s" % os.path.join(self.tree, ".venv314"), out)
        self.assertNotIn("building .venv314", out)

    # ---- KV_PYTHON: one path, no word splitting -----------------------------
    # setup.sh runs `./setup.sh --no-kv`? No: it looks for KV_PYTHON only when it
    # must BUILD the KV venv. Each test forces that path with --no-kv absent and a
    # main venv already complete, so the only variable is KV_PYTHON. The chosen
    # interpreter is what appears in the "using ... for .venv314" line.
    def kv_layout(self):
        """A main venv complete (package present) so setup.sh only works on KV."""
        main = self.real_venv(".venv")
        stub_packet(main)
        hide_zstd(main)          # force the KV search, even on a 3.14 host
        return main

    def clean_path(self, *extra):
        """PATH with a python3.14 only where the test puts one, so a host python3.14
        (e.g. ~/.local/bin) cannot mask the behaviour under test. The host interpreter
        is exposed as `python3` (the main-venv step), never as `python3.14`."""
        d = os.path.join(self.tree, "pathbin")
        os.makedirs(d, exist_ok=True)
        for tool in ("sh", "env", "dirname", "basename", "id", "rm", "mkdir", "cp",
                     "sed", "grep", "uname", "tr", "cat", "head", "tail"):
            src = shutil.which(tool)
            if src and not os.path.exists(os.path.join(d, tool)):
                os.symlink(src, os.path.join(d, tool))
        for name in ("python3", "python"):
            if not os.path.exists(os.path.join(d, name)):
                os.symlink(sys.executable, os.path.join(d, name))
        return os.pathsep.join([d] + list(extra))

    def test_valid_kv_python_is_chosen(self):
        """KV_PYTHON pointing at a valid interpreter is used verbatim and wins over
        the automatic search: with no python3.14 in PATH, only KV_PYTHON can match."""
        self.kv_layout()
        py = fake_py314(os.path.join(self.tree, "mypy", "python3.14"), valid=True)
        rc, out = self.setup(["--no-sudo"], {"KV_PYTHON": py, "PATH": self.clean_path()})
        self.assertIn("using %s" % py, out)

    def test_kv_python_with_spaces_in_the_path(self):
        """A path with spaces must be handled as ONE argument: word splitting would
        break it and the interpreter would never be found."""
        self.kv_layout()
        py = fake_py314(os.path.join(self.tree, "dir with space", "python 3.14"), valid=True)
        self.assertIn(" ", py)   # sanity: the fixture really has a space
        rc, out = self.setup(["--no-sudo"], {"KV_PYTHON": py, "PATH": self.clean_path()})
        self.assertIn("using %s" % py, out)

    def test_invalid_kv_python_falls_back_to_the_automatic_search(self):
        """An invalid KV_PYTHON must not be used: the automatic search still runs.
        Here a valid python3.14 sits in PATH and has to be picked instead."""
        self.kv_layout()
        bad = fake_py314(os.path.join(self.tree, "bad", "python3.14"), valid=False)
        good = fake_py314(os.path.join(self.tree, "pathbin", "python3.14"), valid=True)
        rc, out = self.setup(["--no-sudo"],
                             {"KV_PYTHON": bad, "PATH": self.clean_path()})
        self.assertIn("using %s" % good, out)
        self.assertNotIn("using %s" % bad, out)

    def test_empty_kv_python_keeps_the_current_search(self):
        """KV_PYTHON empty: behaviour is exactly as before — the automatic search
        over python3.16 / python3.15 / python3.14 decides."""
        self.kv_layout()
        good = fake_py314(os.path.join(self.tree, "pathbin", "python3.14"), valid=True)
        rc, out = self.setup(["--no-sudo"], {"KV_PYTHON": "", "PATH": self.clean_path()})
        self.assertIn("using %s" % good, out)

    # ---- privileges (review points 1, 2 and 6) -------------------------------
    # A minimal PATH with NO python: the coreutils setup.sh uses before the Python
    # step (dirname, id), plus logging stubs for the package manager and for sudo.
    # This proves WHAT would be run, without actually installing anything.
    def shim_bin(self, pm="apt", root=False):
        d = os.path.join(self.tree, "shimbin")
        os.makedirs(d, exist_ok=True)
        self.shimlog = os.path.join(self.tree, "shim.log")
        for tool in ("dirname",):
            src = shutil.which(tool)
            os.symlink(src, os.path.join(d, tool))
        if root:
            real_id = shutil.which("id")
            with open(os.path.join(d, "id"), "w", encoding="utf-8") as fh:
                fh.write("#!/bin/sh\n"
                         "if [ \"$1\" = \"-u\" ]; then echo 0; else exec %s \"$@\"; fi\n" % real_id)
                os.chmod(os.path.join(d, "id"), 0o755)
        else:
            os.symlink(shutil.which("id"), os.path.join(d, "id"))
        pm_bin = {"apt": "apt-get", "dnf": "dnf", "pacman": "pacman", "zypper": "zypper"}[pm]
        with open(os.path.join(d, pm_bin), "w", encoding="utf-8") as fh:
            fh.write("#!/bin/sh\necho \"%s $*\" >> \"%s\"\n" % (pm_bin, self.shimlog))
            os.chmod(os.path.join(d, pm_bin), 0o755)
        with open(os.path.join(d, "sudo"), "w", encoding="utf-8") as fh:
            fh.write("#!/bin/sh\necho \"sudo $*\" >> \"%s\"\nexec \"$@\"\n" % self.shimlog)
            os.chmod(os.path.join(d, "sudo"), 0o755)
        return d

    def shim_log(self):
        if not os.path.exists(self.shimlog):
            return ""
        with open(self.shimlog, encoding="utf-8") as fh:
            return fh.read()

    def test_without_python_the_default_is_to_print_the_command_and_stop(self):
        """Without Python and without --install-deps: the script prints the command of
        the detected package manager and exits 1 — calling neither sudo nor the
        package manager (review point 6)."""
        d = self.shim_bin(pm="apt")
        rc, out = self.setup(env={"PATH": d})
        self.assertEqual(rc, 1, out)
        self.assertIn("MISSING: Python 3.10+", out)
        self.assertIn("sudo apt-get update && sudo apt-get install -y python3 python3-venv python3-pip", out)
        self.assertIn("--install-deps", out)
        self.assertEqual(self.shim_log(), "", "default mode must not run sudo or the package manager")

    def test_hint_follows_the_detected_package_manager(self):
        """On a Fedora machine (dnf, no apt-get) the hint shows the dnf command,
        not the apt one (review point 2)."""
        d = self.shim_bin(pm="dnf")
        rc, out = self.setup(env={"PATH": d})
        self.assertEqual(rc, 1, out)
        self.assertIn("sudo dnf install -y python3 python3-pip", out)
        self.assertNotIn("apt-get install", out)

    def test_install_deps_runs_via_sudo_when_not_root(self):
        """--install-deps as a non-root user: the package manager runs via sudo (review point 6)."""
        d = self.shim_bin(pm="apt")
        rc, out = self.setup(["--install-deps"], {"PATH": d})
        self.assertEqual(rc, 1, out)   # installs, but still no python -> stops with an explanation
        self.assertIn("still no Python 3.10+", out)
        log = self.shim_log()
        self.assertIn("sudo apt-get update", log)
        self.assertIn("sudo apt-get install -y python3 python3-venv python3-pip", log)

    def test_install_deps_as_root_calls_directly_without_sudo(self):
        """--install-deps as root (containers, WSL): the package manager runs DIRECTLY,
        without sudo — the case that failed on a clean Ubuntu 24.04 (review point 1)."""
        d = self.shim_bin(pm="apt", root=True)
        rc, out = self.setup(["--install-deps"], {"PATH": d})
        self.assertEqual(rc, 1, out)
        log = self.shim_log()
        self.assertNotIn("sudo", log)
        self.assertIn("apt-get update", log)
        self.assertIn("apt-get install -y python3 python3-venv python3-pip", log)

    def test_root_without_sudo_hint_has_no_prefix(self):
        """As root (id -u = 0) the printed command has no sudo prefix: it is the
        command the root user should run."""
        d = self.shim_bin(pm="apt", root=True)
        rc, out = self.setup(env={"PATH": d})
        self.assertEqual(rc, 1, out)
        self.assertIn("  apt-get update && apt-get install -y python3 python3-venv python3-pip", out)
        self.assertNotIn("sudo apt-get", out)



    def test_venv_main_outside_the_project_removes_nothing(self):
        outside = tempfile.mkdtemp(prefix="mp-outside-")
        marker = os.path.join(outside, "marker.txt")
        with open(marker, "w", encoding="utf-8") as fh:
            fh.write("intact")
        rc, out = self.setup(["--no-sudo"], {"VENV_MAIN": outside})
        self.assertEqual(rc, 2, out)
        self.assertIn("unsafe", out)
        self.assertIn("Nothing was removed", out)
        self.assertTrue(os.path.exists(marker), "nothing may be deleted outside the project")
        shutil.rmtree(outside, ignore_errors=True)

    def test_venv_main_with_dotdot_is_refused(self):
        rc, out = self.setup(["--no-sudo"], {"VENV_MAIN": "../outside"})
        self.assertEqual(rc, 2, out)
        self.assertIn("unsafe", out)

    def test_venv_main_project_root_is_refused(self):
        rc, out = self.setup(["--no-sudo"], {"VENV_MAIN": "."})
        self.assertEqual(rc, 2, out)
        self.assertIn("unsafe", out)

    def test_venv_kv_home_path_is_refused(self):
        rc, out = self.setup(["--no-sudo"], {"HOME": self.tree, "VENV_KV": self.tree + "/.venv314"})
        self.assertEqual(rc, 2, out)
        self.assertIn("unsafe", out)

    def test_venv_main_symlink_is_refused(self):
        target = os.path.join(self.tree, "target")
        os.makedirs(target)
        os.symlink(target, os.path.join(self.tree, ".venv"))
        rc, out = self.setup(["--no-sudo"], {"VENV_MAIN": ".venv"})
        self.assertEqual(rc, 2, out)
        self.assertIn("unsafe", out)

    def test_does_not_remove_a_folder_that_is_not_a_venv(self):
        binp = os.path.join(self.tree, ".venv", "bin")
        os.makedirs(binp)
        python_without_pip(os.path.join(binp, "python"))
        marker = os.path.join(self.tree, ".venv", "marker.txt")
        with open(marker, "w", encoding="utf-8") as fh:
            fh.write("intact")
        rc, out = self.setup(["--no-sudo", "--no-kv"])
        self.assertEqual(rc, 2, out)
        self.assertIn("not a venv", out)
        self.assertTrue(os.path.exists(marker), "without pyvenv.cfg nothing is removed")

    def test_venv_in_a_missing_subfolder_is_accepted(self):
        """A legitimate path inside the project whose parent does not exist yet
        (VENV_MAIN=venvs/kv314): the normal case of someone choosing where to
        create the venv, and it must not be read as 'unsafe'."""
        rc, out = self.setup(env={"VENV_MAIN": "venvs/kv314"})
        self.assertNotIn("unsafe", out)
        self.assertNotEqual(rc, 2, out)

    def test_parent_symlink_pointing_outside_removes_nothing(self):
        """shortcut -> a folder outside; shortcut/.venv is a legitimate venv out there.
        rm -rf would follow the symlink: the validation must catch the ancestor,
        not just the leaf."""
        outside = tempfile.mkdtemp(prefix="mp-outside-")
        external = os.path.join(outside, ".venv")
        os.makedirs(os.path.join(external, "bin"))
        with open(os.path.join(external, "pyvenv.cfg"), "w", encoding="utf-8") as fh:
            fh.write("home = %s\n" % os.path.dirname(sys.executable))
        python_without_pip(os.path.join(external, "bin", "python"))
        marker = os.path.join(external, "marker.txt")
        with open(marker, "w", encoding="utf-8") as fh:
            fh.write("intact")
        os.symlink(outside, os.path.join(self.tree, "shortcut"))
        rc, out = self.setup(["--no-sudo", "--no-kv"], {"VENV_MAIN": "shortcut/.venv"})
        self.assertEqual(rc, 2, out)
        self.assertIn("unsafe", out)
        self.assertIn("Nothing was removed", out)
        self.assertTrue(os.path.isdir(external), "the external venv must not be removed")
        self.assertTrue(os.path.exists(marker), "the external content must not be touched")
        shutil.rmtree(outside, ignore_errors=True)

    def test_ancestor_symlink_in_an_absolute_path_is_also_refused(self):
        outside = tempfile.mkdtemp(prefix="mp-outside-")
        external = os.path.join(outside, ".venv")
        os.makedirs(os.path.join(external, "bin"))
        with open(os.path.join(external, "pyvenv.cfg"), "w", encoding="utf-8") as fh:
            fh.write("home = x\n")
        python_without_pip(os.path.join(external, "bin", "python"))
        marker = os.path.join(external, "marker.txt")
        with open(marker, "w", encoding="utf-8") as fh:
            fh.write("intact")
        os.symlink(outside, os.path.join(self.tree, "shortcut"))
        rc, out = self.setup(["--no-sudo", "--no-kv"],
                             {"VENV_MAIN": os.path.join(self.tree, "shortcut", ".venv")})
        self.assertEqual(rc, 2, out)
        self.assertIn("unsafe", out)
        self.assertTrue(os.path.exists(marker), "the external content must not be touched")
        shutil.rmtree(outside, ignore_errors=True)

    def test_venv_in_a_project_subfolder_is_still_accepted(self):
        """The validation must not restrict beyond what the interface documents: a
        legitimate VENV_MAIN in a subfolder must pass (and the setup carries on)."""
        os.makedirs(os.path.join(self.tree, "sub"))
        self.real_venv(os.path.join("sub", ".venv"))
        rc, out = self.setup(["--no-sudo", "--no-kv"], {"VENV_MAIN": "sub/.venv"})
        self.assertNotEqual(rc, 2, out)
        self.assertNotIn("unsafe", out)
        self.assertEqual(rc, 0, out)

    def test_without_home_defined_the_script_does_not_break(self):
        """HOME may be missing (cron, systemd, env -i). With `set -u` the script must
        not die with 'parameter not set', nor exit with the 2 of the safety rejection."""
        env = dict(os.environ)
        env.pop("HOME", None)
        p = subprocess.run(["./setup.sh", "--check"], cwd=self.tree, env=env,
                           capture_output=True, text=True, timeout=300)
        out = p.stdout + p.stderr
        self.assertNotIn("parameter not set", out)
        self.assertNotEqual(p.returncode, 2, out)
        self.assertIn("MISSING", out)
