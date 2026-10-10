"""Motori: rilevamento, capacità, llama-server con salvataggi, motore OpenAI generico; server MCP.

    python3 -m unittest -v tests.test_engines
"""
from __future__ import annotations

import io
import json
import os
import sqlite3
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ctxproxy import engines  # noqa: E402
from ctxproxy.core import Config, Journal, Store, TokenCounter  # noqa: E402
from ctxproxy.server import Proxy  # noqa: E402
from ctxproxy.upstream import Upstream, UpstreamError  # noqa: E402
from tests.fake_engine import FakeEngine  # noqa: E402
from tests.test_proxy import TOOLS, history  # noqa: E402


class TestDetect(unittest.TestCase):
    def run_eng(self, **kw):
        eng = FakeEngine(**kw)
        url = eng.start()
        self.addCleanup(eng.stop)
        return eng, Upstream(url)

    def test_strata(self):
        _, up = self.run_eng()
        e = engines.detect(up)
        self.assertEqual((e.kind, e.slot_save, e.status_kind, e.detected), ("strata", True, "strata", True))
        self.assertEqual(e.n_ctx, 131072, "finestra da context.native di /v1/status")

    def test_strata_window_from_status(self):
        _, up = self.run_eng(n_ctx=65536)
        e = engines.detect(up)
        self.assertEqual(e.kind, "strata")
        self.assertEqual(e.n_ctx, 65536)
        cfg = Config()
        engines.apply(cfg, e, None, log=None)
        self.assertEqual(cfg.window, 65536, "la finestra di Strata viene dal motore")
        self.assertEqual(cfg.mask_trigger, 40000, "soglie scalate x0.5 con la finestra")
        self.assertEqual(cfg.keep_recent_tokens, 8000, "soglie scalate x0.5 con la finestra")

    def test_llama_with_slots(self):
        eng, up = self.run_eng(flavor="llama", n_ctx=16384)
        e = engines.detect(up)
        self.assertEqual(e.kind, "llama.cpp")
        self.assertTrue(e.slot_save)
        self.assertTrue(e.tokenize)
        self.assertEqual(e.n_ctx, 16384)
        self.assertEqual(e.model, "fake-0.6B.gguf")
        self.assertEqual(eng.slots, [], "la sonda non tocca lo slot")

    def test_llama_without_slot_save_path(self):
        _, up = self.run_eng(flavor="llama", slot_save=False)
        e = engines.detect(up)
        self.assertEqual(e.kind, "llama.cpp")
        self.assertFalse(e.slot_save)
        self.assertTrue(any("--slot-save-path" in n for n in e.notes))

    def test_llama_parallel_slots(self):
        _, up = self.run_eng(flavor="llama", n_slots=4, n_ctx=32768)
        e = engines.detect(up)
        self.assertEqual(e.n_ctx, 8192, "n_ctx per slot")
        self.assertTrue(any("--parallel" in n for n in e.notes))

    def test_openai_generic(self):
        _, up = self.run_eng(flavor="openai")
        e = engines.detect(up)
        self.assertEqual((e.kind, e.slot_save, e.status_kind), ("openai", False, None))

    def test_forced(self):
        _, up = self.run_eng(flavor="llama")
        e = engines.detect(up, "openai")
        self.assertEqual(e.kind, "openai")
        self.assertFalse(e.detected)
        self.assertEqual(engines.detect(up, "llama").kind, "llama.cpp")
        with self.assertRaises(ValueError):
            engines.detect(up, "vllm")

    def test_unreachable_defaults_to_openai(self):
        e = engines.detect(Upstream("http://127.0.0.1:9"))
        self.assertEqual(e.kind, "openai")


class TestApply(unittest.TestCase):
    def test_disables_slot_features(self):
        cfg = Config(slot_save=True, mask_anchor=True, autosave=True, seal_experimental=True, kv_archive=True)
        j = Journal(None)
        w = engines.apply(cfg, engines.Engine("openai", slot_save=False, status_kind=None), j, log=None)
        self.assertEqual(len(w), 1)
        for f in engines.NEEDS_SLOTS:
            self.assertFalse(getattr(cfg, f), f)
        self.assertTrue(cfg.mask_enabled and cfg.segments_enabled, "il resto resta acceso")
        self.assertTrue([e for e in j.mem if e["event"] == "engine_warning"])

    def test_keeps_slot_features_on_strata(self):
        cfg = Config(slot_save=True, autosave=True)
        self.assertEqual(engines.apply(cfg, engines.Engine(), None, log=None), [])
        self.assertTrue(cfg.slot_save and cfg.autosave)

    def test_window_from_n_ctx_scales_defaults(self):
        cfg = Config()
        engines.apply(cfg, engines.Engine("llama.cpp", n_ctx=32768, status_kind="llama.cpp"), None, log=None)
        self.assertEqual(cfg.window, 32768)
        self.assertEqual(cfg.mask_trigger, 20000)
        self.assertEqual(cfg.keep_recent_tokens, 4000)

    def test_explicit_window_wins(self):
        cfg = Config(window=8000, mask_trigger=5000)
        engines.apply(cfg, engines.Engine("llama.cpp", n_ctx=32768, status_kind="llama.cpp"), None,
                      explicit={"window", "mask_trigger"}, log=None)
        self.assertEqual((cfg.window, cfg.mask_trigger), (8000, 5000))


class LlamaBase(unittest.TestCase):
    """Proxy davanti a un llama-server finto con --slot-save-path."""
    flavor, slot_save = "llama", True
    cfg_over: dict = {"autosave": True, "autosave_idle_s": 180, "autosave_min_tokens": 2000,
                      "autorestore_min_gain": 1000, "slot_save": True}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.eng = FakeEngine(flavor=self.flavor, slot_save=self.slot_save, n_ctx=131072)
        self.url = self.eng.start()
        self.slot_dir = os.path.join(self.tmp.name, "slots")
        os.makedirs(self.slot_dir)
        raw = {"window": 131072, "mask_trigger": 30000, "mask_target": 15000, "keep_recent_tokens": 6000,
               "min_mask_tokens": 200, "slot_dir": self.slot_dir, "autosave_min_free_gb": 0, **self.cfg_over}
        cfg = Config.from_dict(raw)
        self.jpath = os.path.join(self.tmp.name, "journal.jsonl")
        up = Upstream(self.url)
        j = Journal(self.jpath)
        self.engine = engines.setup(cfg, up, j, explicit=set(raw), log=None)
        self.proxy = Proxy(cfg, up, Store(os.path.join(self.tmp.name, "a.sqlite")), j, TokenCounter(3.5))
        self.saver = self.proxy.enable_autosave(start=False) if cfg.autosave else None

    def tearDown(self):
        self.eng.stop()
        self.proxy.store.db.close()
        self.tmp.cleanup()

    def send(self, msgs, **kw):
        st, r, hdr = self.proxy.chat({"model": "m", "messages": msgs, "tools": TOOLS, "max_tokens": 1024, **kw})
        self.assertEqual(st, 200, r)
        return r

    def events(self, name):
        with open(self.jpath) as f:
            return [e for e in map(json.loads, f) if e["event"] == name]

    def tick(self):
        ev = self.saver.tick(now=time.time() + 200)
        if ev:
            with open(os.path.join(self.slot_dir, ev["file"]), "wb") as f:
                f.write(b"x" * 16)
        return ev

    def conv(self, first="crea il gioco snake"):
        return history(6, first=first) + [{"role": "assistant", "content": "fatto"},
                                          {"role": "user", "content": "e adesso?"}]


class TestLlamaServer(LlamaBase):
    def test_requests_pin_slot_and_cache_prompt(self):
        self.send(self.conv())
        body = self.eng.requests[-1]
        self.assertEqual(body.get("id_slot"), 0)
        self.assertIs(body.get("cache_prompt"), True)

    def test_autosave_and_autorestore_after_restart(self):
        h = self.conv()
        self.send(h)
        ev = self.tick()
        self.assertIsNotNone(ev, "autosave con lo stato ricavato da /slots")
        self.assertEqual(self.eng.slots[-1][:2], ("save", ev["file"]))
        self.assertIsNone(self.tick(), "stesso stato: niente secondo salvataggio")
        self.eng.restart()
        n = len(self.eng.slots)
        self.send(h + [{"role": "assistant", "content": "r"}, {"role": "user", "content": "continua"}])
        self.assertEqual(self.eng.slots[n][:2], ("restore", ev["file"]))
        ar = self.events("autorestore")
        self.assertEqual(len(ar), 1)
        self.assertGreater(ar[0]["n_restored"], 1000)
        req = self.events("request")[-1]
        self.assertGreater(req["reused"], 0.9 * ev["tokens"])

    def test_no_restore_when_engine_has_it(self):
        h = self.conv()
        self.send(h)
        self.tick()
        self.send(h + [{"role": "assistant", "content": "x"}, {"role": "user", "content": "y"}])
        self.assertFalse(self.events("autorestore"))
        self.assertFalse(self.events("strata_state_lost"), "id_task cresce di 3 per richiesta: non è estraneo")

    def test_foreign_request_detected(self):
        self.send(self.conv())
        Upstream(self.url).chat({"messages": [{"role": "user", "content": "altro client"}]})
        self.assertIsNone(self.tick())
        self.assertEqual(self.events("strata_state_lost")[-1]["reason"], "foreign_request")

    def test_busy_slot_blocks_autosave(self):
        self.send(self.conv())
        self.eng.busy = True
        self.assertIsNone(self.tick())
        self.eng.busy = False
        self.assertIsNotNone(self.tick())

    def test_missing_file_is_404(self):
        with self.assertRaises(UpstreamError) as cm:
            self.proxy.up.up.slot("restore", "manca.bin")
        self.assertEqual(cm.exception.status, 404, "400 di llama-server con file assente -> 404 come Strata")

    def test_missing_autosave_file_marked_deleted(self):
        h = self.conv()
        self.send(h)
        ev = self.tick()
        self.eng.files.pop(ev["file"])
        os.remove(os.path.join(self.slot_dir, ev["file"]))
        self.eng.restart()
        self.send(h + [{"role": "assistant", "content": "r"}, {"role": "user", "content": "continua"}])
        self.assertEqual(self.events("autorestore_error")[-1]["status"], 404)
        self.assertFalse(self.proxy.store.autosaves(), "file sparito: segnato cancellato")


class TestLlamaNoSlotSave(LlamaBase):
    slot_save = False

    def test_features_disabled_rest_works(self):
        self.assertFalse(self.engine.slot_save)
        self.assertFalse(self.proxy.cfg.autosave or self.proxy.cfg.slot_save)
        self.assertTrue(self.events("engine_warning"))
        r = self.send(self.conv())
        self.assertEqual(r["choices"][0]["message"]["content"], "ok")
        self.assertEqual(self.eng.slots, [])


class TestOpenAIGeneric(LlamaBase):
    flavor = "openai"
    cfg_over = {"autosave": True, "slot_save": True, "mask_anchor": True, "inject_recall": True}

    def test_masking_segments_recall_work_without_slots(self):
        self.assertEqual(self.engine.kind, "openai")
        self.assertIsNone(self.saver)
        h = history(14, size=9000)
        h += [{"role": "assistant", "content": "fatto"}, {"role": "user", "content": "e ora?"}]
        r = self.send(h)
        self.assertEqual(r["choices"][0]["message"]["content"], "ok")
        self.assertGreater(r["strata_context"]["masked"], 0, "nascondimento attivo anche senza salvataggi")
        self.assertNotIn("id_slot", self.eng.requests[-1], "niente campi llama.cpp verso un motore generico")
        self.assertEqual(self.eng.slots, [])
        out = self.proxy.mgr.recall(r["strata_context"]["conversation_id"], {"query": "riga 2 del file f2.txt"})
        self.assertIn("f2.txt", out)


class TestRemoteTokenizer(unittest.TestCase):
    def test_counts_via_tokenize(self):
        eng = FakeEngine(flavor="llama")
        up = Upstream(eng.start())
        self.addCleanup(eng.stop)
        cfg = Config(engine_tokenize=True)
        tc = TokenCounter(3.5)
        engines.setup(cfg, up, None, counter=tc, log=None)
        self.assertTrue(tc.exact)
        self.assertEqual(tc.raw("x" * 70), 20)


# ---------------------------------------------------------------- MCP
def make_archive(path):
    st = Store(path)
    now = time.time()
    rows = [("r%011d" % i, "convA", i, role, name, text, len(text) // 4, now + i) for i, (role, name, text) in
            enumerate([("user", None, "crea il gioco snake isometrico"),
                       ("assistant-tool-args", None, 'write {"path": "src/game.js", "content": "const speed = 4"}'),
                       ("tool", "write", "Successfully wrote 120 bytes to src/game.js"),
                       ("assistant", None, "Ho scritto src/game.js con la velocità iniziale startSpeed = 4"),
                       ("user", None, "cambia la velocità a 6")])]
    rows.append(("rzzzzzzzzzzz", "convB", 0, "user", None, "altra conversazione sul parser json", 8, now - 100))
    st.archive_many(rows)
    st.db.close()
    with open(os.path.join(os.path.dirname(path), "journal.jsonl"), "w") as f:
        for i in range(5):
            f.write(json.dumps({"ts": now + i, "event": "request" if i % 2 else "recall", "conv": "convA",
                                "messages": ["x"] * 3}) + "\n")


class TestMcp(unittest.TestCase):
    def setUp(self):
        from ctxproxy import mcp_server
        self.m = mcp_server
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "archive.sqlite")
        make_archive(self.db)
        os.chmod(self.db, 0o444)
        self.mtime = os.stat(self.db).st_mtime_ns
        self.srv = mcp_server.McpServer(mcp_server.Archive(self.db))

    def tearDown(self):
        self.srv.arc.store.db.close()
        os.chmod(self.db, 0o644)
        self.tmp.cleanup()

    def call(self, name, args=None):
        r = self.srv.handle({"jsonrpc": "2.0", "id": 7, "method": "tools/call",
                             "params": {"name": name, "arguments": args or {}}})
        self.assertFalse(r["result"]["isError"], r)
        return r["result"]["content"][0]["text"]

    def test_initialize_and_list(self):
        r = self.srv.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                             "params": {"protocolVersion": "2025-03-26"}})
        self.assertEqual(r["result"]["protocolVersion"], "2025-03-26")
        self.assertIn("tools", r["result"]["capabilities"])
        self.assertIsNone(self.srv.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}))
        names = [t["name"] for t in self.srv.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
                 ["result"]["tools"]]
        self.assertEqual(names, ["recall", "conversations", "journal"])
        self.assertEqual(self.srv.handle({"jsonrpc": "2.0", "id": 3, "method": "nope"})["error"]["code"], -32601)

    def test_recall_query_id_path_tree(self):
        out = self.call("recall", {"query": "velocità iniziale"})
        self.assertIn("conversation convA", out)
        self.assertIn("startSpeed", out)
        self.assertIn("startSpeed", self.call("recall", {"queries": ["start speed", "velocita"]}),
                      "parole flessibili: startSpeed spezzato")
        self.assertIn("Ho scritto", self.call("recall", {"id": "r00000000003"}))
        self.assertIn("src/game.js", self.call("recall", {"path": "src/game.js", "mode": "timeline"}) +
                      self.call("recall", {"query": "game.js"}))
        # 0.3.1: the MCP server answers in English (Config.prompt_language default; its descriptions were already English)
        self.assertIn("map of the session", self.call("recall", {"mode": "tree"}))
        self.assertIn("parser", self.call("recall", {"conversation": "convB", "query": "parser"}))
        self.assertIn("convB", self.call("recall", {"id": "rzzzzzzzzzzz"}), "con id: la conversazione del pezzo")

    def test_conversations_and_journal(self):
        d = json.loads(self.call("conversations"))
        self.assertEqual([c["conversation"] for c in d["conversations"]], ["convA", "convB"])
        self.assertEqual(d["conversations"][0]["messages"], 5)
        self.assertEqual(json.loads(self.call("conversations", {"query": "parser"}))["conversations"][0]
                         ["conversation"], "convB")
        lines = self.call("journal", {"limit": 2, "event": "recall"}).splitlines()
        self.assertEqual(len(lines), 2)
        self.assertTrue(all(json.loads(x)["event"] == "recall" and "messages" not in json.loads(x) for x in lines))

    def test_read_only(self):
        self.call("recall", {"query": "snake"})
        self.call("recall", {"query": "snake"})
        self.assertEqual(os.stat(self.db).st_mtime_ns, self.mtime)
        names = {r[0] for r in sqlite3.connect(self.db).execute("SELECT name FROM sqlite_master")}
        self.assertNotIn("passages", names, "indice dei passaggi solo in TEMP")
        with self.assertRaises(sqlite3.OperationalError):
            self.srv.arc.store.db.execute("INSERT INTO kv VALUES('a','b')")

    def test_stdio_roundtrip(self):
        inp = io.BytesIO(b"\n".join(json.dumps(m).encode() for m in [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
             "params": {"name": "recall", "arguments": {"query": "snake"}}}]) + b"\nnon-json\n")
        out = io.BytesIO()
        self.srv.serve_stdio(inp, out)
        resp = [json.loads(x) for x in out.getvalue().splitlines()]
        self.assertEqual([r.get("id") for r in resp], [1, 2, None])
        self.assertIn("snake", resp[1]["result"]["content"][0]["text"])
        self.assertEqual(resp[2]["error"]["code"], -32700)


if __name__ == "__main__":
    unittest.main()
