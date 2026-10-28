"""CPU regressions from #28 counts, with an explicitly synthetic counter mismatch."""
import contextlib
import io
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from ctxproxy.core import Config, Journal
from ctxproxy.fake_engine import FakeEngine
from ctxproxy.render import render, template_kwargs
from ctxproxy.server import make_handler
from ctxproxy.upstream import Upstream, UpstreamError
from ctxproxy.window import WindowExceeded, WindowGuard

FIXTURE = json.loads((Path(__file__).parent / 'fixtures/issue28-followup.json').read_text())


class ReportedCountEngine(FakeEngine):
    """Real HTTP fake engine, same pre-generation error contract as PolyStrata."""
    def __init__(self, case):
        super().__init__(n_ctx=case['window'])
        self.case = case
        self.strict_window = True

    def window_check(self, body):
        self.checked += 1
        p, w = self.case['prompt'], self.n_ctx
        mt = body.get('max_completion_tokens') or body.get('max_tokens')
        if p + mt + 8 <= w:
            return None
        self.violations.append({'prompt': p, 'max_tokens': mt})
        if p >= w:
            return (f'request exceeds the context window: prompt ({p} tokens) leaves no room to answer '
                    f'in the context ({w}); requests are never truncated')
        return (f'request exceeds the context window: prompt ({p} tokens) + max tokens ({mt}) exceeds '
                f'the context ({w}); requests are never truncated. Send a smaller max_tokens')


def guard(case, journal):
    # Intentionally mismatched rendered count: not an actual pack tokenizer.
    mgr = SimpleNamespace(tc=SimpleNamespace(exact=True),
                          piece_tokens=lambda text: 0, estimate=lambda *a: (0, []))
    g = WindowGuard(Config(window=case['window']), mgr, journal)
    g.count_body = lambda body: case['guard_prompt']
    return g


class ContextFeedbackTests(unittest.TestCase):
    def test_reported_counts_over_http_streaming_and_nonstreaming(self):
        for case in FIXTURE['cases']:
            for stream in (False, True):
                for where in ('client', 'recall:1', 'notes', 'answerfix', 'continuation'):
                    with self.subTest(case=case['name'], stream=stream, where=where):
                        engine = ReportedCountEngine(case)
                        url = engine.start()
                        try:
                            journal = Journal(None)
                            up = Upstream(url)
                            up.window_guard = g = guard(case, journal)
                            g.where = where
                            messages = [{'role': 'user', 'content': 'synthetic count calibration'}]
                            body = {'model': 'fake', 'messages': messages,
                                    'max_tokens': case['max_tokens'],
                                    'max_completion_tokens': case['max_tokens'],
                                    'reasoning_budget_tokens': 1600}
                            if case['prompt'] >= case['window']:
                                with self.assertRaises(WindowExceeded):
                                    up.chat_stream(body) if stream else up.chat(body)
                                self.assertEqual(engine.checked, 1)  # never retry an oversized prompt
                                self.assertEqual(g.refused, 1)
                            else:
                                if stream:
                                    call = up.chat_stream(body)
                                    try:
                                        self.assertTrue(list(call))
                                    finally:
                                        call.close()
                                else:
                                    self.assertIn('choices', up.chat(body))
                                self.assertEqual(engine.checked, 2)
                                sent = engine.requests[-1]
                                self.assertEqual(sent['messages'], messages)
                                self.assertEqual(sent['max_tokens'], 1481)
                                self.assertEqual(sent['max_completion_tokens'], 1481)
                                self.assertLess(sent['reasoning_budget_tokens'], 1481)
                            ev = next(e for e in journal.mem if e.get('action') == 'engine_rejected')
                            self.assertEqual(ev['where'], where)
                            self.assertEqual(ev['counted_prompt'], case['guard_prompt'])
                        finally:
                            engine.stop()

    def test_retry_is_bounded(self):
        case = FIXTURE['cases'][0]
        up = Upstream('http://unused')
        up.window_guard = guard(case, Journal(None))
        body = {'messages': [], 'max_tokens': 1767}
        message = ('request exceeds the context window: prompt (31223 tokens) + max tokens (1767) '
                   'exceeds the context (32768); requests are never truncated')
        error = UpstreamError(400, json.dumps({'error': {'message': message}}).encode())
        send = Mock(side_effect=error)
        with self.assertRaises(UpstreamError):
            up._chat_once_with_context_retry(body, send)
        self.assertEqual(send.call_count, 2)

    def test_other_errors_are_not_retried(self):
        for status, raw in ((500, b'{}'), (400, b'not json'), (400, b'{"error":"bad"}'),
                            (400, b'{"error":{"message":"invalid tool schema"}}')):
            up = Upstream('http://unused')
            up.window_guard = guard(FIXTURE['cases'][0], Journal(None))
            error = UpstreamError(status, raw)
            send = Mock(side_effect=error)
            with self.assertRaises(UpstreamError) as caught:
                up._chat_once_with_context_retry({'max_tokens': 1767}, send)
            self.assertIs(caught.exception, error)
            self.assertEqual(send.call_count, 1)

    def test_guard_disabled_or_unknown_window_does_not_retry(self):
        case = FIXTURE['cases'][0]
        message = ('request exceeds the context window: prompt (31223 tokens) + max tokens (1767) '
                   'exceeds the context (32768); requests are never truncated')
        error = UpstreamError(400, json.dumps({'error': {'message': message}}).encode())
        for enabled, win in ((False, 32768), (True, 0)):
            up = Upstream('http://unused')
            up.window_guard = g = guard(case, Journal(None))
            g.cfg.window_guard, g.cfg.window = enabled, win
            send = Mock(side_effect=error)
            with self.assertRaises(UpstreamError):
                up._chat_once_with_context_retry({'max_tokens':1767}, send)
            self.assertEqual(send.call_count, 1)

    def test_matched_counter_refuses_before_http(self):
        case = FIXTURE['cases'][1]
        up = Upstream('http://unused')
        up.window_guard = guard({**case, 'guard_prompt': case['prompt']}, Journal(None))
        with patch.object(up, 'post_json') as send:
            with self.assertRaises(WindowExceeded):
                up.chat({'messages': [], 'max_tokens': 289})
            send.assert_not_called()

    def test_preserve_thinking_flag_is_counted(self):
        messages = [{'role': 'user', 'content': 'old'},
                    {'role': 'assistant', 'content': 'answer', 'reasoning_content': 'private reasoning'},
                    {'role': 'user', 'content': 'new'}]
        for preserve in (False, True):
            kw = template_kwargs({'chat_template_kwargs': {'preserve_thinking': preserve}})
            self.assertIs(kw['preserve_thinking'], preserve)
            self.assertEqual('private reasoning' in render(messages, None, kw), preserve)


class ConnectionAndLoggingTests(unittest.TestCase):
    def test_disconnect_at_header_or_body_is_quiet(self):
        for stage in ('end_headers', 'write'):
            for error in (BrokenPipeError, ConnectionResetError):
                journal = Journal(None)
                cls = make_handler(SimpleNamespace(journal=journal))
                handler = object.__new__(cls)
                handler.send_response = Mock()
                handler.send_header = Mock()
                handler.end_headers = Mock(side_effect=error if stage == 'end_headers' else None)
                handler.wfile = Mock()
                if stage == 'write':
                    handler.wfile.write.side_effect = error
                handler._send(400, {'error': 'test'})
                self.assertTrue(handler.close_connection)
                self.assertEqual(journal.mem[-1]['event'], 'client_disconnected')

    def test_unrelated_write_errors_are_not_hidden(self):
        cls = make_handler(SimpleNamespace(journal=Journal(None)))
        handler = object.__new__(cls)
        handler.send_response = Mock(side_effect=ValueError('bug'))
        with self.assertRaises(ValueError):
            handler._send(200, {})

    def test_stderr_allowlist_and_quiet_library_default(self):
        for enabled in (False, True):
            journal = Journal(None, stderr=enabled)
            sink = io.StringIO()
            with contextlib.redirect_stderr(sink):
                for event in ('startup', 'window_guard', 'response_clamped', 'mask', 'switch',
                              'switch_skipped', 'client_disconnected'):
                    journal.log(event, prompt=42, action='refused', content='SECRET', notes='SECRET')
                journal.log('other', content='SECRET')
            text = sink.getvalue()
            self.assertNotIn('SECRET', text)
            self.assertEqual(len(text.splitlines()), 7 if enabled else 0)


if __name__ == '__main__':
    unittest.main()
