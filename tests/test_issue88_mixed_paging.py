"""Regressioni del commento #88 e del turno a chiamate miste di #90 (card t_f3c707b9).

    python3 -m unittest -v tests.test_issue88_mixed_paging

Nessuna GPU, nessuna rete esterna: motore finto in-process (`tests.fake_engine`).
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

from ctxproxy.core import RECALL_NAME  # noqa: E402
from ctxproxy.paging import TOOLS_HEAD_EN as TOOLS_HEAD, TOOLS_NAME  # noqa: E402
from ctxproxy.server import make_handler  # noqa: E402
from tests.test_proxy import Base, assert_tool_pairs  # noqa: E402

SYSTEM = {"role": "system", "content": "You are a coding agent. " * 10}
CORE = ["read_file", "write_to_file"]


def tool(name, desc="", **props):
    return {"type": "function", "function": {"name": name, "description": desc or name,
                                             "parameters": {"type": "object", "properties": {
                                                 k: {"type": v} for k, v in props.items()}}}}


CLIENT_TOOLS = [tool("read_file", "Reads a file", path="string"),
                tool("write_to_file", "Writes a file", path="string", content="string"),
                tool("browser_navigate", "Navigate to a URL", url="string"),
                tool("mem0_search", "Searches the memory", query="string")]


def call(cid, name, **args):
    return {"id": cid, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


class PagingBase(Base):
    cfg_over = {"tools_paging": True, "tools_core": CORE}

    def setUp(self):
        self.seen_tools = []
        super().setUp()

    def send(self, msgs, **kw):
        st, r, hdr = self.proxy.chat({"model": "m", "messages": msgs, "tools": CLIENT_TOOLS,
                                      "max_tokens": 1024, **kw})
        self.assertEqual(st, 200, r)
        return r, hdr

    def policy(self, body):
        self.seen_tools.append([t["function"]["name"] for t in body.get("tools") or []])
        last = body["messages"][-1]
        if last.get("role") == "user":
            return {"role": "assistant", "content": "", "reasoning_content": "I need the browser tools.",
                    "tool_calls": [call("s1", TOOLS_NAME, query="browser navigate",
                                        names=["browser_navigate"])]}
        if last.get("role") == "tool" and last.get("tool_call_id") == "s1":
            return {"role": "assistant", "content": "",
                    "tool_calls": [call("c1", "browser_navigate", url="https://example.org")]}
        return {"role": "assistant", "content": "ok"}


class StreamMixin:
    def setUp(self):
        super().setUp()
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.proxy))
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.base = "http://127.0.0.1:%d" % self.srv.server_address[1]

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        super().tearDown()

    def stream(self, msgs, **kw):
        body = json.dumps({"model": "m", "messages": msgs, "tools": CLIENT_TOOLS, "max_tokens": 1024,
                           "stream": True, **kw}).encode()
        req = urllib.request.Request(self.base + "/v1/chat/completions", data=body,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req) as r:
            raw = r.read().decode()
        return [json.loads(l[6:]) for l in raw.split("\n")
                if l.startswith("data: ") and l[6:].strip() != "[DONE]"]

    @staticmethod
    def assembled(chunks):
        """Risposta ricostruita dai delta: id/nome/arguments/finish_reason visti dal client."""
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


# ------------------------------------------------------------------ #88: lingua dei segnaposti in streaming
class TestStreamLoadMarkerLanguage(StreamMixin, PagingBase):
    """#88: il segnaposto del caricamento scritto nel flusso `reasoning_content` deve seguire la lingua della
    conversazione. Prima della correzione era italiano incondizionato (server.py:426)."""

    def test_marker_english_in_english_conversation(self):
        asm = self.assembled(self.stream([SYSTEM, {"role": "user", "content": "take a screenshot of example.org"}]))
        reasoning = asm["choices"][0]["message"]["reasoning_content"]
        self.assertEqual(self.proxy.mgr.lang, "en")
        self.assertNotIn("definizioni caricate", reasoning)
        self.assertIn("[%s: definitions loaded]" % TOOLS_NAME, reasoning)
        # la chiamata del client resta intatta
        tcs = asm["choices"][0]["message"].get("tool_calls") or []
        self.assertEqual([(c["id"], c["function"]["name"]) for c in tcs], [("c1", "browser_navigate")])
        self.assertEqual(asm["choices"][0]["finish_reason"], "tool_calls")


class TestStreamLoadMarkerItalian(TestStreamLoadMarkerLanguage):
    """La stessa conversazione con `prompt_language: it`: il segnaposto resta italiano (nessuna regressione)."""
    cfg_over = {"tools_paging": True, "tools_core": CORE, "prompt_language": "it"}

    def test_marker_english_in_english_conversation(self):
        asm = self.assembled(self.stream([SYSTEM, {"role": "user", "content": "fai uno screenshot di example.org"}]))
        reasoning = asm["choices"][0]["message"]["reasoning_content"]
        self.assertEqual(self.proxy.mgr.lang, "it")
        self.assertIn("[%s: definizioni caricate]" % TOOLS_NAME, reasoning)
        self.assertNotIn("definitions loaded", reasoning)


class TestNonStreamLoadMarker(PagingBase):
    """Percorso non streaming: nessun segnaposto italiano nella risposta al client (già sano, da preservare)."""

    def test_no_italian_marker_in_response(self):
        r, _ = self.send([SYSTEM, {"role": "user", "content": "take a screenshot of example.org"}])
        self.assertNotIn("definizioni caricate", json.dumps(r, ensure_ascii=False))
        self.assertEqual([c["id"] for c in r["choices"][0]["message"]["tool_calls"]], ["c1"])


# --------------------------------------------- #90: turno misto (chiamata di paginazione + chiamata del client)
EN_TASK = "open example.org, take a screenshot and read README.md"


def names_of(resp):
    return [((c.get("function") or {}).get("name")) for c in
            (resp["choices"][0]["message"].get("tool_calls") or [])]


def ids_of(resp):
    return [c.get("id") for c in (resp["choices"][0]["message"].get("tool_calls") or [])]


class MixedBase(PagingBase):
    """Il modello emette nello stesso turno una chiamata di paginazione e una chiamata del client."""

    def policy(self, body):
        self.seen_tools.append([t["function"]["name"] for t in body.get("tools") or []])
        msgs = body["messages"]
        last = msgs[-1]
        cid = str(last.get("tool_call_id") or "")
        if last.get("role") == "user":
            k = sum(1 for m in msgs if m.get("role") == "user")
            return {"role": "assistant", "content": "",
                    "reasoning_content": "I need the browser tools and the project files.",
                    "tool_calls": self.mixed_calls(k)}
        if last.get("role") == "tool" and cid.startswith("rf"):
            # turno dopo: la definizione è stata caricata al turno prima, la chiamata va al client
            return {"role": "assistant", "content": "",
                    "tool_calls": [call("nv1", "browser_navigate", url="https://example.org")]}
        if last.get("role") == "tool" and cid.startswith("nv"):
            # strumento già caricato: turno misto che deve passare per intero
            return {"role": "assistant", "content": "",
                    "tool_calls": [call("nv2", "browser_navigate", url="https://example.org/2"),
                                   call("rf9", "read_file", path="CHANGELOG.md")]}
        return {"role": "assistant", "content": "done"}

    def mixed_calls(self, k):
        """Caricatore esplicito + chiamata del client."""
        return [call("ld%d" % k, TOOLS_NAME, names=["browser_navigate"]),
                call("rf%d" % k, "read_file", path="README.md")]

    def turn2(self, r):
        """Storia del turno successivo: il client restituisce il risultato delle sue chiamate."""
        m = r["choices"][0]["message"]
        return [SYSTEM, {"role": "user", "content": EN_TASK}, m] + \
            [{"role": "tool", "tool_call_id": c["id"], "content": "# README\nproject"}
             for c in (m.get("tool_calls") or [])]


class TestMixedLoaderAndClientCall(MixedBase):
    def test_load_served_and_client_call_intact(self):
        r, _ = self.send([SYSTEM, {"role": "user", "content": EN_TASK}])
        # 1. al client va SOLO la sua chiamata, intatta
        self.assertEqual(names_of(r), ["read_file"])
        self.assertEqual(ids_of(r), ["rf1"])
        self.assertEqual(json.loads(r["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"]),
                         {"path": "README.md"})
        self.assertEqual(r["choices"][0]["finish_reason"], "tool_calls")
        # 2. la richiesta di caricamento è stata eseguita, non scartata
        self.assertEqual(len(self.events("tools_search")), 1)
        self.assertEqual(self.events("tools_search")[0]["loaded_new"], ["browser_navigate"])
        self.assertEqual([e for e in self.events("recall_dropped")
                          if TOOLS_NAME in (e.get("names") or [])], [])
        # 3. il modello NON è stato rilanciato: una sola richiesta a monte in questo turno
        self.assertEqual(len(self.eng.requests), 1)

    def test_definition_available_next_turn_and_no_duplicate_client_call(self):
        r, _ = self.send([SYSTEM, {"role": "user", "content": EN_TASK}])
        r2, _ = self.send(self.turn2(r))
        cur = self.eng.requests[-1]["messages"]
        # il risultato del caricatore è nel prompt del turno dopo, con la definizione vera
        res = [m for m in cur if m.get("role") == "tool" and str(m.get("content") or "").startswith(TOOLS_HEAD)]
        self.assertEqual(len(res), 1, "risultato del caricatore non reinserito")
        self.assertIn('"name": "browser_navigate"', res[0]["content"])
        assert_tool_pairs(self, cur)
        # la chiamata del client del turno prima appare una sola volta: nessuna azione duplicata
        self.assertEqual([c["id"] for m in cur for c in (m.get("tool_calls") or [])].count("rf1"), 1)
        # e ora la chiamata allo strumento caricato passa al client
        self.assertEqual((names_of(r2), ids_of(r2)), (["browser_navigate"], ["nv1"]))
        self.assertEqual(len(self.events("tool_not_loaded")), 0)

    def test_already_loaded_mixed_turn_passes_whole(self):
        r, _ = self.send([SYSTEM, {"role": "user", "content": EN_TASK}])
        h2 = self.turn2(r)
        r2, _ = self.send(h2)
        m2 = r2["choices"][0]["message"]
        h3 = h2 + [m2] + [{"role": "tool", "tool_call_id": c["id"], "content": "Navigated"}
                          for c in (m2.get("tool_calls") or [])]
        r3, _ = self.send(h3)
        # strumento già caricato: il turno misto arriva intero al client, nessun evento di paginazione
        self.assertEqual(sorted(names_of(r3)), ["browser_navigate", "read_file"])
        self.assertEqual(sorted(ids_of(r3)), ["nv2", "rf9"])
        self.assertEqual(self.events("recall_dropped"), [])
        self.assertEqual(len(self.events("tools_search")), 1)
        self.assertEqual(len(self.events("tool_not_loaded")), 0)

    def test_three_turns_make_progress(self):
        """Forma del loop di #90: il client ripete lo stesso turno. Ogni turno deve caricare davvero."""
        h = [SYSTEM, {"role": "user", "content": EN_TASK}]
        for _ in range(3):
            r, _ = self.send(h)
            m = r["choices"][0]["message"]
            h = h + [m] + [{"role": "tool", "tool_call_id": c["id"], "content": "# README"}
                           for c in (m.get("tool_calls") or [])]
            h = h + [{"role": "user", "content": EN_TASK}]
        self.assertEqual(len(self.events("tools_search")), 3)
        self.assertEqual([e for e in self.events("recall_dropped")
                          if TOOLS_NAME in (e.get("names") or [])], [])


class TestMixedHiddenAndClientCall(MixedBase):
    """Chiamata diretta a uno strumento nascosto NON caricato insieme a una chiamata del client."""

    def mixed_calls(self, k):
        return [call("nv%d" % k, "browser_navigate", url="https://example.org"),
                call("rf%d" % k, "read_file", path="README.md")]

    def test_hidden_call_served_not_lost(self):
        r, _ = self.send([SYSTEM, {"role": "user", "content": EN_TASK}])
        self.assertEqual((names_of(r), ids_of(r)), (["read_file"], ["rf1"]))
        self.assertEqual(r["choices"][0]["finish_reason"], "tool_calls")
        ev = self.events("tool_not_loaded")
        self.assertEqual([e["name"] for e in ev], ["browser_navigate"])
        self.assertEqual([e for e in self.events("recall_dropped")
                          if "browser_navigate" in (e.get("names") or [])], [])
        self.assertEqual(len(self.eng.requests), 1, "modello rilanciato nello stesso turno")
        # il proxy NON ha eseguito lo strumento del client: nel prompt c'è la nota, non un risultato inventato
        res = [m for m in self.eng.requests[0]["messages"] if m.get("role") == "tool"]
        self.assertEqual(res, [])

    def test_repeated_hidden_call_reaches_client_next_turn(self):
        r, _ = self.send([SYSTEM, {"role": "user", "content": EN_TASK}])
        r2, _ = self.send(self.turn2(r))
        cur = self.eng.requests[-1]["messages"]
        note = [m for m in cur if m.get("role") == "tool" and "was NOT executed" in str(m.get("content") or "")]
        self.assertEqual(len(note), 1, "nota del caricamento non reinserita")
        self.assertNotIn("non è stata eseguita", note[0]["content"])
        assert_tool_pairs(self, cur)
        self.assertEqual((names_of(r2), ids_of(r2)), (["browser_navigate"], ["nv1"]))
        self.assertEqual(len(self.events("tool_not_loaded")), 1, "strumento già caricato reintercettato")


class TestMixedPagingOff(TestMixedHiddenAndClientCall):
    """Controllo: con tools_paging disattivato il turno misto passa per intero (comportamento da preservare)."""
    cfg_over = {"tools_paging": False, "tools_core": CORE}

    def test_hidden_call_served_not_lost(self):
        r, _ = self.send([SYSTEM, {"role": "user", "content": EN_TASK}])
        self.assertEqual(sorted(names_of(r)), ["browser_navigate", "read_file"])
        self.assertEqual(sorted(ids_of(r)), ["nv1", "rf1"])
        self.assertEqual(self.events("recall_dropped"), [])
        self.assertEqual(len(self.events("tools_search")) + len(self.events("tool_not_loaded")), 0)

    test_repeated_hidden_call_reaches_client_next_turn = None


class TestMixedLoaderStream(StreamMixin, MixedBase):
    def test_stream_client_call_only_and_load_served(self):
        asm = self.assembled(self.stream([SYSTEM, {"role": "user", "content": EN_TASK}]))
        self.assertEqual((names_of(asm), ids_of(asm)), (["read_file"], ["rf1"]))
        self.assertEqual(json.loads(asm["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"]),
                         {"path": "README.md"})
        self.assertEqual(asm["choices"][0]["finish_reason"], "tool_calls")
        self.assertEqual(len(self.events("tools_search")), 1)
        self.assertEqual(self.events("recall_dropped"), [])
        reasoning = asm["choices"][0]["message"]["reasoning_content"]
        self.assertIn("[%s: definitions loaded]" % TOOLS_NAME, reasoning)
        self.assertNotIn("definizioni caricate", reasoning)


class TestMixedRecallStillDropped(PagingBase):
    """Guardia preservata: una chiamata recall in un turno misto resta scartata (non viene eseguita)."""

    def policy(self, body):
        self.seen_tools.append([t["function"]["name"] for t in body.get("tools") or []])
        if body["messages"][-1].get("role") == "user":
            return {"role": "assistant", "content": "",
                    "tool_calls": [call("rc1", RECALL_NAME, query="example"),
                                   call("rf1", "read_file", path="README.md")]}
        return {"role": "assistant", "content": "done"}

    def test_recall_in_mixed_turn_is_dropped(self):
        r, _ = self.send([SYSTEM, {"role": "user", "content": EN_TASK}])
        self.assertEqual((names_of(r), ids_of(r)), (["read_file"], ["rf1"]))
        self.assertEqual(self.events("recall"), [])
        dropped = self.events("recall_dropped")
        self.assertEqual([e["names"] for e in dropped], [[RECALL_NAME]])
        self.assertTrue(dropped[0]["mixed"])


class TestMixedRoundCapStillDrops(MixedBase):
    """Guardia preservata: senza giri interni disponibili la chiamata di paginazione resta scartata."""
    cfg_over = {"tools_paging": True, "tools_core": CORE, "max_recall_rounds": 0}

    def test_load_dropped_when_no_internal_round_is_available(self):
        r, _ = self.send([SYSTEM, {"role": "user", "content": EN_TASK}])
        self.assertEqual((names_of(r), ids_of(r)), (["read_file"], ["rf1"]))
        self.assertEqual(len(self.events("tools_search")), 0)
        self.assertEqual([e["names"] for e in self.events("recall_dropped")], [[TOOLS_NAME]])


if __name__ == "__main__":
    unittest.main()

