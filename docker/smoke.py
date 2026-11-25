"""Exercise the built image on an isolated Linux CI runner; no model or real key.

Run after `docker build -t mnemonic-proxy:ci .` with the package installed.
Raw inspect/top/logs and the reconciled result are saved even on failure.
"""
import hashlib
import http.client
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from contextlib import contextmanager
from ctxproxy.fake_engine import FakeEngine


@contextmanager
def fake_engine():
    engine = FakeEngine(flavor='llama', n_ctx=32768)
    engine.start()
    try:
        yield engine
    finally:
        engine.stop()


OUT = Path(os.environ.get('SMOKE_OUTPUT', 'docker-evidence'))
OUT.mkdir(parents=True, exist_ok=True)
IMAGE = os.environ.get('SMOKE_IMAGE', 'mnemonic-proxy:ci')
PREFIX = 'mnemonic-ci-' + uuid.uuid4().hex[:10]
results = []
containers = []


def docker(*args, check=True):
    result = subprocess.run(['docker', *args], text=True, capture_output=True, timeout=45)
    if check and result.returncode:
        raise RuntimeError(f'docker {args}: {result.stderr}')
    return result.stdout.strip()


def inspect(name):
    return json.loads(docker('inspect', name))[0]


def request_http(port, path, key='', body=None):
    headers = {'Authorization': 'Bearer ' + key} if key else {}
    if body is not None:
        body = json.dumps(body).encode()
        headers['Content-Type'] = 'application/json'
    request = urllib.request.Request(f'http://127.0.0.1:{port}{path}', data=body, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=4) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()


def until(predicate, timeout=25):
    end = time.monotonic() + timeout
    last = None
    while time.monotonic() < end:
        try:
            last = predicate()
            if last:
                return last
        except (OSError, urllib.error.URLError):
            pass
        time.sleep(.1)
    raise AssertionError(f'condition not reached in {timeout}s; last={last!r}')


def port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def save(name, label):
    (OUT / f'{name}-{label}-inspect.json').write_text(json.dumps(inspect(name), indent=2))
    log = subprocess.run(['docker', 'logs', name], text=True, capture_output=True, timeout=15)
    (OUT / f'{name}-{label}-logs.txt').write_text(log.stdout + log.stderr)


def start(label, upstream, data, secrets=None, default_port=False):
    name = PREFIX + '-' + label
    containers.append(name)
    proxy_port, dash_port = port(), 8097 if default_port else port()
    args = ['run', '-d', '--name', name, '--network=host', '--memory=512m', '--cpus=2',
            '--health-interval=1s', '--health-start-period=0s', '--health-retries=3',
            '-e', f'UPSTREAM=http://127.0.0.1:{upstream}',
            '-e', 'PROXY_HOST=127.0.0.1', '-e', f'PROXY_PORT={proxy_port}',
            '--mount', f'type=bind,src={data},dst=/data']
    if not default_port:
        args += ['-e', f'DASH_PORT={dash_port}']
    if secrets:
        for kind in ('engine', 'client'):
            env = 'API_KEY_FILE' if kind == 'engine' else 'CLIENT_KEY_FILE'
            args += ['--mount', f'type=bind,src={secrets / kind},dst=/keys/{kind},readonly',
                     '-e', f'{env}=/keys/{kind}']
    docker(*args, IMAGE)
    key = 'ci-client-key' if secrets else ''
    until(lambda: request_http(proxy_port, '/health', key)[0] == 200)
    until(lambda: request_http(dash_port, '/health')[0] == 200)
    until(lambda: inspect(name)['State']['Health']['Status'] == 'healthy')
    snapshot = docker('top', name, '-eo', 'pid,ppid,args')
    (OUT / f'{name}-running-top.txt').write_text(snapshot)
    pids = [int(line.split()[0]) for line in snapshot.splitlines()[1:]]
    code = '''import pathlib,json
out = {}
for p in pathlib.Path('/proc').iterdir():
    if p.name.isdigit():
        try:
            out[p.name] = (p/'cmdline').read_bytes().decode().split('\\0')
        except FileNotFoundError:
            pass
print(json.dumps(out))
'''
    proc = json.loads(docker('exec', name, 'python', '-c', code))
    assert any('/entrypoint.sh' in word for word in proc['1']), proc
    assert all(k not in snapshot for k in ('ci-client-key', 'ci-engine-key')), snapshot
    if secrets:
        assert all(not mount['RW'] for mount in inspect(name)['Mounts'] if mount['Destination'].startswith('/keys/'))
    save(name, 'running')
    return name, proxy_port, dash_port, pids, proc


def finish(name, proxy_port, dash_port, pids, action='stop', proc=None):
    start_time = time.monotonic()
    if action == 'stop':
        docker('stop', '--time', '8', name)
    else:
        needle = 'ctxproxy.server' if action == 'proxy' else 'dashboard/ctxdash.py'
        assert proc is not None
        target = [int(pid) for pid, argv in proc.items() if needle in argv]
        assert len(target) == 1, (needle, proc)
        docker('exec', name, 'python', '-c', f'import os,signal; os.kill({target[0]},signal.SIGTERM)')
    until(lambda: not inspect(name)['State']['Running'], timeout=6)
    elapsed = time.monotonic() - start_time
    state = inspect(name)['State']
    assert not state['OOMKilled'] and state['ExitCode'] == 143, state
    assert elapsed < 6, elapsed  # less than Docker's 8s SIGKILL fallback
    until(lambda: all(not Path(f'/proc/{pid}').exists() for pid in pids), timeout=3)
    assert not docker('top', name, check=False)
    for listener in (proxy_port, dash_port):
        with socket.socket() as sock:
            assert sock.connect_ex(('127.0.0.1', listener)) != 0, listener
    save(name, action + '-finished')
    return {'action': action, 'exit_code': state['ExitCode'], 'elapsed_seconds': elapsed,
            'host_pids_gone': pids, 'ports_closed': [proxy_port, dash_port]}


def main():
    with tempfile.TemporaryDirectory(prefix=PREFIX) as td:
        root = Path(td)
        secrets = root / 'keys'
        secrets.mkdir()
        for kind in ('engine', 'client'):
            (secrets / kind).write_text(f'ci-{kind}-key')
            (secrets / kind).chmod(0o600)
        hashes = {f.name: hashlib.sha256(f.read_bytes()).hexdigest() for f in secrets.iterdir()}
        with fake_engine() as engine:
            class Gate(BaseHTTPRequestHandler):
                required_key = ''
                unavailable = False
                seen = []
                def relay(self):
                    if self.unavailable:
                        self.send_error(503)
                        return
                    key = self.headers.get('Authorization', '')
                    if key != ('Bearer ' + self.required_key if self.required_key else ''):
                        self.send_error(401)
                        return
                    self.seen.append((self.command, self.path, key))
                    body = self.rfile.read(int(self.headers.get('Content-Length', '0')))
                    assert engine.srv is not None
                    connection = http.client.HTTPConnection('127.0.0.1', engine.srv.server_address[1], timeout=5)
                    try:
                        connection.request(self.command, self.path, body=body, headers={'Content-Type': 'application/json'})
                        response = connection.getresponse()
                        payload = response.read()
                        self.send_response(response.status)
                        self.send_header('Content-Type', response.getheader('Content-Type', 'application/json'))
                        self.send_header('Content-Length', str(len(payload)))
                        self.end_headers()
                        self.wfile.write(payload)
                    finally:
                        connection.close()
                do_GET = do_POST = relay
                def log_message(self, format, *args):
                    pass
            gate = ThreadingHTTPServer(('127.0.0.1', 0), Gate)
            thread = threading.Thread(target=gate.serve_forever, daemon=True)
            thread.start()
            try:
                # No keys: local use stays optional. Docker's default DASH_PORT is exercised.
                data = root / 'noauth'
                data.mkdir()
                name, pp, dp, pids, proc = start('noauth', gate.server_port, data, default_port=True)
                status, reply = request_http(pp, '/v1/chat/completions', body={
                    'model': 'local-model', 'messages': [{'role': 'user', 'content': 'hello-noauth'}]})
                assert status == 200 and json.loads(reply)['choices'][0]['message']['content'] == 'ok', (status, reply)
                results.append({'case': 'default-port-noauth-chat', 'pass': True,
                                **finish(name, pp, dp, pids)})
                Gate.required_key = 'ci-engine-key'
                for action in ('stop', 'proxy', 'dashboard'):
                    data = root / action
                    data.mkdir()
                    name, pp, dp, pids, proc = start(action, gate.server_port, data, secrets)
                    assert request_http(pp, '/health')[0] == 401
                    assert request_http(pp, '/health', 'ci-engine-key')[0] == 401
                    assert request_http(dp, '/api/marks?conv=smoke')[0] == 200
                    marker = 'PERSIST-' + action
                    body = {'model': 'local-model', 'messages': [{'role': 'user', 'content': marker}]}
                    status, reply = request_http(pp, '/v1/chat/completions', 'ci-client-key', body)
                    assert status == 200 and json.loads(reply)['choices'][0]['message']['content'] == 'ok', (status, reply)
                    status, stream = request_http(pp, '/v1/chat/completions', 'ci-client-key', dict(body, stream=True))
                    assert status == 200 and b'data: [DONE]' in stream, (status, stream)
                    assert any(path == '/v1/chat/completions' and key == 'Bearer ci-engine-key' for _, path, key in Gate.seen)
                    if action == 'stop':
                        Gate.unavailable = True
                        try:
                            assert request_http(pp, '/health', 'ci-client-key')[0] == 503
                            assert request_http(dp, '/health')[0] == 200
                        finally:
                            Gate.unavailable = False
                    end = finish(name, pp, dp, pids, action, proc)
                    if action == 'stop':
                        journal = (data / 'journal.jsonl').read_bytes()
                        archive = data / 'archive.sqlite'
                        assert archive.is_file() and journal
                        # Same data, same container, real restart, not a second dummy instance.
                        docker('start', name)
                        until(lambda: request_http(pp, '/health', 'ci-client-key')[0] == 200)
                        import sqlite3
                        with sqlite3.connect(archive) as connection:
                            dump = '\n'.join(connection.iterdump())
                        assert marker in dump, 'previous conversation missing after restart'
                        assert (data / 'journal.jsonl').read_bytes().startswith(journal)
                        snapshot = docker('top', name, '-eo', 'pid,ppid,args')
                        new_pids = [int(line.split()[0]) for line in snapshot.splitlines()[1:]]
                        finish(name, pp, dp, new_pids)
                    results.append({'case': 'readonly-separate-keys-custom-port-' + action,
                                    'pass': True, **end})
                assert {f.name: hashlib.sha256(f.read_bytes()).hexdigest() for f in secrets.iterdir()} == hashes
            finally:
                gate.shutdown()
                gate.server_close()
                thread.join()
                for name in containers:
                    save(name, 'final')
                    docker('rm', '-f', name, check=False)
    assert len(results) == 4


if __name__ == '__main__':
    success = False
    try:
        main()
        success = True
    finally:
        # Also clean up containers on setup failure before the main finally block.
        for name in containers:
            docker('rm', '-f', name, check=False)
        (OUT / 'result.json').write_text(json.dumps({'success': success, 'cases': results,
                                                  'image': IMAGE}, indent=2))
    print(json.dumps({'success': success, 'cases': results}, indent=2))
