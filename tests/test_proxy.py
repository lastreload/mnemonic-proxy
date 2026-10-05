"""Test del proxy contesto virtuale con motore finto (nessuna GPU).   python3 -m unittest -v tests.test_proxy"""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import unittest
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ctxproxy.core import (NOTES_MARK, OUT_PREFIX, RECEIPT_OPEN, RECALL_NAME, Config, Journal, Manager, Store,  # noqa: E402
                           TokenCounter, chain, is_human_user)
from ctxproxy.server import Proxy, make_handler  # noqa: E402
from ctxproxy.upstream import Upstream  # noqa: E402
from tests.fake_engine import FakeEngine, render  # noqa: E402

SYSTEM = {"role": "system", "content": "Sei un agente di programmazione. " * 20}
TOOLS = [{"type": "function", "function": {"name": "bash", "description": "esegue un comando",
                                           "parameters": {"type": "object",
                                                          "properties": {"command": {"type": "string"}}}}}]


def tool_round(k: int, size: int):
    cid = "call_%d" % k
    return [{"role": "assistant", "content": "", "reasoning_content": "penso al passo %d" % k,
             "tool_calls": [{"id": cid, "type": "function",
                             "function": {"name": "bash", "arguments": json.dumps({"command": "cat f%d.txt" % k})}}]},
            {"role": "tool", "tool_call_id": cid, "content": ("riga %d del file f%d.txt\n" % (k, k)) * (size // 22)}]


def history(n_rounds: int, size: int = 3500, first="crea il gioco snake"):
    msgs = [SYSTEM, {"role": "user", "content": first}]
    for k in range(n_rounds):
        msgs += tool_round(k, size)
    return msgs


def default_policy(body):
    return {"role": "assistant", "content": "ok"}


class Base(unittest.TestCase):
    cfg_over: dict = {}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.eng = FakeEngine(getattr(self, "policy", default_policy))
        self.url = self.eng.start()
        cfg = Config.from_dict({"window": 131072, "mask_trigger": 30000, "mask_target": 15000,
                                "keep_recent_tokens": 6000, "min_mask_tokens": 200, **self.cfg_over})
        self.jpath = os.path.join(self.tmp.name, "journal.jsonl")
        self.proxy = Proxy(cfg, Upstream(self.url), Store(os.path.join(self.tmp.name, "a.sqlite")),
                           Journal(self.jpath), TokenCounter(3.5))

    def tearDown(self):
        self.eng.stop()
        self.proxy.store.db.close()
        self.tmp.cleanup()

    def send(self, msgs, **kw):
        st, r, hdr = self.proxy.chat({"model": "m", "messages": msgs, "tools": TOOLS, "max_tokens": 1024, **kw})
        self.assertEqual(st, 200, r)
        return r, hdr

    def events(self, name=None):
        if not os.path.exists(self.jpath):
            return []
        with open(self.jpath) as f:
            ev = [json.loads(l) for l in f]
        return [e for e in ev if name is None or e["event"] == name]


def assert_tool_pairs(tc: unittest.TestCase, msgs):
    """Ogni messaggio tool deve seguire (dopo altri tool) un assistant che contiene la sua chiamata."""
    open_ids = set()
    for m in msgs:
        if m.get("role") == "assistant":
            open_ids = {c["id"] for c in m.get("tool_calls") or []}
        elif m.get("role") == "tool":
            tc.assertIn(m.get("tool_call_id"), open_ids, "risultato tool orfano: %s" % m.get("tool_call_id"))
        else:
            open_ids = set()


class TestMapping(Base):
    def test_continuation_retry_edit(self):
        mgr, store = self.proxy.mgr, self.proxy.store
        h = history(4)
        p1 = mgr.prepare({"messages": h, "tools": TOOLS})
        # continuazione: stessa conversazione
        h2 = h + [{"role": "assistant", "content": "fatto"}, {"role": "user", "content": "continua"}]
        p2 = mgr.prepare({"messages": h2, "tools": TOOLS})
        self.assertEqual(p1.conv, p2.conv)
        # retry: il client rimanda la stessa storia senza l'ultima risposta (rigenerazione) -> stessa conversazione
        p3 = mgr.prepare({"messages": h2[:-2] + [{"role": "user", "content": "riprova"}], "tools": TOOLS})
        self.assertEqual(p1.conv, p3.conv)
        self.assertEqual(len(self.events("branch")), 1)  # rigenerazione = ramo corto dal penultimo messaggio
        # modifica di un messaggio vecchio (indice 3: il primo risultato tool) -> ramo dalla divergenza
        h4 = [dict(m) for m in h2]
        h4[3]["content"] = "contenuto cambiato"
        p4 = mgr.prepare({"messages": h4, "tools": TOOLS})
        self.assertEqual(p4.conv, p1.conv)  # stesso lavoro (prefisso h_0..h_2 noto)
        br = self.events("branch")
        self.assertEqual(len(br), 2)
        self.assertEqual(br[1]["known"], 2)  # ultimo messaggio comune: indice 2 (la modifica è al 3)
        # gli hash dalla modifica in poi sono diversi; quelli prima uguali
        c2, c4 = chain(h2, TOOLS), chain(h4, TOOLS)
        self.assertEqual(c2[:3], c4[:3])
        self.assertTrue(all(a != b for a, b in zip(c2[3:], c4[3:])))
        # nessun hash noto (system diverso): conversazione nuova
        p5 = mgr.prepare({"messages": [{"role": "system", "content": "altro"}] + h[1:], "tools": TOOLS})
        self.assertNotEqual(p5.conv, p1.conv)
        self.assertEqual(store.stats(p1.conv)["archived"] >= len(h2), True)

    def test_pending_tool_calls_not_split(self):
        """Storia che finisce con chiamate tool del client ancora aperte: il proxy non tocca né separa nulla."""
        h = history(3)
        h.append({"role": "assistant", "content": "", "tool_calls": [
            {"id": "a", "type": "function", "function": {"name": "bash", "arguments": "{}"}},
            {"id": "b", "type": "function", "function": {"name": "bash", "arguments": "{}"}}]})
        h.append({"role": "tool", "tool_call_id": "a", "content": "x" * 5000})
        p = self.proxy.mgr.prepare({"messages": h, "tools": TOOLS})
        self.assertEqual(len(p.messages), len(h))
        assert_tool_pairs(self, p.messages)


class TestMasking(Base):
    def test_batch_masking_and_prefix_stability(self):
        h = history(1)
        prompts_before = []
        for k in range(1, 40):
            h += tool_round(k, 3500)
            r, hdr = self.send(h)
            self.assertNotIn("tool_calls", r["choices"][0]["message"])
            prompts_before.append(len(self.eng.prompts) - 1)
        masks = self.events("mask")
        self.assertGreaterEqual(len(masks), 1)
        self.assertLessEqual(len(masks), 6, "il masking deve scattare a pacchetti, non a ogni turno")
        # fra due richieste consecutive senza nuovo pacchetto di masking il prompt precedente è prefisso del nuovo
        reqs = self.events("request")
        breaks = sum(1 for a, b in zip(self.eng.prompts, self.eng.prompts[1:]) if not b.startswith(a))
        self.assertEqual(breaks, len(masks), "il prefisso cambia solo quando scatta un pacchetto di masking")
        # stesso prompt fisico per due richieste identiche consecutive
        self.send(h)
        self.send(h)
        self.assertEqual(self.eng.prompts[-1], self.eng.prompts[-2])
        # rilettura (prompt_read) ≈ solo i messaggi nuovi (~1.050 token per giro) tranne le richieste con un pacchetto
        read = [e["prompt_read"] for e in reqs[1:]]
        big = sum(1 for r in read if r > 1500)
        self.assertEqual(big, len(masks), read)
        # segnaposto presenti, coda recente intatta, coppie tool integre
        last = self.eng.requests[-1]["messages"]
        ph = [m for m in last if m.get("role") == "tool" and (m["content"].startswith(OUT_PREFIX) or m["content"].startswith(RECEIPT_OPEN))]
        self.assertGreater(len(ph), 5)
        # provenienza: comando di origine + inizio vero dell'uscita + istruzione
        self.assertIn("bash `", ph[0]["content"])
        self.assertRegex(ph[0]["content"], r"Inizio:|Ricevuta:")
        self.assertIn("recall id=", ph[0]["content"])
        self.assertFalse(last[-1]["content"].startswith("[uscita"))
        assert_tool_pairs(self, last)
        self.assertLess(self.proxy.tc.count(render(self.eng.requests[-1])), 45000)
        self.assertIn("X-Strata-Context", hdr)

    def test_recall_tool_injected(self):
        r, _ = self.send(history(2))
        names = [t["function"]["name"] for t in self.eng.requests[-1]["tools"]]
        self.assertEqual(names, ["bash", RECALL_NAME])
        self.assertIn("strata_context", r)


class TestRecall(Base):
    def policy(self, body):
        msgs = body["messages"]
        last = msgs[-1]
        if last.get("role") == "tool" and last["content"].startswith("recall: limite"):
            return {"role": "assistant", "content": "rispondo con quello che ho"}
        if any(m.get("role") == "user" and "insistente" in str(m.get("content")) for m in msgs[-12:]):
            return {"role": "assistant", "content": "", "tool_calls": [
                {"id": "z%d" % len(msgs), "type": "function",
                 "function": {"name": RECALL_NAME, "arguments": '{"query":"riga"}'}}]}
        if last.get("role") == "tool" and last["content"].startswith("[recall:"):
            return {"role": "assistant", "content": "trovato: " + last["content"].split("\n", 1)[1][:30]}
        if last.get("role") == "user" and "ricorda" in last["content"]:
            for m in msgs:
                if m.get("role") == "tool" and "recall id=" in m["content"]:
                    rid = re.search(r"recall id=(\w+)", m["content"]).group(1)
                    return {"role": "assistant", "content": "", "reasoning_content": "devo richiamare",
                            "tool_calls": [{"id": "rc1", "type": "function",
                                            "function": {"name": RECALL_NAME, "arguments": json.dumps({"id": rid})}}]}
        if last.get("role") == "user" and "misto" in last["content"]:
            return {"role": "assistant", "content": "", "tool_calls": [
                {"id": "x1", "type": "function", "function": {"name": RECALL_NAME, "arguments": '{"query":"riga"}'}},
                {"id": "x2", "type": "function", "function": {"name": "bash", "arguments": '{"command":"ls"}'}}]}
        return {"role": "assistant", "content": "ok"}

    def test_recall_loop(self):
        h = history(1)
        for k in range(1, 35):
            h += tool_round(k, 3500)
            self.send(h)
        self.assertTrue(self.events("mask"))
        h += [{"role": "assistant", "content": "fatto"}, {"role": "user", "content": "ricorda il primo file"}]
        n0 = len(self.eng.requests)
        r, _ = self.send(h)
        msg = r["choices"][0]["message"]
        self.assertEqual(r["choices"][0]["finish_reason"], "stop")
        self.assertNotIn("tool_calls", msg)
        self.assertTrue(msg["content"].startswith("trovato: riga 0 del file f0.txt"), msg["content"])
        self.assertEqual(len(self.eng.requests) - n0, 2)  # richiesta + ripresa dopo recall
        rec = self.events("recall")
        self.assertEqual(len(rec), 1)
        self.assertEqual(r["strata_context"]["recalls"][0]["tokens"] > 100, True)
        # turno successivo: gli eventi interni (chiamata + risultato recall) sono reinseriti identici -> prefisso stabile
        prev = self.eng.prompts[-1]
        h += [msg, {"role": "user", "content": "bene, prosegui"}]
        self.send(h)
        new = self.eng.prompts[-1]
        # il prompt precedente (fino alla risposta che il modello ha generato) è prefisso del nuovo
        self.assertTrue(new.startswith(prev), "il prefisso deve restare stabile dopo un ciclo recall")
        ids = [c["id"] for m in self.eng.requests[-1]["messages"] for c in (m.get("tool_calls") or [])]
        self.assertIn("rc1", ids)
        assert_tool_pairs(self, self.eng.requests[-1]["messages"])

    def test_mixed_calls_only_client_tools_returned(self):
        h = history(3) + [{"role": "assistant", "content": "ok"}, {"role": "user", "content": "misto"}]
        r, _ = self.send(h)
        calls = r["choices"][0]["message"]["tool_calls"]
        self.assertEqual([c["function"]["name"] for c in calls], ["bash"])
        self.assertEqual(r["choices"][0]["finish_reason"], "tool_calls")
        self.assertTrue(self.events("recall_dropped"))

    def test_recall_rounds_end_with_answer(self):
        h = history(3) + [{"role": "assistant", "content": "ok"}, {"role": "user", "content": "insistente"}]
        n0 = len(self.eng.requests)
        r, _ = self.send(h)
        self.assertEqual(r["choices"][0]["message"]["content"], "rispondo con quello che ho")
        self.assertEqual(len(self.eng.requests) - n0, self.proxy.cfg.max_recall_rounds + 2)

    def test_recall_by_query_and_offset(self):
        mgr = self.proxy.mgr
        h = history(3)
        p = mgr.prepare({"messages": h, "tools": TOOLS})
        out = mgr.recall(p.conv, '{"query": "f2.txt"}')
        self.assertIn("riga 2 del file f2.txt", out)
        self.proxy.cfg.recall_max_chars = 100
        from ctxproxy.core import rid_for
        rid = rid_for(3, h[3])
        out = mgr.recall(p.conv, {"id": rid})
        self.assertIn("[continua: recall id=%s offset=100]" % rid, out)
        self.assertIn("non trovato", mgr.recall(p.conv, {"id": "rnonesiste"}))
        self.assertNotIn("riga 2 del file f2.txt", mgr.recall(p.conv, '{"query": "f2.txt"}', max_idx=1))


class TestSegments(Base):
    cfg_over = {"window": 40000, "mask_enabled": False, "tail_max": 12000, "reserve": 2000, "default_response": 2000,
                "slot_save": True, "seal_experimental": True}

    def policy(self, body):
        last = body["messages"][-1]
        if last.get("role") == "user" and str(last.get("content", "")).startswith(NOTES_MARK):
            return {"role": "assistant", "content": "## Obiettivo\ncreare snake\n## Stato attuale\nf0..f9 letti"}
        return {"role": "assistant", "content": "ok"}

    def test_switch_and_mapping(self):
        h = history(1)
        h += [{"role": "assistant", "content": "primo passo fatto"}, {"role": "user", "content": "continua"}]
        switched_at = None
        for k in range(1, 14):
            h += tool_round(k, 12000)
            n0 = len(self.eng.requests)
            r, _ = self.send(h, max_tokens=1000)
            if self.events("switch") and switched_at is None:
                switched_at = k
                reqs = self.eng.requests[n0:]
                # ordine: note (in coda ad A, stesso prefisso) -> sigillo (max 1 token) -> SAVE -> richiesta su B
                self.assertTrue(reqs[0]["messages"][-1]["content"].startswith(NOTES_MARK))
                self.assertTrue(self.eng.prompts[n0].startswith(self.eng.prompts[n0 - 1][:-200]))
                self.assertEqual(reqs[1]["max_tokens"], 1)
                self.assertEqual(reqs[0]["tools"], reqs[2]["tools"])
                self.assertEqual(self.eng.slots[0][0], "save")
                b = reqs[2]["messages"]
                self.assertEqual(b[0], SYSTEM)
                self.assertIn("## Note di passaggio", b[1]["content"])
                self.assertIn("creare snake", b[1]["content"])
                self.assertIn("recall:", b[1]["content"])  # indice dell'archivio
                assert_tool_pairs(self, b)
                self.assertLess(len(b), len(h))
                # la coda di B parte su un confine sicuro: user umano o assistant dopo un risultato tool
                self.assertIn(b[2]["role"], ("user", "assistant"))
                prevB = self.eng.prompts[-1]
        self.assertIsNotNone(switched_at, "il segmento deve scattare")
        sw = self.events("switch")[0]
        self.assertIn(sw["cut_kind"], ("user", "group", "group-min"))
        for e in ("freeze", "notes", "seal", "save", "switch"):
            self.assertTrue(self.events(e), e)
        # la storia completa del client continua a mapparsi su B: nessun nuovo switch al turno dopo, prefisso stabile
        n_sw = len(self.events("switch"))
        h2 = h + [{"role": "assistant", "content": "ok"}, {"role": "user", "content": "avanti"}]
        before = self.eng.prompts[-1]
        r, _ = self.send(h2, max_tokens=1000)
        if len(self.events("switch")) == n_sw:
            self.assertTrue(self.eng.prompts[-1].startswith(before))
        self.assertGreaterEqual(r["strata_context"]["segment"], 1)
        # modifica di un messaggio vecchio (prima del taglio): il segmento non vale più -> segment_miss
        h3 = [dict(m) for m in h2]
        h3[1]["content"] = "crea un altro gioco"
        self.proxy.mgr.cfg.segments_enabled = False
        r, _ = self.send(h3)
        self.assertTrue(self.events("segment_miss"))
        self.assertEqual(r["strata_context"]["segment"], 0)

    def test_response_floor_and_saved_A_prompt(self):
        """G3: max_tokens piccolo (rigioco) non deve ritardare lo switch se response_floor è impostato; con data_dir
        il prompt fisico di A viene salvato accanto al .bin (serve al consulto con sigillo)."""
        self.proxy.mgr.cfg.response_floor = 2000
        self.proxy.mgr.cfg.data_dir = self.tmp.name
        h = history(1) + [{"role": "assistant", "content": "ok"}, {"role": "user", "content": "continua"}]
        k = 1
        while k < 14 and not (os.path.exists(self.jpath) and self.events("switch")):
            h += tool_round(k, 12000)
            self.send(h, max_tokens=8)
            k += 1
        self.assertTrue(self.events("switch"))
        fr = self.events("freeze")[0]
        self.assertEqual(fr["response"], 2000)
        # lo switch scatta quando est + floor + reserve supera W, non più tardi
        self.assertGreater(fr["est_tokens"] + 2000 + 2000 + 8, 40000)
        sv = self.events("save")[0]
        p = os.path.join(self.tmp.name, sv["file"] + ".json")
        d = json.load(open(p))
        self.assertEqual(d["messages"][0], SYSTEM)
        self.assertTrue(any(t["function"]["name"] == RECALL_NAME for t in d["tools"]))
        self.assertIn("finish", self.events("notes")[0])


class TestJournalAndHTTP(Base):
    def test_http_stream_and_journal(self):
        from http.server import ThreadingHTTPServer
        import threading
        srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.proxy))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        base = "http://127.0.0.1:%d" % srv.server_address[1]
        try:
            body = json.dumps({"model": "m", "messages": history(2), "tools": TOOLS, "stream": True}).encode()
            req = urllib.request.Request(base + "/v1/chat/completions", data=body,
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req) as r:
                self.assertEqual(r.headers["Content-Type"], "text/event-stream")
                # streaming vero: l'header parte prima della risposta, il contesto viaggia nell'ultimo chunk
                lines = [l for l in r.read().decode().split("\n") if l.startswith("data: ")]
            self.assertEqual(lines[-1], "data: [DONE]")
            chunks = [json.loads(l[6:]) for l in lines[:-1]]
            self.assertEqual("".join(c["choices"][0]["delta"].get("content") or "" for c in chunks), "ok")
            self.assertIn("strata_context", chunks[-1])
            with urllib.request.urlopen(base + "/v1/strata/journal") as r:
                self.assertTrue(json.loads(r.read())["events"])
            with urllib.request.urlopen(base + "/health") as r:   # proxy health + engine's /health
                hb = json.loads(r.read())
                self.assertEqual((hb["status"], hb["service"]), ("ok", "mnemonic-proxy"))
                self.assertTrue(hb["upstream"]["fake"])
            for p in ("/v1/engine", "/v1/strata/engine"):
                with urllib.request.urlopen(base + p) as r:
                    self.assertIn("kind", json.loads(r.read()))
        finally:
            srv.shutdown()
            srv.server_close()
        ev = self.events()
        self.assertTrue(all("ts" in e and "event" in e for e in ev))
        req = self.events("request")[0]
        for k in ("prompt_tokens", "reused", "prompt_read", "est_tokens", "ms"):
            self.assertIn(k, req)


class TestReasoningArgsMasking(Base):
    cfg_over = {"mask_reasoning": True, "mask_tool_args": True, "min_batch_tokens": 2000}

    def test_reasoning_and_args_masked_and_recallable(self):
        h = [SYSTEM, {"role": "user", "content": "scrivi i file"}]
        for k in range(1, 18):
            cid = "w%d" % k
            body = ("linea %d del sorgente\n" % k) * 300
            h += [{"role": "assistant", "content": "", "reasoning_content": ("ragiono sul file %d. " % k) * 200,
                   "tool_calls": [{"id": cid, "type": "function", "function": {
                       "name": "bash", "arguments": json.dumps({"command": "cat > f%d.c <<EOF\n%sEOF" % (k, body)})}}]},
                  {"role": "tool", "tool_call_id": cid, "content": "ok"}]
            self.send(h)
        self.assertTrue(self.events("mask"))
        last = self.eng.requests[-1]["messages"]
        # fix live 4/10: il ragionamento vecchio sparisce (nessun segnaposto imitabile); gli argomenti tengono le
        # chiavi piccole e nascondono solo i valori voluminosi
        th = [m for m in last if m.get("role") == "assistant" and m.get("tool_calls") and
              m.get("reasoning_content") == ""]
        self.assertFalse([m for m in last if "[ragionamento omesso:" in str(m.get("reasoning_content", ""))])
        ar = [m for m in last if m.get("role") == "assistant" and m.get("tool_calls") and
              "\u2026" in m["tool_calls"][0]["function"]["arguments"]]
        self.assertTrue(th and ar)
        # nessuna formula imitabile negli argomenti; la nota con recall sta nel risultato dello strumento
        self.assertNotIn("omess", ar[0]["tool_calls"][0]["function"]["arguments"])
        # la coppia chiamata/risultato resta integra (id e nome invariati)
        self.assertEqual(ar[0]["tool_calls"][0]["id"], "w1")
        self.assertEqual(ar[0]["tool_calls"][0]["function"]["name"], "bash")
        res = [m for m in last if m.get("role") == "tool" and m.get("tool_call_id") == "w1"][0]
        self.assertIn("nota del gestore del contesto", res["content"])
        # recall esatto degli argomenti originali e del ragionamento (archivio)
        rid = res["content"].split("recall id=")[1].split(".")[0]
        conv = self.events("request")[-1]["conv"]
        got = self.proxy.mgr.recall(conv, {"id": rid})
        self.assertIn("linea 1 del sorgente", got)
        got2 = self.proxy.mgr.recall(conv, {"query": "ragiono sul file 1."})
        self.assertIn("ragiono sul file 1.", got2)


if __name__ == "__main__":
    unittest.main()


class TestLiveDump(Base):
    cfg_over = {"live_dump": True, "mask_reasoning": True, "min_batch_tokens": 2000}

    def setUp(self):
        super().setUp()
        self.proxy.cfg.data_dir = self.tmp.name

    def test_last_request_written_atomically(self):
        path = os.path.join(self.tmp.name, "live", "last_request.json")
        h = history(40)
        r, _ = self.send(h)
        self.assertTrue(os.path.exists(path))
        with open(path) as f:
            d = json.load(f)
        self.assertEqual(d["conv"], r["strata_context"]["conversation_id"])
        self.assertEqual(d["n_messages"], len(d["messages"]))
        self.assertTrue(d["response"]["done"])
        self.assertEqual(d["response"]["content"], "ok")
        self.assertIn(RECALL_NAME, d["tools"])
        # il prompt fisico registrato è quello mascherato (segnaposto recall presenti)
        self.assertTrue(r["strata_context"]["masked"] > 0)
        self.assertTrue(any("recall id=" in (m.get("content") or "") for m in d["messages"]))
        self.assertEqual([x for x in os.listdir(os.path.dirname(path)) if x.endswith(".tmp")], [])
        req = self.events("request")[-1]
        self.assertEqual(req["virtual_tokens"], d["virtual_tokens"])
        self.assertIn("seg", req)

    def test_disabled_writes_nothing(self):
        self.proxy.cfg.live_dump = False
        self.send(history(2))
        self.assertFalse(os.path.exists(os.path.join(self.tmp.name, "live")))
