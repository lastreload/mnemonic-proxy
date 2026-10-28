# Author: Maurizio Verde — LastReload
"""Explicit --tokenizer must not silently change an exact-count run into an estimate."""
import tempfile
import unittest
from unittest.mock import Mock, patch

from ctxproxy import core, server


class TestTokenizerStartup(unittest.TestCase):
    def test_explicit_tokenizer_failure_stops_before_upstream_or_listener(self):
        for error in (ModuleNotFoundError("No module named 'tokenizers'"),
                      ValueError('tokenizer non riconosciuto: missing.json'),
                      ValueError('invalid tokenizer JSON')):
            with self.subTest(error=str(error)), tempfile.TemporaryDirectory() as d:
                with patch('ctxproxy.core.load_tokenizer', side_effect=error), \
                     patch.object(server, 'Upstream', side_effect=AssertionError('unexpected upstream setup')) as upstream, \
                     patch.object(server, 'ThreadingHTTPServer') as listener:
                    with self.assertRaises(SystemExit) as caught:
                        server.main(['--data', d, '--tokenizer', 'chosen.json'])
                    message = str(caught.exception)
                    self.assertIn('--tokenizer', message)
                    self.assertIn('mnemonic-proxy[exact]', message)
                    self.assertIn(str(error), message)
                    upstream.assert_not_called()
                    listener.assert_not_called()

    def test_no_tokenizer_keeps_estimated_mode(self):
        with tempfile.TemporaryDirectory() as d:
            args = server.build_parser().parse_args(['--data', d])
            with patch.object(server, 'Upstream', side_effect=RuntimeError('startup reached')):
                with self.assertRaisesRegex(RuntimeError, 'startup reached'):
                    server.serve(args, {}, core.Config())
        self.assertFalse(core.TokenCounter().exact)

    def test_loaded_tokenizer_keeps_exact_mode(self):
        with tempfile.TemporaryDirectory() as d:
            args = server.build_parser().parse_args(['--data', d, '--tokenizer', 'chosen.json'])
            tokenizer = Mock()
            with patch('ctxproxy.core.load_tokenizer', return_value=tokenizer), \
                 patch.object(server, 'Upstream', side_effect=RuntimeError('startup reached')):
                with self.assertRaisesRegex(RuntimeError, 'startup reached'):
                    server.serve(args, {}, core.Config())
            with patch('ctxproxy.core.load_tokenizer', return_value=tokenizer):
                self.assertTrue(core.TokenCounter(tokenizer_path='chosen.json').exact)


if __name__ == '__main__':
    unittest.main()
