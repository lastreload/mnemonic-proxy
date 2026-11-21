"""Issue #90: separazione fra CATALOGO RESO (testo del prompt) e ARRAY DI GRAMMATICA (chiamate ammesse).

Modo di compatibilità opt-in (`tools_paging_native_split`) per i motori che costruiscono grammatica e
parser SOLO dall'array `tools` e che rendono nel prompt `chat_template_kwargs.tools` con precedenza
(llama.cpp b11723: misurato in workspaces/mp-issue90-cache-native, 150 assertion native).

    python3 -m unittest -v tests.test_issue90_native_split
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ctxproxy.autosave import prompt_text  # noqa: E402
from ctxproxy.core import Config, Journal, Manager, Store, TokenCounter  # noqa: E402
from ctxproxy.engines import detect  # noqa: E402
from ctxproxy.fake_engine import FakeEngine  # noqa: E402
from ctxproxy.nativetools import GRAMMAR_KEY, canonical_tools, qualify, wire_body  # noqa: E402
from ctxproxy.paging import TOOLS_NAME, ToolPager  # noqa: E402
from ctxproxy.server import Proxy, make_handler  # noqa: E402
from ctxproxy.upstream import Upstream  # noqa: E402
from ctxproxy.window import WindowGuard  # noqa: E402

REAL_TEMPLATE = ("/home/mverde/projects/flash-next-local/workspaces/mp-issue90-consult/astra/"
                 "runtime-template.jinja")
GOOD_TEMPLATE = ("{%- if tools %}<tools>{{ tools | tojson }}</tools>\n"
                 "format: <tool_call>\n<function=name>\n<parameter=p>\nv\n</parameter>\n</function>\n"
                 "</tool_call>{%- endif %}")
# template che genera la chiamata componendo il nome: handler escluso (marcatore di esclusione reale)
BAD_TEMPLATE = GOOD_TEMPLATE + "\n{{- '<tool_call><function=' ~ tool_call.name ~ '>' }}"


def tool(name, desc="", **props):
    return {"type": "function", "function": {"name": name, "description": desc or name,
                                             "parameters": {"type": "object", "properties": {
                                                 k: {"type": v} for k, v in props.items()}}}}


class TestCanonicalForm(unittest.TestCase):
    """Forma canonica identica a common_chat_tools_to_json_oaicompat (misurata in nativo)."""

    def test_only_three_keys_in_this_order(self):
        src = [{"type": "function", "function": {"parameters": {"type": "object", "properties": {}},
                                                 "name": "navigate", "description": "Vai a un URL",
                                                 "strict": True}}]
        out = canonical_tools(src)
        self.assertEqual(json.dumps(out), json.dumps(
            [{"type": "function", "function": {"name": "navigate", "description": "Vai a un URL",
                                               "parameters": {"type": "object", "properties": {}}}}]))

    def test_defaults_for_missing_description_and_parameters(self):
        out = canonical_tools([{"type": "function", "function": {"name": "x"}}])
        self.assertEqual(out[0]["function"]["description"], "")
        self.assertEqual(out[0]["function"]["parameters"], {})

    def test_parameters_copied_verbatim_with_their_own_order(self):
        params = {"type": "object", "properties": {"b": {"type": "string", "description": "B"},
                                                   "a": {"type": "number"}},
                  "required": ["b"], "additionalProperties": False}
        out = canonical_tools([{"type": "function", "function": {"name": "f", "description": "d",
                                                                 "parameters": params}}])
        self.assertEqual(json.dumps(out[0]["function"]["parameters"]), json.dumps(params))

    def test_no_mutation_of_the_input(self):
        src = [{"type": "function", "function": {"name": "f", "strict": True}}]
        before = json.dumps(src)
        canonical_tools(src)
        self.assertEqual(json.dumps(src), before)

    def test_empty_and_none(self):
        self.assertIsNone(canonical_tools(None))
        self.assertEqual(canonical_tools([]), [])


class TestWireBody(unittest.TestCase):
    """Trasformazione finale del corpo: `tools` = grammatica, kwargs = catalogo canonico."""

    def body(self, **over):
        return {"model": "m", "messages": [{"role": "user", "content": "x"}],
                "tools": [tool("read", path="string")], **over}

    def test_without_the_private_field_nothing_changes(self):
        b = self.body()
        out = wire_body(b)
        self.assertEqual(json.dumps(out), json.dumps(b))

    def test_private_field_is_always_removed(self):
        out = wire_body(self.body(**{GRAMMAR_KEY: [tool("read", path="string"), tool("navigate", url="string")]}))
        self.assertNotIn(GRAMMAR_KEY, out)
        self.assertNotIn(GRAMMAR_KEY, json.dumps(out))

    def test_tools_becomes_the_grammar_array_and_kwargs_the_rendered_catalog(self):
        rendered = [tool("read", path="string")]
        grammar = rendered + [tool("navigate", "Vai a un URL", url="string")]
        out = wire_body(self.body(tools=rendered, **{GRAMMAR_KEY: grammar}))
        self.assertEqual([t["function"]["name"] for t in out["tools"]], ["read", "navigate"])
        self.assertEqual(json.dumps(out["chat_template_kwargs"]["tools"]),
                         json.dumps(canonical_tools(rendered)))

    def test_client_kwargs_are_merged_not_replaced(self):
        rendered = [tool("read", path="string")]
        out = wire_body(self.body(tools=rendered, chat_template_kwargs={"enable_thinking": False, "mp_flag": 1},
                                  **{GRAMMAR_KEY: rendered}))
        ctk = out["chat_template_kwargs"]
        self.assertIs(ctk["enable_thinking"], False)
        self.assertEqual(ctk["mp_flag"], 1)
        self.assertIn("tools", ctk)

    def test_client_kwargs_tools_is_never_overwritten(self):
        """Difesa in profondità: la decisione sta a monte, ma qui il catalogo del cliente resta suo."""
        mine = [tool("read", path="string")]
        his = [tool("read", path="string"), tool("suo", x="string")]
        out = wire_body(self.body(tools=mine, chat_template_kwargs={"tools": his},
                                  **{GRAMMAR_KEY: mine + [tool("navigate", url="string")]}))
        self.assertEqual(json.dumps(out["chat_template_kwargs"]["tools"]), json.dumps(his))
        self.assertEqual([t["function"]["name"] for t in out["tools"]], ["read"])  # nessuna promozione
        self.assertNotIn(GRAMMAR_KEY, out)

    def test_round_zero_sends_the_catalog_even_with_nothing_promoted(self):
        rendered = [tool("read", path="string")]
        out = wire_body(self.body(tools=rendered, **{GRAMMAR_KEY: rendered}))
        self.assertEqual(json.dumps(out["chat_template_kwargs"]["tools"]), json.dumps(canonical_tools(rendered)))
        self.assertEqual(json.dumps(out["tools"]), json.dumps(rendered))


class TestGate(unittest.TestCase):
    """Qualifica opt-in: nessuna attivazione per «un llama.cpp qualunque»."""

    def props(self, **over):
        return {"build_info": "0.7.0-dev (build 11723, commit 31288c9c6)",
                "chat_template": GOOD_TEMPLATE, "total_slots": 1, **over}

    def test_qualified_llama_cpp(self):
        ok, reason, detail = qualify("llama.cpp", self.props())
        self.assertTrue(ok, reason)
        self.assertEqual(reason, "")
        self.assertEqual(detail["build_info"], "0.7.0-dev (build 11723, commit 31288c9c6)")
        self.assertEqual(detail["markers"], ["<tool_call>", "<function=", "<parameter="])

    def test_other_engines_never_qualify(self):
        for kind in ("strata", "openai", "generic", ""):
            ok, reason, _ = qualify(kind, self.props())
            self.assertFalse(ok)
            self.assertIn("engine", reason)

    def test_missing_props_or_build_info(self):
        ok, reason, _ = qualify("llama.cpp", None)
        self.assertFalse(ok)
        self.assertIn("props", reason)
        ok, reason, _ = qualify("llama.cpp", self.props(build_info=""))
        self.assertFalse(ok)
        self.assertIn("build_info", reason)

    def test_template_without_the_three_markers(self):
        ok, reason, detail = qualify("llama.cpp", self.props(chat_template="{{ messages }}"))
        self.assertFalse(ok)
        self.assertIn("marker", reason)
        self.assertEqual(detail["markers"], [])

    def test_template_with_the_exclusion_marker(self):
        ok, reason, _ = qualify("llama.cpp", self.props(chat_template=BAD_TEMPLATE))
        self.assertFalse(ok)
        self.assertIn("exclusion", reason)

    def test_expected_build_is_checked_when_asked(self):
        ok, _, _ = qualify("llama.cpp", self.props(), expected_build="11723")
        self.assertTrue(ok)
        ok, reason, _ = qualify("llama.cpp", self.props(), expected_build="99999")
        self.assertFalse(ok)
        self.assertIn("build", reason)

    @unittest.skipUnless(os.path.exists(REAL_TEMPLATE), "template reale non presente su questa macchina")
    def test_the_real_production_template_qualifies(self):
        with open(REAL_TEMPLATE, encoding="utf-8") as f:
            src = f.read()
        ok, reason, _ = qualify("llama.cpp", self.props(chat_template=src), expected_build="11723")
        self.assertTrue(ok, reason)


class _Up:
    """Finto motore ai soli fini del gate: espone engine.kind e GET /props."""

    class _E:
        def __init__(self, kind):
            self.kind = kind

    def __init__(self, kind="llama.cpp", props=None, base="http://127.0.0.1:1"):
        self.engine = self._E(kind)
        self.base = base
        self._props = props if props is not None else {
            "build_info": "0.7.0-dev (build 11723, commit 31288c9c6)", "chat_template": GOOD_TEMPLATE,
            "total_slots": 1}
        self.gets = 0

    def raw(self, method, path, body=None, headers=None, timeout=None):
        if method == "GET" and path.startswith("/props"):
            self.gets += 1
            if self._props is None:
                return 503, {}, b"{}"
            return 200, {}, json.dumps(self._props).encode()
        return 404, {}, b"{}"


CLIENT_TOOLS = [tool("read", path="string"), tool("bash", command="string"), tool("edit", path="string"),
                tool("write", path="string"), tool("grep", pattern="string"),
                tool("playwright_browser_navigate", "Navigate to a URL", url="string"),
                tool("playwright_browser_take_screenshot", "Take a screenshot", filename="string"),
                tool("todo", "Gestisce la lista dei compiti", action="string")]
CORE = ("read", "bash", "edit", "write", "grep")


class PrepareBase(unittest.TestCase):
    def mk(self, **over):
        cfg = Config.from_dict({"window": 131072, "tools_paging": True, "tools_core": list(CORE),
                                "mask_enabled": False, **over})
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.jpath = os.path.join(self.tmp.name, "j.jsonl")
        return Manager(cfg, Store(os.path.join(self.tmp.name, "a.sqlite")), TokenCounter(3.5),
                       Journal(self.jpath))

    def events(self, name=None):
        if not os.path.exists(self.jpath):
            return []
        with open(self.jpath) as f:
            ev = [json.loads(line) for line in f]
        return [e for e in ev if name is None or e["event"] == name]

    def history(self, loaded=("playwright_browser_navigate",), lang="en"):
        pg = ToolPager(tuple(CORE) + ("recall", TOOLS_NAME))
        _, catalog = pg.split(CLIENT_TOOLS)
        res = pg.result(catalog, list(loaded), set(), lang=lang)
        return [{"role": "system", "content": "sei un agente"},
                {"role": "user", "content": "usa playwright per aprire http://127.0.0.1:8099/pagina.html"},
                {"role": "assistant", "content": "", "tool_calls": [
                    {"id": "s1", "type": "function",
                     "function": {"name": TOOLS_NAME, "arguments": json.dumps({"names": list(loaded)})}}]},
                {"role": "tool", "tool_call_id": "s1", "content": res}]

    @staticmethod
    def names(tools):
        return [(t.get("function") or {}).get("name") for t in tools or []]


class TestPrepareSplit(PrepareBase):
    """`Manager.prepare`: la lista RESA non cresce, la lista di GRAMMATICA sì."""

    def test_rendered_stays_and_grammar_grows(self):
        mgr = self.mk(tools_paging_native_split=True)
        p = mgr.prepare({"messages": self.history(), "tools": CLIENT_TOOLS, "max_tokens": 1024},
                        upstream=_Up())
        self.assertEqual(self.names(p.tools), ["read", "bash", "edit", "write", "grep", "recall", TOOLS_NAME])
        self.assertEqual(self.names(p.grammar_tools),
                         ["read", "bash", "edit", "write", "grep", "recall", TOOLS_NAME,
                          "playwright_browser_navigate"])
        self.assertIn("url", p.grammar_tools[-1]["function"]["parameters"]["properties"])

    def test_counters_follow_the_rendered_list_not_the_grammar(self):
        off = self.mk()
        p_off = off.prepare({"messages": self.history(), "tools": CLIENT_TOOLS, "max_tokens": 1024})
        on = self.mk(tools_paging_native_split=True)
        p_on = on.prepare({"messages": self.history(), "tools": CLIENT_TOOLS, "max_tokens": 1024},
                          upstream=_Up())
        self.assertEqual(p_on.est_tokens, p_off.est_tokens)
        self.assertEqual(p_on.response_limit, p_off.response_limit)

    def test_rendered_list_is_canonical(self):
        mgr = self.mk(tools_paging_native_split=True)
        dirty = [{"type": "function", "function": {"name": "read", "strict": True,
                                                   "parameters": {"type": "object", "properties": {}},
                                                   "description": "legge"}}] + CLIENT_TOOLS[1:]
        p = mgr.prepare({"messages": self.history(), "tools": dirty, "max_tokens": 1024}, upstream=_Up())
        self.assertEqual(json.dumps(p.tools[0]), json.dumps(canonical_tools([dirty[0]])[0]))
        self.assertNotIn("strict", json.dumps(p.tools))

    def test_a_new_load_does_not_invalidate_the_prefix(self):
        mgr = self.mk(tools_paging_native_split=True)
        up = _Up()
        h1 = self.history()
        p1 = mgr.prepare({"messages": h1, "tools": CLIENT_TOOLS, "max_tokens": 1024}, upstream=up)
        self.assertEqual(p1.invalidated["cause"], "prima")
        h2 = h1 + [{"role": "assistant", "content": "", "tool_calls": [
            {"id": "s2", "type": "function",
             "function": {"name": TOOLS_NAME,
                          "arguments": json.dumps({"names": ["playwright_browser_take_screenshot"]})}}]},
            {"role": "tool", "tool_call_id": "s2", "content": ToolPager(
                tuple(CORE) + ("recall", TOOLS_NAME)).result(
                    ToolPager(tuple(CORE) + ("recall", TOOLS_NAME)).split(CLIENT_TOOLS)[1],
                    ["playwright_browser_take_screenshot"], {"playwright_browser_navigate"}, lang="en")}]
        p2 = mgr.prepare({"messages": h2, "tools": CLIENT_TOOLS, "max_tokens": 1024}, upstream=up)
        # secondo caricamento: la lista resa è la stessa, il prefisso non è invalidato
        self.assertEqual(json.dumps(p1.tools), json.dumps(p2.tools))
        self.assertEqual(p2.invalidated["tokens"], 0)
        self.assertNotEqual(p2.invalidated.get("cause"), "strumenti")
        # la grammatica invece contiene ora entrambi gli schemi
        self.assertEqual(self.names(p2.grammar_tools)[-2:],
                         ["playwright_browser_navigate", "playwright_browser_take_screenshot"])

    def test_old_promotion_mode_still_invalidates_the_prefix(self):
        """Controllo di contrasto: la vecchia promozione (braccio A della prova reale) resta com'era."""
        mgr = self.mk(tools_paging_native_schemas=True)
        h1 = [{"role": "system", "content": "sei un agente"},
              {"role": "user", "content": "apri la pagina"}]
        mgr.prepare({"messages": h1, "tools": CLIENT_TOOLS, "max_tokens": 1024})
        p2 = mgr.prepare({"messages": self.history(), "tools": CLIENT_TOOLS, "max_tokens": 1024})
        self.assertIn("playwright_browser_navigate", self.names(p2.tools))
        self.assertEqual(p2.invalidated["cause"], "strumenti")

    def test_a_revoked_tool_changes_the_rendered_list_and_the_conversation(self):
        """Nessun trucco: una revoca del catalogo del cliente cambia la lista RESA (e con essa l'identità
        della conversazione: `chain` include gli strumenti del cliente), quindi non è un caricamento
        mascherato. Lo schema di uno strumento revocato non entra nemmeno nella grammatica."""
        mgr = self.mk(tools_paging_native_split=True)
        up = _Up()
        h = self.history()
        p1 = mgr.prepare({"messages": h, "tools": CLIENT_TOOLS, "max_tokens": 1024}, upstream=up)
        shrunk = [t for t in CLIENT_TOOLS if t["function"]["name"] not in ("grep", "playwright_browser_navigate")]
        p2 = mgr.prepare({"messages": h, "tools": shrunk, "max_tokens": 1024}, upstream=up)
        self.assertNotIn("grep", self.names(p2.tools))
        self.assertNotEqual(p1.conv, p2.conv)
        self.assertNotIn("playwright_browser_navigate", self.names(p2.grammar_tools))
        self.assertNotEqual(json.dumps(p1.tools), json.dumps(p2.tools))


class TestGateInPrepare(PrepareBase):
    def test_gate_not_qualified_means_no_split_and_an_explicit_diagnostic(self):
        mgr = self.mk(tools_paging_native_split=True)
        p = mgr.prepare({"messages": self.history(), "tools": CLIENT_TOOLS, "max_tokens": 1024},
                        upstream=_Up(kind="strata"))
        self.assertIsNone(p.grammar_tools)
        self.assertEqual(self.names(p.tools), ["read", "bash", "edit", "write", "grep", "recall", TOOLS_NAME])
        ev = self.events("native_split_gate")
        self.assertEqual(len(ev), 1)
        self.assertFalse(ev[0]["ok"])
        self.assertIn("llama.cpp", ev[0]["reason"])

    def test_gate_is_evaluated_once_per_engine(self):
        mgr = self.mk(tools_paging_native_split=True)
        up = _Up()
        for _ in range(3):
            mgr.prepare({"messages": self.history(), "tools": CLIENT_TOOLS, "max_tokens": 1024}, upstream=up)
        self.assertEqual(up.gets, 1)
        self.assertEqual(len(self.events("native_split_gate")), 1)
        self.assertTrue(self.events("native_split_gate")[0]["ok"])

    def test_option_off_changes_nothing(self):
        mgr = self.mk()
        p = mgr.prepare({"messages": self.history(), "tools": CLIENT_TOOLS, "max_tokens": 1024}, upstream=_Up())
        self.assertIsNone(p.grammar_tools)
        self.assertEqual(self.events("native_split_gate"), [])

    def test_paging_off_ignores_the_option(self):
        mgr = self.mk(tools_paging=False, tools_paging_native_split=True)
        p = mgr.prepare({"messages": self.history(), "tools": CLIENT_TOOLS, "max_tokens": 1024}, upstream=_Up())
        self.assertEqual(p.catalog, {})
        self.assertIsNone(p.grammar_tools)

    def test_without_an_upstream_the_gate_cannot_qualify(self):
        mgr = self.mk(tools_paging_native_split=True)
        p = mgr.prepare({"messages": self.history(), "tools": CLIENT_TOOLS, "max_tokens": 1024})
        self.assertIsNone(p.grammar_tools)
        self.assertFalse(self.events("native_split_gate")[0]["ok"])


class ServerBase(unittest.TestCase):
    """Proxy reale davanti a un motore finto di tipo llama-server qualificato (prompt reso con la
    precedenza misurata di `chat_template_kwargs.tools` su `tools`)."""
    cfg_over: dict = {}
    URL = "http://127.0.0.1:8099/pagina.html"
    BUILD = "0.7.0-dev (build 11723, commit 31288c9c6)"

    def setUp(self):
        self.seen_bodies: list = []
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.eng = FakeEngine(self.policy, flavor="llama")
        self.eng.version = self.BUILD
        self.eng.chat_template = GOOD_TEMPLATE
        self.url = self.eng.start()
        cfg = Config.from_dict({"window": 131072, "mask_enabled": False, "tools_paging": True,
                                "tools_core": list(CORE), **self.cfg_over})
        self.jpath = os.path.join(self.tmp.name, "journal.jsonl")
        up = Upstream(self.url)
        up.engine = detect(up, "auto")
        self.assertEqual(up.engine.kind, "llama.cpp")
        self.proxy = Proxy(cfg, up, Store(os.path.join(self.tmp.name, "a.sqlite")),
                           Journal(self.jpath), TokenCounter(3.5))

    def tearDown(self):
        self.eng.stop()
        self.proxy.store.db.close()
        self.tmp.cleanup()

    def events(self, name=None):
        if not os.path.exists(self.jpath):
            return []
        with open(self.jpath) as f:
            ev = [json.loads(line) for line in f]
        return [e for e in ev if name is None or e["event"] == name]

    def policy(self, body):
        self.seen_bodies.append(json.loads(json.dumps(body)))
        last = body["messages"][-1]
        if last.get("role") == "user":
            return {"role": "assistant", "content": "", "tool_calls": [
                {"id": "s1", "type": "function", "function": {
                    "name": TOOLS_NAME,
                    "arguments": json.dumps({"names": ["playwright_browser_navigate"]})}}]}
        if last.get("role") == "tool" and last.get("tool_call_id") == "s1":
            # il motore nativo può emettere questa chiamata SOLO se lo schema è nell'array `tools`
            assert "playwright_browser_navigate" in [t["function"]["name"] for t in body.get("tools") or []]
            return {"role": "assistant", "content": "", "tool_calls": [
                {"id": "nv1", "type": "function", "function": {
                    "name": "playwright_browser_navigate", "arguments": json.dumps({"url": self.URL})}}]}
        return {"role": "assistant", "content": "ok"}

    def send(self, msgs, **kw):
        st, r, _ = self.proxy.chat({"model": "m", "messages": msgs, "tools": CLIENT_TOOLS,
                                    "max_tokens": 1024, **kw})
        self.assertEqual(st, 200, r)
        return r

    def start(self):
        return [{"role": "system", "content": "You are a coding agent. " * 10},
                {"role": "user", "content": "use playwright to open " + self.URL}]

    @staticmethod
    def names(tools):
        return [(t.get("function") or {}).get("name") for t in tools or []]

    def ctk_tools(self, i):
        return (self.seen_bodies[i].get("chat_template_kwargs") or {}).get("tools")


class TestServerSplit(ServerBase):
    cfg_over = {"tools_paging_native_split": True, "tools_paging_native_split_build": "11723"}

    def test_the_catalog_in_the_prompt_never_changes_while_the_grammar_grows(self):
        r = self.send(self.start())
        self.assertGreaterEqual(len(self.seen_bodies), 2)
        # il catalogo reso è canonico e IDENTICO in ogni giro, dal giro 0
        first = json.dumps(self.ctk_tools(0))
        self.assertEqual(self.names(self.ctk_tools(0)),
                         ["read", "bash", "edit", "write", "grep", "recall", TOOLS_NAME])
        for i in range(len(self.seen_bodies)):
            self.assertEqual(json.dumps(self.ctk_tools(i)), first, "giro %d" % i)
        # l'array di grammatica invece cresce col caricamento
        self.assertEqual(self.names(self.seen_bodies[0]["tools"]), self.names(self.ctk_tools(0)))
        self.assertIn("playwright_browser_navigate", self.names(self.seen_bodies[1]["tools"]))
        nav = [t for t in self.seen_bodies[1]["tools"]
               if t["function"]["name"] == "playwright_browser_navigate"][0]
        self.assertIn("url", nav["function"]["parameters"]["properties"])
        # il campo privato non esce mai dal proxy
        for b in self.seen_bodies:
            self.assertNotIn(GRAMMAR_KEY, b)
        # e la chiamata nativa arriva al client
        calls = r["choices"][0]["message"].get("tool_calls") or []
        self.assertEqual([c["function"]["name"] for c in calls], ["playwright_browser_navigate"])
        self.assertEqual(json.loads(calls[0]["function"]["arguments"]), {"url": self.URL})

    def test_the_prompt_prefix_is_reused_after_the_load(self):
        """Motore finto, cache a prefisso simulata: il giro dopo il caricatore riusa la testa."""
        self.send(self.start())
        head0 = self.eng.prompts[0].split("</tools>")[0]
        head1 = self.eng.prompts[1].split("</tools>")[0]
        self.assertEqual(head0, head1)
        # il prompt del giro 1 comincia col prompt del giro 0 (nessuna rilettura della testa)
        self.assertTrue(self.eng.prompts[1].startswith(self.eng.prompts[0]))

    def test_rounds_with_tool_choice_none_keep_the_same_catalog(self):
        pg = ToolPager(tuple(CORE) + ("recall", TOOLS_NAME))
        _, catalog = pg.split(CLIENT_TOOLS)
        loader_result = pg.result(catalog, ["playwright_browser_navigate"], set(), lang="en")
        self.send(self.start() + [{"role": "assistant", "content": "", "tool_calls": [
            {"id": "s1", "type": "function",
             "function": {"name": TOOLS_NAME,
                          "arguments": json.dumps({"names": ["playwright_browser_navigate"]})}}]},
            {"role": "tool", "tool_call_id": "s1", "content": loader_result}])
        first = json.dumps(self.ctk_tools(0))
        self.assertTrue(first and first != "null")
        for i in range(len(self.seen_bodies)):
            self.assertEqual(json.dumps(self.ctk_tools(i)), first, "giro %d" % i)

    def test_gate_journaled_once_and_qualified(self):
        self.send(self.start())
        ev = self.events("native_split_gate")
        self.assertEqual(len(ev), 1)
        self.assertTrue(ev[0]["ok"])
        self.assertEqual(ev[0]["build_info"], self.BUILD)


class TestServerSplitOffIsUnchanged(ServerBase):
    """Opzione spenta: nessun `chat_template_kwargs`, array come prima (7 nomi)."""
    cfg_over = {}

    def policy(self, body):
        self.seen_bodies.append(json.loads(json.dumps(body)))
        last = body["messages"][-1]
        if last.get("role") == "user":
            return {"role": "assistant", "content": "", "tool_calls": [
                {"id": "s1", "type": "function", "function": {
                    "name": TOOLS_NAME,
                    "arguments": json.dumps({"names": ["playwright_browser_navigate"]})}}]}
        return {"role": "assistant", "content": "ok"}

    def test_nothing_changes(self):
        self.send(self.start())
        for b in self.seen_bodies:
            self.assertNotIn("tools", b.get("chat_template_kwargs") or {})
            self.assertEqual(self.names(b["tools"]),
                             ["read", "bash", "edit", "write", "grep", "recall", TOOLS_NAME])
        self.assertEqual(self.events("native_split_gate"), [])


class TestServerOldPromotionStillRereadsTheHead(ServerBase):
    """Contrasto misurato: la vecchia promozione cambia la TESTA del prompt (prefisso riletto)."""
    cfg_over = {"tools_paging_native_schemas": True}

    def test_head_changes_and_prefix_is_not_reused(self):
        self.send(self.start())
        head0 = self.eng.prompts[0].split("</tools>")[0]
        head1 = self.eng.prompts[1].split("</tools>")[0]
        self.assertNotEqual(head0, head1)
        self.assertFalse(self.eng.prompts[1].startswith(self.eng.prompts[0]))


class TestClientKwargsToolsConflict(ServerBase):
    cfg_over = {"tools_paging_native_split": True, "tools_paging_native_split_build": "11723"}

    def policy(self, body):
        """Il modello NON può emettere la chiamata nativa (nessuno schema in `tools`): conseguenza
        dichiarata del conflitto, non un fallimento del test."""
        self.seen_bodies.append(json.loads(json.dumps(body)))
        last = body["messages"][-1]
        if last.get("role") == "user":
            return {"role": "assistant", "content": "", "tool_calls": [
                {"id": "s1", "type": "function", "function": {
                    "name": TOOLS_NAME,
                    "arguments": json.dumps({"names": ["playwright_browser_navigate"]})}}]}
        return {"role": "assistant", "content": "ok"}

    def test_the_client_catalog_is_not_overwritten_and_the_split_is_skipped(self):
        his = canonical_tools([tool("suo", x="string")])
        self.send(self.start(), chat_template_kwargs={"tools": his, "enable_thinking": False})
        for b in self.seen_bodies:
            self.assertEqual(json.dumps((b.get("chat_template_kwargs") or {}).get("tools")), json.dumps(his))
            self.assertNotIn("playwright_browser_navigate", self.names(b["tools"]))
        ev = self.events("native_split_skip")
        self.assertTrue(ev)
        self.assertEqual(ev[0]["reason"], "client_kwargs_tools")


class TestRenderAndCountFollowTheRenderedCatalog(unittest.TestCase):
    """Tokenizer locale, guardia di finestra e identità dello stato leggono la stessa lista del motore:
    `chat_template_kwargs.tools` quando c'è, altrimenti `tools`. Senza questa precedenza il proxy conterebbe
    (e firmerebbe) un prompt che il motore non legge."""

    def setUp(self):
        self.rendered = canonical_tools([tool("read", path="string"), tool("recall", query="string")])
        self.grammar = self.rendered + canonical_tools([tool("navigate", "Vai a un URL", url="string")])
        self.msgs = [{"role": "system", "content": "sei un agente"}, {"role": "user", "content": "apri"}]

    def bodies(self):
        plain = {"model": "m", "messages": self.msgs, "tools": self.rendered}
        split = wire_body({**plain, GRAMMAR_KEY: self.grammar})
        return plain, split

    def test_prompt_text_is_identical(self):
        plain, split = self.bodies()
        self.assertEqual(prompt_text(split), prompt_text(plain))
        self.assertIsNotNone(prompt_text(plain))
        # controprova: senza la precedenza, la grammatica avrebbe cambiato il testo
        self.assertNotEqual(prompt_text({**plain, "tools": self.grammar}), prompt_text(plain))

    def test_window_count_is_identical(self):
        plain, split = self.bodies()
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        cfg = Config.from_dict({"window": 131072})
        mgr = Manager(cfg, Store(os.path.join(tmp.name, "a.sqlite")), TokenCounter(3.5),
                      Journal(os.path.join(tmp.name, "j.jsonl")))
        wg = WindowGuard(cfg, mgr, None)
        self.assertEqual(wg.count_body(split), wg.count_body(plain))
        self.assertGreater(wg.count_body({**plain, "tools": self.grammar}), wg.count_body(plain))


class StreamMixin:
    """Percorso streaming reale: HTTP + SSE verso il proxy."""

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
        return [json.loads(line[6:]) for line in raw.split("\n")
                if line.startswith("data: ") and line[6:].strip() != "[DONE]"]


class TestServerSplitStream(StreamMixin, ServerBase):
    cfg_over = {"tools_paging_native_split": True, "tools_paging_native_split_build": "11723"}

    def test_stream_keeps_the_catalog_and_grows_the_grammar(self):
        chunks = self.stream(self.start())
        self.assertTrue(chunks)
        first = json.dumps(self.ctk_tools(0))
        for i in range(len(self.seen_bodies)):
            self.assertEqual(json.dumps(self.ctk_tools(i)), first, "giro %d" % i)
        self.assertIn("playwright_browser_navigate", self.names(self.seen_bodies[1]["tools"]))
        for b in self.seen_bodies:
            self.assertNotIn(GRAMMAR_KEY, b)
        self.assertTrue(self.eng.prompts[1].startswith(self.eng.prompts[0]))
        names = [((c.get("choices") or [{}])[0].get("delta") or {}).get("tool_calls") for c in chunks]
        flat = [f["function"]["name"] for group in names if group for f in group if (f.get("function") or {}).get("name")]
        self.assertIn("playwright_browser_navigate", flat)


class TestRegressioniSeparazione(ServerBase):
    """Guardie di regressione sulle attese della card (comportamento già implementato, non nuovo):
    turno misto, strumenti non autorizzati, risultato del caricatore tagliato dal prompt fisico."""
    cfg_over = {"tools_paging_native_split": True, "tools_paging_native_split_build": "11723"}

    def policy(self, body):
        self.seen_bodies.append(json.loads(json.dumps(body)))
        last = body["messages"][-1]
        if last.get("role") == "user":
            return {"role": "assistant", "content": "", "tool_calls": [
                {"id": "s1", "type": "function", "function": {
                    "name": TOOLS_NAME,
                    "arguments": json.dumps({"names": ["playwright_browser_navigate"]})}},
                {"id": "rd1", "type": "function", "function": {
                    "name": "read", "arguments": json.dumps({"path": "README.md"})}}]}
        if last.get("role") == "tool" and last.get("tool_call_id") == "s1":
            assert "playwright_browser_navigate" in self.names(body.get("tools"))
            return {"role": "assistant", "content": "", "tool_calls": [
                {"id": "nv1", "type": "function", "function": {
                    "name": "playwright_browser_navigate", "arguments": json.dumps({"url": self.URL})}}]}
        return {"role": "assistant", "content": "ok"}

    def test_mixed_turn_keeps_the_client_call_and_the_same_catalog(self):
        r = self.send(self.start())
        names = [c["function"]["name"] for c in r["choices"][0]["message"].get("tool_calls") or []]
        self.assertIn("read", names)                    # la chiamata del cliente non va perduta
        self.assertNotIn(TOOLS_NAME, names)             # il caricatore non arriva al cliente
        ids = [c["id"] for c in r["choices"][0]["message"].get("tool_calls") or []]
        self.assertEqual(len(ids), len(set(ids)))       # nessun duplicato
        first = json.dumps(self.ctk_tools(0))
        for i in range(len(self.seen_bodies)):
            self.assertEqual(json.dumps(self.ctk_tools(i)), first, "giro %d" % i)

    def test_a_catalog_tool_not_loaded_never_enters_the_grammar(self):
        self.send(self.start())
        for b in self.seen_bodies:
            self.assertNotIn("playwright_browser_take_screenshot", self.names(b["tools"]))
            self.assertNotIn("todo", self.names(b["tools"]))

    def test_if_the_loader_result_leaves_the_physical_prompt_the_grammar_shrinks(self):
        """Segmento o pacchetto di mascheramento che taglia il risultato del caricatore: niente promozione."""
        pg = ToolPager(tuple(CORE) + ("recall", TOOLS_NAME))
        base, catalog = pg.split(CLIENT_TOOLS)
        senza = [{"role": "user", "content": "riprendi"},
                 {"role": "tool", "tool_call_id": "x", "content": "⟪ctx-archive id=r0123456789ab⟫"}]
        self.assertIs(self.proxy.mgr.grammar_tools(base, catalog, senza), base)
        con = senza + [{"role": "tool", "tool_call_id": "s9",
                        "content": pg.result(catalog, ["playwright_browser_navigate"], set(), lang="en")}]
        self.assertEqual(self.names(self.proxy.mgr.grammar_tools(base, catalog, con))[-1],
                         "playwright_browser_navigate")


QUALIFIED_TEMPLATE_SHA = "7f0e529032c25183bcd66c7f238da2d377f43be754a94e2725a58c4e16d2ed67"


class TestQualifiedPins(unittest.TestCase):
    """Il modo è sperimentale: di serie la qualifica è legata alla build MISURATA, e il template
    può essere inchiodato al suo sha256 (opzione, vuota di serie: restano i marcatori)."""

    def props(self, build="b11723-31288c9c6", tpl=GOOD_TEMPLATE):
        return {"build_info": build, "chat_template": tpl}

    def test_the_default_build_pin_is_the_measured_revision(self):
        cfg = Config.from_dict({})
        self.assertEqual(cfg.tools_paging_native_split_build, "31288c9c6")
        self.assertEqual(cfg.tools_paging_native_split_template_sha256, "")

    def test_default_pin_refuses_another_build(self):
        cfg = Config.from_dict({"tools_paging": True, "tools_paging_native_split": True})
        ok, reason, _ = qualify("llama.cpp", self.props(build="b11999-0badc0de"),
                                cfg.tools_paging_native_split_build)
        self.assertFalse(ok)
        self.assertIn("expected build", reason)

    def test_the_operator_can_widen_the_pin_on_purpose(self):
        ok, reason, _ = qualify("llama.cpp", self.props(build="b11999-0badc0de"), "")
        self.assertTrue(ok, reason)

    def test_optional_template_pin(self):
        tpl = GOOD_TEMPLATE
        sha = hashlib.sha256(tpl.encode("utf-8")).hexdigest()
        ok, reason, detail = qualify("llama.cpp", self.props(tpl=tpl), "11723", sha)
        self.assertTrue(ok, reason)
        self.assertEqual(detail["expected_template_sha256"], sha)
        ok, reason, _ = qualify("llama.cpp", self.props(tpl=tpl), "11723", QUALIFIED_TEMPLATE_SHA)
        self.assertFalse(ok)
        self.assertIn("template", reason)

    def test_template_pin_accepts_a_prefix(self):
        sha = hashlib.sha256(GOOD_TEMPLATE.encode("utf-8")).hexdigest()
        ok, reason, _ = qualify("llama.cpp", self.props(), "11723", sha[:12].upper())
        self.assertTrue(ok, reason)


class TestBothFlagsOn(PrepareBase):
    """Due flag accesi insieme: la separazione VINCE e la vecchia promozione resta spenta, perché
    promuovere gli schemi nella lista RESA rimetterebbe il prefisso in testa (il costo che la
    separazione esiste per evitare). Detto una volta nel giornale, non in silenzio."""

    def test_the_split_suppresses_the_old_promotion_in_the_rendered_list(self):
        mgr = self.mk(tools_paging_native_split=True, tools_paging_native_split_build="11723",
                      tools_paging_native_schemas=True)
        up = _Up()
        p = mgr.prepare({"messages": self.history(), "tools": CLIENT_TOOLS, "max_tokens": 1024}, upstream=up)
        self.assertEqual(self.names(p.tools), ["read", "bash", "edit", "write", "grep", "recall", TOOLS_NAME])
        self.assertIn("playwright_browser_navigate", self.names(p.grammar_tools))
        ev = self.events("native_split_overrides_schemas")
        self.assertEqual(len(ev), 1)

    def test_the_prefix_still_survives_a_second_load_with_both_flags(self):
        mgr = self.mk(tools_paging_native_split=True, tools_paging_native_split_build="11723",
                      tools_paging_native_schemas=True)
        up = _Up()
        p1 = mgr.prepare({"messages": self.history(), "tools": CLIENT_TOOLS, "max_tokens": 1024}, upstream=up)
        h2 = self.history(loaded=("playwright_browser_navigate", "playwright_browser_take_screenshot"))
        p2 = mgr.prepare({"messages": h2, "tools": CLIENT_TOOLS, "max_tokens": 1024}, upstream=up)
        self.assertEqual(json.dumps(p1.tools), json.dumps(p2.tools))
        self.assertNotEqual(p2.invalidated.get("cause"), "strumenti")
        self.assertEqual(len(self.events("native_split_overrides_schemas")), 1)

    def test_with_the_gate_closed_both_flags_keep_the_old_promotion(self):
        """Gate non superato: la separazione non c'è, quindi la promozione costosa resta quella che
        l'operatore ha chiesto esplicitamente (nessuna sorpresa in nessuna delle due direzioni)."""
        mgr = self.mk(tools_paging_native_split=True, tools_paging_native_schemas=True)
        p = mgr.prepare({"messages": self.history(), "tools": CLIENT_TOOLS, "max_tokens": 1024},
                        upstream=_Up(kind="strata"))
        self.assertIsNone(p.grammar_tools)
        self.assertIn("playwright_browser_navigate", self.names(p.tools))
        self.assertEqual(self.events("native_split_overrides_schemas"), [])


if __name__ == "__main__":
    unittest.main()
