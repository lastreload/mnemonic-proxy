"""Level 2 of the README tests: the `ci` blocks are really executed.

Every command line comes from the README itself (the blocks labeled
`<!-- readme-test: ci -->`), not from a copy kept here. Only the parts that cannot be
hard-coded in a document are substituted at run time:

  --upstream  -> the fake engine started by the test (`ctxproxy.fake_engine`)
  --port      -> a free port
  --data      -> a temporary folder
  --config    -> a file written from the README's own recommended configuration block
  --db        -> the archive the demo leaves behind with `--keep`

What is asserted, per the card:
  demo                          -> PASS, exit 0
  check with the fake engine     -> READY  (exit 0 or 2)
  check with no engine at all    -> NOT READY (exit 1) and "start the engine first"
  the proxy in front of the fake -> one Chat Completions request answered HTTP 200
  forget --list                  -> exit 0 on a real archive
  mnemonic-mcp                   -> answers an MCP initialize / tools/list on stdio

Both fake-engine flavors used by CI are covered (llama-server and Strata). No network
beyond localhost; nothing is installed by the test itself.

Set README_RUN=1 (tools/readme_run.sh does) to run this; it is skipped by default so the
normal suite stays fast. MNEMONIC_PROXY_CMD / MNEMONIC_MCP_CMD override the executables,
which is how the wheel job points the test at the installed console scripts.
"""
import importlib.util
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
README = os.path.join(REPO, "README.md")
ENABLED = os.environ.get("README_RUN")
TIMEOUT = float(os.environ.get("README_RUN_TIMEOUT", "180"))


def _tool(name):
    path = os.path.join(REPO, "tools", name + ".py")
    mod_name = "readme_tools_" + name
    spec = importlib.util.spec_from_file_location(mod_name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


blocks_mod = _tool("readme_blocks")
cli = _tool("readme_cli")


def proxy_cmd():
    """How to run `mnemonic-proxy`: the installed console script if there is one."""
    env = os.environ.get("MNEMONIC_PROXY_CMD")
    if env:
        return env.split()
    found = shutil.which("mnemonic-proxy")
    if found:
        return [found]
    return [sys.executable, "-m", "ctxproxy.server"]


def mcp_cmd():
    env = os.environ.get("MNEMONIC_MCP_CMD")
    if env:
        return env.split()
    found = shutil.which("mnemonic-mcp")
    if found:
        return [found]
    return [sys.executable, "-m", "ctxproxy.mcp_server"]


def child_env(extra=None):
    """Environment for a child process.

    PYTHONPATH is added only when this test itself is running against the source tree: in
    the wheel job the package comes from the installed wheel and must not be shadowed.
    """
    import ctxproxy
    e = dict(os.environ)
    e["PYTHONUNBUFFERED"] = "1"
    pkg = os.path.dirname(os.path.dirname(os.path.abspath(ctxproxy.__file__)))
    if os.path.normpath(pkg) == os.path.normpath(REPO):
        e["PYTHONPATH"] = REPO + (os.pathsep + e["PYTHONPATH"] if e.get("PYTHONPATH") else "")
    if extra:
        e.update(extra)
    return e


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def readme_text():
    with open(README, encoding="utf-8") as f:
        return f.read()


def ci_commands():
    """argv lists of our own programs, taken from the `ci` blocks of the README."""
    out = []
    for b in blocks_mod.load(README):
        if b.label != "ci" or b.lang in ("json", "yaml", "toml"):
            continue
        for argv in cli.commands(b.text):
            if cli.program(argv) is not None:
                out.append((b.line, argv))
    return out


def pick(kind, predicate=None):
    """One README command line of a given kind (server / check / demo / forget / mcp)."""
    for line, argv in ci_commands():
        prog = cli.program(argv)
        rest = argv[1:]
        if prog == "server" and rest and rest[0] == "-m":
            rest = rest[2:]
        sub = rest[0] if rest and not rest[0].startswith("-") else ""
        this = "mcp" if prog == "mcp" else (sub or "server")
        if this != kind:
            continue
        if predicate and not predicate(argv):
            continue
        return line, argv
    raise AssertionError("no README `ci` block runs `%s`" % kind)


def substitute(argv, *, upstream=None, port=None, data=None, config=None, db=None, extra=()):
    """Replace in a README command line only what a document cannot know."""
    out = list(argv)
    repl = {"--upstream": upstream, "--port": port and str(port), "--data": data,
            "--config": config, "--db": db}
    i = 0
    while i < len(out):
        if out[i] in repl and repl[out[i]] is not None and i + 1 < len(out):
            out[i + 1] = repl[out[i]]
            i += 2
            continue
        i += 1
    for flag, value in repl.items():
        if value is not None and flag not in out and flag != "--db":
            out += [flag, value]
    return out + list(extra)


def readme_config():
    """The proxy configuration the README recommends, as a dict."""
    for b in blocks_mod.load(README):
        if b.lang == "json" and b.config_kind == "proxy":
            return json.loads(b.text)
    raise AssertionError("the README has no `config: proxy` JSON block")


def http_get(url, timeout=5):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return r.status, r.read().decode("utf-8", "replace")


def wait_http(url, deadline_s=40):
    end = time.time() + deadline_s
    last = None
    while time.time() < end:
        try:
            return http_get(url, timeout=3)
        except Exception as e:                       # noqa: BLE001
            last = e
            time.sleep(0.2)
    raise AssertionError("%s never answered: %r" % (url, last))


class Engine:
    """ctxproxy.fake_engine as a separate process, like a real engine would be."""

    def __init__(self, flavor, workdir):
        self.port = free_port()
        self.url = "http://127.0.0.1:%d" % self.port
        self.slots = os.path.join(workdir, "slots")
        os.makedirs(self.slots, exist_ok=True)
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "ctxproxy.fake_engine", "--port", str(self.port),
             "--flavor", flavor, "--n-ctx", "131072"],
            cwd=workdir, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            env=child_env())

    def wait(self):
        wait_http(self.url + "/health")
        return self

    def stop(self):
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        if self.proc.stdout is not None:
            self.proc.stdout.close()


def run(argv, cwd, timeout=TIMEOUT, env=None):
    return subprocess.run(argv, cwd=cwd, env=child_env(env), capture_output=True, text=True,
                          timeout=timeout)


@unittest.skipUnless(ENABLED, "set README_RUN=1 (tools/readme_run.sh) to execute the README's `ci` blocks")
class ReadmeRunTests(unittest.TestCase):
    flavors = ("llama", "strata")

    def setUp(self):
        self.work = tempfile.mkdtemp(prefix="readme-run-")
        self.addCleanup(shutil.rmtree, self.work, True)
        self.proxy = proxy_cmd()
        self.mcp = mcp_cmd()

    def workdir(self, name):
        d = os.path.join(self.work, name)
        os.makedirs(d, exist_ok=True)
        return d

    def config_file(self, folder, slots):
        cfg = readme_config()
        for key in ("slot_dir",):
            if key in cfg:
                cfg[key] = slots
        path = os.path.join(folder, "config.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=1)
        return path

    # ---------------------------------------------------------------- demo

    def test_every_demo_command_prints_PASS(self):
        """`mnemonic-proxy demo` of the first screen, the Install block and T0."""
        seen = 0
        for line, argv in ci_commands():
            if cli.program(argv) != "server" or "demo" not in argv:
                continue
            seen += 1
            with self.subTest(readme_line=line):
                d = self.workdir("demo-%d" % line)
                p = run(self.proxy + argv[argv.index("demo"):], d)
                self.assertEqual(0, p.returncode, p.stdout + p.stderr)
                self.assertIn("PASS", p.stdout + p.stderr)
        self.assertGreaterEqual(seen, 3, "the README should run the demo on its first screen, in Install and in T0")

    def test_demo_keep_leaves_a_usable_archive(self):
        d = self.workdir("demo-keep")
        keep = os.path.join(d, "kept")
        p = run(self.proxy + ["demo", "--keep", keep], d)
        self.assertEqual(0, p.returncode, p.stdout + p.stderr)
        self.assertTrue(os.path.exists(os.path.join(keep, "archive.sqlite")), os.listdir(keep))

    # ---------------------------------------------------------------- check

    def test_check_is_READY_against_the_fake_engine(self):
        line, argv = pick("check")
        for flavor in self.flavors:
            with self.subTest(flavor=flavor, readme_line=line):
                d = self.workdir("check-%s" % flavor)
                eng = Engine(flavor, d)
                self.addCleanup(eng.stop)
                eng.wait()
                cfg = self.config_file(d, eng.slots)
                cmd = substitute(argv, upstream=eng.url, port=str(free_port()),
                                 data=os.path.join(d, "data"), config=cfg)
                p = run(self.proxy + cmd[1:], d)
                text = p.stdout + p.stderr
                self.assertIn("READY", text, text)
                self.assertNotIn("NOT READY", text, text)
                self.assertIn(p.returncode, (0, 2), "%d\n%s" % (p.returncode, text))

    def test_check_says_NOT_READY_and_how_to_fix_it_without_an_engine(self):
        line, argv = pick("check")
        d = self.workdir("check-no-engine")
        cmd = substitute(argv, upstream="http://127.0.0.1:%d" % free_port(),
                         port=str(free_port()), data=os.path.join(d, "data"),
                         config=self.config_file(d, os.path.join(d, "slots")))
        p = run(self.proxy + cmd[1:], d)
        text = p.stdout + p.stderr
        self.assertEqual(1, p.returncode, text)
        self.assertIn("NOT READY", text)
        self.assertIn("start the engine first", text)

    def test_check_json_is_machine_readable(self):
        d = self.workdir("check-json")
        eng = Engine("llama", d)
        self.addCleanup(eng.stop)
        eng.wait()
        p = run(self.proxy + ["check", "--json", "--upstream", eng.url,
                              "--data", os.path.join(d, "data"),
                              "--config", self.config_file(d, eng.slots)], d)
        payload = json.loads(p.stdout)
        self.assertTrue(payload["ready"], payload)
        self.assertIn(payload["exit_code"], (0, 2))
        self.assertTrue(payload["checks"])

    # ---------------------------------------------------------------- the proxy itself

    def test_the_proxy_answers_a_chat_completions_request(self):
        line, argv = pick("server")
        for flavor in self.flavors:
            with self.subTest(flavor=flavor, readme_line=line):
                d = self.workdir("serve-%s" % flavor)
                eng = Engine(flavor, d)
                self.addCleanup(eng.stop)
                eng.wait()
                port = free_port()
                cmd = substitute(argv, upstream=eng.url, port=str(port),
                                 data=os.path.join(d, "data"),
                                 config=self.config_file(d, eng.slots))
                proc = subprocess.Popen(self.proxy + cmd[1:], cwd=d, text=True,
                                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                        env=child_env())
                self.addCleanup(lambda p=proc: p.poll() is None and p.kill())
                base = "http://127.0.0.1:%d" % port
                try:
                    status, _ = wait_http(base + "/health")
                    self.assertEqual(200, status)
                    body = json.dumps({"model": "local-model", "max_tokens": 64, "messages": [
                        {"role": "user", "content": "say hello"}]}).encode()
                    req = urllib.request.Request(base + "/v1/chat/completions", data=body,
                                                 headers={"Content-Type": "application/json"})
                    with urllib.request.urlopen(req, timeout=60) as r:
                        self.assertEqual(200, r.status)
                        answer = json.loads(r.read())
                    self.assertTrue(answer["choices"][0]["message"], answer)
                    status, engine = http_get(base + "/v1/engine")
                    self.assertEqual(200, status)
                    self.assertIn(json.loads(engine)["kind"], ("llama.cpp", "strata"))
                finally:
                    proc.terminate()
                    try:
                        proc.wait(timeout=15)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                    if proc.stdout is not None:
                        proc.stdout.close()

    # ---------------------------------------------------------------- forget, mcp

    def test_forget_list_runs_on_the_archive_the_demo_left(self):
        line, argv = pick("forget", lambda a: "--list" in a)
        d = self.workdir("forget")
        data = os.path.join(d, "data")
        self.assertEqual(0, run(self.proxy + ["demo", "--keep", data], d).returncode)
        p = run(self.proxy + substitute(argv, data=data)[1:], d)
        self.assertEqual(0, p.returncode, p.stdout + p.stderr)
        self.assertIn("slot_dir", p.stdout)

    def test_mcp_server_answers_initialize_and_lists_its_tools(self):
        line, argv = pick("mcp", lambda a: "--http" not in a)
        d = self.workdir("mcp")
        data = os.path.join(d, "data")
        self.assertEqual(0, run(self.proxy + ["demo", "--keep", data], d).returncode)
        cmd = self.mcp + substitute(argv, db=os.path.join(data, "archive.sqlite"))[1:]
        requests = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                        "clientInfo": {"name": "readme-test", "version": "1"}}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        ]
        stdin = "".join(json.dumps(r) + "\n" for r in requests)
        p = subprocess.run(cmd, cwd=d, input=stdin, capture_output=True, text=True, timeout=60,
                           env=child_env())
        lines = [json.loads(line) for line in p.stdout.splitlines() if line.strip().startswith("{")]
        self.assertTrue(lines, p.stdout + p.stderr)
        tools = [t["name"] for r in lines for t in r.get("result", {}).get("tools", [])]
        for name in ("recall", "conversations", "journal"):
            self.assertIn(name, tools, "%r\n%s" % (tools, p.stderr))

    # ---------------------------------------------------------------- the harness itself

    def test_the_readme_has_ci_blocks_to_run(self):
        self.assertGreaterEqual(len(ci_commands()), 6)

    def test_substitution_only_touches_values(self):
        argv = ["mnemonic-proxy", "check", "--upstream", "http://127.0.0.1:8095", "--data", "./data"]
        out = substitute(argv, upstream="http://127.0.0.1:1", data="/tmp/d", port=42, config="/tmp/c.json")
        self.assertEqual(out[:2], ["mnemonic-proxy", "check"])
        self.assertEqual("http://127.0.0.1:1", out[out.index("--upstream") + 1])
        self.assertEqual("/tmp/d", out[out.index("--data") + 1])
        self.assertEqual("42", out[out.index("--port") + 1])
        self.assertEqual("/tmp/c.json", out[out.index("--config") + 1])


if __name__ == "__main__":
    unittest.main()
