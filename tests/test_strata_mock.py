"""Integrazione col MOCK ENGINE DI STRATA (serve/server.py, template chat vero, parser tool vero), in-process.

    STRATA_DIR=/path/to/Strata python3 -m unittest -v tests.test_strata_mock

Porta: 18095 (verificata libera con `ss -ltn` prima del run; mai 8095)."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
STRATA = os.environ.get("STRATA_DIR", os.path.abspath(os.path.join(HERE, "..", "Strata")))
sys.path.insert(0, STRATA)

from ctxproxy.core import Config, Journal, Store, TokenCounter  # noqa: E402
from ctxproxy.server import Proxy  # noqa: E402
from ctxproxy.upstream import Upstream  # noqa: E402
from tests.test_proxy import TOOLS, history, tool_round  # noqa: E402

try:
    from pathlib import Path
    from serve.frontend import ChatTemplate
    from serve.server import ByteTokenizer, MockEngine, Service, serve
except Exception as e:  # noqa: BLE001
    raise unittest.SkipTest("Strata non importabile: %s" % e)

PORT = int(os.environ.get("STRATA_MOCK_PORT", "18095"))
RECALL_CALL = ("Cerco nell'archivio.</think>\n\n<tool_call>\n<function=strata_recall>\n<parameter=query>\n"
               "riga 0 del file f0.txt\n</parameter>\n</function>\n</tool_call>")
FINAL = "Ho letto.</think>\n\nRisposta finale dopo il recall."


class Recording(MockEngine):
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.prompts = []

    def generate(self, ids, max_new, sampling, cancel, embeddings=None):
        self.prompts.append(list(ids))
        yield from super().generate(ids, max_new, sampling, cancel, embeddings)


def common(a, b):
    n = 0
    for x, y in zip(a, b):
        if x != y:
            break
        n += 1
    return n


class StrataMock(unittest.TestCase):
    def setUp(self):
        tok = ByteTokenizer()
        self.engine = Recording(tok, ["</think>\n\nok"], max_context=1 << 20)
        self.svc = Service(self.engine, tok, ChatTemplate(Path(STRATA) / "serve/chat_template.jinja"))
        self.httpd = serve(self.svc, port=PORT)
        self.tmp = tempfile.TemporaryDirectory()
        # ByteTokenizer = 1 token per byte: soglie in "byte"
        cfg = Config.from_dict({"window": 1 << 20, "chars_per_token": 1.0, "mask_trigger": 90000,
                                "mask_target": 45000, "keep_recent_tokens": 20000, "segments_enabled": False})
        self.jpath = os.path.join(self.tmp.name, "j.jsonl")
        self.proxy = Proxy(cfg, Upstream("http://127.0.0.1:%d" % PORT), Store(os.path.join(self.tmp.name, "a.db")),
                           Journal(self.jpath), TokenCounter(1.0))

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.proxy.store.db.close()
        self.tmp.cleanup()

    def send(self, msgs):
        st, r, _ = self.proxy.chat({"model": "m", "messages": msgs, "tools": TOOLS, "max_tokens": 200})
        self.assertEqual(st, 200, r)
        return r

    def test_prefix_stability_real_template(self):
        h = history(1, size=3500)
        h[-2]["reasoning_content"] = None
        for k in range(1, 30):
            rnd = tool_round(k, 3500)
            rnd[0].pop("reasoning_content")   # come pi dopo il turno: il template toglie comunque i <think> vecchi
            h += rnd
            self.send(h)
        p = self.engine.prompts
        masks = [json.loads(l) for l in open(self.jpath) if '"mask"' in l]
        breaks = [i for i in range(1, len(p)) if common(p[i - 1], p[i]) < len(p[i - 1]) - 64]
        print("\n[strata-mock] richieste=%d pacchetti mask=%d rotture prefisso=%d prompt finale=%d token(byte) "
              "storia virtuale=%d" % (len(p), len(masks), len(breaks), len(p[-1]),
                                      sum(len(json.dumps(m)) for m in h)))
        self.assertGreaterEqual(len(masks), 1)
        self.assertEqual(len(breaks), len(masks))
        # stesso prompt fisico (token) per due richieste identiche
        self.send(h)
        self.send(h)
        self.assertEqual(self.engine.prompts[-1], self.engine.prompts[-2])

    def test_recall_loop_real_parser(self):
        h = history(1, size=3500)
        for k in range(1, 30):
            h += tool_round(k, 3500)
            self.send(h)
        self.engine.scripts = [ByteTokenizer().encode(x) + ByteTokenizer().encode("<|im_end|>", parse_special=True)
                               for x in (RECALL_CALL, FINAL)]
        self.engine.turns = 0
        h += [{"role": "assistant", "content": "fatto"}, {"role": "user", "content": "cosa c'era in f0?"}]
        r = self.send(h)
        msg = r["choices"][0]["message"]
        self.assertEqual(msg["content"], "Risposta finale dopo il recall.")
        self.assertNotIn("tool_calls", msg)
        last = self.engine.prompts[-1]
        text = ByteTokenizer().decode(last)
        self.assertIn("riga 0 del file f0.txt", text.split("<tool_response>")[-1])
        print("\n[strata-mock] ciclo recall OK: strata_context=%s" % json.dumps(r["strata_context"])[:300])


if __name__ == "__main__":
    unittest.main()
