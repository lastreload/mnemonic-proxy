# Author: Maurizio Verde — LastReload
"""ds4-server dietro al proxy, con un ds4 finto (tests/fake_engine.py, flavor="ds4"; nessuna GPU).

    python3 -m unittest -v tests.test_ds4

Cosa si verifica (comportamento del vero ds4-server letto in ds4_server.c):
- riconoscimento da GET /v1/models (owned_by "ds4.c", context_length) e capacità: niente save/restore, niente
  kv_archive, finestra da --ctx, mask_tool_args spento (ds4 rimette le chiamate campionate prese per id);
- id degli strumenti INTATTI nei tre protocolli (Chat Completions, Anthropic Messages, Responses): l'id generato
  dal motore arriva al client e torna al motore identico, quindi ds4 ritrova il testo campionato (nessun rendering
  canonico, nessun prefisso riletto);
- recall interna: la richiesta successiva al motore contiene la chiamata assistant originale con l'id del motore,
  e il turno dopo del client la reinserisce identica;
- un risultato di strumento con id sconosciuto e senza la chiamata precedente viene rifiutato (400) come dal vero;
- Responses senza stato con mask_reasoning: il ragionamento dell'ultimo turno con chiamate resta integro;
- allineamento dei pacchetti di masking ai checkpoint su disco (checkpoint_align_tokens, spento di serie).
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

from ctxproxy import engines  # noqa: E402
from ctxproxy.core import RECALL_NAME, Config, Journal, Store, TokenCounter  # noqa: E402
from ctxproxy.server import Proxy, make_handler  # noqa: E402
from ctxproxy.upstream import Upstream  # noqa: E402
from tests.fake_engine import FakeEngine  # noqa: E402
from tests.test_proxy import TOOLS, history  # noqa: E402


def policy(body):
    """Le chiamate NON hanno id: lo assegna il ds4 finto (call_<hex>), come il vero."""
    msgs = body["messages"]
    last = msgs[-1]
    if last.get("role") == "tool":
        prev = next(m for m in reversed(msgs) if m.get("role") == "assistant")
        names = [(c.get("function") or {}).get("name") for c in prev.get("tool_calls") or []]
        if RECALL_NAME in names:
            return {"role": "assistant", "content": "dopo la ricerca", "reasoning_content": "ho trovato"}
        return {"role": "assistant", "content": "fatto: " + str(last.get("content"))[:20]}
    t = str(last.get("content"))
    if "due comandi" in t:
        return {"role": "assistant", "content": "eseguo", "reasoning_content": "servono due comandi",
                "tool_calls": [
                    {"type": "function", "function": {"name": "bash", "arguments": '{"command":"ls"}'}},
                    {"type": "function", "function": {"name": "bash", "arguments": '{"command":"pwd"}'}}]}
    if "cerca" in t:
        return {"role": "assistant", "content": "", "reasoning_content": "cerco nell'archivio", "tool_calls": [
            {"type": "function", "function": {"name": RECALL_NAME, "arguments": '{"query":"snake"}'}}]}
    return {"role": "assistant", "content": "ok", "reasoning_content": "rispondo"}


class Ds4Base(unittest.TestCase):
    cfg_raw: dict = {}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.eng = FakeEngine(policy, flavor="ds4", n_ctx=65536)
        self.url = self.eng.start()
        raw = {"slot_save": True, "mask_anchor": True, "autosave": True, "kv_archive": True, "mask_tool_args": True,
               "mask_reasoning": True, **self.cfg_raw}
        cfg = Config.from_dict(raw)
        self.jpath = os.path.join(self.tmp.name, "journal.jsonl")
        up = Upstream(self.url)
        j = Journal(self.jpath)
        self.engine = engines.setup(cfg, up, j, explicit=set(raw), log=None)
        self.cfg = cfg
        self.proxy = Proxy(cfg, up, Store(os.path.join(self.tmp.name, "a.sqlite")), j, TokenCounter(3.5))
        self.proxy.engine = self.engine
        self.srv = None

    def tearDown(self):
        if self.srv:
            self.srv.shutdown()
            self.srv.server_close()
        self.eng.stop()
        self.proxy.store.db.close()
        self.tmp.cleanup()

    # ---- HTTP (per Anthropic/Responses) ----
    def http(self):
        from http.server import ThreadingHTTPServer
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.proxy))
        self.srv.daemon_threads = True
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        return "http://127.0.0.1:%d" % self.srv.server_address[1]

    def post(self, base, path, obj):
        req = urllib.request.Request(base + path, json.dumps(obj).encode(), method="POST",
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
                    data = json.loads(line[5:].strip())
            if data is not None:
                out.append((ev, data))
        return out

    def assert_no_reprefill_by_id(self):
        self.assertEqual(self.eng.ds4_replay["canonical"], 0,
                         "chiamate rimandate con id sconosciuto: %s" % self.eng.ds4_replay["missing_ids"])
        self.assertEqual(self.eng.rejected, [])
        self.assertEqual(self.eng.slots, [], "nessuna chiamata a /slots o ad altri endpoint")


# ====================================================================== motore e capacità
class TestDs4Engine(Ds4Base):
    def test_detect(self):
        e = self.engine
        self.assertEqual((e.kind, e.slot_save, e.status_kind, e.detected), ("ds4", False, None, True))
        self.assertEqual(e.n_ctx, 65536)
        self.assertEqual(e.model, "Qwen3.8-Flash-Next")

    def test_capabilities_applied(self):
        for f in engines.NEEDS_SLOTS:
            self.assertFalse(getattr(self.cfg, f), f)
        self.assertFalse(self.cfg.mask_tool_args, "ds4 rimette le chiamate campionate per id")
        self.assertTrue(self.cfg.mask_reasoning, "il resto resta come configurato")
        self.assertEqual(self.cfg.window, 65536, "finestra da context_length (= --ctx)")
        w = [x for x in self.proxy.journal.mem if x.get("event") == "engine_warning"] if hasattr(
            self.proxy.journal, "mem") else []
        self.assertTrue(w or os.path.exists(self.jpath))

    def test_not_confused_with_others(self):
        for flavor, kind in (("openai", "openai"), ("strata", "strata"), ("llama", "llama.cpp")):
            eng = FakeEngine(flavor=flavor)
            url = eng.start()
            try:
                self.assertEqual(engines.detect(Upstream(url)).kind, kind, flavor)
            finally:
                eng.stop()

    def test_forced_and_aliases(self):
        self.assertEqual(engines.norm_kind("ds4-server"), "ds4")
        e = engines.detect(Upstream("http://127.0.0.1:9"), "ds4")
        self.assertEqual((e.kind, e.slot_save, e.detected), ("ds4", False, False))
        self.assertTrue(e.notes)

    def test_exact_replay_off_keeps_tool_args(self):
        cfg = Config(mask_tool_args=True, ds4_exact_tool_replay=False)
        engines.apply(cfg, engines.Engine("ds4", slot_save=False, status_kind=None), None, log=None)
        self.assertTrue(cfg.mask_tool_args)

    def test_response_floor_fits_small_window(self):
        """Visto davvero (GLM su ds4, --ctx 16384): response_floor 16384 scritto in configurazione = un cambio di
        segmento a ogni richiesta. Con una finestra piccola il minimo si abbassa a un quarto della finestra."""
        cfg = Config(response_floor=16384)
        engines.apply(cfg, engines.Engine("ds4", slot_save=False, status_kind=None, n_ctx=16384), None,
                      explicit={"response_floor"}, log=None)
        self.assertEqual((cfg.window, cfg.response_floor), (16384, 4096))

    def test_fake_rejects_unknown_tool_result(self):
        """Il ds4 finto fa come il vero: risultato con id sconosciuto e senza chiamata precedente -> 400."""
        up = Upstream(self.url)
        st, _, body = up.raw("POST", "/v1/chat/completions", json.dumps({"messages": [
            {"role": "user", "content": "x"}, {"role": "tool", "tool_call_id": "call_ignoto", "content": "r"}]}).encode())
        self.assertEqual(st, 400)
        self.assertIn(b"replaying the full", body)


# ====================================================================== id intatti: Chat Completions
class TestDs4ChatIds(Ds4Base):
    def send(self, msgs):
        st, r, _ = self.proxy.chat({"model": "qwen3.8-flash-next", "messages": msgs, "tools": TOOLS,
                                    "max_tokens": 512})
        self.assertEqual(st, 200, r)
        return r["choices"][0]["message"]

    def test_engine_ids_roundtrip(self):
        msgs = [{"role": "system", "content": "S"}, {"role": "user", "content": "fai due comandi"}]
        m = self.send(msgs)
        ids = [c["id"] for c in m["tool_calls"]]
        self.assertEqual(ids, sorted(self.eng.ds4_live_ids, key=ids.index), "id del motore al client, intatti")
        self.assertTrue(all(i.startswith("call_") and len(i) > 20 for i in ids))
        msgs += [m, {"role": "tool", "tool_call_id": ids[0], "content": "a"},
                 {"role": "tool", "tool_call_id": ids[1], "content": "/tmp"}]
        self.assertEqual(self.send(msgs)["content"], "fatto: /tmp")
        sent = self.eng.requests[-1]["messages"]
        self.assertEqual([c["id"] for c in sent[2]["tool_calls"]], ids)
        self.assertEqual([x.get("tool_call_id") for x in sent if x["role"] == "tool"], ids)
        self.assert_no_reprefill_by_id()

    def test_internal_recall_keeps_engine_id(self):
        msgs = [{"role": "system", "content": "S"}, {"role": "user", "content": "cerca snake"}]
        m = self.send(msgs)
        self.assertEqual(m["content"], "dopo la ricerca")
        self.assertFalse(m.get("tool_calls"), "la recall non arriva al client")
        self.assertEqual(len(self.eng.requests), 2)
        first_id = [k for k, v in self.eng.ds4_memory.items()][0]
        second = self.eng.requests[1]["messages"]
        a = [x for x in second if x["role"] == "assistant"][-1]
        self.assertEqual([c["id"] for c in a["tool_calls"]], [first_id], "chiamata originale con l'id del motore")
        self.assertEqual(a["reasoning_content"], "cerco nell'archivio")
        self.assertEqual(second[-1], {"role": "tool", "tool_call_id": first_id, "content": second[-1]["content"]})
        # turno dopo del client: la recall interna è reinserita identica (stesso id) -> nessun 400, nessun canonico
        msgs += [m, {"role": "user", "content": "grazie"}]
        self.send(msgs)
        third = self.eng.requests[2]["messages"]
        a3 = [x for x in third if x.get("tool_calls")]
        self.assertEqual([c["id"] for c in a3[0]["tool_calls"]], [first_id])
        self.assertTrue(self.eng.prompts[2].startswith(self.eng.prompts[1][:len(self.eng.prompts[1]) - 200]))
        self.assert_no_reprefill_by_id()

    def test_unknown_tool_result_surfaces_400(self):
        st, r, _ = self.proxy.chat({"model": "m", "tools": TOOLS, "messages": [
            {"role": "user", "content": "x"}, {"role": "tool", "tool_call_id": "call_perso", "content": "r"}]})
        self.assertEqual(st, 400)
        self.assertIn("replaying the full", json.dumps(r))


# ====================================================================== id intatti: Anthropic e Responses
class TestDs4ClientIds(Ds4Base):
    ATOOLS = [{"name": "bash", "description": "esegue", "input_schema": {
        "type": "object", "properties": {"command": {"type": "string"}}}}]
    RTOOLS = [{"type": "function", "name": "bash", "description": "esegue", "parameters": {
        "type": "object", "properties": {"command": {"type": "string"}}}}]

    def test_anthropic_ids_intact_stream_and_not(self):
        base = self.http()
        for stream in (False, True):
            msgs = [{"role": "user", "content": "fai due comandi"}]
            st, raw = self.post(base, "/v1/messages", {"model": "q", "max_tokens": 256, "system": "S",
                                                       "tools": self.ATOOLS, "messages": msgs, "stream": stream})
            self.assertEqual(st, 200, raw)
            if stream:
                ev = self.sse(raw)
                blocks = [d["content_block"] for e, d in ev if e == "content_block_start"]
                tu = [b for b in blocks if b["type"] == "tool_use"]
                js = {}
                for e, d in ev:
                    if e == "content_block_delta" and d["delta"]["type"] == "input_json_delta":
                        js[d["index"]] = js.get(d["index"], "") + d["delta"]["partial_json"]
                idx = [i for i, b in enumerate(blocks) if b["type"] == "tool_use"]
                content = [{"type": "thinking", "thinking": "servono due comandi", "signature": "x"},
                           {"type": "text", "text": "eseguo"}] + [
                    {**b, "input": json.loads(js[i])} for b, i in zip(tu, idx)]
            else:
                content = json.loads(raw)["content"]
                tu = [b for b in content if b["type"] == "tool_use"]
            ids = [b["id"] for b in tu]
            self.assertEqual(set(ids), self.eng.ds4_live_ids, "id del motore, senza prefisso toolu_")
            msgs += [{"role": "assistant", "content": content}, {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": ids[0], "content": "a"},
                {"type": "tool_result", "tool_use_id": ids[1], "content": "/tmp"}]}]
            st, raw = self.post(base, "/v1/messages", {"model": "q", "max_tokens": 256, "system": "S",
                                                       "tools": self.ATOOLS, "messages": msgs})
            self.assertEqual(st, 200, raw)
            sent = self.eng.requests[-1]["messages"]
            self.assertEqual([x.get("tool_call_id") for x in sent if x["role"] == "tool"], ids)
            self.assertEqual([c["id"] for c in sent[2]["tool_calls"]], ids)
        self.assert_no_reprefill_by_id()

    def test_responses_ids_and_last_reasoning_intact(self):
        base = self.http()
        inp = [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": "fai due comandi"}]}]
        st, raw = self.post(base, "/v1/responses", {"model": "q", "instructions": "S", "tools": self.RTOOLS,
                                                    "store": False, "input": inp, "stream": True})
        self.assertEqual(st, 200, raw)
        comp = self.sse(raw)[-1][1]["response"]
        calls = [o for o in comp["output"] if o["type"] == "function_call"]
        ids = [c["call_id"] for c in calls]
        self.assertEqual(set(ids), self.eng.ds4_live_ids)
        inp = inp + comp["output"] + [{"type": "function_call_output", "call_id": ids[0], "output": "a"},
                                      {"type": "function_call_output", "call_id": ids[1], "output": "/tmp"}]
        st, raw = self.post(base, "/v1/responses", {"model": "q", "instructions": "S", "tools": self.RTOOLS,
                                                    "store": False, "input": inp})
        self.assertEqual(st, 200, raw)
        sent = self.eng.requests[-1]["messages"]
        a = sent[2]
        self.assertEqual([c["id"] for c in a["tool_calls"]], ids)
        self.assertEqual(a["reasoning_content"], "servono due comandi", "ragionamento dell'ultimo turno integro")
        self.assert_no_reprefill_by_id()


# ====================================================================== ragionamento: ultimo turno integro
class TestDs4LastTurnReasoning(Ds4Base):
    cfg_raw = {"mask_trigger": 30000, "mask_target": 15000, "keep_recent_tokens": 6000, "min_mask_tokens": 200,
               "min_batch_tokens": 1000, "window": 131072}

    def test_masking_spares_last_turn_reasoning(self):
        msgs = history(12, size=6000)
        for m in msgs:
            if m.get("role") == "assistant":
                m["reasoning_content"] = "ragionamento lungo del passo. " * 120
        last = msgs[-2]
        self.assertEqual(last["role"], "assistant")
        st, r, _ = self.proxy.chat({"model": "m", "messages": msgs, "tools": TOOLS, "max_tokens": 512})
        self.assertEqual(st, 200, r)
        self.assertTrue([e for e in self.proxy.journal.mem if e.get("event") == "mask"] if hasattr(
            self.proxy.journal, "mem") else self._events("mask"), "il pacchetto di masking è scattato")
        sent = self.eng.requests[-1]["messages"]
        s_last = [m for m in sent if m.get("role") == "assistant"][-1]
        self.assertEqual(s_last["tool_calls"][0]["id"], last["tool_calls"][0]["id"])
        self.assertEqual(s_last["reasoning_content"], last["reasoning_content"], "ultimo turno con chiamate integro")
        self.assertEqual(s_last["tool_calls"], last["tool_calls"])
        s_first = [m for m in sent if m.get("role") == "assistant"][0]
        self.assertEqual(s_first["reasoning_content"], "", "le parti vecchie sì, nascoste")
        self.assertEqual(self.eng.rejected, [])

    def _events(self, name):
        with open(self.jpath) as f:
            return [e for e in map(json.loads, f) if e["event"] == name]


# ====================================================================== allineamento ai checkpoint su disco
class TestCheckpointAlign(Ds4Base):
    cfg_raw = {"mask_trigger": 30000, "mask_target": 15000, "keep_recent_tokens": 6000, "min_mask_tokens": 200,
               "min_batch_tokens": 1000, "window": 131072}

    def mask_event(self, align):
        self.cfg.checkpoint_align_tokens = align
        msgs = history(20, size=9000, first="allineamento %d" % align)
        st, r, _ = self.proxy.chat({"model": "m", "messages": msgs, "tools": TOOLS, "max_tokens": 512})
        self.assertEqual(st, 200, r)
        with open(self.jpath) as f:
            return [e for e in map(json.loads, f) if e["event"] == "mask"][-1]

    def test_off_by_default(self):
        self.assertEqual(Config().checkpoint_align_tokens, 0)
        ev = self.mask_event(0)
        self.assertNotIn("align", ev)

    def test_first_change_lands_after_a_multiple(self):
        ev0 = self.mask_event(0)
        n = 4096
        ev = self.mask_event(n)
        self.assertEqual(ev["align"], n)
        self.assertLessEqual(ev["first_offset_est"] % n, 0.25 * n)
        self.assertGreaterEqual(ev["first_index"], ev0["first_index"])
        self.assertGreaterEqual(ev["tokens_saved"], self.cfg.min_batch_tokens)


if __name__ == "__main__":
    unittest.main()
