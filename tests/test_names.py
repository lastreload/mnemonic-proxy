"""Nomi degli strumenti del proxy (0.2.0): recall/tools, alias storici strata_recall/strata_tools, collisione con
strumenti del client, conversazioni nate prima della 0.2.0 (prefisso invariato).   python3 -m unittest tests.test_names
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ctxproxy.core import (LEGACY_RECALL_NAME, NAMES_KEY, RECALL_NAME, Config, Journal, Store,  # noqa: E402
                           TokenCounter, chain)
from ctxproxy.paging import LEGACY_TOOLS_NAME, TOOLS_NAME  # noqa: E402
from ctxproxy.server import Proxy  # noqa: E402
from ctxproxy.upstream import Upstream  # noqa: E402
from tests.fake_engine import FakeEngine  # noqa: E402


def fn(name, desc="x"):
    return {"type": "function", "function": {"name": name, "description": desc,
                                             "parameters": {"type": "object", "properties": {"q": {"type": "string"}}}}}


BASH = fn("bash", "esegue un comando")
SYSTEM = {"role": "system", "content": "Sei un agente. " * 10}
HIST = [SYSTEM, {"role": "user", "content": "il segreto è pappagallo-42"}, {"role": "assistant", "content": "ok"},
        {"role": "user", "content": "qual era il segreto?"}]


class Base(unittest.TestCase):
    cfg_over: dict = {}
    call_name = RECALL_NAME

    def policy(self, body):
        last = body["messages"][-1]
        if last.get("role") == "tool":
            return {"role": "assistant", "content": "risposta: " + last["content"][:60]}
        return {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": self.call_name,
                                                          "arguments": json.dumps({"query": "pappagallo"})}}]}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.eng = FakeEngine(self.policy)
        self.url = self.eng.start()
        self.db = os.path.join(self.tmp.name, "a.sqlite")
        self.jpath = os.path.join(self.tmp.name, "journal.jsonl")
        self.make_proxy()

    def make_proxy(self):
        cfg = Config.from_dict({"window": 131072, **self.cfg_over})
        self.proxy = Proxy(cfg, Upstream(self.url), Store(self.db), Journal(self.jpath), TokenCounter(3.5))

    def tearDown(self):
        self.eng.stop()
        self.proxy.store.db.close()
        self.tmp.cleanup()

    def send(self, msgs, tools):
        st, r, _ = self.proxy.chat({"model": "m", "messages": msgs, "tools": tools, "max_tokens": 512})
        self.assertEqual(st, 200, r)
        return r

    def sent_names(self, k=0):
        return [t["function"]["name"] for t in self.eng.requests[k]["tools"]]


class TestDefaultName(Base):
    def test_model_sees_recall_and_call_resolves(self):
        r = self.send(HIST, [BASH])
        self.assertEqual(self.sent_names(), ["bash", "recall"])
        msg = r["choices"][0]["message"]
        self.assertNotIn("tool_calls", msg, "la chiamata a recall non deve arrivare al client")
        self.assertIn("pappagallo", msg["content"])
        self.assertEqual(len(self.eng.requests), 2)


class TestLegacyAlias(Base):
    call_name = LEGACY_RECALL_NAME

    def test_strata_recall_call_still_resolved(self):
        r = self.send(HIST, [BASH])
        self.assertEqual(self.sent_names(), ["bash", "recall"])
        msg = r["choices"][0]["message"]
        self.assertNotIn("tool_calls", msg, "strata_recall è un alias: risolto dal proxy")
        self.assertIn("pappagallo", msg["content"])

    def test_stream_strata_recall_not_forwarded(self):
        chunks = []
        st, r, _ = self.proxy.chat({"model": "m", "messages": HIST, "tools": [BASH], "max_tokens": 512,
                                    "stream": True}, emit=lambda c: chunks.append(c) if c else None)
        self.assertEqual(st, 200)
        calls = [tc for c in chunks for ch in c.get("choices") or [] for tc in (ch.get("delta") or {}).get(
            "tool_calls") or []]
        self.assertEqual(calls, [], "la chiamata interna strata_recall non deve arrivare al client")
        text = "".join((ch.get("delta") or {}).get("content") or "" for c in chunks for ch in c.get("choices") or [])
        self.assertIn("pappagallo", text)


class TestCollision(Base):
    call_name = "history_recall"

    def test_client_recall_tool_kept_and_alternate_used(self):
        mine = fn("recall", "strumento del CLIENTE")
        r = self.send(HIST, [BASH, mine])
        tools = self.eng.requests[0]["tools"]
        names = [t["function"]["name"] for t in tools]
        self.assertEqual(names, ["bash", "recall", "history_recall"])
        self.assertEqual(tools[1]["function"]["description"], "strumento del CLIENTE", "non sovrascritto")
        self.assertNotIn("tool_calls", r["choices"][0]["message"])
        self.assertIn("pappagallo", r["choices"][0]["message"]["content"])

    def test_call_to_client_recall_goes_to_client(self):
        self.call_name = "recall"
        r = self.send(HIST, [BASH, fn("recall", "strumento del CLIENTE")])
        msg = r["choices"][0]["message"]
        self.assertEqual([c["function"]["name"] for c in msg["tool_calls"]], ["recall"])
        self.assertEqual(len(self.eng.requests), 1)

    def test_names_stable_across_requests(self):
        tools = [BASH, fn("recall", "del cliente")]
        self.send(HIST, tools)
        h2 = HIST + [{"role": "assistant", "content": "fatto"}, {"role": "user", "content": "ancora"}]
        self.send(h2, tools)
        self.assertEqual(self.eng.requests[0]["tools"], self.eng.requests[-1]["tools"])


class TestConfigName(Base):
    cfg_over = {"recall_tool_name": "archive_lookup"}
    call_name = "archive_lookup"

    def test_configured_name(self):
        r = self.send(HIST, [BASH])
        self.assertEqual(self.sent_names(), ["bash", "archive_lookup"])
        self.assertIn("pappagallo", r["choices"][0]["message"]["content"])


class TestPreexistingConversation(Base):
    """Conversazione già nell'archivio prima della 0.2.0 (nessun nome registrato): tiene strata_recall."""
    call_name = LEGACY_RECALL_NAME

    def test_legacy_conversation_keeps_old_name(self):
        tools = [BASH]
        self.send(HIST, tools)
        conv = self.proxy.store.find_conv(chain(HIST, tools))[0]
        assert conv
        # simula un archivio 0.1: nessun nome registrato per la conversazione
        self.proxy.store.db.execute("DELETE FROM kv WHERE k=?", (NAMES_KEY + conv,))
        self.eng.requests.clear()
        h2 = HIST + [{"role": "assistant", "content": "fatto"}, {"role": "user", "content": "e adesso?"}]
        r = self.send(h2, tools)
        self.assertEqual(self.sent_names(), ["bash", LEGACY_RECALL_NAME])
        self.assertIn("pappagallo", r["choices"][0]["message"]["content"])
        # e resta così anche dopo un riavvio del proxy
        self.proxy.store.db.close()
        self.make_proxy()
        self.eng.requests.clear()
        self.send(h2 + [{"role": "assistant", "content": "x"}, {"role": "user", "content": "y"}], tools)
        self.assertEqual(self.sent_names(), ["bash", LEGACY_RECALL_NAME])

    def test_new_conversation_uses_new_name(self):
        self.call_name = RECALL_NAME
        self.send(HIST, [BASH])
        self.assertEqual(self.sent_names(), ["bash", RECALL_NAME])


class TestPagingNames(unittest.TestCase):
    def test_tools_loader_name_and_legacy(self):
        from ctxproxy.paging import ToolPager, is_tools_result
        pg = ToolPager(("read",))
        cat = {"todo_add": fn("todo_add")}
        self.assertEqual(pg.tools_tool(cat)["function"]["name"], TOOLS_NAME)
        self.assertEqual(pg.tools_tool(cat, LEGACY_TOOLS_NAME)["function"]["name"], LEGACY_TOOLS_NAME)
        old = "[strata_tools: definizioni caricate: todo_add]\n{}"
        new = ToolPager.result(cat, ["todo_add"], set())
        self.assertTrue(is_tools_result(old) and is_tools_result(new))
        self.assertEqual(ToolPager.loaded_in([{"role": "tool", "content": old}]), {"todo_add"})
        self.assertEqual(ToolPager.loaded_in([{"role": "tool", "content": new}]), {"todo_add"})


if __name__ == "__main__":
    unittest.main()
