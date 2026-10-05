"""Test della card t_e41e63fd: definizioni strumenti accorciate (ctxproxy/tooldefs.py), spente per default."""
from __future__ import annotations

import copy
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ctxproxy.core import RECALL_NAME, Config, Journal, Manager, Store, TokenCounter  # noqa: E402
from ctxproxy.render import render_pieces  # noqa: E402
from ctxproxy.tooldefs import MARK, ToolShortener, shorten_text, shorten_tool  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
PI_REQ = os.path.join(HERE, "../../g3/pi-first-request.json")
LONG = ("Query the diagnostic state. mode=delta is instant; mode=full is expensive.\n\nIMPORTANT: this tool covers "
        "ALL runners: LSP errors, tree-sitter rules, lint findings, complexity violations, and more. " * 3)
TOOL = {"type": "function", "function": {
    "name": "lens", "description": LONG,
    "parameters": {"type": "object", "required": ["mode"], "properties": {
        "mode": {"type": "string", "enum": ["delta", "all", "full"], "default": "delta",
                 "description": "delta = current turn. all = session diagnostics for edited files. full = expensive "
                                "active project-wide LSP scan plus cached runner diagnostics."},
        "paths": {"type": "array", "maxItems": 200, "items": {"type": "string", "description": "x " * 200},
                  "description": "Restrict any mode to an explicit list. " + "Entries may be relative. " * 20},
        "refresh": {"anyOf": [{"type": "boolean"}, {"type": "string", "enum": ["cheap", "all"],
                                                    "description": "fresh run " * 40}]}}}}}
BASH = {"type": "function", "function": {"name": "bash", "description": "Run a command. " * 40,
                                         "parameters": {"type": "object", "properties": {}}}}


def strip_desc(x):
    if isinstance(x, dict):
        return {k: strip_desc(v) for k, v in x.items() if k != "description"}
    if isinstance(x, list):
        return [strip_desc(v) for v in x]
    return x


def descs(x, out=None):
    out = [] if out is None else out
    if isinstance(x, dict):
        for k, v in x.items():
            if k == "description" and isinstance(v, str):
                out.append(v)
            else:
                descs(v, out)
    elif isinstance(x, list):
        for v in x:
            descs(v, out)
    return out


class TestShortenText(unittest.TestCase):
    def test_short_unchanged(self):
        self.assertEqual(shorten_text("breve.", 100), "breve.")
        self.assertEqual(shorten_text(LONG, 0), LONG)

    def test_first_paragraph_then_sentences_then_words(self):
        s = shorten_text(LONG, 120)
        self.assertEqual(s, "Query the diagnostic state. mode=delta is instant; mode=full is expensive." + MARK)
        s = shorten_text("Prima frase corta. Seconda frase molto molto lunga che non ci sta nel limite.", 30)
        self.assertEqual(s, "Prima frase corta." + MARK)
        s = shorten_text("una sola frase lunghissima senza punti " * 5, 50)
        self.assertTrue(s.endswith(MARK) and len(s) <= 50 + len(MARK) and not s[:-len(MARK)].endswith(" "))

    def test_deterministic(self):
        self.assertEqual(shorten_text(LONG, 90), shorten_text(LONG, 90))


class TestShortenTool(unittest.TestCase):
    def test_structure_preserved_only_descriptions_change(self):
        orig = copy.deepcopy(TOOL)
        s = shorten_tool(TOOL, 120, 60)
        self.assertEqual(TOOL, orig, "l'originale non deve cambiare")
        self.assertEqual(strip_desc(s), strip_desc(TOOL))   # nomi, tipi, enum, required, default, anyOf, items
        self.assertEqual(json.dumps(strip_desc(s)), json.dumps(strip_desc(TOOL)))   # anche l'ordine delle chiavi
        for d in descs(s):
            self.assertLessEqual(len(d), 120 + len(MARK))
        # annidati: items.description e anyOf[].description accorciati
        p = s["function"]["parameters"]["properties"]
        self.assertTrue(p["paths"]["items"]["description"].endswith(MARK))
        self.assertTrue(p["refresh"]["anyOf"][1]["description"].endswith(MARK))

    def test_shortener_off_by_default_and_keep(self):
        self.assertEqual(Config().tooldefs_desc_max, 0)
        self.assertEqual(Config().tooldefs_param_max, 0)
        tools = [TOOL, BASH]
        self.assertIs(ToolShortener()(tools), tools)
        out = ToolShortener(100, 40, keep=("bash",))(tools)
        self.assertIs(out[1], BASH)
        self.assertNotEqual(out[0], TOOL)

    def test_stable_across_calls_and_instances(self):
        a = ToolShortener(100, 40)([TOOL, BASH])
        b = ToolShortener(100, 40)(json.loads(json.dumps([TOOL, BASH])))
        self.assertEqual(json.dumps(a, ensure_ascii=False), json.dumps(b, ensure_ascii=False))

    @unittest.skipUnless(os.path.exists(PI_REQ), "manca g3/pi-first-request.json")
    def test_real_pi_tools(self):
        tools = json.load(open(PI_REQ))["body"]["tools"]
        out = ToolShortener(300, 120, Config().tooldefs_keep)(tools)
        self.assertEqual([t["function"]["name"] for t in out], [t["function"]["name"] for t in tools])
        for a, b in zip(tools, out):
            self.assertEqual(strip_desc(a), strip_desc(b))
            if a["function"]["name"] in Config().tooldefs_keep:
                self.assertIs(a, b)
        self.assertLess(len(json.dumps(out)), 0.8 * len(json.dumps(tools)))


class TestManager(unittest.TestCase):
    def mk(self, **over):
        tmp = tempfile.TemporaryDirectory()
        cfg = Config.from_dict({"mask_anchor": False, **over})
        return Manager(cfg, Store(os.path.join(tmp.name, "a.sqlite")), TokenCounter(3.5),
                       Journal(os.path.join(tmp.name, "j.jsonl"))), tmp

    def req(self, n=3):
        msgs = [{"role": "system", "content": "sei un agente"}, {"role": "user", "content": "ciao"}]
        for k in range(n):
            msgs.append({"role": "assistant", "content": "passo %d" % k})
            msgs.append({"role": "user", "content": "continua %d" % k})
        return {"messages": msgs, "tools": [TOOL, BASH], "max_tokens": 256}

    def test_default_passes_client_tools_unchanged(self):
        mgr, tmp = self.mk()
        p = mgr.prepare(self.req())
        self.assertEqual(p.tools[:2], [TOOL, BASH])
        self.assertEqual(p.tools[2]["function"]["name"], RECALL_NAME)
        tmp.cleanup()

    def test_enabled_shortens_and_keeps_prefix_stable(self):
        mgr, tmp = self.mk(tooldefs_desc_max=100, tooldefs_param_max=40)
        base, tb = self.mk()
        p1 = mgr.prepare(self.req(2))
        p2 = mgr.prepare(self.req(3))
        self.assertEqual(json.dumps(p1.tools), json.dumps(p2.tools))      # stesso testo a ogni richiesta
        h1 = "".join(t for i, t in render_pieces(p1.messages, p1.tools) if i is None)
        r1 = "".join(t for _, t in render_pieces(p1.messages, p1.tools, add_generation_prompt=False))
        r2 = "".join(t for _, t in render_pieces(p2.messages, p2.tools))
        self.assertTrue(r2.startswith(r1) and r2.startswith(h1.split("<|im_end|>")[0]))   # prefisso riusabile
        self.assertIs(p2.tools[1], BASH)                                   # bash in keep: intatto
        q = base.prepare(self.req(2))
        self.assertLess(p1.est_tokens, q.est_tokens)
        # la conversazione è la stessa (catena di hash sugli strumenti DEL CLIENT, non su quelli accorciati)
        self.assertEqual(p1.hs, q.hs)
        tmp.cleanup()
        tb.cleanup()


if __name__ == "__main__":
    unittest.main()
