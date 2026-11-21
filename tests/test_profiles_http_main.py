"""Public authenticated HTTP matrix on the real handlers; upstream is a CPU FakeEngine."""
import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from ctxproxy.server import make_handler
from tests.test_model_profiles import ProfileBase, managed_requests


class ProfileHTTPCurrentMainTests(ProfileBase):
    def listener(self):
        server = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(self.proxy, 'public-test-fixture'))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        def close():
            server.shutdown(); server.server_close(); thread.join()
        self.addCleanup(close)
        return 'http://127.0.0.1:%d' % server.server_address[1]

    def post(self, base, path, payload, authorized=True):
        headers={'Content-Type': 'application/json'}
        if authorized:
            headers['Authorization']='Bearer public-test-fixture'
        request=urllib.request.Request(base+path, data=json.dumps(payload).encode(), headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, response.headers.get('Content-Type',''), response.read()
        except urllib.error.HTTPError as e:
            with e:
                return e.code, e.headers.get('Content-Type',''), e.read()

    def body(self, path, model, stream=False):
        body={'model':model, 'max_tokens':128, 'stream':stream}
        if 'responses' in path:
            body.update(input='hello', max_output_tokens=128)
        else:
            body['messages']=[{'role':'user','content':'hello'}]
        return body

    def test_auth_runs_before_profile_resolution_on_every_api(self):
        self.build({'profiles': {'alpha': {}, 'beta': {}}})
        base=self.listener()
        before=len(self.engine.requests)
        for path in ('/v1/chat/completions','/v1/responses','/v1/messages','/v1/messages/count_tokens'):
            with self.subTest(path=path):
                status, typ, body=self.post(base,path,self.body(path,'unknown'),authorized=False)
                self.assertEqual(status,401,body)
        self.assertEqual(len(self.engine.requests),before)
        self.assertEqual(self.proxy.contexts,{})

    def test_unknown_is_json_400_before_sse_on_all_adapters_and_aliases(self):
        self.build({'profiles': {'alpha': {}, 'beta': {}}})
        base=self.listener()
        for path in ('/v1/chat/completions','/chat/completions','/v1/responses','/responses',
                     '/v1/messages','/messages','/v1/messages/count_tokens','/messages/count_tokens'):
            for stream in (False,True):
                with self.subTest(path=path,stream=stream):
                    status, typ, body=self.post(base,path,self.body(path,'unknown',stream))
                    self.assertEqual(status,400,body)
                    self.assertIn('application/json',typ)
                    self.assertIn('unknown model',body.decode())
        self.assertEqual(self.engine.requests,[])

    def test_known_profiles_work_on_each_protocol_streaming_and_nonstreaming(self):
        self.build({'profiles': {'alpha': {}, 'beta': {}}})
        base=self.listener()
        for path in ('/v1/chat/completions','/v1/responses','/v1/messages'):
            for stream in (False,True):
                model='alpha' if not stream else 'beta'
                before=len(self.engine.requests)
                with self.subTest(path=path,stream=stream):
                    status, typ, body=self.post(base,path,self.body(path,model,stream))
                    self.assertEqual(status,200,body)
                    if stream:
                        self.assertIn('text/event-stream',typ)
                        self.assertIn(b'data:',body)
                    else:
                        self.assertIsInstance(json.loads(body),dict)
                    sent=managed_requests(self.engine)[-1]
                    self.assertEqual(sent['model'],model)
                    self.assertGreater(len(self.engine.requests),before)
        status, _, body=self.post(base,'/v1/messages/count_tokens',self.body('/v1/messages','alpha'))
        self.assertEqual(status,200,body)
        self.assertGreater(json.loads(body)['input_tokens'],0)

    def test_off_profiles_remain_unmanaged_in_both_chat_forms(self):
        self.build({'auto_config':False,'profiles':{'alpha':{},'beta':{}}},mode='off')
        base=self.listener()
        for model,stream in (('alpha',False),('beta',True),('alpha',True)):
            status, _, body=self.post(base,'/v1/chat/completions',self.body('/v1/chat/completions',model,stream))
            self.assertEqual(status,200,body)
            self.assertNotIn('recall',json.dumps(self.engine.requests[-1].get('tools')))
        for child in self.proxy.contexts.values():
            self.assertEqual(child.store.db.execute('SELECT count(*) FROM archive').fetchone()[0],0)
        self.assertEqual(self.proxy.workers(),[])
