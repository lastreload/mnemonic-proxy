"""Test della card t_00c5aef6: strumenti su richiesta, ricevute tipizzate, richiamo automatico bm25, token di cache
invalidati, rapporto S/Δ dei blocchi.

    python3 -m unittest -v tests.test_paging
"""
from __future__ import annotations

import json
import os
import sys
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ctxproxy.core import AUTO_SUFFIX, OUT_PREFIX, RECEIPT_OPEN, RECALL_NAME, Config, Journal, Manager, Store, TokenCounter  # noqa
from ctxproxy.paging import (AUTO_HEAD, TOOLS_HEAD, TOOLS_NAME, ToolPager, query_terms, receipt_kind,  # noqa: E402
                             typed_receipt)
from ctxproxy.render import render_pieces  # noqa: E402
from ctxproxy.server import make_handler  # noqa: E402
from tests.test_proxy import Base, assert_tool_pairs, history, tool_round  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
TOKDIR = os.environ.get("CTX_TOKDIR", os.path.join(HERE, "../../cases/nibble2/tok"))
SYSTEM = {"role": "system", "content": "sei un agente di programmazione"}


def tool(name, desc="", **props):
    return {"type": "function", "function": {"name": name, "description": desc or name,
                                             "parameters": {"type": "object", "properties": {
                                                 k: {"type": v} for k, v in props.items()}}}}


PI_TOOLS = [tool("read", path="string"), tool("bash", command="string"), tool("edit", path="string"),
            tool("write", path="string", content="string"), tool("todo", "Gestisce la lista dei compiti", action="string"),
            tool("bg_run", "Avvia un processo in background", command="string"),
            tool("bg_logs", "Legge i log di un processo in background", id="string"),
            tool("github_list_issues", "Elenca le issue di un repository", repo="string"),
            tool("github_search_code", "Cerca codice su GitHub", q="string"),
            tool("web_fetch", "Scarica una pagina web", url="string")]


def call(cid, name, **args):
    return {"id": cid, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


# ------------------------------------------------------------------ ricevute tipizzate
class TestReceipts(unittest.TestCase):
    def info(self, name, **args):
        return {"name": name, "args": args, "path": args.get("path", ""), "cmd": args.get("command", "")}

    def test_kinds(self):
        I = self.info
        self.assertEqual(receipt_kind(I("write", path="a.js", content="x"), "Successfully wrote 1 bytes", False),
                         "scrittura")
        self.assertEqual(receipt_kind(I("edit", path="a.js"), "Successfully replaced 1 block(s)", False), "modifica")
        self.assertEqual(receipt_kind(I("read", path="a.js"), "riga\nriga", False), "lettura")
        self.assertEqual(receipt_kind(I("bash", command="ls -la"), "a\nb\nc", False), "shell_ok")
        self.assertEqual(receipt_kind(I("bash", command="node x.mjs"), "TypeError: boom\n\nCommand exited with code 1",
                                      True), "shell_errore")
        self.assertEqual(receipt_kind(I("bash", command="npm test"), "# pass 12\n# fail 0", False), "test")
        self.assertEqual(receipt_kind(I("bash", command="python3 -m pytest -q"),
                                      "FAILED tests/t.py::test_a\n1 failed, 3 passed", True), "test")
        # un "Error" nel mezzo dell'uscita di un grep riuscito non è un fallimento
        self.assertEqual(receipt_kind(I("bash", command="grep -n Error src/*.js"),
                                      "src/a.js:3: throw new Error('x')\nsrc/b.js:9: Error", False), "shell_ok")

    def test_texts(self):
        I = self.info
        k, r = typed_receipt(I("write", path="a.js", content="ciao"), "Successfully wrote 4 bytes", False, "aabc")
        self.assertIn("scrittura: riuscito, 4 byte, sha256", r)
        self.assertIn("recall id=aabc", r)
        k, r = typed_receipt(I("edit", path="a.js", edits=[{}, {}]), "Error: oldText not found in the current file",
                             True)
        self.assertIn("FALLITO", r)
        self.assertIn("2 blocchi", r)
        self.assertIn("oldText not found", r)
        k, r = typed_receipt(I("bash", command="node x.mjs"),
                             "a\nb\nTypeError: x is undefined\n    at f (x.mjs:3)\n\nCommand exited with code 1", True)
        self.assertEqual(k, "shell_errore")
        self.assertIn("codice d'uscita 1", r)
        self.assertIn("TypeError: x is undefined", r)
        k, r = typed_receipt(I("bash", command="pytest"), "FAILED t.py::test_a - assert\nFAILED t.py::test_b\n"
                             "2 failed, 40 passed in 1.2s\n\nCommand exited with code 1", True)
        self.assertEqual(k, "test")
        self.assertIn("passati 40", r)
        self.assertIn("falliti 2", r)
        self.assertIn("t.py::test_a", r)
        self.assertIn("codice d'uscita 1", r)
        k, r = typed_receipt(I("read", path="a.js", offset=10, limit=5), "1\n2\n3\n4\n5", False)
        self.assertIn("righe 10\u201314", r)
        self.assertIn("5 righe", r)
        k, r = typed_receipt(I("bash", command="ls"), "a\nb", False)
        self.assertEqual(r, "comando riuscito: codice d'uscita 0, 2 righe di uscita. Ultima riga: \u00abb\u00bb")

    def test_deterministic(self):
        I = self.info
        a = typed_receipt(I("bash", command="npm test"), "# pass 3\n# fail 1\nnot ok 4 - collisione", True)
        b = typed_receipt(I("bash", command="npm test"), "# pass 3\n# fail 1\nnot ok 4 - collisione", True)
        self.assertEqual(a, b)
        self.assertIn("collisione", a[1])

    def test_error_dedup(self):
        k, r = typed_receipt(self.info("bash", command="node x.mjs"),
                             "Error: ENOENT a.png\n" * 50 + "Error: boom\n\nCommand exited with code 1", True)
        self.assertEqual(r.count("ENOENT"), 1)
        self.assertIn("(\u00d750)", r)
        self.assertIn("boom", r)

    def test_guard(self):
        from ctxproxy.paging import receipt_contaminated
        rec = OUT_RECEIPT_EX
        self.assertTrue(receipt_contaminated("write", json.dumps({"path": "a.js", "content": "x\n" + rec})))
        self.assertTrue(receipt_contaminated("edit", {"path": "a.js", "newText": "nascosta per spazio (900 token)"}))
        # anche se gli argomenti arrivano con escape \\uXXXX
        self.assertTrue(receipt_contaminated("bash", json.dumps({"command": "echo '%s'" % rec}, ensure_ascii=True)))
        self.assertFalse(receipt_contaminated("read", json.dumps({"path": rec})))
        self.assertFalse(receipt_contaminated("write", json.dumps({"path": "a.js", "content": "const a = 1;"})))
        self.assertFalse(receipt_contaminated("bash", json.dumps({"command": "grep -rn ctx-archive src"})))


OUT_RECEIPT_EX = ("\u27eactx-archive id=r0123456789ab \u00b7 uscita di read src/a.js \u00b7 questo agente \u00b7 nascosta "
                  "per spazio (900 token)\u27eb Ricevuta: lettura: file intero")


class TestGuardProxy(Base):
    """Il modello copia una ricevuta in una write: il proxy la blocca, risponde con l'errore, il modello rilegge."""

    def policy(self, body):
        last = body["messages"][-1]
        if last.get("role") == "user" and "scrivi" in str(last.get("content")):
            return {"role": "assistant", "content": "", "tool_calls": [
                call("w1", "write", path="a.js", content="// " + OUT_RECEIPT_EX)]}
        if last.get("role") == "tool" and last.get("tool_call_id") == "w1":
            assert "chiamata NON eseguita" in last["content"]
            return {"role": "assistant", "content": "", "tool_calls": [call("r1", "read", path="a.js")]}
        return {"role": "assistant", "content": "ok"}

    def test_blocks_and_counts(self):
        r, _ = self.send([SYSTEM, {"role": "user", "content": "scrivi il file"}])
        m = r["choices"][0]["message"]
        self.assertEqual([c["function"]["name"] for c in m["tool_calls"]], ["read"])
        ev = self.events("receipt_guard")
        self.assertEqual(len(ev), 1)
        self.assertEqual(ev[0]["name"], "write")


class TestReceiptsInManager(unittest.TestCase):
    def mk(self, **over):
        import tempfile
        cfg = Config.from_dict({"window": 131072, "mask_trigger": 4000, "mask_target": 2000, "keep_recent_tokens": 800,
                                "min_mask_tokens": 100, "min_batch_tokens": 200, **over})
        self.tmp = tempfile.TemporaryDirectory()
        return Manager(cfg, Store(os.path.join(self.tmp.name, "a.sqlite")), TokenCounter(3.5),
                       Journal(os.path.join(self.tmp.name, "j.jsonl")))

    def session(self):
        msgs = [SYSTEM, {"role": "user", "content": "fai il gioco"}]
        for k in range(10):
            if k % 3 == 0:
                msgs.append({"role": "assistant", "content": "", "tool_calls": [call("t%d" % k, "bash",
                                                                                     command="npm test")]})
                msgs.append({"role": "tool", "tool_call_id": "t%d" % k,
                             "content": "x\n" * 900 + "# pass %d\n# fail 1\nnot ok 3 - urto%d\n" % (k, k)})
            elif k % 3 == 1:
                msgs.append({"role": "assistant", "content": "", "tool_calls": [call("r%d" % k, "read",
                                                                                     path="src/a%d.js" % k)]})
                msgs.append({"role": "tool", "tool_call_id": "r%d" % k, "content": "codice\n" * 600})
            else:
                msgs.append({"role": "assistant", "content": "", "tool_calls": [call("b%d" % k, "bash",
                                                                                     command="node run.mjs")]})
                msgs.append({"role": "tool", "tool_call_id": "b%d" % k,
                             "content": "log\n" * 700 + "ReferenceError: zz is not defined\n\nCommand exited with code 1"})
            msgs.append({"role": "assistant", "content": "fatto %d" % k})
            msgs.append({"role": "user", "content": "continua %d" % k})
        return msgs

    def test_receipts_in_tool_results_and_stable(self):
        mgr = self.mk()
        msgs = self.session()
        p1 = mgr.prepare({"messages": msgs, "max_tokens": 100})
        ph = [m["content"] for m in p1.messages if m.get("role") == "tool" and (m["content"].startswith(OUT_PREFIX) or m["content"].startswith(RECEIPT_OPEN))]
        self.assertTrue(ph)
        self.assertTrue(any("Ricevuta: test:" in x and "urto0" in x for x in ph), ph)
        self.assertTrue(any("Ricevuta: lettura:" in x for x in ph), ph)
        self.assertTrue(any("comando FALLITO: codice d'uscita 1" in x and "ReferenceError" in x for x in ph), ph)
        for m in p1.messages:   # mai ricevute dove scrive il modello
            if m.get("role") == "assistant":
                self.assertNotIn("Ricevuta", json.dumps(m))
        p2 = mgr.prepare({"messages": msgs + [{"role": "assistant", "content": "ok"}, {"role": "user", "content": "e"}],
                          "max_tokens": 100})
        n = len(p1.messages)
        self.assertEqual(p1.messages, p2.messages[:n])   # stesso testo per lo stesso pezzo
        self.tmp.cleanup()

    def test_typed_receipts_off_keeps_old_format(self):
        mgr = self.mk(typed_receipts=False)
        p = mgr.prepare({"messages": self.session(), "max_tokens": 100})
        ph = [m["content"] for m in p.messages if m.get("role") == "tool" and (m["content"].startswith(OUT_PREFIX) or m["content"].startswith(RECEIPT_OPEN))]
        self.assertTrue(ph and all("Inizio:" in x for x in ph))
        self.tmp.cleanup()


# ------------------------------------------------------------------ strumenti su richiesta
class TestPager(unittest.TestCase):
    def test_split_and_search(self):
        pg = ToolPager(("read", "bash", "edit", "write", RECALL_NAME, TOOLS_NAME))
        base, cat = pg.split(PI_TOOLS)
        self.assertEqual([t["function"]["name"] for t in base], ["read", "bash", "edit", "write"])
        self.assertEqual(set(cat), {"todo", "bg_run", "bg_logs", "github_list_issues", "github_search_code",
                                    "web_fetch"})
        self.assertEqual(pg.search(cat, "", ["todo"]), ["todo"])
        self.assertEqual(pg.search(cat, "lista todo", ["todo"]), ["todo"])          # per nome: niente extra
        self.assertEqual(pg.search(cat, "", ["todo", "bg_run"]), ["todo", "bg_run"])
        self.assertIn("todo", pg.search(cat, "", ["todo", "inesistente_xyz"]))       # un nome sbagliato: ricerca
        self.assertEqual(set(pg.search(cat, "processo in background")[:2]), {"bg_run", "bg_logs"})
        self.assertEqual(pg.search(cat, "github issue")[0], "github_list_issues")
        self.assertEqual(pg.search(cat, "zzzz"), [])
        t1, t2 = pg.tools_tool(cat), pg.tools_tool(dict(reversed(list(cat.items()))))
        self.assertEqual(json.dumps(t1), json.dumps(t2))   # stabile, indipendente dall'ordine
        d = t1["function"]["description"]
        for n in cat:
            self.assertIn(n, d)

    def test_result_and_loaded(self):
        pg = ToolPager(())
        _, cat = pg.split(PI_TOOLS)
        out = pg.result(cat, ["todo", "bg_run"], set())
        self.assertTrue(out.startswith(TOOLS_HEAD + "todo, bg_run]"))
        self.assertIn('"name": "todo"', out)
        phys = [{"role": "tool", "tool_call_id": "x", "content": out}]
        self.assertEqual(pg.loaded_in(phys), {"todo", "bg_run"})
        out2 = pg.result(cat, ["todo"], {"todo"})
        self.assertIn("Già caricati", out2)
        self.assertNotIn('"name": "todo"', out2)


class TestPagingProxy(Base):
    """Proxy + motore finto: il modello cerca uno strumento, lo carica, lo chiama; chiamata diretta a uno strumento
    non caricato; prefisso stabile."""
    cfg_over = {"tools_paging": True, "tools_core": ["read", "bash", "edit", "write"]}

    def policy(self, body):
        msgs = body["messages"]
        last = msgs[-1]
        names = [t["function"]["name"] for t in body.get("tools") or []]
        self.seen_tools.append(names)
        txt = json.dumps(last.get("content"), ensure_ascii=False)
        if last.get("role") == "user" and "lista dei compiti" in txt:
            return {"role": "assistant", "content": "", "tool_calls": [call("s1", TOOLS_NAME, query="todo compiti")]}
        if last.get("role") == "tool" and last.get("tool_call_id") == "s1":
            assert '"name": "todo"' in last["content"], last["content"]
            return {"role": "assistant", "content": "", "tool_calls": [call("c1", "todo", action="list")]}
        if last.get("role") == "user" and "scrivi" in txt:
            return {"role": "assistant", "content": "", "tool_calls": [
                call("w1", "write", path="a.js", content="// " + OUT_RECEIPT_EX)]}
        if last.get("role") == "tool" and last.get("tool_call_id") == "w1":
            return {"role": "assistant", "content": "", "tool_calls": [call("r1", "read", path="a.js")]}
        if last.get("role") == "user" and "log" in txt:
            # chiamata diretta a uno strumento mai caricato
            return {"role": "assistant", "content": "", "tool_calls": [call("d1", "bg_logs", id="7")]}
        if last.get("role") == "tool" and last.get("tool_call_id") == "d1":
            assert "NON è stata eseguita" in last["content"]
            return {"role": "assistant", "content": "", "tool_calls": [call("d2", "bg_logs", id="7")]}
        return {"role": "assistant", "content": "ok"}

    def setUp(self):
        self.seen_tools = []
        super().setUp()

    def send(self, msgs, **kw):
        st, r, hdr = self.proxy.chat({"model": "m", "messages": msgs, "tools": PI_TOOLS, "max_tokens": 1024, **kw})
        self.assertEqual(st, 200, r)
        return r, hdr

    def test_search_load_call(self):
        h = [SYSTEM, {"role": "user", "content": "mostrami la lista dei compiti"}]
        r, _ = self.send(h)
        m = r["choices"][0]["message"]
        self.assertEqual([c["function"]["name"] for c in m["tool_calls"]], ["todo"])   # al client solo la sua
        self.assertEqual(r["choices"][0]["finish_reason"], "tool_calls")
        # elenco strumenti sempre identico: base + strata_recall + strata_tools
        for names in self.seen_tools:
            self.assertEqual(names, ["read", "bash", "edit", "write", RECALL_NAME, TOOLS_NAME])
        ev = self.events("tools_search")
        self.assertEqual(ev[0]["found"][0], "todo")
        # turno dopo: il risultato di strata_tools è reinserito identico, prefisso stabile
        n_before = len(self.eng.requests[-1]["messages"])
        h2 = h + [m, {"role": "tool", "tool_call_id": "c1", "content": "nessun compito"}]
        self.send(h2)
        prev, cur = self.eng.requests[-2]["messages"], self.eng.requests[-1]["messages"]
        self.assertEqual(prev[:n_before], cur[:n_before])
        self.assertEqual(self.eng.requests[-2]["tools"], self.eng.requests[-1]["tools"])
        self.assertTrue(any(x.get("role") == "tool" and str(x.get("content")).startswith(TOOLS_HEAD) for x in cur))
        assert_tool_pairs(self, cur)
        inv = [e for e in self.events("request") if e.get("round") == 0][-1]
        self.assertEqual(inv["invalidated_suffix_tokens"], 0)

    def test_direct_call_not_loaded(self):
        h = [SYSTEM, {"role": "user", "content": "guarda il log del processo 7"}]
        r, _ = self.send(h)
        m = r["choices"][0]["message"]
        self.assertEqual([c["function"]["name"] for c in m["tool_calls"]], ["bg_logs"])
        self.assertEqual(m["tool_calls"][0]["id"], "d2")         # la seconda, dopo il caricamento
        ev = self.events("tool_not_loaded")
        self.assertEqual(len(ev), 1)
        self.assertEqual(ev[0]["name"], "bg_logs")

    def test_off_by_default(self):
        self.assertFalse(Config().tools_paging)
        self.assertFalse(Config().auto_recall)


class TestPagingStream(TestPagingProxy):
    def setUp(self):
        super().setUp()
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.proxy))
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.base = "http://127.0.0.1:%d" % self.srv.server_address[1]

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        super().tearDown()

    def test_stream_hides_internal_calls(self):
        body = json.dumps({"model": "m", "messages": [SYSTEM, {"role": "user", "content": "la lista dei compiti"}],
                           "tools": PI_TOOLS, "stream": True}).encode()
        req = urllib.request.Request(self.base + "/v1/chat/completions", data=body,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req) as r:
            lines = [l for l in r.read().decode().split("\n") if l.startswith("data: ")]
        chunks = [json.loads(l[6:]) for l in lines[:-1]]
        names = [(tc.get("function") or {}).get("name") for c in chunks if c.get("choices")
                 for tc in c["choices"][0].get("delta", {}).get("tool_calls") or []]
        self.assertEqual([n for n in names if n], ["todo"])

    def test_stream_guard_holds_effect_calls(self):
        """In streaming le chiamate write/edit/bash pulite arrivano intere a fine giro; quelle contaminate mai."""
        self.policy_override = True
        body = json.dumps({"model": "m", "messages": [SYSTEM, {"role": "user", "content": "scrivi il file"}],
                           "tools": PI_TOOLS, "stream": True}).encode()
        req = urllib.request.Request(self.base + "/v1/chat/completions", data=body,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req) as r:
            lines = [l for l in r.read().decode().split("\n") if l.startswith("data: ")]
        chunks = [json.loads(l[6:]) for l in lines[:-1]]
        tcs = [tc for c in chunks if c.get("choices") for tc in c["choices"][0].get("delta", {}).get("tool_calls") or []]
        self.assertEqual([(tc["function"].get("name"), tc["index"]) for tc in tcs if tc["function"].get("name")],
                         [("read", 0)])
        self.assertNotIn("ctx-archive", json.dumps(tcs, ensure_ascii=False))
        self.assertEqual(len(self.events("receipt_guard")), 1)

    test_search_load_call = test_direct_call_not_loaded = test_off_by_default = None


@unittest.skipUnless(os.path.exists(os.path.join(TOKDIR, "vocab.json")), "tokenizer del pack non disponibile")
class TestTemplatePlacement(unittest.TestCase):
    """Verifica sul template vero (render.py = chat_template.jinja): cambiare l'elenco strumenti riscrive dal primo
    token; un risultato di strata_tools in coda lascia identico tutto il prefisso."""

    def test_where_definitions_can_go(self):
        msgs = [SYSTEM, {"role": "user", "content": "ciao"}, {"role": "assistant", "content": "ok"},
                {"role": "user", "content": "avanti"}]
        a = "".join(t for _, t in render_pieces(msgs, PI_TOOLS[:4], {}))
        b = "".join(t for _, t in render_pieces(msgs, PI_TOOLS[:5], {}))
        k = next(i for i, (x, y) in enumerate(zip(a, b)) if x != y)
        self.assertLess(k, 2000)            # il blocco <tools> è in testa: cambia quasi subito
        tail = msgs + [{"role": "assistant", "content": "", "tool_calls": [call("s", TOOLS_NAME, query="todo")]},
                       {"role": "tool", "tool_call_id": "s", "content": TOOLS_HEAD + "todo]\n{}"}]
        c = "".join(t for _, t in render_pieces(tail, PI_TOOLS[:4], {}))
        self.assertTrue(c.startswith(a))


# ------------------------------------------------------------------ richiamo automatico
class TestAutoRecall(Base):
    cfg_over = {"auto_recall": True, "auto_recall_min_score": 0.5, "auto_recall_min_terms": 1,
                "mask_trigger": 20000, "mask_target": 9000, "keep_recent_tokens": 3000, "min_batch_tokens": 1000}

    def test_injects_hidden_pieces_once_and_stably(self):
        h = history(1)
        h[2]["tool_calls"][0]["function"]["arguments"] = json.dumps({"command": "cat config.txt"})
        h[3]["content"] = "SEED_MAGICO=27013482\n" + "riga di config\n" * 150
        for k in range(1, 40):
            h += tool_round(k, 3500)
        h += [{"role": "assistant", "content": "fatto"}, {"role": "user", "content": "qual era SEED_MAGICO?"}]
        self.send(h)      # il pacchetto di masking avviene qui: il pezzo diventa nascosto e torna in coda
        self.assertTrue(self.events("mask"))
        ar = self.events("auto_recall")
        self.assertTrue(ar[-1]["injected"] >= 1, ar[-1])
        phys = self.eng.requests[-1]["messages"]
        inj = [m for m in phys if str(m.get("content")).startswith(AUTO_HEAD)]
        self.assertEqual(len(inj), 1)
        self.assertIn("27013482", inj[0]["content"])
        self.assertTrue(str(phys[-1]["content"]).startswith(AUTO_HEAD))   # in coda, dopo la richiesta
        # stessa storia + risposta: l'iniezione è reinserita identica, non ricalcolata, e non duplicata
        n = len(phys)
        h += [{"role": "assistant", "content": "non so"}, {"role": "user", "content": "il valore di SEED_MAGICO?"}]
        self.send(h)
        self.assertEqual(self.eng.requests[-1]["messages"][:n], phys)
        self.assertEqual(len([e for e in self.events("auto_recall") if e["index"] == ar[-1]["index"]]), 1)
        self.assertEqual(self.events("auto_recall")[-1]["injected"], 0)   # già presente: non ripetuto
        assert_tool_pairs(self, self.eng.requests[-1]["messages"])

    def test_dedup_and_max_args(self):
        """Comandi identici ripetuti: un solo pezzo per testo, e al massimo auto_recall_max_args argomenti."""
        h = history(1)
        for k in range(1, 40):
            h += tool_round(k, 3500)
            h[-2]["tool_calls"][0]["function"]["arguments"] = json.dumps({"command": "cat SEED_MAGICO config"})
        h += [{"role": "assistant", "content": "fatto"}, {"role": "user", "content": "qual era SEED_MAGICO config?"}]
        self.send(h)
        ar = self.events("auto_recall")[-1]
        args = [p for p in ar["pieces"] if p["role"] == "assistant-tool-args"]
        self.assertLessEqual(len(args), 1, ar)

    def test_only_hidden_pieces(self):
        h = history(1) + [{"role": "assistant", "content": "x"},
                          {"role": "user", "content": "riga 0 del file f0.txt"}]
        self.send(h)                         # niente nascosto: nessuna iniezione
        ar = self.events("auto_recall")
        self.assertEqual(ar[-1]["injected"], 0)
        self.assertFalse(any(str(m.get("content")).startswith(AUTO_HEAD) for m in self.eng.requests[-1]["messages"]))

    def test_query_terms(self):
        t = query_terms("perché drawFloorCell fallisce in render.js?", ["src/game.js"],
                        ["TypeError: cannot read tileSize"], "node tools/sim.mjs")
        self.assertEqual(t[:2], ["drawfloorcell", "render"])
        self.assertIn("game", t)
        self.assertIn("typeerror", t)
        self.assertNotIn("perché", t)


# ------------------------------------------------------------------ invalidazione e S/Δ
class TestInvalidation(Base):
    cfg_over = {"mask_trigger": 20000, "mask_target": 9000, "keep_recent_tokens": 3000, "min_batch_tokens": 1000}

    def test_invalidated_tokens_and_ratio(self):
        h = history(1)
        for k in range(1, 40):
            h += tool_round(k, 3500)
            self.send(h)
        reqs = [e for e in self.events("request") if e.get("round") == 0]
        masks = self.events("mask")
        self.assertTrue(masks)
        causes = {r["invalidated_cause"] for r in reqs if r["invalidated_suffix_tokens"]}
        self.assertEqual(causes, {"blocco"})
        # solo appendere: zero invalidati
        self.assertTrue(any(r["invalidated_suffix_tokens"] == 0 and r["invalidated_cause"] is None for r in reqs))
        cost = self.events("mask_cost")
        self.assertEqual(len(cost), len(masks))
        for c in cost:
            self.assertGreater(c["delta"], 0)
            self.assertIsNotNone(c["S_measured"])
            self.assertAlmostEqual(c["ratio_measured"], c["S_measured"] / c["delta"], places=2)
        # coerenza con ciò che il motore ha davvero riletto (cache simulata del motore finto)
        big = [r for r in reqs if r["invalidated_suffix_tokens"]]
        for r in big:
            self.assertGreater(r["prompt_read"], 0)


if __name__ == "__main__":
    unittest.main()
