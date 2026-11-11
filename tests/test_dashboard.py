"""Static dashboard contracts (no browser dependency in the unit suite)."""
import re
import unittest
from html.parser import HTMLParser
from pathlib import Path
from unittest.mock import MagicMock, patch

from dashboard.ctxdash import http_json


class IdParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids = []

    def handle_starttag(self, tag, attrs):
        self.ids.extend(value for key, value in attrs if key == "id")


class DashboardTests(unittest.TestCase):
    @patch("dashboard.ctxdash.urllib.request.urlopen")
    def test_strata_api_key_is_sent_as_bearer(self, urlopen):
        response = MagicMock()
        response.__enter__.return_value.read.return_value = b'{"ok": true}'
        urlopen.return_value = response

        data, error = http_json("http://127.0.0.1:5000/status", api_key="test-key")

        request = urlopen.call_args.args[0]
        self.assertEqual(request.get_header("Authorization"), "Bearer test-key")
        self.assertEqual(data, {"ok": True})
        self.assertIsNone(error)

    def test_literal_id_references_exist(self):
        html = (Path(__file__).resolve().parents[1] / "dashboard" / "index.html").read_text(encoding="utf-8")
        parser = IdParser()
        parser.feed(html)
        refs = set(re.findall(r'\$\([\"\']([^\"\']+)[\"\']\)', html))
        self.assertTrue(refs)
        self.assertEqual(refs - set(parser.ids), set())
        self.assertEqual(len(parser.ids), len(set(parser.ids)), "duplicate HTML IDs")

    def test_the_three_language_dicts_have_the_same_keys(self):
        html = (Path(__file__).resolve().parents[1] / "dashboard" / "index.html").read_text(encoding="utf-8")
        dicts = {lang: set(re.findall(r'[,{]\s*(\w+):"', body))
                 for lang, body in re.findall(r'I18N\.(\w+)=(\{.*?\});', html, re.S)}
        self.assertEqual(set(dicts), {"it", "en", "fr"})
        self.assertEqual(dicts["it"], dicts["en"], "I18N.en keys differ from I18N.it")
        self.assertEqual(dicts["it"], dicts["fr"], "I18N.fr keys differ from I18N.it")
