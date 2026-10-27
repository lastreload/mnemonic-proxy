"""Offline forget regressions; only local SQLite and the scripted fake engine, no model/GPU."""
from contextlib import redirect_stderr, redirect_stdout
import hashlib
from http.server import ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import urllib.request

from ctxproxy.core import Config, Journal, Store, TokenCounter
from ctxproxy.datalock import DataLock
from ctxproxy.fake_engine import FakeEngine
from ctxproxy.forget import main, schema
from ctxproxy.kvarchive import KvArchive, zstd
from ctxproxy.recall2 import P_SCHEMA
from ctxproxy.server import Proxy, make_handler
from ctxproxy.upstream import Upstream
from tests.test_proxy import history, tool_round, TOOLS


def hashes(root):
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in root.rglob("*") if p.is_file()}


def command(data, *args):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        status = main(["--data", str(data), *args])
    return status, out.getvalue(), err.getvalue()


class TestForget(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.data = self.root / "data"
        self.data.mkdir()
        # A real proxy creates its Windows sibling control lock before opening the archive.
        with DataLock(self.data):
            pass
        self.slot = self.root / "slots"
        self.slot.mkdir()
        self.archive = self.root / "cold"
        self.config = self.root / "config.json"
        self.config.write_text(json.dumps({"slot_dir": str(self.slot), "kv_archive_dir": str(self.archive)}))
        store = Store(str(self.data / "archive.sqlite"))
        db = store.db
        for c in ("c1", "c10"):
            store.archive_many([("rid" + c, c, 0, "tool", "bash", "secretword" + c, 7, 10.0)])
            store.add_chain(c, ["hash" + c])
            store.add_segment(c, 0, 0, "hash" + c, "notes", "initial")
            store.add_masks([("mask" + c, c, 0, "rid" + c, 7, 10.0, 1)])
            store.add_insert("ins" + c, c, [{"role": "user", "content": c}])
            store.set_anchor(c, 0, 0, "hash", c + "-seg0-anchor1.bin", 7)
            store.add_pin(c, 0, "pin")
            store.add_autosave(c + "-seg0-auto-hash.bin", c, 0, "hash", 1, 7, 4, 7)
            store.add_drop(c, 0)
            store.add_fileops([(c, 0, "call", "read", "x", "ok", "rid" + c, "rid" + c, "head")])
            for prefix in ("tool_names:", "prompt_language:", "response_clamp_warned:"):
                store.kv_set(prefix + c, "en")
            for suffix in ("A.bin", "anchor1.bin", "auto-hash.bin", "new-state.bin"):
                (self.slot / (c + "-seg0-" + suffix)).write_bytes(c.encode() * 10)
        store.kv_set("strata_state", json.dumps({"conv": "c1", "text": "secretwordc1"}))
        store.kv_set("unrelated", "keep")
        db.execute('CREATE TABLE "future table"(conv TEXT, payload TEXT)')
        db.executemany('INSERT INTO "future table" VALUES(?,?)', [("c1", "private"), ("c10", "keep")])
        db.executescript(P_SCHEMA)
        db.executemany("INSERT INTO passages VALUES(?,?,?,?,?,?,?)", [(1, "ridc1", "c1", 0, "tool", 0, 12),
                                                                      (2, "ridc10", "c10", 0, "tool", 0, 12)])
        db.executemany("INSERT INTO passages_done VALUES(?,?)", [("ridc1", "raw"), ("ridc10", "raw")])
        db.execute("INSERT INTO p_raw(rowid,t) VALUES(1,'secretwordc1')")
        db.execute("INSERT INTO p_norm(rowid,t) VALUES(1,'secretwordc1')")
        db.close()
        events = [{"conv": "c1", "event": "request"}, {"conv": "c10", "event": "request"},
                  {"file": "c1-seg0-A.bin", "event": "kv_archive"},
                  {"event": "nested", "state": {"conv": "c1"}}]
        (self.data / "journal.jsonl").write_bytes(b"".join(json.dumps(e).encode() + b"\n" for e in events))
        (self.data / "live").mkdir()
        (self.data / "live" / "last_request.json").write_text(json.dumps({"conv": "c1", "messages": ["secret"]}))
        (self.data / "router-slot-models.json").write_text(json.dumps({"c1-seg0-A.bin": {"model": "a"},
                                                                     "c10-seg0-A.bin": {"model": "b"}}))

    def run_forget(self, *args):
        return command(self.data, "--config", str(self.config), *args)

    def test_dry_run_and_list_are_byte_identical(self):
        before = hashes(self.root)
        st, out, err = self.run_forget("--conv", "c1", "--dry-run")
        self.assertEqual(st, 0, err)
        self.assertIn("future table: 1 rows", out)
        self.assertIn("journal: 3 lines", out)
        self.assertIn("DRY RUN", out)
        self.assertEqual(hashes(self.root), before)
        st, out, err = self.run_forget("--list")
        self.assertEqual(st, 0, err)
        self.assertIn("c1\t", out)
        self.assertIn("c10\t", out)
        self.assertIn("1970-01-01T00:00:10", out)
        self.assertEqual(hashes(self.root), before)

    def test_schema_driven_deletion_fts_vacuum_and_isolation(self):
        before_slots = {p.name: p.read_bytes() for p in self.slot.iterdir() if p.name.startswith("c10-")}
        st, out, err = self.run_forget("--conv", "c1")
        self.assertEqual(st, 0, (out, err))
        db = sqlite3.connect(self.data / "archive.sqlite")
        try:
            for t, cols in schema(db).items():
                if "conv" in cols:
                    self.assertEqual(db.execute('SELECT count(*) FROM "%s" WHERE conv=?' % t,
                                                ("c1",)).fetchone()[0], 0, t)
            self.assertEqual(db.execute("SELECT count(*) FROM archive_fts WHERE archive_fts MATCH 'secretwordc1'").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT count(*) FROM archive_fts WHERE archive_fts MATCH 'secretwordc10'").fetchone()[0], 1)
            self.assertFalse(db.execute("SELECT * FROM kv WHERE k LIKE '%:c1' OR k='strata_state'").fetchall())
            self.assertEqual(db.execute("SELECT v FROM kv WHERE k='unrelated'").fetchone()[0], "keep")
            self.assertNotIn("p_raw", schema(db))
            self.assertEqual(db.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(db.execute("PRAGMA freelist_count").fetchone()[0], 0)
        finally:
            db.close()
        self.assertFalse(any(p.name.startswith("c1-") for p in self.slot.iterdir()))
        self.assertEqual({p.name: p.read_bytes() for p in self.slot.iterdir()}, before_slots)
        self.assertEqual((self.data / "live/last_request.json").read_bytes(), b"")
        self.assertEqual(json.loads((self.data / "router-slot-models.json").read_bytes()), {"c10-seg0-A.bin": {"model": "b"}})
        self.assertEqual((self.data / "journal.jsonl").read_bytes(), b'{"conv": "c10", "event": "request"}\n')
        self.assertNotIn(b"secretwordc1\x00", (self.data / "archive.sqlite").read_bytes())

    def test_unknown_id_no_changes(self):
        before = hashes(self.root)
        st, _, err = self.run_forget("--conv", "unknown")
        self.assertEqual(st, 1)
        self.assertIn("conversation not found", err)
        self.assertEqual(hashes(self.root), before)

    def test_lock_is_shared_and_canonical(self):
        before = hashes(self.data)
        with DataLock(self.data):
            for args in (("--conv", "c1"), ("--conv", "c1", "--dry-run"), ("--list",)):
                st, _, err = self.run_forget(*args)
                self.assertEqual(st, 1)
                self.assertIn("in use", err)
            alias = self.data / ".." / "data"
            with self.assertRaisesRegex(ValueError, "in use"):
                with DataLock(alias):
                    pass
        self.assertEqual(hashes(self.data), before)

    def test_environment_config_and_explicit_overrides(self):
        with patch.dict(os.environ, {"MNEMONIC_CONFIG": str(self.config)}):
            st, out, err = command(self.data, "--conv", "c1", "--dry-run")
            self.assertEqual(st, 0, err)
            self.assertIn(str(self.slot), out)
            self.assertIn(str(self.archive), out)
            other = self.root / "other.json"
            other.write_text(json.dumps({"slot_dir": str(self.slot)}))
            st, out, err = command(self.data, "--conv", "c1", "--dry-run", "--config", str(other),
                                   "--slot-dir", str(self.slot), "--kv-archive", str(self.archive))
            self.assertEqual(st, 0, err)
            self.assertIn("source: --slot-dir", out)
            self.assertIn("source: --kv-archive", out)

    def test_malformed_journal_refused_before_mutation(self):
        (self.data / "journal.jsonl").write_bytes(b'{"conv":"c1"}\nbroken')
        before = hashes(self.root)
        st, _, err = self.run_forget("--conv", "c1")
        self.assertEqual(st, 1)
        self.assertIn("invalid journal JSON", err)
        self.assertEqual(hashes(self.root), before)

    def test_unlink_failure_is_nonzero_and_retry_cleans_orphans(self):
        original = Path.unlink
        failed = self.slot / "c1-seg0-A.bin"
        def unlink(p, *args, **kwargs):
            if p == failed:
                raise PermissionError("deliberate unlink failure")
            return original(p, *args, **kwargs)
        with patch.object(Path, "unlink", unlink):
            st, _, err = self.run_forget("--conv", "c1")
        self.assertEqual(st, 1)
        self.assertIn("INCOMPLETE", err)
        self.assertTrue(failed.exists())
        st, _, err = self.run_forget("--conv", "c1")
        self.assertEqual(st, 0, err)
        self.assertFalse(failed.exists())

    def test_other_live_request_and_tracker_are_unchanged(self):
        live = self.data / "live/last_request.json"
        body = b'{"conv":"c10", "messages":["keep"]}'
        live.write_bytes(body)
        db = sqlite3.connect(self.data / "archive.sqlite")
        db.execute("UPDATE kv SET v=? WHERE k='strata_state'", (json.dumps({"conv": "c10"}),))
        db.commit()
        db.close()
        st, _, err = self.run_forget("--conv", "c1")
        self.assertEqual(st, 0, err)
        self.assertEqual(live.read_bytes(), body)
        db = sqlite3.connect(self.data / "archive.sqlite")
        self.assertEqual(json.loads(db.execute("SELECT v FROM kv WHERE k='strata_state'").fetchone()[0]), {"conv": "c10"})
        db.close()

    def test_invalid_id_and_missing_data_do_not_create_data(self):
        with self.assertRaises(SystemExit), redirect_stderr(io.StringIO()):
            self.run_forget("--conv", "c1*")
        st, _, err = command(self.root / "absent", "--conv", "c1")
        self.assertEqual(st, 1)
        self.assertIn("does not exist", err)
        self.assertFalse((self.root / "absent").exists())

    def test_dry_run_reads_committed_wal_without_mutating_source(self):
        db = sqlite3.connect(self.data / "archive.sqlite")
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("INSERT INTO archive VALUES('walrid','walconv',0,'user','','walword',1,12)")
        db.commit()
        before = hashes(self.root)
        st, out, err = self.run_forget("--conv", "walconv", "--dry-run")
        self.assertEqual(st, 0, err)
        self.assertIn("archive: 1 rows", out)
        self.assertEqual(hashes(self.root), before)
        db.close()

    def test_kv_archive_gc_without_compression_dependency(self):
        # Metadata-only management works on 3.10 too; shared blocks must survive.
        (self.archive / "manifests").mkdir(parents=True)
        (self.archive / "blocks/aa").mkdir(parents=True)
        for h in ("aaprivate", "aashared", "aaother", "aaorphan"):
            (self.archive / "blocks/aa" / (h + ".kvb")).write_bytes(h.encode())
        from ctxproxy.kvarchive import VERSION
        for c, hs in (("c1", ("aaprivate", "aashared")), ("c10", ("aaother", "aashared"))):
            (self.archive / "manifests" / (c + "-seg0-A.bin.kva")).write_text(
                json.dumps({"version": VERSION, "blocks": [[h, 1] for h in hs]}))
        before = hashes(self.root)
        with patch("ctxproxy.kvarchive.zstd", None):
            st, out, err = self.run_forget("--conv", "c1", "--dry-run")
            self.assertEqual(st, 0, err)
            self.assertIn("2 gc files", out)
            self.assertEqual(hashes(self.root), before)
            st, _, err = self.run_forget("--conv", "c1")
            self.assertEqual(st, 0, err)
        self.assertEqual({p.name for p in (self.archive / "blocks/aa").iterdir()}, {"aashared.kvb", "aaother.kvb"})
        self.assertEqual({p.name for p in (self.archive / "manifests").iterdir()}, {"c10-seg0-A.bin.kva"})

    def test_corrupt_archive_is_refused_before_deletion(self):
        (self.archive / "manifests").mkdir(parents=True)
        (self.archive / "blocks").mkdir()
        (self.archive / "manifests/c1-seg0-A.bin.kva").write_text("broken")
        before = hashes(self.root)
        st, _, err = self.run_forget("--conv", "c1", "--dry-run")
        self.assertEqual(st, 1)
        self.assertIn("manifest", err)
        self.assertEqual(hashes(self.root), before)


class DiskSlots(dict):
    """The fake engine's saved strings on disk, not real GPU/KV state."""
    def __init__(self, root):
        super().__init__()
        self.root = root

    def __setitem__(self, key, value):
        super().__setitem__(key, value)
        (self.root / key).write_text(value, encoding="utf-8")


def recall_policy(body):
    last = body["messages"][-1]
    if last.get("role") == "user" and last.get("content", "").startswith("READ:"):
        return {"role": "assistant", "content": "", "tool_calls": [{"id": "readback", "type": "function",
                "function": {"name": "recall", "arguments": json.dumps({"id": last["content"][5:]})}}]}
    if last.get("role") == "tool" and last.get("tool_call_id") == "readback":
        return {"role": "assistant", "content": last["content"]}
    return {"role": "assistant", "content": "A short handoff note."}


def exercise(root):
    """Persistent, real HTTP proxy session with two conversations; also used to capture command examples."""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    data, slot = root / "data", root / "slots"
    data.mkdir()
    slot.mkdir()
    cfg = Config.from_dict({"window": 18000, "mask_trigger": 6000, "mask_target": 3000,
                           "keep_recent_tokens": 1500, "min_batch_tokens": 500, "min_age_turns": 1,
                           "reserve": 512, "default_response": 512, "tail_max": 2000, "notes_max_tokens": 512,
                           "recall_max_tokens": 6000, "recall_struct": True, "recall_flex": True,
                           "slot_save": True, "mask_anchor": True, "slot_dir": str(slot), "autosave": True,
                           "autosave_min_tokens": 100, "autosave_min_free_gb": 0, "autosave_keep": 100,
                           "live_dump": True, "data_dir": str(data)})
    config = root / "config.json"
    config.write_text(json.dumps(cfg.__dict__))
    engine = FakeEngine(recall_policy)
    engine.files = DiskSlots(slot)
    engine_url = engine.start(0)
    proxy = Proxy(cfg, Upstream(engine_url), Store(str(data / "archive.sqlite")),
                  Journal(str(data / "journal.jsonl")), TokenCounter(3.5))
    saver = proxy.enable_autosave(start=False)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(proxy))
    thread = threading.Thread(target=srv.serve_forever)
    thread.start()
    url = "http://127.0.0.1:%d/v1/chat/completions" % srv.server_port
    def send(h, hint):
        req = urllib.request.Request(url, data=json.dumps({"model": "fake", "messages": h,
                                      "tools": TOOLS, "max_tokens": 512}).encode(),
                                     headers={"Content-Type": "application/json", "X-Strata-Conversation": hint})
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)
    records = {}
    try:
        with DataLock(data):
            # Last active conversation is the one to forget: exercises the global tracker and live dump too.
            for hint in ("survivor", "forgetme"):
                h = history(0, first="Project " + hint)
                unique = "survivorwordxyz" if hint == "survivor" else "erasewordxyz"
                log = (unique + " release log\n") * 150
                h += tool_round(0, 3500)
                h[-1]["content"] = log
                r = send(h, hint)
                c = r["strata_context"]["conversation_id"]
                rid = proxy.store.db.execute("SELECT rid FROM archive WHERE conv=? AND content=?", (c, log)).fetchone()[0]
                for k in range(1, 21):
                    h += [{"role": "assistant", "content": "Design decision %d. " % k + "durable choice " * 350},
                          {"role": "user", "content": "Continue project " + hint}] + tool_round(k, 3500)
                    r = send(h, hint)
                ev = saver.tick(now=time.time() + 1000)
                if ev is None:
                    raise AssertionError("fake session did not autosave")
                recalled = send(h + [{"role": "assistant", "content": "done"},
                                    {"role": "user", "content": "READ:" + rid}], hint)["choices"][0]["message"]["content"]
                if log not in recalled:
                    raise AssertionError("HTTP recall did not return original log")
                query_recall = proxy.mgr.recall(c, {"query": unique})  # Populate both lazy structured FTS indices.
                if unique not in query_recall:
                    raise AssertionError("structured recall did not find the unique word")
                stats = proxy.store.stats(c)
                if not (stats["masked"] and stats["segments"] > 1):
                    raise AssertionError("session did not produce masks and multiple segments: %r" % stats)
                records[hint] = {"conv": c, "rid": rid, "log": log, "recall": recalled,
                                 "query_recall": query_recall, "stats": stats}
            st, _, err = command(data, "--config", str(config), "--conv", records["forgetme"]["conv"])
            if st != 1 or "in use" not in err:
                raise AssertionError("active proxy was not refused")
    finally:
        srv.shutdown()
        srv.server_close()
        thread.join()
        proxy.store.db.close()
        engine.stop()
    # Real compressed archives on 3.14; all Python versions also test metadata-only gc above.
    compressed = None
    if zstd is not None:
        arc = KvArchive(str(root / "kv-archive"), threads=1)
        for key in records:
            name = next(p for p in slot.iterdir() if p.name.startswith(records[key]["conv"] + "-"))
            arc.archive(str(name), name.name)
        survivor_name = next(n for n in arc.names() if n.startswith(records["survivor"]["conv"] + "-"))
        compressed = (arc, survivor_name, arc.manifest(survivor_name)["sha256"])
    c = records["forgetme"]["conv"]
    examples = {}
    before = hashes(root)
    for name, args in (("list", ("--list",)), ("dry-run", ("--conv", c, "--dry-run"))):
        st, out, err = command(data, "--config", str(config), *args)
        if st:
            raise AssertionError(err)
        examples[name] = out
        if hashes(root) != before:
            raise AssertionError("list/dry-run modified files")
    survivor_files = {p: p.read_bytes() for p in slot.iterdir() if p.name.startswith(records["survivor"]["conv"] + "-")}
    st, out, err = command(data, "--config", str(config), "--conv", c)
    if st:
        raise AssertionError(err)
    examples["forget"] = out
    # Inspect every SQLite table's values (FTS shadow tables included), and all remaining on-disk bytes.
    db = sqlite3.connect(data / "archive.sqlite")
    for table in schema(db):
        for row in db.execute('SELECT * FROM "%s"' % table.replace('"', '""')):
            if any(c in str(value) for value in row):
                raise AssertionError("conversation id remains in table " + table)
    if db.execute("SELECT count(*) FROM archive_fts WHERE archive_fts MATCH 'erasewordxyz'").fetchone()[0]:
        raise AssertionError("forgotten word remains in FTS")
    db.close()
    for p in root.rglob("*"):
        if p.is_file() and (c in p.name or c.encode() in p.read_bytes()):
            raise AssertionError("conversation id remains on disk: " + str(p))
    if any(p.read_bytes() != b for p, b in survivor_files.items()):
        raise AssertionError("survivor saved-state changed")
    store = Store(str(data / "archive.sqlite"))
    try:
        from ctxproxy.core import Manager
        mgr = Manager(cfg, store, TokenCounter(3.5), Journal(None))
        other = records["survivor"]
        after = mgr.recall(other["conv"], {"id": other["rid"]})
        if after != other["recall"]:
            raise AssertionError("survivor recall changed")
        if mgr.recall(other["conv"], {"query": "survivorwordxyz"}) != other["query_recall"]:
            raise AssertionError("survivor structured recall failed after lazy index rebuild")
    finally:
        store.db.close()
    if compressed:
        arc, name, sha = compressed
        if not arc.verify(name) or arc.manifest(name)["sha256"] != sha:
            raise AssertionError("shared archive survivor changed")
    return records, examples


class TestForgetHTTP(unittest.TestCase):
    def test_two_real_http_conversations_masks_segments_saves_and_recall(self):
        with tempfile.TemporaryDirectory() as root:
            exercise(Path(root) / "session")

    def test_actual_server_cli_holds_lock_and_releases_after_exit(self):
        with tempfile.TemporaryDirectory() as root:
            data = Path(root) / "data"
            engine = FakeEngine()
            url = engine.start(0)
            self.addCleanup(engine.stop)
            sock = socket.socket()
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
            sock.close()
            log = Path(root) / "server.log"
            with log.open("w") as f:
                p = subprocess.Popen([sys.executable, "-m", "ctxproxy.server", "--data", str(data),
                                      "--upstream", url, "--port", str(port)], stdout=f, stderr=f)
                try:
                    deadline = time.monotonic() + 15
                    while time.monotonic() < deadline:
                        if p.poll() is not None:
                            self.fail(log.read_text())
                        try:
                            with urllib.request.urlopen("http://127.0.0.1:%d/health" % port, timeout=1):
                                break
                        except OSError:
                            time.sleep(.05)
                    else:
                        self.fail("proxy never became healthy: " + log.read_text())
                    result = subprocess.run([sys.executable, "-m", "ctxproxy.server", "forget", "--data", str(data),
                                             "--conv", "missing", "--dry-run"], capture_output=True, text=True, timeout=10)
                    self.assertEqual(result.returncode, 1)
                    self.assertIn("in use", result.stderr)
                    again = subprocess.run([sys.executable, "-m", "ctxproxy.server", "--data", str(data),
                                            "--upstream", url, "--port", str(port)], capture_output=True, text=True, timeout=10)
                    self.assertNotEqual(again.returncode, 0)
                    self.assertIn("in use", again.stderr)
                finally:
                    if os.name == "nt":
                        p.terminate()
                    else:
                        p.send_signal(signal.SIGINT)
                    try:
                        p.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        p.kill()
                        p.wait()
            with DataLock(data):
                pass


if __name__ == "__main__":
    unittest.main()
