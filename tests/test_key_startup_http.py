"""Real CLI startup failure matrix against a loopback fake engine, never production."""
import contextlib
import io
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from ctxproxy import server
from ctxproxy.datalock import DataLock
from ctxproxy.fake_engine import FakeEngine
from dashboard import ctxdash


class KeyStartupHTTPTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.engine=FakeEngine(flavor='strata'); self.url=self.engine.start()
        self.addCleanup(self.engine.stop)
        self.calls=[]
        orig=self.engine.srv.RequestHandlerClass
        calls=self.calls
        class Record(orig):
            def do_GET(self): calls.append(self.path); return super().do_GET()
            def do_POST(self): calls.append(self.path); return super().do_POST()
        self.engine.srv.RequestHandlerClass=Record

    def _assert_invalid_path_before_side_effects(self, f, kind):
        commands=[(['-m','ctxproxy.server'],flag) for flag in ('--api-key-file','--client-key-file')]
        commands += [(['-m','ctxproxy.server','check','--json'],flag) for flag in ('--api-key-file','--client-key-file')]
        commands += [(['dashboard/ctxdash.py'],flag) for flag in ('--api-key-file','--proxy-api-key-file')]
        for command,flag in commands:
            with self.subTest(kind=kind,command=command,flag=flag):
                with socket.socket() as sock:
                    sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
                data=self.root/'new-data'
                options=['--data',str(data),'--port',str(port)]
                options+=['--strata',self.url,'--proxy',self.url] if command==['dashboard/ctxdash.py'] else ['--upstream',self.url,'--api-key','fake-inline']
                r=subprocess.run([sys.executable,*command,*options,flag,str(f)],capture_output=True,timeout=5)
                self.assertNotEqual(r.returncode,0)
                self.assertIn(flag.encode(),r.stderr)
                self.assertNotIn(b'Traceback',r.stderr)
                self.assertNotIn(b'fake-secret',r.stdout+r.stderr)
                if kind=='utf8':self.assertIn(b'invalid UTF-8',r.stderr)
                self.assertEqual(self.calls,[])
                self.assertFalse(data.exists())
                with socket.socket() as sock:self.assertNotEqual(sock.connect_ex(('127.0.0.1',port)),0)

    def test_all_invalid_files_real_cli_before_bind_network_or_data(self):
        values={'missing':None,'empty':b'','space':b' \r\n\t','utf8':b'fake-secret-prefix\xff','directory':None}
        for kind,contents in values.items():
            f=self.root/(kind+'.key')
            if kind=='directory':f.mkdir()
            elif contents is not None:f.write_bytes(contents)
            self._assert_invalid_path_before_side_effects(f,kind)

    @unittest.skipUnless(os.name=='posix','chmod mode bits do not deny file reads on Windows; directory rejection is tested there')
    def test_posix_unreadable_file_real_cli_before_side_effects(self):
        if os.geteuid()==0:self.skipTest('requires an unprivileged process for a real POSIX read denial')
        f=self.root/'denied.key';f.write_bytes(b'fake-denied');f.chmod(0)
        try:
            # Validate the fixture itself: Windows chmod(0) does not establish this precondition.
            with self.assertRaises(PermissionError):f.read_bytes()
            self._assert_invalid_path_before_side_effects(f,'denied')
        finally:f.chmod(0o600)

    def test_explicit_empty_paths_real_cli(self):
        for command,flag in [(['-m','ctxproxy.server'],'--client-key-file'),
                              (['-m','ctxproxy.server'],'--api-key-file'),
                              (['-m','ctxproxy.server','check'],'--client-key-file'),
                              (['-m','ctxproxy.server','check'],'--api-key-file'),
                              (['dashboard/ctxdash.py'],'--api-key-file'),
                              (['dashboard/ctxdash.py'],'--proxy-api-key-file')]:
            with self.subTest(command=command,flag=flag):
                data=self.root/'new'
                opts=['--strata',self.url,'--proxy',self.url] if command==['dashboard/ctxdash.py'] else ['--upstream',self.url,'--api-key','fake-inline']
                r=subprocess.run([sys.executable,*command,*opts,'--data',str(data),flag,''],capture_output=True,timeout=5)
                self.assertNotEqual(r.returncode,0);self.assertIn(flag.encode(),r.stderr)
                self.assertFalse(data.exists());self.assertEqual(self.calls,[])

    def test_invalid_path_precedes_existing_busy_lock(self):
        data=self.root/'busy';data.mkdir()
        with DataLock(str(data)):
            with self.assertRaises(SystemExit) as err:
                server.main(['--data',str(data),'--upstream',self.url,'--client-key-file',''])
            self.assertIn('--client-key-file',str(err.exception));self.assertEqual(self.calls,[])

    def test_dashboard_file_keys_on_real_http(self):
        from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
        import http.client
        import threading
        seen=[]
        class Backend(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_GET(self):
                seen.append((self.path,self.headers.get('Authorization')))
                body=b'{}';self.send_response(200);self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
        backend=ThreadingHTTPServer(('127.0.0.1',0),Backend)
        thread=threading.Thread(target=backend.serve_forever,daemon=True);thread.start()
        url='http://127.0.0.1:'+str(backend.server_port)
        try:
            for proxy_key in ('','fake-client','fake-engine'):
                state=ctxdash.State(url,str(self.root),'fake-engine',proxy_key);state.proxy=url
                dash=ThreadingHTTPServer(('127.0.0.1',0),ctxdash.make_handler(state))
                dt=threading.Thread(target=dash.serve_forever,daemon=True);dt.start()
                c=http.client.HTTPConnection('127.0.0.1',dash.server_port,timeout=2)
                try:
                    seen.clear()
                    for path in ('/api/state','/api/marks?conv=synthetic'):
                        c.request('GET',path);r=c.getresponse();self.assertEqual(r.status,200);r.read()
                    self.assertEqual(seen,[('/status','Bearer fake-engine'),('/metrics','Bearer fake-engine'),
                                           ('/v1/strata/marks/synthetic','Bearer '+proxy_key if proxy_key else None)])
                finally:c.close();dash.shutdown();dash.server_close();dt.join()
        finally:backend.shutdown();backend.server_close();thread.join()

    def test_dashboard_cli_explicit_separate_and_same_file(self):
        engine=self.root/'engine.key';engine.write_text('fake-engine\n')
        client=self.root/'client.key';client.write_text('fake-client\n')
        for extra,expected in [([],('fake-engine','')),
                               (['--proxy-api-key-file',str(client)],('fake-engine','fake-client')),
                               (['--proxy-api-key-file',str(engine)],('fake-engine','fake-engine'))]:
            with self.subTest(extra=extra), patch.object(ctxdash,'State',wraps=ctxdash.State) as state:
                # Avoid refreshing or binding, but exercise the actual CLI file readers and State constructor.
                with patch.object(ctxdash.JournalTail,'poll'), patch.object(ctxdash.VirtualEstimator,'poll'), \
                     patch.object(ctxdash,'ThreadingHTTPServer'), contextlib.redirect_stdout(io.StringIO()):
                    ctxdash.main(['--data',str(self.root),'--strata',self.url,'--proxy',self.url,
                                  '--api-key-file',str(engine),*extra])
                self.assertEqual(state.call_args.args[2:],expected)
