"""Issue #90: promozione selettiva degli SCHEMI COMPLETI già caricati nell'array `tools`.

Modo di compatibilità opt-in (`tools_paging_native_schemas`) per i motori che costruiscono
grammatica e parser delle chiamate SOLO dall'array `tools` (llama.cpp b11723: misurato in
workspaces/mp-issue90-grammar). Spento di serie: il percorso Strata e `tools_paging` off non
cambiano.

    python3 -m unittest -v tests.test_issue90_native_schemas
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ctxproxy.core import Config, Journal, Manager, Store, TokenCounter  # noqa: E402
from ctxproxy.paging import TOOLS_HEAD_EN, TOOLS_NAME, ToolPager, promote_loaded_schemas  # noqa: E402
from ctxproxy.server import make_handler  # noqa: E402
from tests.test_proxy import Base  # noqa: E402


def tool(name, desc="", **props):
    return {"type": "function", "function": {"name": name, "description": desc or name,
                                             "parameters": {"type": "object", "properties": {
                                                 k: {"type": v} for k, v in props.items()}}}}


CLIENT_TOOLS = [tool("read", path="string"), tool("bash", command="string"), tool("edit", path="string"),
                tool("write", path="string"), tool("grep", pattern="string"),
                tool("playwright_browser_navigate", "Navigate to a URL", url="string"),
                tool("playwright_browser_take_screenshot", "Take a screenshot", filename="string"),
                tool("todo", "Gestisce la lista dei compiti", action="string")]
CORE = ("read", "bash", "edit", "write", "grep", "recall", TOOLS_NAME)


class TestPromoteLoadedSchemas(unittest.TestCase):
    """Funzione pura: che cosa entra nell'array e che cosa no."""

    def setUp(self):
        self.pg = ToolPager(CORE)
        self.base, self.catalog = self.pg.split(CLIENT_TOOLS)

    def names(self, tools):
        return [(t.get("function") or {}).get("name") for t in tools]

    def test_promotes_only_loaded_catalog_schemas(self):
        out = promote_loaded_schemas(self.base, self.catalog, {"playwright_browser_navigate"})
        self.assertEqual(self.names(out), ["read", "bash", "edit", "write", "grep",
                                           "playwright_browser_navigate"])
        # schema COMPLETO, identico a quello del catalogo (non un segnaposto senza parametri)
        self.assertEqual(out[-1], self.catalog["playwright_browser_navigate"])
        self.assertIn("url", out[-1]["function"]["parameters"]["properties"])

    def test_deterministic_order_and_dedup(self):
        loaded = {"todo", "playwright_browser_take_screenshot", "playwright_browser_navigate"}
        a = promote_loaded_schemas(self.base, self.catalog, loaded)
        b = promote_loaded_schemas(self.base, dict(reversed(list(self.catalog.items()))), loaded)
        self.assertEqual(json.dumps(a), json.dumps(b))
        self.assertEqual(self.names(a)[5:], ["playwright_browser_navigate",
                                             "playwright_browser_take_screenshot", "todo"])
        again = promote_loaded_schemas(a, self.catalog, loaded)     # idempotente: nessun duplicato
        self.assertEqual(json.dumps(again), json.dumps(a))
        self.assertIs(again, a)

    def test_unknown_or_revoked_names_are_not_promoted(self):
        # nome mai esistito e nome revocato dal client (non più nel catalogo corrente)
        out = promote_loaded_schemas(self.base, self.catalog, {"inesistente_xyz", "todo"})
        self.assertEqual(self.names(out), ["read", "bash", "edit", "write", "grep", "todo"])
        shrunk = {k: v for k, v in self.catalog.items() if k != "todo"}
        out2 = promote_loaded_schemas(self.base, shrunk, {"todo"})
        self.assertIs(out2, self.base)

    def test_no_mutation_and_no_useless_copy(self):
        before = json.dumps(self.base)
        cat_before = json.dumps(self.catalog, sort_keys=True)
        self.assertIs(promote_loaded_schemas(self.base, self.catalog, set()), self.base)
        self.assertIs(promote_loaded_schemas(self.base, {}, {"todo"}), self.base)
        out = promote_loaded_schemas(self.base, self.catalog, {"todo"})
        self.assertEqual(json.dumps(self.base), before)                 # lista d'ingresso intatta
        self.assertEqual(json.dumps(self.catalog, sort_keys=True), cat_before)  # catalogo non mutato
        self.assertIsNot(out, self.base)

    def test_loaded_set_comes_from_the_physical_prompt(self):
        out = self.pg.result(self.catalog, ["playwright_browser_navigate"], set(), lang="en")
        phys = [{"role": "user", "content": "vai"}, {"role": "tool", "tool_call_id": "s1", "content": out}]
        self.assertTrue(out.startswith(TOOLS_HEAD_EN + "playwright_browser_navigate]"))
        promoted = promote_loaded_schemas(self.base, self.catalog, self.pg.loaded_in(phys))
        self.assertEqual(self.names(promoted)[-1], "playwright_browser_navigate")
        # tagliato il risultato del caricatore (cambio di segmento / mascheramento): niente promozione
        self.assertIs(promote_loaded_schemas(self.base, self.catalog, self.pg.loaded_in(phys[:1])), self.base)


class TestPrepareLevel(unittest.TestCase):
    """`Manager.prepare`: l'array `tools` e il conteggio del contesto con l'opzione accesa e spenta."""

    def mk(self, **over):
        cfg = Config.from_dict({"window": 131072, "tools_paging": True, "tools_core": list(CORE[:5]),
                                "mask_enabled": False, **over})
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        return Manager(cfg, Store(os.path.join(self.tmp.name, "a.sqlite")), TokenCounter(3.5),
                       Journal(os.path.join(self.tmp.name, "j.jsonl")))

    def history(self, loaded=("playwright_browser_navigate",), lang="en"):
        pg = ToolPager(CORE)
        _, catalog = pg.split(CLIENT_TOOLS)
        res = pg.result(catalog, list(loaded), set(), lang=lang)
        return [{"role": "system", "content": "sei un agente"},
                {"role": "user", "content": "usa playwright per aprire http://127.0.0.1:8099/pagina.html"},
                {"role": "assistant", "content": "", "tool_calls": [
                    {"id": "s1", "type": "function",
                     "function": {"name": TOOLS_NAME, "arguments": json.dumps({"names": list(loaded)})}}]},
                {"role": "tool", "tool_call_id": "s1", "content": res}]

    def names(self, tools):
        return [(t.get("function") or {}).get("name") for t in tools or []]

    def test_option_on_promotes_loaded_schema_and_counts_it(self):
        off = self.mk()
        p_off = off.prepare({"messages": self.history(), "tools": CLIENT_TOOLS, "max_tokens": 1024})
        on = self.mk(tools_paging_native_schemas=True)
        p_on = on.prepare({"messages": self.history(), "tools": CLIENT_TOOLS, "max_tokens": 1024})
        self.assertNotIn("playwright_browser_navigate", self.names(p_off.tools))
        self.assertEqual(self.names(p_on.tools)[:len(self.names(p_off.tools))], self.names(p_off.tools))
        self.assertEqual(self.names(p_on.tools)[-1], "playwright_browser_navigate")
        self.assertIn("url", p_on.tools[-1]["function"]["parameters"]["properties"])
        # conteggio del contesto reale: gli schemi promossi pesano
        self.assertGreater(p_on.est_tokens, p_off.est_tokens)
        # catalogo e messaggi invariati
        self.assertEqual(set(p_on.catalog), set(p_off.catalog))
        self.assertEqual(p_on.messages, p_off.messages)

    def test_option_off_is_byte_identical(self):
        base = self.mk()
        p1 = base.prepare({"messages": self.history(), "tools": CLIENT_TOOLS, "max_tokens": 1024})
        self.assertEqual(self.names(p1.tools), ["read", "bash", "edit", "write", "grep", "recall", TOOLS_NAME])

    def test_paging_off_ignores_the_option(self):
        mgr = self.mk(tools_paging=False, tools_paging_native_schemas=True)
        p = mgr.prepare({"messages": self.history(), "tools": CLIENT_TOOLS, "max_tokens": 1024})
        self.assertEqual(p.catalog, {})
        self.assertEqual(self.names(p.tools), [t["function"]["name"] for t in CLIENT_TOOLS] + ["recall"])

    def test_stable_between_requests_no_extra_invalidation(self):
        mgr = self.mk(tools_paging_native_schemas=True)
        h = self.history()
        p1 = mgr.prepare({"messages": h, "tools": CLIENT_TOOLS, "max_tokens": 1024})
        h2 = h + [{"role": "assistant", "content": "", "tool_calls": [
            {"id": "c1", "type": "function",
             "function": {"name": "playwright_browser_navigate",
                          "arguments": json.dumps({"url": "http://127.0.0.1:8099/pagina.html"})}}]},
            {"role": "tool", "tool_call_id": "c1", "content": "### Ran Playwright code\nnavigato"}]
        p2 = mgr.prepare({"messages": h2, "tools": CLIENT_TOOLS, "max_tokens": 1024})
        self.assertEqual(json.dumps(p1.tools), json.dumps(p2.tools))     # stesso array: nessuna mutazione inutile
        self.assertEqual(p2.invalidated["tokens"], 0)                    # prefisso non invalidato dal 2° turno

    def test_revoked_tool_is_not_promoted(self):
        mgr = self.mk(tools_paging_native_schemas=True)
        shrunk = [t for t in CLIENT_TOOLS if t["function"]["name"] != "playwright_browser_navigate"]
        p = mgr.prepare({"messages": self.history(), "tools": shrunk, "max_tokens": 1024})
        self.assertNotIn("playwright_browser_navigate", self.names(p.tools))

    def test_repeated_loads_do_not_duplicate(self):
        mgr = self.mk(tools_paging_native_schemas=True)
        h = self.history()
        pg = ToolPager(CORE)
        _, catalog = pg.split(CLIENT_TOOLS)
        h = h + [{"role": "assistant", "content": "", "tool_calls": [
            {"id": "s2", "type": "function",
             "function": {"name": TOOLS_NAME,
                          "arguments": json.dumps({"names": ["playwright_browser_navigate"]})}}]},
            {"role": "tool", "tool_call_id": "s2",
             "content": pg.result(catalog, ["playwright_browser_navigate"],
                                  {"playwright_browser_navigate"}, lang="en")}]
        p = mgr.prepare({"messages": h, "tools": CLIENT_TOOLS, "max_tokens": 1024})
        got = self.names(p.tools)
        self.assertEqual(got.count("playwright_browser_navigate"), 1)


class StreamMixin:
    """Percorso streaming reale: HTTP + SSE verso il proxy, risposta ricostruita dai delta."""

    def setUp(self):
        super().setUp()
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.proxy))
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.http = "http://127.0.0.1:%d" % self.srv.server_address[1]

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        super().tearDown()

    def stream(self, msgs, **kw):
        body = json.dumps({"model": "m", "messages": msgs, "tools": CLIENT_TOOLS, "max_tokens": 1024,
                           "stream": True, **kw}).encode()
        req = urllib.request.Request(self.http + "/v1/chat/completions", data=body,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req) as r:
            raw = r.read().decode()
        return [json.loads(l[6:]) for l in raw.split("\n")
                if l.startswith("data: ") and l[6:].strip() != "[DONE]"]

    @staticmethod
    def assembled(chunks):
        calls: dict = {}
        finish, content, reasoning = None, [], []
        for c in chunks:
            for ch in c.get("choices") or []:
                d = ch.get("delta") or {}
                if d.get("content"):
                    content.append(d["content"])
                if d.get("reasoning_content"):
                    reasoning.append(d["reasoning_content"])
                for tc in d.get("tool_calls") or []:
                    cur = calls.setdefault(tc.get("index", 0),
                                           {"id": None, "function": {"name": None, "arguments": ""}})
                    if tc.get("id"):
                        cur["id"] = tc["id"]
                    f = tc.get("function") or {}
                    if f.get("name"):
                        cur["function"]["name"] = f["name"]
                    cur["function"]["arguments"] += f.get("arguments") or ""
                if ch.get("finish_reason"):
                    finish = ch["finish_reason"]
        msg = {"role": "assistant", "content": "".join(content), "reasoning_content": "".join(reasoning)}
        if calls:
            msg["tool_calls"] = [calls[k] for k in sorted(calls)]
        return {"choices": [{"message": msg, "finish_reason": finish}]}


class ServerBase(Base):
    """Giro interno dopo il caricatore: è la richiesta reale di issue #90 (`evidence/req39`, 4 messaggi).

    Il risultato del caricatore è prodotto dal PROXY dentro il ciclo della stessa richiesta del client:
    la correzione deve agire sul giro, non solo alla preparazione."""
    cfg_over = {"tools_paging": True, "tools_core": list(CORE[:5]),
                "tools_paging_native_schemas": True}
    URL = "http://127.0.0.1:8099/pagina.html"

    def setUp(self):
        self.seen_tools = []
        self.seen_bodies = []
        super().setUp()

    def policy(self, body):
        self.seen_tools.append([t["function"]["name"] for t in body.get("tools") or []])
        self.seen_bodies.append(body)
        last = body["messages"][-1]
        if last.get("role") == "user":
            return {"role": "assistant", "content": "",
                    "reasoning_content": "I need the playwright tools.",
                    "tool_calls": [{"id": "s1", "type": "function", "function": {
                        "name": TOOLS_NAME,
                        "arguments": json.dumps({"query": "playwright navigate",
                                                 "names": ["playwright_browser_navigate"]})}}]}
        if last.get("role") == "tool" and last.get("tool_call_id") == "s1":
            # il motore nativo può emettere questa chiamata SOLO se lo schema è nell'array tools
            assert "playwright_browser_navigate" in self.seen_tools[-1], self.seen_tools[-1]
            return {"role": "assistant", "content": "", "tool_calls": [
                {"id": "nv1", "type": "function",
                 "function": {"name": "playwright_browser_navigate",
                              "arguments": json.dumps({"url": self.URL})}}]}
        return {"role": "assistant", "content": "ok"}

    def send(self, msgs, **kw):
        st, r, hdr = self.proxy.chat({"model": "m", "messages": msgs, "tools": CLIENT_TOOLS,
                                      "max_tokens": 1024, **kw})
        self.assertEqual(st, 200, r)
        return r, hdr


class TestServerRoundPromotion(ServerBase):
    def test_round_after_loader_carries_the_schema(self):
        r, _ = self.send([{"role": "system", "content": "You are a coding agent. " * 10},
                          {"role": "user", "content": "use playwright to open " + self.URL}])
        # giro 0: nessuno schema del catalogo; giro 1 (dopo il caricatore): schema presente, una volta sola
        self.assertNotIn("playwright_browser_navigate", self.seen_tools[0])
        self.assertIn("playwright_browser_navigate", self.seen_tools[1])
        self.assertEqual(self.seen_tools[1].count("playwright_browser_navigate"), 1)
        self.assertEqual(self.seen_tools[1][:len(self.seen_tools[0])], self.seen_tools[0])
        # lo schema inviato è quello completo del catalogo, con i parametri
        nav = [t for t in self.seen_bodies[1]["tools"]
               if t["function"]["name"] == "playwright_browser_navigate"][0]
        self.assertIn("url", nav["function"]["parameters"]["properties"])
        # al client arriva la chiamata vera, una sola volta, con l'URL esatto
        calls = r["choices"][0]["message"].get("tool_calls") or []
        self.assertEqual([c["function"]["name"] for c in calls], ["playwright_browser_navigate"])
        self.assertEqual(json.loads(calls[0]["function"]["arguments"]), {"url": self.URL})
        self.assertEqual(r["choices"][0]["finish_reason"], "tool_calls")
        # il caricatore resta un caricatore: testo, non esecuzione
        self.assertEqual(len(self.events("tools_search")), 1)
        self.assertEqual(self.events("tool_not_loaded"), [])

    def test_not_loaded_tool_is_still_intercepted(self):
        """Guardia preservata: uno strumento del catalogo NON caricato non entra nell'array e resta intercettato."""
        self.send([{"role": "system", "content": "You are a coding agent. " * 10},
                   {"role": "user", "content": "use playwright to open " + self.URL}])
        for names in self.seen_tools:
            self.assertNotIn("playwright_browser_take_screenshot", names)
            self.assertNotIn("todo", names)


class TestServerRoundPromotionOff(ServerBase):
    """Controllo: con l'opzione spenta il giro dopo il caricatore resta come prima (7 nomi, senza schema)."""
    cfg_over = {"tools_paging": True, "tools_core": list(CORE[:5]),
                "tools_paging_native_schemas": False}

    def policy(self, body):
        self.seen_tools.append([t["function"]["name"] for t in body.get("tools") or []])
        self.seen_bodies.append(body)
        last = body["messages"][-1]
        if last.get("role") == "user":
            return {"role": "assistant", "content": "", "tool_calls": [
                {"id": "s1", "type": "function", "function": {
                    "name": TOOLS_NAME,
                    "arguments": json.dumps({"names": ["playwright_browser_navigate"]})}}]}
        return {"role": "assistant", "content": "ok"}

    def test_array_unchanged(self):
        self.send([{"role": "system", "content": "You are a coding agent. " * 10},
                   {"role": "user", "content": "use playwright to open " + self.URL}])
        self.assertGreaterEqual(len(self.seen_tools), 2)
        for names in self.seen_tools:
            self.assertEqual(names, ["read", "bash", "edit", "write", "grep", "recall", TOOLS_NAME])


class TestServerRoundPromotionStream(StreamMixin, ServerBase):
    def test_stream_same_array_and_single_client_call(self):
        asm = self.assembled(self.stream([{"role": "system", "content": "You are a coding agent. " * 10},
                                          {"role": "user", "content": "use playwright to open " + self.URL}]))
        self.assertNotIn("playwright_browser_navigate", self.seen_tools[0])
        self.assertIn("playwright_browser_navigate", self.seen_tools[1])
        calls = asm["choices"][0]["message"].get("tool_calls") or []
        self.assertEqual([c["function"]["name"] for c in calls], ["playwright_browser_navigate"])
        self.assertEqual(json.loads(calls[0]["function"]["arguments"]), {"url": self.URL})
        self.assertEqual(asm["choices"][0]["finish_reason"], "tool_calls")


class TestRegressionsPreserved(ServerBase):
    """Regressioni da preservare (queste attese esistevano già: nessun comportamento nuovo richiesto)."""

    def test_next_turn_is_stable_and_call_not_repeated(self):
        start = [{"role": "system", "content": "You are a coding agent. " * 10},
                 {"role": "user", "content": "use playwright to open " + self.URL}]
        r, _ = self.send(start)
        calls = r["choices"][0]["message"]["tool_calls"]
        turn2 = start + [r["choices"][0]["message"],
                         {"role": "tool", "tool_call_id": calls[0]["id"],
                          "content": "### Ran Playwright code\nnavigated to " + self.URL}]
        n_before = len(self.seen_tools)
        r2, _ = self.send(turn2)
        # il turno dopo riparte con lo stesso array promosso del giro precedente: nessuna mutazione inutile
        self.assertEqual(self.seen_tools[n_before], self.seen_tools[n_before - 1])
        self.assertIn("playwright_browser_navigate", self.seen_tools[n_before])
        # nessun loop: una sola ricerca del caricatore in tutto, nessuna re-intercettazione
        self.assertEqual(len(self.events("tools_search")), 1)
        self.assertEqual(self.events("tool_not_loaded"), [])
        self.assertIsNone(r2["choices"][0]["message"].get("tool_calls"))

    def test_segment_or_mask_cut_removes_the_promotion(self):
        """Stato `loaded` coerente: se il risultato del caricatore non è nel prompt fisico, niente promozione."""
        pg = ToolPager(CORE)
        _, catalog = pg.split(CLIENT_TOOLS)
        base = [t for t in CLIENT_TOOLS if t["function"]["name"] in CORE[:5]]
        phys_no_loader = [{"role": "user", "content": "riprendi"},
                          {"role": "tool", "tool_call_id": "x", "content": "⟪ctx-archive id=r0123456789ab⟫"}]
        self.assertIs(self.proxy.mgr.promote_tools(base, catalog, phys_no_loader), base)
        phys_loader = phys_no_loader + [{"role": "tool", "tool_call_id": "s9", "content": pg.result(
            catalog, ["playwright_browser_navigate"], set(), lang="en")}]
        self.assertEqual([t["function"]["name"]
                          for t in self.proxy.mgr.promote_tools(base, catalog, phys_loader)][-1],
                         "playwright_browser_navigate")


class TestMixedTurnStillServed(ServerBase):
    """#88/#90: turno MISTO (caricatore + chiamata del client nello stesso turno) già corretto: resta corretto."""

    def policy(self, body):
        self.seen_tools.append([t["function"]["name"] for t in body.get("tools") or []])
        self.seen_bodies.append(body)
        last = body["messages"][-1]
        if last.get("role") == "user":
            return {"role": "assistant", "content": "", "tool_calls": [
                {"id": "s1", "type": "function", "function": {
                    "name": TOOLS_NAME,
                    "arguments": json.dumps({"names": ["playwright_browser_navigate"]})}},
                {"id": "rd1", "type": "function", "function": {
                    "name": "read", "arguments": json.dumps({"path": "README.md"})}}]}
        if last.get("role") == "tool" and last.get("tool_call_id") == "s1":
            return {"role": "assistant", "content": "", "tool_calls": [
                {"id": "nv1", "type": "function", "function": {
                    "name": "playwright_browser_navigate",
                    "arguments": json.dumps({"url": self.URL})}}]}
        return {"role": "assistant", "content": "ok"}

    def test_mixed_turn_client_call_survives(self):
        r, _ = self.send([{"role": "system", "content": "You are a coding agent. " * 10},
                          {"role": "user", "content": "read README and open " + self.URL}])
        names = [c["function"]["name"] for c in r["choices"][0]["message"].get("tool_calls") or []]
        self.assertIn("read", names)                      # la chiamata del client non va perduta
        self.assertNotIn(TOOLS_NAME, names)               # il caricatore non arriva al client
        self.assertEqual(len(self.events("tools_search")), 1)


if __name__ == "__main__":
    unittest.main()
