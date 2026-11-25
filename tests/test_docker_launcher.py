"""Fast host-shell regressions; Docker/PID1 qualification lives in docker/smoke.py."""
import ctypes
import http.server
import json
import os
import re
from pathlib import Path
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(sys.platform == 'linux' and shutil.which('bash'), 'Linux Bash lifecycle fixture')
class LauncherTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.libc = ctypes.CDLL(None)
        previous = ctypes.c_int()
        assert cls.libc.prctl(37, ctypes.byref(previous), 0, 0, 0) == 0
        cls.previous = previous.value
        assert cls.libc.prctl(36, 1, 0, 0, 0) == 0

    @classmethod
    def tearDownClass(cls):
        cls.libc.prctl(36, cls.previous, 0, 0, 0)

    def run_launcher(self, action, expected, boundary=''):
        with tempfile.TemporaryDirectory() as td:
            work = Path(td)
            binpath = work / 'bin'
            binpath.mkdir()
            stub = binpath / 'python'
            stub.write_text('#!' + sys.executable + '\n' + '''import json, os, sys, time
from pathlib import Path
kind = 'proxy' if 'ctxproxy.server' in sys.argv else 'dashboard'
p = Path(os.environ['FIXTURE'])
(p / (kind + '.json')).write_text(json.dumps({'pid': os.getpid(), 'ppid': os.getppid()}))
while not (p / (kind + '.exit')).exists(): time.sleep(.01)
sys.exit(int((p / (kind + '.exit')).read_text()))
''')
            stub.chmod(0o755)
            env = dict(os.environ, PATH=str(binpath) + ':' + os.environ['PATH'],
                       DATA=str(work / 'data'), FIXTURE=td, API_KEY='', CLIENT_KEY='',
                       UPSTREAM='http://127.0.0.1:1', PROXY_HOST='127.0.0.1', PROXY_PORT='1',
                       DASH_HOST='127.0.0.1', DASH_PORT='2', PROXY_ARGS='')
            for k in ('API_KEY_FILE', 'CLIENT_KEY_FILE', 'BASH_ENV'):
                env.pop(k, None)
            if boundary:
                # Deterministically interrupt between spawning a child and recording $!.
                hook = work / 'hook.sh'
                hook.write_text('trap ' + shlex.quote('if [[ $BASH_COMMAND == ' + shlex.quote(boundary) + ' ]]; then trap - DEBUG; kill -TERM $$; fi') + ' DEBUG\n')
                env['BASH_ENV'] = str(hook)
            p = subprocess.Popen(['bash', str(ROOT / 'docker/entrypoint.sh')],
                                 env=env, cwd=ROOT, start_new_session=True,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            children = []
            try:
                if not boundary:
                    end = time.monotonic() + 3
                    while time.monotonic() < end and not all((work / (k + '.json')).exists() for k in ('proxy', 'dashboard')):
                        time.sleep(.01)
                    self.assertTrue(all((work / (k + '.json')).exists() for k in ('proxy', 'dashboard')))
                    if action in ('proxy', 'dashboard'):
                        (work / (action + '.exit')).write_text(str(expected))
                    else:
                        os.kill(p.pid, getattr(signal, action))
                p.wait(timeout=3)
                time.sleep(.08)
                children = [json.loads(f.read_text())['pid'] for f in work.glob('*.json')]
                self.assertEqual(p.returncode, expected)
                self.assertFalse([pid for pid in children if Path('/proc/' + str(pid)).exists()],
                                 'launcher left live or unreaped fixture children')
            finally:
                try:
                    os.killpg(p.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                p.communicate(timeout=3)
                children = [json.loads(f.read_text())['pid'] for f in work.glob('*.json')]
                for pid in children:
                    try:
                        os.waitpid(pid, 0)
                    except ChildProcessError:
                        pass

    def test_term_cleans_and_reaps_both(self):
        self.run_launcher('SIGTERM', 143)

    def test_int_cleans_and_reaps_both(self):
        self.run_launcher('SIGINT', 130)

    def test_proxy_exit_status_is_preserved(self):
        self.run_launcher('proxy', 7)

    def test_dashboard_exit_status_is_preserved(self):
        self.run_launcher('dashboard', 9)

    def test_clean_child_exit_is_preserved(self):
        self.run_launcher('proxy', 0)

    def test_term_before_first_child(self):
        self.run_launcher('', 143, '"${proxy[@]}"')

    def test_term_before_proxy_pid_assignment(self):
        self.run_launcher('', 143, 'proxy_pid=$!')

    def test_term_before_dashboard_pid_assignment(self):
        self.run_launcher('', 143, 'dash_pid=$!')


class HealthcheckTests(unittest.TestCase):
    def health(self, status):
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(status)
                self.end_headers()
            def log_message(self, format, *args):
                pass
        server = http.server.HTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            text = (ROOT / 'Dockerfile').read_text().replace('\\\n', '')
            cmd = re.search(r'\bCMD (.+)', text).group(1)
            code = shlex.split(cmd)[2]
            # Never contact the default host port during a regression RED.
            guard = "import urllib.request, os; real = urllib.request.urlopen\ndef checked(url, **kw):\n assert url == 'http://127.0.0.1:' + os.environ['DASH_PORT'] + '/health', url\n return real(url, **kw)\nurllib.request.urlopen = checked\n"
            return subprocess.run([sys.executable, '-c', guard + code],
                                  env=dict(os.environ, DASH_PORT=str(server.server_port)),
                                  capture_output=True, text=True)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_custom_port_is_used(self):
        result = self.health(200)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_http_failure_is_nonzero(self):
        result = self.health(503)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('503', result.stderr)


if __name__ == '__main__':
    unittest.main()
