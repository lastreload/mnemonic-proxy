"""Fail-closed startup contracts; all credentials and paths are synthetic."""
import contextlib
import io
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from ctxproxy import server
from ctxproxy.core import Config
from dashboard import ctxdash


class KeyStartupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_empty_paths_rejected_before_config_directory_or_lock(self):
        for flag in ('--client-key-file', '--api-key-file'):
            with self.subTest(flag=flag), patch.object(server, 'load_config', side_effect=SystemExit('config reached')) as load:
                with self.assertRaises(SystemExit) as err:
                    server.main(['--data', str(self.root/'new'), '--config', str(self.root/'missing'),
                                 '--api-key', 'fake-inline', flag, ''])
                self.assertIn(flag, str(err.exception))
                load.assert_not_called()
                self.assertFalse((self.root/'new').exists())

    def test_check_and_dashboard_empty_paths_fail_without_network_or_data(self):
        cases = [('ctxproxy.check', f) for f in ('--client-key-file','--api-key-file')]
        cases += [('dashboard.ctxdash', f) for f in ('--api-key-file','--proxy-api-key-file')]
        for module, flag in cases:
            with self.subTest(module=module, flag=flag):
                # Stop the unpatched path before any network, directory or bind.
                program = ('from unittest.mock import patch; import '+module+' as m; '
                           'from contextlib import ExitStack; '
                           's=ExitStack(); '
                           + ('s.enter_context(patch.object(m,"run_checks",side_effect=SystemExit("probe reached"))); '
                              if module=='ctxproxy.check' else
                              's.enter_context(patch.object(m.State,"refresh")); '
                              's.enter_context(patch.object(m,"ThreadingHTTPServer",side_effect=SystemExit("bind reached"))); ')
                           + 'm.main('+repr(['--data',str(self.root/'new'),flag,''])+')')
                r = subprocess.run([sys.executable,'-c',program], capture_output=True,timeout=5)
                self.assertNotEqual(r.returncode,0)
                self.assertIn(flag.encode(),r.stderr)
                self.assertNotIn(b'Traceback',r.stderr)
                self.assertNotIn(b'SyntaxError',r.stderr)
                if module=='dashboard.ctxdash': self.assertEqual(r.returncode,2)
                self.assertFalse((self.root/'new').exists())

    def test_utf8_diagnostics_do_not_include_key_bytes(self):
        f = self.root/'bad.key'; f.write_bytes(b'fake-secret-prefix\xfffake-secret-suffix')
        for command, flag in [(['-m','ctxproxy.server'],'--client-key-file'),
                              (['-m','ctxproxy.server','check'],'--api-key-file'),
                              (['dashboard/ctxdash.py'],'--api-key-file'),
                              (['dashboard/ctxdash.py'],'--proxy-api-key-file')]:
            with self.subTest(command=command,flag=flag):
                r = subprocess.run([sys.executable,*command,'--data',str(self.root/'new'),flag,str(f)],
                                   capture_output=True,timeout=5)
                self.assertNotEqual(r.returncode,0)
                self.assertIn(b'invalid UTF-8',r.stderr)
                self.assertIn(flag.encode(),r.stderr)
                self.assertNotIn(b'Traceback',r.stderr)
                self.assertNotIn(b'fake-secret',r.stdout+r.stderr)
                self.assertFalse((self.root/'new').exists())

    def test_main_resolves_once_and_direct_serve_resolves_before_work(self):
        paths=[]
        for name in ('engine','client'):
            p=self.root/(name+'.key'); p.write_text('fake-'+name+'\n'); paths.append(str(p))
        argv=['--data',str(self.root/'data'),'--api-key-file',paths[0],'--client-key-file',paths[1]]
        # Drive the real serve entry but stop before engine/storage; no fake file reader.
        with patch.object(server,'read_key_file',wraps=server.read_key_file) as read, \
             patch.object(server,'TokenCounter',side_effect=RuntimeError('stop before backend')):
            with self.assertRaisesRegex(RuntimeError,'stop before backend'):
                server.main(argv)
            self.assertEqual(read.call_count,2)
        a=server.build_parser().parse_args(['--client-key-file',''])
        with patch.object(server,'TokenCounter',side_effect=SystemExit('work reached')) as counter:
            with self.assertRaises(SystemExit) as err: server.serve(a,{},Config())
            self.assertIn('--client-key-file',str(err.exception))
            counter.assert_not_called()

    def test_absence_trim_precedence_and_paths_are_not_stripped(self):
        a=server.build_parser().parse_args(['--api-key','fake-inline'])
        server.resolve_keys(a)
        self.assertEqual((a.api_key,a.client_key),('fake-inline',''))
        f=self.root/' key with spaces '; f.write_text(' \tfake-file\r\n')
        a=server.build_parser().parse_args(['--api-key','fake-inline','--api-key-file',str(f)])
        server.resolve_keys(a)
        self.assertEqual(a.api_key,'fake-file')

    def test_dashboard_keys_are_explicitly_separate(self):
        a=ctxdash.State('http://127.0.0.1:1',str(self.root),'fake-engine')
        self.assertEqual(a.proxy_key,'')
        b=ctxdash.State('http://127.0.0.1:1',str(self.root),'fake-engine','fake-client')
        self.assertEqual((b.api_key,b.proxy_key),('fake-engine','fake-client'))
        c=ctxdash.State('http://127.0.0.1:1',str(self.root),'fake-same','fake-same')
        self.assertEqual((c.api_key,c.proxy_key),('fake-same','fake-same'))
