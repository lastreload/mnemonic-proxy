"""Static dashboard contracts (no browser dependency in the unit suite)."""
import re
import unittest
from html.parser import HTMLParser
from pathlib import Path


class IdParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids = []

    def handle_starttag(self, tag, attrs):
        self.ids.extend(value for key, value in attrs if key == "id")


class DashboardTests(unittest.TestCase):
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
