# Author: Maurizio Verde — LastReload
"""0.3.0 — recall a finestra piccola (card t_5de89914, Qwen3.8 su ds4-ROCm, pi, --ctx 8192).

Dal vivo: `cat server.log` (9.282 caratteri) mascherato; il modello chiama recall per id, poi id+offset, poi
id+query "ERROR"; il proxy restituisce pezzi dall'inizio (o da un offset), poi li taglia a zero perché lo spazio
calcolato è negativo, e la 3a riga ERROR (rq-2290, shard 3, al carattere ~6.183) non arriva mai al modello.

Qui: stessa uscita, motore finto ds4 con finestra 8K, stessa sequenza di recall. La 3a riga ERROR deve arrivare.

    python3 -m unittest -v tests.test_recall_small_window
"""
from __future__ import annotations

import json
import os
import random
import re
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ctxproxy import engines  # noqa: E402
from ctxproxy.core import RECALL_NAME, Config, Journal, Store, TokenCounter  # noqa: E402
from ctxproxy.server import Proxy  # noqa: E402
from ctxproxy.upstream import Upstream  # noqa: E402
from tests.fake_engine import FakeEngine  # noqa: E402
from tests.test_proxy import TOOLS  # noqa: E402


def server_log() -> str:
    """Lo stesso server.log della prova dal vivo (pi-qwen.sh, seed 7)."""
    random.seed(7)
    svc = ["auth", "billing", "search", "upload", "mailer"]
    out = []
    for i in range(140):
        h, m, s = 8 + i // 20, (i * 7) % 60, svc[i % 5]
        if i in (17, 58, 93, 121):
            rid = {17: "rq-4471", 58: "rq-8812", 93: "rq-2290", 121: "rq-6603"}[i]
            out.append(f"2026-10-06 {h:02d}:{m:02d}:{i % 60:02d} ERROR {s} request {rid} failed: upstream timeout "
                       f"after {1200 + i * 3} ms (shard {i % 9})")
        else:
            out.append(f"2026-10-06 {h:02d}:{m:02d}:{i % 60:02d} INFO {s} handled request rq-{1000 + i * 37} in "
                       f"{20 + random.randint(0, 180)} ms")
    return "\n".join(out) + "\n"


LOG = server_log()
THIRD = next(l for l in LOG.splitlines() if "rq-2290" in l)


class TestRecallSmallWindow(unittest.TestCase):
    # la sequenza vista dal vivo (round 0..3), poi la risposta
    SEQ = [lambda rid: {"id": rid}, lambda rid: {"id": rid, "offset": 1198},
           lambda rid: {"id": rid, "offset": 1198, "query": "ERROR"}, lambda rid: {"id": rid, "offset": 3300}]

    def policy(self, body):
        msgs = body["messages"]
        last = msgs[-1]
        if last.get("role") == "user" and "TERZO" in str(last.get("content")):
            self.k = 0
        if last.get("role") == "tool" and str(last.get("tool_call_id", "")).startswith("call_") and self.k is not None:
            self.got.append(str(last.get("content")))
        if self.k is not None and self.k < len(self.SEQ):
            rid = re.findall(r"r[0-9a-f]{12}", json.dumps(msgs))[0]
            a = self.SEQ[self.k](rid)
            self.k += 1
            return {"role": "assistant", "content": "", "tool_calls": [
                {"type": "function", "function": {"name": RECALL_NAME, "arguments": json.dumps(a)}}]}
        if self.k is not None:
            return {"role": "assistant", "content": "fine"}
        return {"role": "assistant", "content": "ok"}

    def setUp(self):
        self.k, self.got = None, []
        self.tmp = tempfile.TemporaryDirectory()
        self.eng = FakeEngine(self.policy, flavor="ds4", n_ctx=8192)
        url = self.eng.start()
        with open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "examples",
                               "config.ds4.json")) as f:
            raw = json.load(f)
        cfg = Config.from_dict(raw)
        self.jpath = os.path.join(self.tmp.name, "journal.jsonl")
        up, j = Upstream(url), Journal(self.jpath)
        eng = engines.setup(cfg, up, j, explicit=set(raw), log=None)
        self.proxy = Proxy(cfg, up, Store(os.path.join(self.tmp.name, "a.sqlite")), j, TokenCounter(3.5))
        self.proxy.engine = eng

    def tearDown(self):
        self.eng.stop()
        self.proxy.store.db.close()
        self.tmp.cleanup()

    def send(self, msgs):
        st, r, _ = self.proxy.chat({"model": "m", "messages": msgs, "tools": TOOLS, "max_tokens": 2048})
        self.assertEqual(st, 200, r)
        return r

    def test_third_error_reaches_the_model(self):
        h = [{"role": "system", "content": "sei un agente di programmazione"},
             {"role": "user", "content": "Esegui: cat server.log . Dimmi solo quante righe ERROR ci sono."},
             {"role": "assistant", "content": "", "tool_calls": [{"id": "call_cat", "type": "function", "function": {
                 "name": "bash", "arguments": json.dumps({"command": "cat server.log"})}}]},
             {"role": "tool", "tool_call_id": "call_cat", "content": LOG},
             {"role": "assistant", "content": "Ci sono 4 righe ERROR."}]
        # lavoro successivo (count.py, --level): giri di strumenti piccoli finché il prompt è vicino alla finestra
        for k in range(14):
            h += [{"role": "user", "content": "passo %d: modifica count.py ed eseguilo" % k},
                  {"role": "assistant", "content": "", "tool_calls": [{"id": "call_s%d" % k, "type": "function",
                                                                       "function": {"name": "bash", "arguments": json.dumps(
                                                                           {"command": "python3 count.py --level ERROR"})}}]},
                  {"role": "tool", "tool_call_id": "call_s%d" % k, "content": ("auth 1\nbilling 1\nsearch 1\n" * 12)},
                  {"role": "assistant", "content": "Fatto, passo %d." % k}]
            self.send(h + [{"role": "user", "content": "ok"}])
        h.append({"role": "user", "content": "Senza rieseguire comandi: qual era esattamente il request id e lo shard "
                                             "del TERZO errore nel server.log? Se non lo vedi più, usa recall."})
        self.send(h)
        with open(self.jpath) as f:
            ev = [json.loads(l) for l in f]
        self.assertTrue([e for e in ev if e["event"] in ("mask", "switch")], "l'uscita di cat deve essere tolta")
        self.assertTrue(self.got, "le recall devono essere arrivate al motore")
        self.assertTrue(any(THIRD in g for g in self.got),
                        "la 3a riga ERROR non è mai arrivata al modello: %s" % [g[:120] for g in self.got])


if __name__ == "__main__":
    unittest.main()
