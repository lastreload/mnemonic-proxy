"""Archivio freddo .kva (ctxproxy/kvarchive.py): andata e ritorno, deduplica, errori, riferimenti, interruzioni,
integrazione nel proxy (restore di un file archiviato, archiviazione in background, scarto)."""
import hashlib
import json
import os
import random
import struct
import tempfile
import threading
import time
import unittest

from ctxproxy import kvarchive as K
from ctxproxy.core import Config, Journal, Store


def make_session(tokens: int, seed: int, prefix_seed: int = 0, prefix_tokens: int = 0, layers: int = 3,
                 gdn_bytes: int = 300_000) -> bytes:
    """File con la struttura del format v1 di Strata (header, geometria, stato vivo + 1 checkpoint, strati KV con
    k/v/scale/pooled e draft, coda). I primi `prefix_tokens` token di K/V/scale/pooled dipendono solo da
    prefix_seed (prefisso identico fra file della stessa \"conversazione\"); il resto da seed."""
    ps, heads, hd, idim = 4, 2, 64, 32
    cells = -(-tokens // ps) * ps
    pages = cells // ps

    def rnd(s, n):
        return random.Random(repr(s)).randbytes(n)

    def part_bytes(name, per_page, layer):
        pre_pages = min(prefix_tokens // ps, pages)
        a = rnd((prefix_seed, name, layer), pre_pages * per_page) if pre_pages else b""
        b = rnd((seed, name, layer), (pages - pre_pages) * per_page)
        return a + b

    def blob(b):
        return struct.pack("<Q", len(b)) + b

    def ckpt(s, ntok):
        ids = struct.pack("<%di" % ntok, *[(i * 7 + 3) % 1000 for i in range(ntok)])
        out = struct.pack("<Q", ntok) + ids + struct.pack("<Q", 0)
        out += blob(rnd((s, "gdn"), gdn_bytes)) + blob(b"\x01" * 64) + blob(b"\x02" * 32) + blob(b"") + blob(b"\x03" * 8)
        return out + struct.pack("<Q", ntok)

    pay = struct.pack("<18q", *range(18)) + struct.pack("<qqQ", 0, 48, 1)
    pay += ckpt(seed, tokens) + struct.pack("<Q", 1) + ckpt(seed + 1, max(tokens - 5, 0))
    pay += struct.pack("<Q", layers + 1)
    for li in range(layers + 1):
        prow = tokens // ps + 1
        pay += struct.pack("<7q", 1, cells, heads, hd, ps, prow, idim)
        pay += blob(part_bytes("k", heads * ps * hd, li)) + blob(part_bytes("v", heads * ps * hd, li))
        pay += blob(part_bytes("ks", heads * ps * hd // 64 * 2, li)) + blob(part_bytes("vs", heads * ps * hd // 64 * 2, li))
        pre_rows = min(prefix_tokens // ps, prow)
        pooled = (rnd((prefix_seed, "p", li), pre_rows * idim * 4) if pre_rows else b"") + \
            rnd((seed, "p", li), (prow - pre_rows) * idim * 4)
        pay += blob(pooled)
    hdr = b"STRSESS\x01" + struct.pack("<II", 1, 64) + struct.pack("<QQQ", 1, 2, len(pay)) + b"\0" * 16 + b"\0" * 8
    assert len(hdr) == 64
    return hdr + pay + b"\0" * 8 + b"STRSEND\x01"


def sha(p):
    with open(p, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = self.tmp.name
        self.arc = K.KvArchive(os.path.join(self.d, "arc"), threads=2)

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, name, data):
        p = os.path.join(self.d, name)
        with open(p, "wb") as f:
            f.write(data)
        return p


class TestFormat(Base):
    def test_regions_cover_file(self):
        data = make_session(9000, 1)
        regs = K.regions(data)
        self.assertEqual(regs[0][0], 0)
        o = 0
        for off, n, kind, _, _ in regs:
            self.assertEqual(off, o)
            o += n
        self.assertEqual(o, len(data))
        kinds = {r[2] for r in regs}
        self.assertTrue({"kv", "draft_kv", "scale", "pooled", "gdn", "ids"} <= kinds, kinds)
        ts = {r[2]: r[4] for r in regs if r[2] != "misc"}
        self.assertEqual(ts, {"kv": 64, "draft_kv": 64, "scale": 2, "pooled": 4, "gdn": 4, "ids": 4})

    def test_kv_blocks_aligned_to_2048_tokens(self):
        data = make_session(9000, 1)
        kvb = [b for b in K.split_blocks(data) if b[2] == "kv"]
        per_token = 2 * 64        # heads * head_dim (int8)
        self.assertEqual(kvb[0][1], 2048 * per_token)

    def test_non_v1_file_fixed_blocks(self):
        data = os.urandom(3 * K.BIG + 17)
        bl = K.split_blocks(data)
        self.assertEqual([b[1] for b in bl], [K.BIG] * 3 + [17])

    def test_shuffle_roundtrip(self):
        b = os.urandom(4096 + 3)
        for ts in (1, 2, 4, 8):
            self.assertEqual(K.unshuffle(K.shuffle(b, ts), ts), b)
        b = os.urandom(4096)
        self.assertEqual(K.unshuffle(K.shuffle(b, 4), 4), b)


class TestRoundtrip(Base):
    def test_roundtrip_byte_identical(self):
        p = self.write("a.bin", make_session(9000, 1))
        r = self.arc.archive(p)
        out = os.path.join(self.d, "out.bin")
        self.arc.restore("a.bin", out)
        self.assertEqual(sha(out), sha(p))
        self.assertEqual(r["sha256"], sha(p))
        self.assertTrue(self.arc.verify("a.bin"))
        self.assertFalse(any(f.startswith(".") for f in os.listdir(self.d)))   # nessun temporaneo lasciato

    def test_roundtrip_odd_and_empty_files(self):
        for i, data in enumerate((b"", b"x", os.urandom(K.BIG + 1), b"STRSESS\x01" + b"\0" * 200)):
            p = self.write("f%d.bin" % i, data)
            self.arc.archive(p)
            out = os.path.join(self.d, "o%d.bin" % i)
            self.arc.restore("f%d.bin" % i, out)
            self.assertEqual(sha(out), hashlib.sha256(data).hexdigest())

    def test_delete_original_only_after_verify(self):
        p = self.write("a.bin", make_session(5000, 1))
        h = sha(p)
        r = self.arc.archive(p, delete_original=True)
        self.assertTrue(r["deleted_original"])
        self.assertFalse(os.path.exists(p))
        self.arc.restore("a.bin", p)
        self.assertEqual(sha(p), h)

    def test_before_delete_can_veto(self):
        p = self.write("a.bin", make_session(5000, 1))
        r = self.arc.archive(p, delete_original=True, before_delete=lambda st: False)
        self.assertFalse(r["deleted_original"])
        self.assertTrue(os.path.exists(p))


class TestDedup(Base):
    def test_common_prefix_dedup(self):
        a = self.write("c-seg0-A.bin", make_session(12000, 1, prefix_seed=99, prefix_tokens=12000))
        b = self.write("c-seg0-auto.bin", make_session(16000, 5, prefix_seed=99, prefix_tokens=12000))
        ra = self.arc.archive(a)
        rb = self.arc.archive(b)
        self.assertLess(ra["dedup_bytes"], 2000)          # solo pezzetti ripetuti dentro il file (ple/tails)
        # i blocchi KV dei primi 10240 token (5 blocchi da 2048) sono condivisi, per ogni parte e strato
        self.assertGreater(rb["dedup_bytes"], 0)
        kinds = self.arc.manifest("c-seg0-auto.bin")["stats"]["kinds"]
        self.assertGreater(kinds["kv"]["dedup_raw"], 0.5 * kinds["kv"]["raw"])
        self.assertEqual(kinds["gdn"]["dedup_raw"], 0)
        for n, p in (("c-seg0-A.bin", a), ("c-seg0-auto.bin", b)):
            out = os.path.join(self.d, "r-" + n)
            self.arc.restore(n, out)
            self.assertEqual(sha(out), sha(p))

    def test_same_file_twice_stores_nothing_new(self):
        p = self.write("a.bin", make_session(5000, 1))
        self.arc.archive(p)
        r = self.arc.archive(p, name="copia.bin")
        self.assertEqual(r["new_stored_bytes"], 0)
        self.assertEqual(r["dedup_bytes"], os.path.getsize(p))


class TestErrors(Base):
    def test_missing_block_clear_error_no_partial_file(self):
        p = self.write("a.bin", make_session(5000, 1))
        self.arc.archive(p)
        h = self.arc.manifest("a.bin")["blocks"][3][0]
        os.remove(self.arc.block_path(h))
        out = os.path.join(self.d, "out.bin")
        with self.assertRaises(K.MissingBlock) as cm:
            self.arc.restore("a.bin", out)
        self.assertIn("mancante", str(cm.exception))
        self.assertIn(h, str(cm.exception))
        self.assertFalse(os.path.exists(out))
        self.assertEqual([f for f in os.listdir(self.d) if "kva-tmp" in f], [])
        self.assertRaises(K.MissingBlock, self.arc.verify, "a.bin")

    def test_corrupt_block_detected(self):
        p = self.write("a.bin", make_session(5000, 1))
        self.arc.archive(p)
        h = self.arc.manifest("a.bin")["blocks"][-2][0]
        bp = self.arc.block_path(h)
        blob = bytearray(open(bp, "rb").read())
        blob[-1] ^= 0xFF
        open(bp, "wb").write(bytes(blob))
        with self.assertRaises(Exception):
            self.arc.restore("a.bin", os.path.join(self.d, "out.bin"))
        self.assertFalse(os.path.exists(os.path.join(self.d, "out.bin")))

    def test_unknown_name(self):
        with self.assertRaises(K.KvaError):
            self.arc.restore("nessuno.bin", os.path.join(self.d, "x"))
        with self.assertRaises(K.KvaError):
            self.arc.manifest_path("../evil")


class TestRefcount(Base):
    def test_shared_block_survives_remove(self):
        a = self.write("a.bin", make_session(12000, 1, prefix_seed=9, prefix_tokens=12000))
        b = self.write("b.bin", make_session(14000, 2, prefix_seed=9, prefix_tokens=12000))
        self.arc.archive(a)
        self.arc.archive(b)
        ha = {h for h, _ in self.arc.manifest("a.bin")["blocks"]}
        hb = {h for h, _ in self.arc.manifest("b.bin")["blocks"]}
        shared = ha & hb
        self.assertTrue(shared)
        rc = self.arc.refcounts()
        self.assertTrue(all(rc[h] >= 2 for h in shared))
        n_before = len(self.arc.stored_blocks())
        r = self.arc.remove("a.bin")
        self.assertTrue(r["removed"])
        self.assertGreater(r["freed_blocks"], 0)
        left = self.arc.stored_blocks()
        self.assertLess(len(left), n_before)
        for h in shared:
            self.assertIn(h, left)                    # usato ancora da b: non cancellato
        out = os.path.join(self.d, "rb.bin")
        self.arc.restore("b.bin", out)
        self.assertEqual(sha(out), sha(b))
        self.arc.remove("b.bin")
        self.assertEqual(self.arc.stored_blocks(), {})

    def test_gc_removes_only_orphans(self):
        a = self.write("a.bin", make_session(5000, 1))
        self.arc.archive(a)
        orphan = self.arc.block_path("ff" * 32)
        os.makedirs(os.path.dirname(orphan), exist_ok=True)
        open(orphan, "wb").write(b"x")
        r = self.arc.gc()
        self.assertEqual(r["freed_blocks"], 1)
        self.assertTrue(self.arc.verify("a.bin"))


class TestInterruption(Base):
    def test_abort_midway_keeps_original_and_no_manifest(self):
        p = self.write("a.bin", make_session(9000, 1))
        h = sha(p)
        calls = [0]

        def abort():
            calls[0] += 1
            return calls[0] > 5

        with self.assertRaises(K.Aborted):
            self.arc.archive(p, delete_original=True, should_abort=abort)
        self.assertTrue(os.path.exists(p))
        self.assertEqual(sha(p), h)
        self.assertFalse(self.arc.has("a.bin"))
        self.assertGreater(len(self.arc.stored_blocks()), 0)       # orfani...
        self.arc.gc()
        self.assertEqual(self.arc.stored_blocks(), {})              # ...tolti da gc
        r = self.arc.archive(p, delete_original=True)               # ripresa
        self.assertTrue(r["deleted_original"])
        self.arc.restore("a.bin", p)
        self.assertEqual(sha(p), h)

    def test_failed_verification_keeps_original(self):
        p = self.write("a.bin", make_session(5000, 1))
        h = sha(p)
        orig = K.KvArchive._verify_entries
        K.KvArchive._verify_entries = lambda self, e: ("0" * 64, 0)
        try:
            with self.assertRaises(K.CorruptArchive):
                self.arc.archive(p, delete_original=True)
        finally:
            K.KvArchive._verify_entries = orig
        self.assertEqual(sha(p), h)
        self.assertFalse(self.arc.has("a.bin"))
        self.assertEqual([f for f in os.listdir(os.path.join(self.arc.root, "manifests"))], [])

    def test_crash_simulation_tmp_manifest_ignored(self):
        p = self.write("a.bin", make_session(5000, 1))
        self.arc.archive(p)
        open(os.path.join(self.arc.root, "manifests", "b.bin.kva.tmp-1"), "w").write("{")
        self.assertEqual(self.arc.names(), ["a.bin"])

    def test_original_modified_during_archive_not_deleted(self):
        p = self.write("a.bin", make_session(5000, 1))
        calls = [0]

        def touch():
            calls[0] += 1
            if calls[0] == 2:
                with open(p, "ab") as f:
                    f.write(b"z")
            return False

        r = self.arc.archive(p, delete_original=True, should_abort=touch)
        self.assertFalse(r["deleted_original"])
        self.assertTrue(os.path.exists(p))


# ----------------------------------------------------------------------------- integrazione nel proxy
T0 = time.time() + 10


class FakeUp:
    def __init__(self, slot_dir):
        self.slot_dir = slot_dir
        self.calls = []
        self.requests = 5
        self.in_flight = False

    def slot(self, action, filename):
        p = os.path.join(self.slot_dir, filename)
        self.calls.append((action, filename, os.path.exists(p)))
        if action == "restore":
            if not os.path.exists(p):
                from ctxproxy.upstream import UpstreamError
                raise UpstreamError(404, b"no file")
            return {"n_restored": 1, "sha": sha(p)}
        with open(p, "wb") as f:
            f.write(make_session(5000, 7))
        return {"n_written": os.path.getsize(p)}

    def raw(self, method, path, body=None):
        return 200, {}, json.dumps({"loaded": True, "activity": {"requests": self.requests,
                                                                 "in_flight": self.in_flight}}).encode()


class FakeProxy:
    def __init__(self, cfg, up, store, journal):
        self.cfg, self.up, self.store, self.journal = cfg, up, store, journal
        self.lock = threading.Lock()


class TestProxyIntegration(Base):
    def setUp(self):
        global T0
        T0 = time.time() + 10      # rispetto all'ora di creazione dei file del test (non all'import del modulo)
        super().setUp()
        self.slot = os.path.join(self.d, "sessions")
        os.makedirs(self.slot)
        self.cfg = Config(kv_archive=True, slot_dir=self.slot, kv_archive_idle_s=0, kv_archive_min_age_s=0,
                          kv_archive_min_bytes=0, kv_archive_threads=2)
        self.store = Store(":memory:")
        self.journal = Journal(None)
        self.up = FakeUp(self.slot)
        self.proxy = FakeProxy(self.cfg, self.up, self.store, self.journal)
        self.archiver = K.enable(self.proxy, start=False)

    def events(self, name):
        return [e for e in self.journal.mem if e["event"] == name]

    def test_default_off(self):
        self.assertFalse(Config().kv_archive)

    def test_archive_then_restore_through_proxy(self):
        p = os.path.join(self.slot, "c1-seg0-A.bin")
        open(p, "wb").write(make_session(9000, 3))
        h = sha(p)
        self.archiver.tick(now=T0 + 1)            # primo giro: annota requests
        ev = self.archiver.tick(now=T0 + 2)
        self.assertIsNotNone(ev)
        self.assertEqual(ev["event"], "kv_archive")
        self.assertTrue(ev["deleted_original"])
        self.assertEqual(ev["bytes_before"], len(make_session(9000, 3)))
        self.assertIn("ms", ev)
        self.assertFalse(os.path.exists(p))
        r = self.proxy.up.slot("restore", "c1-seg0-A.bin")            # il proxy ricostruisce e poi chiama Strata
        self.assertEqual(r["sha"], h)
        self.assertEqual(self.up.calls[-1], ("restore", "c1-seg0-A.bin", True))
        ev = self.events("kv_unarchive")
        self.assertEqual(len(ev), 1)
        self.assertEqual(ev[0]["bytes"], len(make_session(9000, 3)))
        self.assertEqual(sha(p), h)

    def test_not_idle_no_archive(self):
        open(os.path.join(self.slot, "c1-seg0-A.bin"), "wb").write(make_session(5000, 3))
        self.archiver.tick(now=T0 + 1)
        self.up.requests += 1                       # qualcuno ha usato Strata
        self.assertIsNone(self.archiver.tick(now=T0 + 2))
        self.up.in_flight = True
        self.assertIsNone(self.archiver.tick(now=T0 + 3))
        self.up.in_flight = False
        with self.proxy.lock:
            self.assertIsNone(self.archiver.tick(now=T0 + 4))
        self.assertTrue(os.path.exists(os.path.join(self.slot, "c1-seg0-A.bin")))

    def test_busy_during_archive_aborts_keeps_original(self):
        p = os.path.join(self.slot, "c1-seg0-A.bin")
        open(p, "wb").write(make_session(9000, 3))
        self.archiver.tick(now=T0 + 1)
        orig = self.archiver.arc.archive

        def busy_archive(*a, **kw):
            self.proxy.lock.acquire()
            try:
                return orig(*a, **kw)
            finally:
                self.proxy.lock.release()
        self.archiver.arc.archive = busy_archive
        self.assertIsNone(self.archiver.tick(now=T0 + 2))
        self.assertTrue(os.path.exists(p))
        self.assertEqual(len(self.events("kv_archive_skip")), 1)

    def test_protected_files_not_archived(self):
        self.store.add_autosave("c1-seg0-auto-new.bin", "c1", 0, "ph", 10, 20000, 1, 1)
        for f in ("c1-seg0-auto-new.bin", "c2-seg1-A.bin", "x.txt"):
            open(os.path.join(self.slot, f), "wb").write(make_session(3000, 1))
        self.proxy.up.up.state = None
        self.assertEqual(self.archiver.candidates(now=10 ** 10), ["c2-seg1-A.bin"])
        self.proxy.up.state = {"conv": "c2", "seg": 1}
        self.assertEqual(self.archiver.candidates(now=10 ** 10), [])

    def test_save_over_archived_name_discards_archive(self):
        p = os.path.join(self.slot, "c1-seg0-A.bin")
        open(p, "wb").write(make_session(5000, 3))
        self.archiver.arc.archive(p, delete_original=True)
        self.proxy.up.slot("save", "c1-seg0-A.bin")
        self.assertFalse(self.archiver.arc.has("c1-seg0-A.bin"))
        self.assertEqual(len(self.events("kv_archive_drop")), 1)

    def test_missing_block_restore_logs_error(self):
        p = os.path.join(self.slot, "c1-seg0-A.bin")
        open(p, "wb").write(make_session(5000, 3))
        self.archiver.arc.archive(p, delete_original=True)
        h = self.archiver.arc.manifest("c1-seg0-A.bin")["blocks"][0][0]
        os.remove(self.archiver.arc.block_path(h))
        with self.assertRaises(K.MissingBlock):
            self.proxy.up.slot("restore", "c1-seg0-A.bin")
        self.assertEqual(self.events("kv_archive_error")[0]["step"], "unarchive")
        self.assertEqual(self.up.calls, [])                            # Strata non chiamato con un file mancante

    def test_tracker_wrapping(self):
        from ctxproxy.autosave import Tracker
        up = FakeUp(self.slot)
        proxy = FakeProxy(self.cfg, Tracker(up, self.store, self.journal, self.cfg), self.store, self.journal)
        K.enable(proxy, start=False)
        self.assertIsInstance(proxy.up, Tracker)
        self.assertIsInstance(proxy.up.up, K.ArchivingUpstream)


if __name__ == "__main__":
    unittest.main()
