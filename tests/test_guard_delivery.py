"""Test della card t_419fc085: consegna delle chiamate di strumento nei tre punti corretti in 0.3.32.

1. `--mode off` è passaggio trasparente anche in streaming (nessuna trattenuta, nessuno scarto).
2. Ordine di consegna al client identico all'ordine del motore anche dopo una chiamata trattenuta,
   sulle tre API (Chat Completions, Messages, Responses) e in non-streaming.
3. Turno misto: la chiamata bloccata dalla guardia non svanisce; il modello riceve `guard_msg` al turno
   successivo, una volta sola, senza rilancio e senza azioni duplicate.

Motore finto HTTP reale (nessun modello, nessuna GPU), archivio in cartella temporanea.

    python3 -m unittest -v tests.test_guard_delivery
"""
from __future__ import annotations

import json
import os
import socket
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ctxproxy.core import RECALL_NAME, Config, Journal, Store, TokenCounter  # noqa: E402
from ctxproxy.paging import GUARD_MSG, GUARD_MSG_EN  # noqa: E402
from ctxproxy.server import Proxy, make_handler  # noqa: E402
from ctxproxy.upstream import Upstream  # noqa: E402
from tests.fake_engine import FakeEngine  # noqa: E402
from tests.test_proxy import assert_tool_pairs  # noqa: E402

# riga fatta di sole ellissi dentro un valore: il segno che `_CUT_LINE` tratta come ricevuta copiata
ELLIPSIS = "riga uno\n\u2026\nriga tre"
CONT_ARGS = json.dumps({"path": "a.py", "content": ELLIPSIS}, ensure_ascii=False)
CLEAN_ARGS = json.dumps({"path": "a.py", "content": "const a = 1;"}, ensure_ascii=False)
READ_ARGS = json.dumps({"path": "b.py"}, ensure_ascii=False)
SYSTEM = "sei un agente di programmazione"


def fcall(cid, name, args):
    return {"id": cid, "type": "function", "function": {"name": name, "arguments": args}}


def tool_spec(name, **props):
    return {"type": "function", "function": {"name": name, "description": name, "parameters": {
        "type": "object", "properties": {k: {"type": v} for k, v in props.items()}}}}


CC_TOOLS = [tool_spec("read", path="string"), tool_spec("write", path="string", content="string"),
            tool_spec("bash", command="string")]
A_TOOLS = [{"name": n, "description": n, "input_schema": {"type": "object", "properties": {}}}
           for n in ("read", "write", "bash")]
R_TOOLS = [{"type": "function", "name": n, "description": n,
            "parameters": {"type": "object", "properties": {}}} for n in ("read", "write", "bash")]

WRITE_CONT = fcall("w1", "write", CONT_ARGS)
WRITE_CLEAN = fcall("w1", "write", CLEAN_ARGS)
READ = fcall("r1", "read", READ_ARGS)
RECALL = fcall("rc1", RECALL_NAME, json.dumps({"query": "snake"}))


def _deltas_for(calls, split=False, indices=None):
    """Delta espliciti per il motore finto. `split`: nome e argomenti intercalati fra le chiamate."""
    idx = indices or list(range(len(calls)))
    out = [{"tool_calls": [{"index": i, "id": c["id"], "type": "function",
                            "function": {"name": c["function"]["name"], "arguments": ""}}]}
           for i, c in zip(idx, calls)]
    if not split:
        for i, c in zip(idx, calls):
            a = c["function"]["arguments"]
            out.append({"tool_calls": [{"index": i, "function": {"arguments": a}}]})
        return out
    # intercalazione: mezzi argomenti di ciascuna chiamata, alternati
    halves = [(c["function"]["arguments"][:7], c["function"]["arguments"][7:]) for c in calls]
    for k in (0, 1):
        for i, h in zip(idx, halves):
            out.append({"tool_calls": [{"index": i, "function": {"arguments": h[k]}}]})
    return out


def policy(body):
    """Risposta del motore finto, scelta dal marcatore nell'ultimo messaggio del client."""
    msgs = body.get("messages") or []
    last = msgs[-1] if msgs else {}
    if last.get("role") == "tool":
        return {"role": "assistant", "content": "fatto"}
    t = json.dumps(last.get("content"), ensure_ascii=False)
    if "OFF-CONT" in t:
        return {"role": "assistant", "content": "", "tool_calls": [WRITE_CONT]}
    if "OFF-RECALL" in t:
        return {"role": "assistant", "content": "", "tool_calls": [fcall("x1", RECALL_NAME, READ_ARGS)]}
    if "ORD-HR" in t:
        return {"role": "assistant", "content": "", "tool_calls": [WRITE_CLEAN, READ]}
    if "ORD-RH" in t:
        return {"role": "assistant", "content": "", "tool_calls": [READ, WRITE_CLEAN]}
    if "ORD-SPLIT" in t:
        return {"role": "assistant", "content": "", "tool_calls": [WRITE_CLEAN, READ],
                "_deltas": _deltas_for([WRITE_CLEAN, READ], split=True, indices=[0, 5])}
    if "ORD-INT" in t:
        return {"role": "assistant", "content": "", "tool_calls": [RECALL, WRITE_CLEAN, READ]}
    if "MIX" in t:
        return {"role": "assistant", "content": "", "reasoning_content": "scrivo e leggo",
                "tool_calls": [WRITE_CONT, READ]}
    return {"role": "assistant", "content": "ciao"}


class Base(unittest.TestCase):
    mode = "tools"
    cfg_over: dict = {}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.eng = FakeEngine(policy)
        url = self.eng.start()
        cfg = Config.from_dict({"window": 131072, **self.cfg_over})
        self.jpath = os.path.join(self.tmp.name, "j.jsonl")
        self.proxy = Proxy(cfg, Upstream(url), Store(os.path.join(self.tmp.name, "a.sqlite")),
                           Journal(self.jpath), TokenCounter(3.5), mode=self.mode)
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.proxy))
        self.srv.daemon_threads = True
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.base = "http://127.0.0.1:%d" % self.srv.server_address[1]

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        self.eng.stop()
        self.proxy.store.db.close()
        self.tmp.cleanup()

    def events(self, name=None):
        if not os.path.exists(self.jpath):
            return []
        with open(self.jpath) as f:
            ev = [json.loads(line) for line in f]
        return [e for e in ev if name is None or e["event"] == name]

    # ---------------------------------------------------------------- HTTP
    def post(self, path, obj):
        req = urllib.request.Request(self.base + path, json.dumps(obj).encode(), method="POST",
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    @staticmethod
    def sse(raw: bytes):
        out = []
        for block in raw.decode().split("\n\n"):
            ev, data = None, None
            for line in block.split("\n"):
                if line.startswith("event:"):
                    ev = line[6:].strip()
                elif line.startswith("data:"):
                    body = line[5:].strip()
                    if body and body != "[DONE]":
                        data = json.loads(body)
            if data is not None:
                out.append((ev, data))
        return out

    # ---------------------------------------------------------------- richieste per API
    def cc(self, text, stream, history=None):
        msgs = history or [{"role": "system", "content": SYSTEM}, {"role": "user", "content": text}]
        st, raw = self.post("/v1/chat/completions", {"model": "m", "messages": msgs, "tools": CC_TOOLS,
                                                     "max_tokens": 1024, "stream": bool(stream)})
        return st, raw

    def am(self, text, stream):
        st, raw = self.post("/v1/messages", {"model": "m", "max_tokens": 1024, "system": SYSTEM,
                                             "messages": [{"role": "user", "content": text}],
                                             "tools": A_TOOLS, "stream": bool(stream)})
        return st, raw

    def rp(self, text, stream):
        st, raw = self.post("/v1/responses", {"model": "m", "instructions": SYSTEM, "tools": R_TOOLS,
                                              "input": [{"role": "user", "content": [
                                                  {"type": "input_text", "text": text}]}],
                                              "stream": bool(stream)})
        return st, raw

    # ---------------------------------------------------------------- lettura delle chiamate consegnate
    def cc_calls(self, raw, stream):
        """-> [(nome, argomenti, indice del client, n. di delta con argomenti)] nell'ordine di consegna."""
        if not stream:
            r = json.loads(raw)
            msg = r["choices"][0]["message"]
            return [(c["function"]["name"], c["function"]["arguments"], i, 1)
                    for i, c in enumerate(msg.get("tool_calls") or [])], r["choices"][0].get("finish_reason")
        order, acc, fin = [], {}, None
        for _, d in self.sse(raw):
            ch = (d.get("choices") or [None])[0]
            if not ch:
                continue
            if ch.get("finish_reason"):
                fin = ch["finish_reason"]
            for tc in (ch.get("delta") or {}).get("tool_calls") or []:
                i = tc["index"]
                f = tc.get("function") or {}
                if i not in acc:
                    acc[i] = {"name": f.get("name") or "", "args": "", "n": 0}
                    order.append(i)
                if f.get("name"):
                    acc[i]["name"] = f["name"]
                if f.get("arguments"):
                    acc[i]["args"] += f["arguments"]
                    acc[i]["n"] += 1
        return [(acc[i]["name"], acc[i]["args"], i, acc[i]["n"]) for i in order], fin

    def am_calls(self, raw, stream):
        if not stream:
            r = json.loads(raw)
            calls = [(b["name"], json.dumps(b["input"], ensure_ascii=False), i, 1)
                     for i, b in enumerate(r.get("content") or []) if b.get("type") == "tool_use"]
            return calls, r.get("stop_reason")
        order, acc, blocks, stop = [], {}, {}, None
        for ev, d in self.sse(raw):
            if ev == "content_block_start" and (d.get("content_block") or {}).get("type") == "tool_use":
                i = d["index"]
                blocks[i] = {"name": d["content_block"]["name"], "args": "", "n": 0}
                order.append(i)
            elif ev == "content_block_delta" and (d.get("delta") or {}).get("type") == "input_json_delta":
                b = blocks.get(d["index"])
                if b is not None:
                    b["args"] += d["delta"]["partial_json"]
                    b["n"] += 1
            elif ev == "message_delta":
                stop = (d.get("delta") or {}).get("stop_reason")
        acc = blocks
        return [(acc[i]["name"], acc[i]["args"], i, acc[i]["n"]) for i in order], stop

    def rp_calls(self, raw, stream):
        if not stream:
            r = json.loads(raw)
            calls = [(it["name"], it.get("arguments") or it.get("input") or "", i, 1)
                     for i, it in enumerate(r.get("output") or [])
                     if it.get("type") in ("function_call", "custom_tool_call")]
            return calls, r.get("status")
        order, items, status = [], {}, None
        for ev, d in self.sse(raw):
            if ev == "response.output_item.added" and (d.get("item") or {}).get("type") == "function_call":
                oi = d["output_index"]
                items[oi] = {"name": d["item"]["name"], "args": "", "n": 0}
                order.append(oi)
            elif ev == "response.function_call_arguments.delta":
                it = items.get(d["output_index"])
                if it is not None:
                    it["args"] += d["delta"]
                    it["n"] += 1
            elif ev in ("response.completed", "response.incomplete"):
                status = (d.get("response") or {}).get("status")
        return [(items[i]["name"], items[i]["args"], i, items[i]["n"]) for i in order], status

    def names(self, calls):
        return [c[0] for c in calls]


# ====================================================================== 1. --mode off
class TestOffPassThrough(Base):
    """`off` = passaggio trasparente documentato (docs/configuration.md:329, server.py:208/1535):
    lo streaming non trattiene e non scarta nulla, e dà lo stesso risultato del non-streaming."""
    mode = "off"

    def test_contaminated_write_passes_both_paths(self):
        calls_ns, fin_ns = self.cc_calls(self.cc("OFF-CONT", False)[1], False)
        calls_st, fin_st = self.cc_calls(self.cc("OFF-CONT", True)[1], True)
        self.assertEqual(self.names(calls_ns), ["write"])
        self.assertEqual(self.names(calls_st), ["write"], "in off lo streaming non deve trattenere nulla")
        self.assertEqual(calls_st[0][1], CONT_ARGS, "argomenti trasmessi invariati, riga di ellissi compresa")
        self.assertEqual(json.loads(calls_st[0][1])["content"], ELLIPSIS)
        self.assertEqual(calls_ns[0][1], calls_st[0][1])
        self.assertEqual((fin_ns, fin_st), ("tool_calls", "tool_calls"))
        self.assertEqual(self.events("receipt_guard"), [], "in off il proxy non aggiunge guardie")
        self.assertEqual(self.events("recall_dropped"), [])

    def test_internal_name_from_client_passes(self):
        """In `off` nessun ciclo interno esiste: una chiamata del client con il nome dello strumento interno
        non ha nessun consumatore e deve arrivare al client come qualunque altra."""
        calls_ns, _ = self.cc_calls(self.cc("OFF-RECALL", False)[1], False)
        calls_st, _ = self.cc_calls(self.cc("OFF-RECALL", True)[1], True)
        self.assertEqual(self.names(calls_ns), [RECALL_NAME])
        self.assertEqual(self.names(calls_st), [RECALL_NAME])

    def test_native_apis_off(self):
        for getter, reader in ((self.am, self.am_calls), (self.rp, self.rp_calls)):
            with self.subTest(api=getter.__name__):
                ns, _ = reader(getter("OFF-CONT", False)[1], False)
                st, _ = reader(getter("OFF-CONT", True)[1], True)
                self.assertEqual(self.names(ns), ["write"])
                self.assertEqual(self.names(st), ["write"])
                self.assertEqual(json.loads(st[0][1])["content"], ELLIPSIS)

    def test_engine_error_surfaces_in_stream(self):
        self.eng.strict_window = True
        self.eng.counter = lambda b: 10 ** 9
        st, raw = self.cc("OFF-CONT", True)
        self.assertEqual(st, 200)
        errs = [d for _, d in self.sse(raw) if d.get("error")]
        self.assertTrue(errs, "un errore del motore dopo l'header SSE deve arrivare come evento di errore")


# ====================================================================== 2. ordine di consegna
class TestDeliveryOrder(Base):
    """L'ordine delle chiamate consegnate al client è quello del motore, su tutte e tre le API e in
    entrambi i percorsi, anche quando la prima è trattenuta dalla guardia."""

    def check_order(self, marker, expected):
        for getter, reader, api in ((self.cc, self.cc_calls, "chat"), (self.am, self.am_calls, "messages"),
                                    (self.rp, self.rp_calls, "responses")):
            with self.subTest(api=api, marker=marker):
                ns, _ = reader(getter(marker, False)[1], False)
                st, _ = reader(getter(marker, True)[1], True)
                self.assertEqual(self.names(ns), expected)
                self.assertEqual(self.names(st), expected,
                                 "ordine di consegna diverso dall'ordine del motore in streaming")
                want = {"write": json.loads(CLEAN_ARGS), "read": json.loads(READ_ARGS)}
                for calls in (ns, st):
                    for name, a, _, _ in calls:
                        self.assertEqual(json.loads(a) if isinstance(a, str) else a, want[name],
                                         "argomenti corrotti: %s" % name)

    def test_held_then_client_call(self):
        """[write pulita (trattenuta), read] -> il client riceve [write, read]."""
        self.check_order("ORD-HR", ["write", "read"])

    def test_client_call_then_held(self):
        """[read, write pulita] -> invariato rispetto a 0.3.31."""
        self.check_order("ORD-RH", ["read", "write"])

    def test_split_and_interleaved_deltas(self):
        """Nomi e argomenti intercalati, con buchi negli indici del motore (0 e 5)."""
        self.check_order("ORD-SPLIT", ["write", "read"])

    def test_internal_call_in_the_round(self):
        """[recall (interna), write pulita, read]: la recall non arriva al client, l'ordine delle altre due sì."""
        self.check_order("ORD-INT", ["write", "read"])
        self.assertTrue(self.events("recall_dropped"))

    def test_client_indices_contiguous_and_whole(self):
        """Gli indici consegnati al client partono da 0 senza buchi; le chiamate dopo una trattenuta
        arrivano intere (costo dichiarato: perdono lo streaming incrementale)."""
        calls, fin = self.cc_calls(self.cc("ORD-SPLIT", True)[1], True)
        self.assertEqual([c[2] for c in calls], [0, 1])
        self.assertEqual([c[3] for c in calls], [1, 1])
        self.assertEqual(fin, "tool_calls")

    def test_contaminated_first_then_client_call(self):
        """La trattenuta contaminata non esce; la chiamata successiva esce comunque, all'indice 0."""
        calls, fin = self.cc_calls(self.cc("MIX", True)[1], True)
        self.assertEqual(self.names(calls), ["read"])
        self.assertEqual([c[2] for c in calls], [0])
        self.assertEqual(fin, "tool_calls")
        self.assertNotIn(ELLIPSIS, calls[0][1])


# ====================================================================== 3. turno misto
class TestMixedTurnFeedback(Base):
    """Turno misto [write contaminata, read]: una sola generazione, `read` una volta sola, `write` mai al
    client, un evento `receipt_guard`, e al turno successivo `guard_msg` una volta sola con ID accoppiato."""

    def history_turn2(self, text="MIX"):
        return [{"role": "system", "content": SYSTEM}, {"role": "user", "content": text},
                {"role": "assistant", "content": "", "tool_calls": [READ]},
                {"role": "tool", "tool_call_id": "r1", "content": "contenuto di b.py"}]

    def run_turn(self, stream):
        n0 = len(self.eng.requests)
        calls, fin = self.cc_calls(self.cc("MIX", stream)[1], stream)
        self.assertEqual(len(self.eng.requests) - n0, 1, "una sola generazione del motore nel turno")
        self.assertEqual(self.names(calls), ["read"])
        self.assertEqual(fin, "tool_calls")
        guards = self.events("receipt_guard")
        self.assertEqual(len(guards), 1, "la chiamata bloccata deve lasciare un evento receipt_guard")
        self.assertEqual(guards[0]["name"], "write")
        self.assertTrue(guards[0].get("delivered"), "il messaggio di guardia è nel gruppo interno")
        # turno successivo: il feedback al modello, una volta sola, accoppiato
        self.cc(None, stream, history=self.history_turn2())
        phys = self.eng.requests[-1]["messages"]
        txt = json.dumps(phys, ensure_ascii=False)
        self.assertEqual(txt.count(GUARD_MSG[:60]) + txt.count(GUARD_MSG_EN[:60]), 1,
                         "il messaggio di guardia deve comparire una volta sola")
        gm = [m for m in phys if m.get("role") == "tool" and str(m.get("content", "")).startswith(
            (GUARD_MSG[:40], GUARD_MSG_EN[:40]))]
        self.assertEqual(len(gm), 1)
        self.assertEqual(gm[0]["tool_call_id"], "w1")
        owner = [m for m in phys if m.get("role") == "assistant"
                 and any(c.get("id") == "w1" for c in m.get("tool_calls") or [])]
        self.assertEqual(len(owner), 1, "la chiamata bloccata deve stare in un assistant interno")
        self.assertNotIn(ELLIPSIS, json.dumps([m for m in phys if m.get("role") == "tool"],
                                              ensure_ascii=False))
        assert_tool_pairs(self, phys)
        # nessuna azione duplicata: il client non ha mai visto la write, e non la rivede
        self.assertEqual(len(self.events("receipt_guard")), 1)
        self.assertEqual(sum(1 for e in self.events("request") if e.get("round") == 0), 2)

    def test_mixed_turn_stream(self):
        self.run_turn(True)

    def test_mixed_turn_non_stream(self):
        self.run_turn(False)

    def test_blocked_call_never_reaches_the_client(self):
        for stream in (False, True):
            with self.subTest(stream=stream):
                st, raw = self.cc("MIX", stream)
                self.assertEqual(st, 200)
                self.assertNotIn("w1", raw.decode())
                self.assertNotIn("\u2026\n", raw.decode())


# ====================================================================== disconnessione del client
class TestClientGone(Base):
    def test_disconnect_with_buffered_calls(self):
        """Il client chiude mentre il giro ha chiamate bufferizzate: il proxy lo registra, chiude lo stream
        verso il motore e resta utilizzabile."""
        body = json.dumps({"model": "m", "messages": [{"role": "system", "content": SYSTEM},
                                                      {"role": "user", "content": "ORD-HR"}],
                           "tools": CC_TOOLS, "stream": True}).encode()
        s = socket.create_connection(self.srv.server_address)
        s.sendall(b"POST /v1/chat/completions HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\n"
                  b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body)
        s.recv(256)
        s.close()
        deadline = time.time() + 10
        while time.time() < deadline and not self.events("client_disconnect"):
            time.sleep(0.1)
        calls, _ = self.cc_calls(self.cc("ORD-HR", True)[1], True)
        self.assertEqual(self.names(calls), ["write", "read"])


if __name__ == "__main__":
    unittest.main()
