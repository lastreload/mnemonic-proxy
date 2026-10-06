"""Ingressi Anthropic Messages (Claude Code) e OpenAI Responses (Codex) davanti al motore finto (nessuna GPU).

    python3 -m unittest -v tests.test_clients
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ctxproxy import api_anthropic as A  # noqa: E402
from ctxproxy import api_responses as R  # noqa: E402
from ctxproxy.core import RECALL_NAME, Config, Journal, Store, TokenCounter, chain  # noqa: E402
from ctxproxy.server import Proxy, make_handler  # noqa: E402
from ctxproxy.upstream import Upstream  # noqa: E402
from tests.fake_engine import FakeEngine  # noqa: E402


def policy(body):
    msgs = body["messages"]
    last = msgs[-1]
    if last.get("role") == "tool":
        if last.get("tool_call_id") == "rc1":
            return {"role": "assistant", "content": "dopo la ricerca", "reasoning_content": "ho trovato"}
        return {"role": "assistant", "content": "fatto: " + str(last.get("content"))[:20]}
    t = str(last.get("content"))
    if "due comandi" in t:
        return {"role": "assistant", "content": "eseguo", "reasoning_content": "servono due comandi",
                "tool_calls": [
                    {"id": "call_a1", "type": "function", "function": {"name": "Bash", "arguments": '{"command":"ls"}'}},
                    {"id": "call_a2", "type": "function", "function": {"name": "Bash",
                                                                         "arguments": '{"command":"pwd"}'}}]}
    if "patch" in t:
        return {"role": "assistant", "content": "", "tool_calls": [
            {"id": "call_p1", "type": "function",
             "function": {"name": "apply_patch", "arguments": json.dumps({"input": "*** Begin Patch\n*** End Patch"})}}]}
    if "cerca" in t:
        return {"role": "assistant", "content": "", "reasoning_content": "cerco nell'archivio", "tool_calls": [
            {"id": "rc1", "type": "function", "function": {"name": RECALL_NAME, "arguments": '{"query":"snake"}'}}]}
    return {"role": "assistant", "content": "ciao", "reasoning_content": "saluto"}


ATOOLS = [{"name": "Bash", "description": "esegue", "input_schema": {
    "type": "object", "properties": {"command": {"type": "string"}}}, "cache_control": {"type": "ephemeral"}}]
RTOOLS = [{"type": "function", "name": "shell", "description": "esegue", "parameters": {
    "type": "object", "properties": {"command": {"type": "string"}}}},
    {"type": "custom", "name": "apply_patch", "description": "applica una patch",
     "format": {"type": "grammar", "syntax": "lark", "definition": "start: /.+/"}}]


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.eng = FakeEngine(policy)
        url = self.eng.start()
        cfg = Config.from_dict({"window": 131072})
        self.proxy = Proxy(cfg, Upstream(url), Store(os.path.join(self.tmp.name, "a.sqlite")),
                           Journal(os.path.join(self.tmp.name, "j.jsonl")), TokenCounter(3.5))
        from http.server import ThreadingHTTPServer
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

    def post(self, path, obj):
        req = urllib.request.Request(self.base + path, json.dumps(obj).encode(), method="POST",
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status, r.headers, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.headers, e.read()

    @staticmethod
    def sse(raw: bytes):
        out = []
        for block in raw.decode().split("\n\n"):
            ev, data = None, None
            for line in block.split("\n"):
                if line.startswith("event:"):
                    ev = line[6:].strip()
                elif line.startswith("data:"):
                    data = json.loads(line[5:].strip())
            if data is not None:
                out.append((ev, data))
        return out


# ====================================================================== Anthropic
class TestAnthropicConvert(unittest.TestCase):
    def test_request_conversion(self):
        a = {"model": "m", "max_tokens": 100, "system": [
            {"type": "text", "text": "x-anthropic-billing-header: cc_version=1; cch=abc;"},
            {"type": "text", "text": "Sei Claude Code.", "cache_control": {"type": "ephemeral"}}],
            "tools": ATOOLS, "messages": [
            {"role": "user", "content": [{"type": "text", "text": "fai due comandi", "cache_control": {"t": 1}}]},
            {"role": "assistant", "content": [
                {"type": "thinking", "thinking": "penso", "signature": "zzz"},
                {"type": "text", "text": "eseguo"},
                {"type": "tool_use", "id": "toolu_1", "name": "Bash", "input": {"command": "ls"}},
                {"type": "tool_use", "id": "toolu_2", "name": "Bash", "input": {"command": "pwd"}}]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "toolu_1", "content": [{"type": "text", "text": "a.txt"}]},
                {"type": "tool_result", "tool_use_id": "toolu_2", "content": "permesso negato", "is_error": True},
                {"type": "text", "text": "e ora?"}]}]}
        req = A.to_chat_request(a)
        m = req["messages"]
        self.assertEqual(m[0], {"role": "system", "content": "Sei Claude Code."})  # billing header scartato
        self.assertEqual(m[2]["reasoning_content"], "penso")
        self.assertEqual([c["id"] for c in m[2]["tool_calls"]], ["toolu_1", "toolu_2"])
        self.assertEqual(json.loads(m[2]["tool_calls"][1]["function"]["arguments"]), {"command": "pwd"})
        self.assertEqual(m[3], {"role": "tool", "tool_call_id": "toolu_1", "content": "a.txt"})
        self.assertEqual(m[4]["content"], "Error: permesso negato")
        self.assertEqual(m[5], {"role": "user", "content": "e ora?"})
        self.assertEqual(req["tools"][0]["function"]["parameters"]["properties"], {"command": {"type": "string"}})
        self.assertNotIn("cache_control", json.dumps(req))

    def test_hash_chain_stable_across_turns(self):
        """Claude Code rimanda la storia: stessa conversione -> stessa catena (cambia solo il billing header)."""
        base = [{"role": "user", "content": "ciao"}]
        r1 = A.to_chat_request({"max_tokens": 10, "messages": base,
                                "system": [{"type": "text", "text": "x-anthropic-billing-header: cch=1;"},
                                           {"type": "text", "text": "S"}]})
        turn2 = base + [{"role": "assistant", "content": [{"type": "thinking", "thinking": "t", "signature": "s"},
                                                           {"type": "text", "text": "ciao"}]},
                        {"role": "user", "content": "altro"}]
        r2 = A.to_chat_request({"max_tokens": 10, "messages": turn2,
                                "system": [{"type": "text", "text": "x-anthropic-billing-header: cch=2;"},
                                           {"type": "text", "text": "S"}]})
        c1, c2 = chain(r1["messages"], None), chain(r2["messages"], None)
        self.assertEqual(c1, c2[:len(c1)])

    def test_errors(self):
        with self.assertRaisesRegex(A.BadRequest, "max_tokens"):
            A.to_chat_request({"messages": []})
        with self.assertRaisesRegex(A.BadRequest, "image"):
            A.to_chat_request({"max_tokens": 5, "messages": [{"role": "user", "content": [
                {"type": "image", "source": {"type": "base64", "data": "xx"}}]}]})
        # immagine dentro un tool_result: nota testuale, non errore
        r = A.to_chat_request({"max_tokens": 5, "messages": [
            {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "Read", "input": {}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": [
                {"type": "image", "source": {}}]}]}]})
        self.assertEqual(r["messages"][1]["content"], A.IMAGE_NOTE)

    def test_system_role_inside_messages(self):
        """Claude Code 2.1.x manda anche messaggi role=system dentro messages (trovato nella prova dal vivo)."""
        r = A.to_chat_request({"max_tokens": 5, "system": "S", "messages": [
            {"role": "system", "content": [{"type": "text", "text": "# Environment"}]},
            {"role": "user", "content": "ciao"},
            {"role": "system", "content": "<total_tokens>1 left</total_tokens>"}]})
        self.assertEqual([m["role"] for m in r["messages"]], ["system", "user", "user"])
        self.assertEqual(r["messages"][0]["content"], "S\n\n# Environment")
        self.assertTrue(r["messages"][2]["content"].startswith("[system]\n"))

    def test_tool_id(self):
        self.assertEqual(A.out_tool_id("call_a1"), "call_a1")  # id valido: intatto (ds4 lo riconosce)
        self.assertEqual(A.out_tool_id("toolu_x"), "toolu_x")
        self.assertEqual(A.out_tool_id("a.b:c"), "toolu_a_b_c")


class TestAnthropicHTTP(Base):
    def req(self, text, stream=False, **kw):
        return {"model": "qwen", "max_tokens": 512, "stream": stream, "system": "S", "tools": ATOOLS,
                "messages": [{"role": "user", "content": text}], **kw}

    def test_non_stream_text_and_thinking(self):
        st, _, raw = self.post("/v1/messages", self.req("ciao"))
        self.assertEqual(st, 200, raw)
        r = json.loads(raw)
        self.assertEqual(r["type"], "message")
        self.assertEqual([b["type"] for b in r["content"]], ["thinking", "text"])
        self.assertTrue(r["content"][0]["signature"])
        self.assertEqual(r["stop_reason"], "end_turn")
        self.assertIn("input_tokens", r["usage"])
        # cache_control ignorato, il motore riceve Chat Completions
        body = self.eng.requests[-1]
        self.assertEqual(body["messages"][0]["role"], "system")
        self.assertIn(RECALL_NAME, [t["function"]["name"] for t in body["tools"]])

    def test_non_stream_tool_use(self):
        st, _, raw = self.post("/v1/messages", self.req("fai due comandi"))
        r = json.loads(raw)
        tu = [b for b in r["content"] if b["type"] == "tool_use"]
        self.assertEqual([b["id"] for b in tu], ["call_a1", "call_a2"])
        self.assertEqual(tu[1]["input"], {"command": "pwd"})
        self.assertEqual(r["stop_reason"], "tool_use")
        # giro successivo con i tool_result: stessa conversazione, id intatti passati al motore
        msgs = [{"role": "user", "content": "fai due comandi"}, {"role": "assistant", "content": r["content"]},
                {"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": "call_a1", "content": "x"},
                    {"type": "tool_result", "tool_use_id": "call_a2", "content": [{"type": "text",
                                                                                        "text": "/tmp"}]}]}]
        st, _, raw = self.post("/v1/messages", {**self.req(""), "messages": msgs})
        self.assertEqual(st, 200, raw)
        self.assertEqual(json.loads(raw)["content"][-1]["text"], "fatto: /tmp")
        sent = self.eng.requests[-1]["messages"]
        self.assertEqual([m.get("tool_call_id") for m in sent if m["role"] == "tool"],
                         ["call_a1", "call_a2"])
        convs = {e.get("conv") for e in self.proxy.journal.mem if e.get("event") == "request"}
        self.assertEqual(len(convs), 1)

    def test_stream_tool_use(self):
        st, hdr, raw = self.post("/v1/messages", self.req("fai due comandi", stream=True))
        self.assertEqual(st, 200)
        self.assertEqual(hdr["Content-Type"], "text/event-stream")
        ev = self.sse(raw)
        names = [e for e, _ in ev]
        self.assertEqual(names[0], "message_start")
        self.assertEqual(names[-2:], ["message_delta", "message_stop"])
        starts = [d["content_block"] for e, d in ev if e == "content_block_start"]
        self.assertEqual([b["type"] for b in starts], ["thinking", "text", "tool_use", "tool_use"])
        self.assertEqual(starts[2]["id"], "call_a1")
        # indici consecutivi, ogni blocco aperto e chiuso
        self.assertEqual([d["index"] for e, d in ev if e == "content_block_stop"], [0, 1, 2, 3])
        js = {}
        for e, d in ev:
            if e == "content_block_delta" and d["delta"]["type"] == "input_json_delta":
                js[d["index"]] = js.get(d["index"], "") + d["delta"]["partial_json"]
        self.assertEqual(json.loads(js[3]), {"command": "pwd"})
        sig = [d for e, d in ev if e == "content_block_delta" and d["delta"]["type"] == "signature_delta"]
        self.assertEqual(len(sig), 1)
        md = [d for e, d in ev if e == "message_delta"][0]
        self.assertEqual(md["delta"]["stop_reason"], "tool_use")

    def test_stream_internal_recall_hidden(self):
        """strata_recall è risolto dal proxy: il client vede solo testo/ragionamento, nessun tool_use."""
        st, _, raw = self.post("/v1/messages", self.req("cerca snake", stream=True))
        ev = self.sse(raw)
        starts = [d["content_block"]["type"] for e, d in ev if e == "content_block_start"]
        self.assertNotIn("tool_use", starts)
        text = "".join(d["delta"].get("text", "") for e, d in ev if e == "content_block_delta")
        self.assertEqual(text, "dopo la ricerca")
        md = [d for e, d in ev if e == "message_delta"][0]
        self.assertEqual(md["delta"]["stop_reason"], "end_turn")
        self.assertEqual(len(self.eng.requests), 2)
        # non stream: idem
        st, _, raw = self.post("/v1/messages", self.req("cerca snake di nuovo"))
        r = json.loads(raw)
        self.assertEqual([b["type"] for b in r["content"]], ["thinking", "text"])

    def test_count_tokens_matches_counter(self):
        a = self.req("ciao " * 200)
        st, _, raw = self.post("/v1/messages/count_tokens", a)
        self.assertEqual(st, 200)
        n = json.loads(raw)["input_tokens"]
        req = A.to_chat_request(a)
        self.assertEqual(n, self.proxy.mgr.estimate(req["messages"], req["tools"])[0])
        self.assertGreater(n, 250)

    def test_errors_in_client_format(self):
        st, _, raw = self.post("/v1/messages", {"model": "m", "messages": [{"role": "user", "content": "x"}]})
        self.assertEqual(st, 400)
        e = json.loads(raw)
        self.assertEqual(e["type"], "error")
        self.assertEqual(e["error"]["type"], "invalid_request_error")
        self.assertIn("max_tokens", e["error"]["message"])
        st, _, raw = self.post("/v1/messages", self.req([{"type": "image", "source": {}}]))
        self.assertEqual(st, 400)
        self.assertIn("image", json.loads(raw)["error"]["message"])


# ====================================================================== Responses
class TestResponsesConvert(unittest.TestCase):
    def test_request_conversion(self):
        r = {"model": "m", "instructions": "Sei Codex.", "tools": RTOOLS, "max_output_tokens": 300,
             "reasoning": {"effort": "low"}, "input": [
                 {"type": "message", "role": "developer", "content": [{"type": "input_text", "text": "regole"}]},
                 {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "fai due comandi"}]},
                 {"type": "reasoning", "id": "rs_1", "summary": [{"type": "summary_text", "text": "penso"}],
                  "encrypted_content": "gAAA"},
                 {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "eseguo"}]},
                 {"type": "function_call", "call_id": "call_1", "name": "shell", "arguments": '{"command":"ls"}'},
                 {"type": "function_call", "call_id": "call_2", "name": "shell", "arguments": '{"command":"pwd"}'},
                 {"type": "function_call_output", "call_id": "call_1", "output": "a.txt"},
                 {"type": "function_call_output", "call_id": "call_2", "output": "/tmp"},
                 {"type": "custom_tool_call", "call_id": "call_3", "name": "apply_patch", "input": "*** Begin"},
                 {"type": "custom_tool_call_output", "call_id": "call_3", "output": "ok"},
                 {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "e ora?"}]}]}
        req, custom = R.to_chat_request(r)
        m = req["messages"]
        self.assertEqual(m[0], {"role": "system", "content": "Sei Codex.\n\nregole"})
        self.assertEqual(m[1], {"role": "user", "content": "fai due comandi"})
        self.assertEqual(m[2]["reasoning_content"], "penso")
        self.assertEqual(m[2]["content"], "eseguo")
        self.assertEqual([c["id"] for c in m[2]["tool_calls"]], ["call_1", "call_2"])
        self.assertEqual([x["role"] for x in m[3:]], ["tool", "tool", "assistant", "tool", "user"])
        self.assertEqual(json.loads(m[5]["tool_calls"][0]["function"]["arguments"]), {"input": "*** Begin"})
        self.assertEqual(custom, {"apply_patch"})
        self.assertEqual(req["max_tokens"], 300)
        self.assertEqual(req["reasoning_effort"], "low")
        self.assertIn("grammatica", req["tools"][1]["function"]["description"])

    def test_errors(self):
        with self.assertRaisesRegex(A.BadRequest, "previous_response_id"):
            R.to_chat_request({"input": "x", "previous_response_id": "resp_1"})
        with self.assertRaisesRegex(A.BadRequest, "input_image"):
            R.to_chat_request({"input": [{"type": "message", "role": "user",
                                          "content": [{"type": "input_image", "image_url": "data:"}]}]})


class TestResponsesHTTP(Base):
    def req(self, text, stream=False, **kw):
        return {"model": "qwen", "instructions": "S", "tools": RTOOLS, "stream": stream, "store": False,
                "input": [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": text}]}],
                **kw}

    def test_non_stream(self):
        st, _, raw = self.post("/v1/responses", self.req("fai due comandi"))
        self.assertEqual(st, 200, raw)
        r = json.loads(raw)
        self.assertEqual(r["object"], "response")
        self.assertEqual(r["status"], "completed")
        self.assertEqual([o["type"] for o in r["output"]], ["reasoning", "message", "function_call", "function_call"])
        self.assertEqual(r["output"][3]["call_id"], "call_a2")
        self.assertEqual(r["output"][1]["content"][0]["text"], "eseguo")

    def test_stream_function_calls_and_followup(self):
        st, hdr, raw = self.post("/v1/responses", self.req("fai due comandi", stream=True))
        self.assertEqual(st, 200)
        ev = self.sse(raw)
        names = [e for e, _ in ev]
        self.assertEqual(names[:2], ["response.created", "response.in_progress"])
        self.assertEqual(names[-1], "response.completed")
        self.assertEqual([d["sequence_number"] for _, d in ev], list(range(len(ev))))
        added = [d["item"]["type"] for e, d in ev if e == "response.output_item.added"]
        self.assertEqual(added, ["reasoning", "message", "function_call", "function_call"])
        done = [d["item"] for e, d in ev if e == "response.output_item.done"]
        self.assertEqual(json.loads(done[3]["arguments"]), {"command": "pwd"})
        args = "".join(d["delta"] for e, d in ev if e == "response.function_call_arguments.delta"
                       and d["output_index"] == 2)
        self.assertEqual(json.loads(args), {"command": "ls"})
        text = "".join(d["delta"] for e, d in ev if e == "response.output_text.delta")
        self.assertEqual(text, "eseguo")
        comp = ev[-1][1]["response"]
        self.assertEqual(len(comp["output"]), 4)
        self.assertIn("strata_context", comp)
        # giro successivo: Codex rimanda tutto (output come input) + function_call_output
        inp = self.req("fai due comandi")["input"] + comp["output"] + [
            {"type": "function_call_output", "call_id": "call_a1", "output": "a"},
            {"type": "function_call_output", "call_id": "call_a2", "output": "/tmp"}]
        st, _, raw = self.post("/v1/responses", {**self.req("", stream=True), "input": inp})
        ev = self.sse(raw)
        self.assertEqual("".join(d["delta"] for e, d in ev if e == "response.output_text.delta"), "fatto: /tmp")
        sent = self.eng.requests[-1]["messages"]
        self.assertEqual(sent[2]["reasoning_content"], "servono due comandi")
        convs = {e.get("conv") for e in self.proxy.journal.mem if e.get("event") == "request"}
        self.assertEqual(len(convs), 1)

    def test_custom_tool_roundtrip(self):
        st, _, raw = self.post("/v1/responses", self.req("applica la patch", stream=True))
        ev = self.sse(raw)
        done = [d["item"] for e, d in ev if e == "response.output_item.done"]
        self.assertEqual(done[-1]["type"], "custom_tool_call")
        self.assertEqual(done[-1]["input"], "*** Begin Patch\n*** End Patch")

    def test_internal_recall_hidden(self):
        st, _, raw = self.post("/v1/responses", self.req("cerca snake", stream=True))
        ev = self.sse(raw)
        added = [d["item"]["type"] for e, d in ev if e == "response.output_item.added"]
        self.assertNotIn("function_call", added)
        self.assertEqual("".join(d["delta"] for e, d in ev if e == "response.output_text.delta"), "dopo la ricerca")

    def test_errors(self):
        st, _, raw = self.post("/v1/responses", {"model": "m"})
        self.assertEqual(st, 400)
        self.assertIn("input", json.loads(raw)["error"]["message"])


if __name__ == "__main__":
    unittest.main()
