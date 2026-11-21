"""HTTP loopback integration using the existing canned FakeEngine, never a real model."""
import copy
import json
import os
from pathlib import Path
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from ctxproxy.server import Proxy, make_handler
from ctxproxy.upstream import Upstream
from tests.fake_engine import FakeEngine
from tests import test_issue100_reasoning as reasoning


@unittest.skipUnless(reasoning.EXACT, 'install mnemonic-proxy[exact]')
class ReasoningHTTPTests(unittest.TestCase):
    def setUp(self):
        reasoning.SparkReasoningTests.setUp(self)
        self.engine = FakeEngine(lambda body: {'role': 'assistant', 'content': 'PONG'})
        self.addCleanup(self.engine.stop)
        self.proxy = Proxy(self.cfg, Upstream(self.engine.start()), self.store, self.journal, self.tc)
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(self.proxy))
        self.server.daemon_threads = True
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.base = 'http://127.0.0.1:%d' % self.server.server_address[1]

    def post(self, path, body):
        request = urllib.request.Request(self.base + path, json.dumps(body).encode(),
                                         headers={'Content-Type': 'application/json'}, method='POST')
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                status, raw = response.status, response.read()
        except urllib.error.HTTPError as error:
            with error:
                status, raw = error.code, error.read()
        output = os.environ.get('MNEMONIC_ISSUE100_HTTP_LOG')
        if output:
            with open(output, 'a', encoding='utf-8') as handle:
                handle.write(json.dumps({'path': path, 'request': body, 'status': status,
                                         'response': raw.decode(), 'mode': self.proxy.mode,
                                         'upstream_requests': len(self.engine.requests)}, ensure_ascii=False) + '\n')
        return status, raw

    def test_chat_efforts_count_and_forward_without_promotion(self):
        cases = [(name, {'reasoning_effort': name}, name in ('off', 'low', 'high'))
                 for name in ('off', 'low', 'medium', 'high', 'xhigh', 'banana')]
        cases += [('absent', {}, True), ('nested-high', {'reasoning': {'effort': 'high'}}, True),
                  ('override', {'reasoning_effort': 'high', 'chat_template_kwargs': {'reasoning_effort': 'low'}}, True),
                  ('disabled', {'reasoning_effort': 'high', 'chat_template_kwargs': {'enable_thinking': False}}, True)]
        for mode in ('off', 'tools'):
            self.proxy.mode = mode
            for streaming in (False, True):
                for name, extra, accepted in cases:
                    with self.subTest(mode=mode, stream=streaming, case=name):
                        body = {**self.body, **extra, 'stream': streaming}
                        original = copy.deepcopy(body)
                        before = len(self.engine.requests)
                        errors_before = self.proxy.wguard.count_errors
                        status, raw = self.post('/v1/chat/completions', body)
                        self.assertEqual(status, 200 if streaming or accepted else 400, raw)
                        self.assertEqual(body, original)
                        if accepted:
                            self.assertIn(b'PONG', raw)
                            self.assertEqual(len(self.engine.requests), before + 1)
                            sent = self.engine.requests[-1]
                            for key in ('reasoning_effort', 'reasoning', 'chat_template_kwargs'):
                                self.assertEqual(sent.get(key), body.get(key))
                            self.assertEqual(self.proxy.wguard.count_errors, errors_before)
                        else:
                            self.assertEqual(len(self.engine.requests), before)
                            self.assertIn(b'prompt_count_failed', raw)
                            self.assertIn(('Unexpected reasoning effort ' + name).encode(), raw)
                            self.assertNotIn(b'leaves no room', raw)

    def test_responses_nested_high_is_forwarded_as_high(self):
        for streaming in (False, True):
            with self.subTest(stream=streaming):
                body = {'model': 'spark-fixture', 'input': 'Reply with exactly: PONG',
                        'max_output_tokens': 64, 'reasoning': {'effort': 'high'}, 'stream': streaming}
                status, raw = self.post('/v1/responses', body)
                self.assertEqual(status, 200, raw)
                self.assertIn(b'PONG', raw)
                self.assertEqual(self.engine.requests[-1]['reasoning_effort'], 'high')

    def test_responses_rejected_effort_preserves_cause(self):
        for streaming in (False, True):
            with self.subTest(stream=streaming):
                before = len(self.engine.requests)
                status, raw = self.post('/v1/responses', {
                    'model': 'spark-fixture', 'input': 'Reply with exactly: PONG',
                    'max_output_tokens': 64, 'reasoning': {'effort': 'medium'}, 'stream': streaming})
                self.assertEqual(status, 200 if streaming else 400, raw)
                self.assertIn(b'prompt count failed', raw)
                self.assertIn(b'Unexpected reasoning effort medium', raw)
                self.assertNotIn(b'context_window_exceeded', raw)
                self.assertNotIn(b'leaves no room', raw)
                self.assertEqual(len(self.engine.requests), before)
                if streaming:
                    self.assertIn(b'response.failed', raw)

    def test_all_protocols_report_render_failure_without_sending(self):
        from ctxproxy.chattemplate import PackTemplate
        broken = self.path / 'broken.jinja'
        broken.write_text("{{ raise_exception('render-witness-100') }}", encoding='utf-8')
        self.tc.prompt_template = PackTemplate(broken)
        for mode in ('off', 'tools'):
            self.proxy.mode = mode
            for streaming in (False, True):
                for path, payload in (
                    ('/v1/chat/completions', self.body),
                    ('/v1/responses', {'model': 'spark-fixture', 'input': 'PONG', 'max_output_tokens': 64}),
                    ('/v1/messages', self.body)):
                    with self.subTest(mode=mode, stream=streaming, path=path):
                        before = len(self.engine.requests)
                        status, raw = self.post(path, {**payload, 'stream': streaming})
                        self.assertEqual(status, 200 if streaming else 400, raw)
                        self.assertIn(b'prompt count failed', raw)
                        self.assertIn(b'render-witness-100', raw)
                        self.assertNotIn(b'context_window_exceeded', raw)
                        self.assertNotIn(b'leaves no room', raw)
                        self.assertEqual(len(self.engine.requests), before)
                        if streaming:
                            self.assertIn(b'response.failed' if path.endswith('responses') else b'error', raw)

    def test_count_failure_does_not_call_upstream_or_context_retry(self):
        from ctxproxy.upstream import UpstreamError
        for streaming in (False, True):
            before = len(self.engine.requests)
            send = self.proxy.up.chat_stream if streaming else self.proxy.up.chat
            with self.assertRaises(UpstreamError) as ctx:
                send({**self.body, 'reasoning_effort': 'banana'})
            self.assertEqual(json.loads(ctx.exception.body)['error']['code'], 'prompt_count_failed')
            self.assertEqual(len(self.engine.requests), before)
            records = [json.loads(line) for line in (self.path / 'journal.jsonl').read_text().splitlines()]
            self.assertTrue(any(r.get('reason') == 'count_error' for r in records))
            self.assertFalse(any(r.get('action') == 'engine_retry' for r in records))


if __name__ == '__main__':
    unittest.main()
