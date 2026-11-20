"""Dashboard-only HTTP gate. Raw sockets prove pre-body close and keep-alive framing."""
import http.client
from http.server import ThreadingHTTPServer
import socket
import sys
import threading
import unittest
from unittest.mock import Mock, patch
from dashboard import ctxdash


class DashboardGateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.state = ctxdash.State('http://127.0.0.1:1','unused','fake-engine','fake-client')
        cls.forward = Mock(return_value=(200,b'{}'))
        cls.patch = patch.object(ctxdash,'proxy_call',cls.forward); cls.patch.start()
        cls.srv = ThreadingHTTPServer(('127.0.0.1',0),ctxdash.make_handler(cls.state))
        cls.srv.daemon_threads = True
        cls.thread = threading.Thread(target=cls.srv.serve_forever,daemon=True); cls.thread.start()
        cls.port = cls.srv.server_port
        cls.host = '127.0.0.1:'+str(cls.port)

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown(); cls.srv.server_close(); cls.thread.join(); cls.patch.stop()

    def raw_reject(self, headers, status, path='/api/marks', method='POST'):
        # Do not send the advertised body. A rejection must not wait for it.
        before = self.forward.call_count
        with socket.create_connection(('127.0.0.1',self.port),timeout=2) as s:
            s.settimeout(2)
            request = f'{method} {path} HTTP/1.1\r\n'+''.join(k+': '+v+'\r\n' for k,v in headers)+'\r\n'
            s.sendall(request.encode('latin1'))
            data=b''
            try:
                while True:
                    b=s.recv(65536)
                    if not b: break
                    data+=b
            except socket.timeout:
                self.fail('rejected request did not close before reading body')
        self.assertTrue(data.startswith(('HTTP/1.1 '+str(status)+' ').encode()),data[:200])
        self.assertIn(b'Connection: close\r\n',data)
        self.assertEqual(data.count(b'HTTP/1.1 '),1)
        self.assertEqual(self.forward.call_count,before)
        self.assertNotIn(b'fake-client',data)
        self.assertNotIn(b'Access-Control-Allow-Origin',data)

    def base_headers(self):
        return [('Host',self.host),('Content-Type','application/json'),('Content-Length','100')]

    def test_cross_origin_simple_cannot_borrow_held_key(self):
        before=self.forward.call_count
        c=http.client.HTTPConnection('127.0.0.1',self.port,timeout=2)
        try:
            c.request('POST','/api/marks',b'{"conv":"fake","action":"pin","index":0}',
                      {'Content-Type':'text/plain','Origin':'http://localhost:1'})
            r=c.getresponse(); r.read()
            self.assertEqual(self.forward.call_count,before,'dashboard forwarded with its held client key')
            self.assertEqual(r.status,403)
        finally: c.close()

    def test_expect_continue_is_rejected_before_interim_response(self):
        self.raw_reject(self.base_headers()+[('Origin','null'),('Expect','100-continue')],403)

    def test_host_rejected_before_reads_and_posts(self):
        for method,path in [('GET','/'),('GET','/api/state'),('GET','/api/export?conv=x'),('POST','/api/marks')]:
            for values,status in [([],400),([self.host,self.host],400),(['evil.invalid:'+str(self.port)],403),
                                  (['127.0.0.1:1'],403),(['localhost'],403),(['user@'+self.host],403),
                                  ([self.host+'/'],403),([self.host+',evil'],403),(['127.0.0.1:0'],403),
                                  (['127.0.0.1:'+('9'*5000)],403)]:
                with self.subTest(method=method,path=path,host=[v[:70] for v in values]):
                    h=[('Host',v) for v in values]+[('Content-Length','100'),('Content-Type','application/json'),
                        ('X-Forwarded-Host',self.host),('Forwarded','host='+self.host)]
                    self.raw_reject(h,status,path,method)

    def test_mime_origin_and_fetch_matrix(self):
        for field,values,status in [
            ('Content-Type',[],415),('Content-Type',['text/plain'],415),
            ('Content-Type',['application/x-www-form-urlencoded'],415),
            ('Content-Type',['multipart/form-data; boundary=x'],415),
            ('Content-Type',['application/json','application/json'],415),
            ('Content-Type',['application/json, text/plain'],415),
            ('Origin',['null'],403),('Origin',[''],403),('Origin',['not-an-origin'],403),
            ('Origin',['http://'+self.host+','+'http://'+self.host],403),
            ('Origin',['http://localhost:'+('9'*5000)],403),
            ('Origin',['http://'+self.host+'/'],403),
            ('Origin',['https://'+self.host],403),('Origin',['http://localhost:'+str(self.port)],403),
            ('Origin',['http://127.0.0.1:1'],403),('Origin',['http://'+self.host+'?x'],403),
            ('Origin',['http://'+self.host+'#x'],403),('Origin',['http://'+self.host+' http://'+self.host],403),
            ('Origin',['http://'+self.host,'http://'+self.host],403),
            ('Sec-Fetch-Site',['same-site'],403),('Sec-Fetch-Site',['cross-site'],403),
            ('Sec-Fetch-Site',['none'],403),('Sec-Fetch-Site',[''],403),
            ('Sec-Fetch-Site',['same-origin','same-origin'],403)]:
            with self.subTest(field=field,values=values):
                h=[(k,v) for k,v in self.base_headers() if k!=field]+[(field,v) for v in values]
                self.raw_reject(h,status)

    def test_framing_and_unknown_path_rejected_without_read(self):
        for extra in [[('Transfer-Encoding','chunked')],[('Transfer-Encoding','')],
                      [('Content-Length','-1')],[('Content-Length','1x')],
                      [('Content-Length','+1')],[('Content-Length','')],
                      [('Content-Length','1,1')],[('Content-Length','1'),('Content-Length','1')],
                      [('Content-Length','\xb2')],[('Content-Length',str(sys.maxsize+1))],
                      [('Content-Length','9'*5000)]]:
            with self.subTest(extra=[(k,v[:80]) for k,v in extra]):
                self.raw_reject([('Host',self.host),('Content-Type','application/json')]+extra,400)
        self.raw_reject(self.base_headers(),404,'/unknown')

    def test_refused_body_is_never_a_second_request(self):
        before=self.forward.call_count
        body=f'GET /api/marks?conv=x HTTP/1.1\r\nHost: {self.host}\r\n\r\n'
        for header,status,path in [('Host: evil.invalid',403,'/api/marks'),
                                   ('Host: '+self.host,415,'/api/marks'),
                                   ('Host: '+self.host,404,'/unknown')]:
            with self.subTest(status=status), socket.create_connection(('127.0.0.1',self.port),timeout=2) as s:
                s.settimeout(2)
                s.sendall((f'POST {path} HTTP/1.1\r\n{header}\r\nContent-Length: {len(body)}\r\n\r\n'+body).encode())
                data=b''
                try:
                    while b:=s.recv(65536): data+=b
                except ConnectionResetError: pass  # OS may reset after an unread hostile body
                self.assertTrue(data.startswith(f'HTTP/1.1 {status} '.encode()),data)
                self.assertEqual(data.count(b'HTTP/1.1 '),1)
        self.assertEqual(self.forward.call_count,before)

    def test_headless_and_same_origin_json_keepalive(self):
        c=http.client.HTTPConnection('127.0.0.1',self.port,timeout=2)
        self.addCleanup(c.close)
        for extra in ({},{'Origin':'http://'+self.host},{'Sec-Fetch-Site':'same-origin'},
                      {'Origin':'http://'+self.host,'Sec-Fetch-Site':'same-origin'}):
            c.request('POST','/api/marks',b'{}',{'Content-Type':'Application/JSON; charset=utf-8',**extra})
            r=c.getresponse(); self.assertEqual(r.status,200); r.read()
            sock=c.sock
            c.request('GET','/health',headers={'Sec-Fetch-Site':'none','Origin':'null'})
            r=c.getresponse(); self.assertEqual(r.status,200); r.read()
            self.assertIs(c.sock,sock)
        self.assertEqual(self.forward.call_args.kwargs['api_key'],'fake-client')
        # Missing length keeps the historical empty-body {} contract.
        c.putrequest('POST','/api/marks'); c.putheader('Content-Type','application/json'); c.endheaders()
        r=c.getresponse(); self.assertEqual(r.status,200); r.read()
        self.assertEqual(self.forward.call_args.args[1],b'{}')


class AuthorityTests(unittest.TestCase):
    def test_strict_total_loopback_authority(self):
        good={'localhost':('localhost',80),'LOCALHOST:80':('localhost',80),
              '127.0.0.1:1':('127.0.0.1',1),'[::1]':('[::1]',80),
              '[::1]:65535':('[::1]',65535)}
        for value,expected in good.items():
            with self.subTest(value=value): self.assertEqual(ctxdash._loopback_authority(value),expected)
        for value in ['', 'localhost:', 'localhost:0','localhost:080','localhost:65536',
                      'localhost:１２','localhost:+80',' localhost','localhost ', 'localhost\t',
                      'localhost,localhost','localhost/path','localhost?x','localhost#x','x@localhost',
                      'localhost.','127.1','2130706433','::1','[0:0:0:0:0:0:0:1]','[::ffff:127.0.0.1]',
                      '[::1]:','[::1]evil','localhost:'+('9'*100000)]:
            with self.subTest(value=value[:70]): self.assertIsNone(ctxdash._loopback_authority(value))

    def test_body_read_representation_errors_do_not_forward(self):
        from email.message import Message
        from types import SimpleNamespace
        H=ctxdash.make_handler(ctxdash.State('http://127.0.0.1:1','unused'))
        for error in (OverflowError,MemoryError,ValueError):
            h=object.__new__(H);h.headers=Message()
            h.headers['Host']='localhost:8097';h.headers['Content-Type']='application/json'
            h.headers['Content-Length']=str(sys.maxsize)
            h.server=SimpleNamespace(server_address=('127.0.0.1',8097));h.path='/api/marks'
            h.rfile=Mock();h.rfile.read.side_effect=error
            h._send=Mock()
            with patch.object(ctxdash,'proxy_call') as forward:
                h.do_POST();forward.assert_not_called()
                self.assertEqual(h._send.call_args.args[0],400)
                self.assertTrue(h._send.call_args.kwargs['close'])

    def test_implicit_port_80_origin_equivalence(self):
        # Pure handler test: no privileged port or IPv6 listener is required.
        from email.message import Message
        from types import SimpleNamespace
        H=ctxdash.make_handler(None)
        for host,origin in [('LOCALHOST:80','http://localhost'),('localhost','http://LOCALHOST:80'),
                            ('[::1]','http://[::1]:80')]:
            h=object.__new__(H); h.headers=Message(); h.server=SimpleNamespace(server_address=('ignored',80))
            h.headers['Host']=host; h.headers['Origin']=origin; h.headers['Content-Type']='application/json'
            h._reject=Mock()
            authority=h._check_host()
            self.assertIsNotNone(authority)
            self.assertTrue(h._check_mutation(authority))
            h._reject.assert_not_called()
