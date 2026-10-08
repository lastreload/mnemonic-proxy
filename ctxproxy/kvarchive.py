# Author: Maurizio Verde — LastReload
"""Archivio freddo dei salvataggi di Strata (.kva): blocchi indirizzati per contenuto + zstd (card t_6c016d94).

Solo libreria standard (Python >= 3.14: `compression.zstd`). Non modifica Strata: un file archiviato torna un `.bin`
byte-identico (sha256 verificato) prima di un restore.

Formato
-------
<root>/blocks/<h[:2]>/<h>.kvb   blocco: magic "KVB1", codec u8, typesize u16, livello u8, raw_len u64, dati zstd.
                                 h = blake2b-256 dei byte ORIGINALI del blocco (indipendente dal codec): due file con
                                 lo stesso contenuto in quella posizione condividono il blocco (deduplica).
<root>/manifests/<nome>.kva     JSON: versione, nome, dimensione, sha256 del file intero, mtime, elenco ordinato dei
                                 blocchi [hash, raw_len], statistiche. Ricostruzione = concatenazione dei blocchi.
<root>/.lock                    flock: archiviazione, rimozione e pulizia dei blocchi sono serializzate.

Taglio dei blocchi: allineato alle parti del format v1 di Strata (vedi `regions`). Ogni parte KV (k, v, scale, pooled)
è tagliata a multipli di 2048 token dall'inizio della parte, quindi il prefisso identico fra due salvataggi della
stessa conversazione (bit per bit, KV-COMPRESSION-RESULT §4) dà blocchi identici. Lo stato DeltaNet e il resto a
blocchi da 1 MiB; i pezzi piccoli fra le parti (contatori, geometria, header) sono blocchi a sé. Un file che non è
format v1 si taglia a blocchi fissi da 1 MiB (funziona lo stesso, senza allineamento).

Riordino (codec 1): prima di zstd i byte del blocco sono raggruppati per posizione modulo `typesize` (byte shuffle,
come Blosc): scale fp16 typesize 2, pooled/GDN fp32 e ids i32 typesize 4, draft K/V typesize head_dim (= trasposizione
per canale). Reversibile esatto. Le K/V principali restano zstd semplice (vedi DEFAULT_CODECS).

Conteggio dei riferimenti: un blocco vive finché un manifest lo elenca (`refcounts`); `gc` cancella solo i blocchi
che nessun manifest usa, sotto lock. Un archivio interrotto lascia al più blocchi orfani (li toglie `gc`), mai un
manifest senza blocchi, e l'originale si cancella solo dopo la ricostruzione verificata (sha256) dal disco.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import struct
import threading
import time
from concurrent.futures import ThreadPoolExecutor

try:
    from compression import zstd
except ImportError:  # pragma: no cover - Python < 3.14
    zstd = None

VERSION = 1
BLOCK_MAGIC = b"KVB1"
BLOCK_HDR = struct.Struct("<4sBHBQ")          # magic, codec, typesize, level, raw_len  (16 byte)
CODEC_ZSTD, CODEC_SHUF_ZSTD, CODEC_RAW = 0, 1, 2
BIG = 1 << 20                                 # blocco standard (GDN, file non v1)
TOKENS_PER_BLOCK = 2048                       # parti KV: blocchi da 2048 token (512 pagine da 4)
KV_PARTS = ("k", "v", "k_scale", "v_scale", "pooled")
CK_VECS = ("gdn", "ple", "tails", "dead", "block_pos")

# codec per tipo di regione. CODEC_SHUF_ZSTD usa il typesize della regione: K/V int8 = head_dim (raggruppa i byte
# per canale: trasposizione per canale), scale fp16 = 2, pooled/GDN fp32 = 4, ids i32 = 4. Scelta misurata in
# KV-ARCHIVE-RESULT.md §2: sulle K/V principali il riordino vale 0,7 punti di spazio ma raddoppia archivio e
# ricostruzione (shuffle in Python puro), quindi di serie K/V principali vanno con zstd semplice ("mixed").
DEFAULT_CODECS = {
    "kv": CODEC_ZSTD,
    "draft_kv": CODEC_SHUF_ZSTD,
    "scale": CODEC_SHUF_ZSTD,
    "pooled": CODEC_SHUF_ZSTD,
    "gdn": CODEC_SHUF_ZSTD,
    "ids": CODEC_SHUF_ZSTD,
    "misc": CODEC_ZSTD,
}
FULL_CODECS = {**DEFAULT_CODECS, "kv": CODEC_SHUF_ZSTD}     # riordino ovunque (-0,7 punti, ~2x più lento)
PLAIN_CODECS = {k: CODEC_ZSTD for k in DEFAULT_CODECS}     # zstd semplice ovunque (confronto)
CODEC_SETS = {"mixed": DEFAULT_CODECS, "full": FULL_CODECS, "plain": PLAIN_CODECS}


class KvaError(Exception):
    pass


class MissingBlock(KvaError):
    pass


class CorruptArchive(KvaError):
    pass


class Aborted(KvaError):
    pass


# --------------------------------------------------------------------------- parser (sola lettura) del format v1
def regions(buf) -> list[tuple[int, int, str, int, int]]:
    """-> [(offset, nbytes, kind, block_bytes, typesize)] che coprono il file per intero, in ordine.
    Se il file non è un salvataggio Strata v1 integro: blocchi fissi da 1 MiB (kind 'misc')."""
    n = len(buf)
    try:
        return _regions_v1(buf)
    except (KvaError, struct.error, ValueError, IndexError, AssertionError):
        return [(0, n, "misc", BIG, 1)] if n else []


def _regions_v1(buf):
    n = len(buf)
    if n < 96 or bytes(buf[:8]) != b"STRSESS\x01" or bytes(buf[n - 8:]) != b"STRSEND\x01":
        raise KvaError("non v1")
    hs = struct.unpack_from("<I", buf, 12)[0]
    out, p = [], [hs + 18 * 8 + 24]
    misc_start = [0]

    def u64():
        v = struct.unpack_from("<Q", buf, p[0])[0]
        p[0] += 8
        return v

    def part(nbytes, kind, block, ts=1):
        if p[0] + nbytes > n:
            raise KvaError("parte oltre la fine")
        if p[0] > misc_start[0]:
            out.append((misc_start[0], p[0] - misc_start[0], "misc", BIG, 1))
        if nbytes:
            out.append((p[0], nbytes, kind, max(1, block), ts))
        p[0] += nbytes
        misc_start[0] = p[0]

    def ckpt():
        part(u64() * 4, "ids", BIG, 4)
        nimg = u64()
        p[0] += nimg * 16
        for v in CK_VECS:
            part(u64(), "gdn" if v == "gdn" else "misc", BIG, 4 if v == "gdn" else 1)
        p[0] += 8

    ckpt()
    for _ in range(u64()):
        ckpt()
    nkv = u64()
    if nkv > 4096:
        raise KvaError("troppi strati")
    for i in range(nkv):
        fmt, cells, heads, hd, ps, prow, idim = struct.unpack_from("<7q", buf, p[0])
        p[0] += 56
        draft = i == nkv - 1
        pages = max(1, -(-cells // max(ps, 1)))
        for name in KV_PARTS:
            nb = u64()
            if name == "pooled":
                unit = nb // prow if prow > 0 else 0          # byte per riga (= page_size token)
                kind, ts = "pooled", 4
            else:
                unit = nb // pages                            # byte per pagina
                if name.endswith("scale"):
                    kind, ts = "scale", 2
                else:
                    kind, ts = ("draft_kv" if draft else "kv"), (hd if 0 < hd <= 4096 else 1)
            block = unit * (TOKENS_PER_BLOCK // max(ps, 1)) if unit else BIG
            part(nb, kind, block if 0 < block <= 8 * BIG else BIG, ts)
    if p[0] + 16 != n:
        raise KvaError("lunghezza incoerente")
    out.append((misc_start[0], n - misc_start[0], "misc", BIG, 1))
    return out


def split_blocks(buf, max_misc: int = BIG) -> list[tuple[int, int, str, int]]:
    """-> [(offset, nbytes, kind, typesize)] blocchi in ordine di file."""
    blocks = []
    for off, nb, kind, bsz, ts in regions(buf):
        bsz = bsz if kind != "misc" else max_misc
        o, end = off, off + nb
        while o < end:
            ln = min(bsz, end - o)
            blocks.append((o, ln, kind, ts))
            o += ln
    return blocks


# --------------------------------------------------------------------------- codec dei blocchi
def shuffle(data, ts: int) -> bytes:
    if ts <= 1 or len(data) % ts:
        return bytes(data)
    mv = memoryview(data)
    return b"".join(bytes(mv[i::ts]) for i in range(ts))


def unshuffle(data, ts: int) -> bytes:
    n = len(data)
    if ts <= 1 or n % ts:
        return bytes(data)
    out = bytearray(n)
    m = n // ts
    mv = memoryview(data)
    for i in range(ts):
        out[i::ts] = mv[i * m:(i + 1) * m]
    return bytes(out)


def encode_block(raw, codec: int, ts: int, level: int) -> bytes:
    if codec == CODEC_SHUF_ZSTD and (ts <= 1 or len(raw) % ts):
        codec, ts = CODEC_ZSTD, 1
    if codec == CODEC_ZSTD:
        body = zstd.compress(raw, level)
    elif codec == CODEC_SHUF_ZSTD:
        body = zstd.compress(shuffle(raw, ts), level)
    else:
        body = bytes(raw)
    if codec != CODEC_RAW and len(body) >= len(raw):
        codec, ts, body = CODEC_RAW, 1, bytes(raw)      # incomprimibile: tenuto com'è
    return BLOCK_HDR.pack(BLOCK_MAGIC, codec, ts, level, len(raw)) + body


def decode_block(blob: bytes) -> bytes:
    if len(blob) < BLOCK_HDR.size:
        raise CorruptArchive("blocco troncato")
    magic, codec, ts, _lvl, raw_len = BLOCK_HDR.unpack_from(blob)
    if magic != BLOCK_MAGIC:
        raise CorruptArchive("wrong block magic")
    body = memoryview(blob)[BLOCK_HDR.size:]
    if codec == CODEC_RAW:
        raw = bytes(body)
    elif codec == CODEC_ZSTD:
        raw = zstd.decompress(body)
    elif codec == CODEC_SHUF_ZSTD:
        raw = unshuffle(zstd.decompress(body), ts)
    else:
        raise CorruptArchive("codec sconosciuto %d" % codec)
    if len(raw) != raw_len:
        raise CorruptArchive("wrong block length")
    return raw


def bhash(data) -> str:
    return hashlib.blake2b(data, digest_size=32).hexdigest()


def _lower_priority():
    """Thread a priorità bassa: nice 19 (per thread, Linux) e I/O idle (ioprio_set), best effort."""
    try:
        os.setpriority(os.PRIO_PROCESS, threading.get_native_id(), 19)
    except (OSError, AttributeError):
        pass
    try:
        import ctypes
        libc = ctypes.CDLL(None, use_errno=True)
        # ioprio_set(IOPRIO_WHO_PROCESS=1, tid, IOPRIO_CLASS_IDLE<<13); x86_64 syscall 251, aarch64 30
        nr = {"x86_64": 251, "aarch64": 30}.get(os.uname().machine)
        if nr:
            libc.syscall(nr, 1, threading.get_native_id(), 3 << 13)
    except Exception:  # noqa: BLE001
        pass


# --------------------------------------------------------------------------- archivio
class KvArchive:
    def __init__(self, root: str, level: int = 3, threads: int = 4, codecs: dict | None = None,
                 low_priority: bool = False):
        if zstd is None:
            raise KvaError("serve Python >= 3.14 (compression.zstd)")
        self.root = os.path.abspath(root)
        self.level, self.threads = level, max(1, threads)
        self.codecs = {**DEFAULT_CODECS, **(codecs or {})}
        self.low_priority = low_priority
        os.makedirs(os.path.join(self.root, "blocks"), exist_ok=True)
        os.makedirs(os.path.join(self.root, "manifests"), exist_ok=True)
        self._tlock = threading.RLock()

    # ---------- percorsi / lock ----------
    def block_path(self, h: str) -> str:
        return os.path.join(self.root, "blocks", h[:2], h + ".kvb")

    def manifest_path(self, name: str) -> str:
        if "/" in name or name.startswith("."):
            raise KvaError("nome non valido: %r" % name)
        return os.path.join(self.root, "manifests", name + ".kva")

    class _Lock:
        def __init__(self, arc):
            self.arc = arc

        def __enter__(self):
            self.arc._tlock.acquire()
            self.fd = os.open(os.path.join(self.arc.root, ".lock"), os.O_RDWR | os.O_CREAT, 0o600)
            fcntl.flock(self.fd, fcntl.LOCK_EX)
            return self

        def __exit__(self, *a):
            fcntl.flock(self.fd, fcntl.LOCK_UN)
            os.close(self.fd)
            self.arc._tlock.release()

    def lock(self):
        return KvArchive._Lock(self)

    def _pool(self):
        return ThreadPoolExecutor(self.threads, initializer=_lower_priority if self.low_priority else None)

    # ---------- interrogazioni ----------
    def names(self) -> list[str]:
        d = os.path.join(self.root, "manifests")
        return sorted(f[:-4] for f in os.listdir(d) if f.endswith(".kva"))

    def has(self, name: str) -> bool:
        return os.path.exists(self.manifest_path(name))

    def manifest(self, name: str) -> dict:
        try:
            with open(self.manifest_path(name), encoding="utf-8") as f:
                m = json.load(f)
        except FileNotFoundError:
            raise KvaError("no archive for %r" % name) from None
        except ValueError as e:
            raise CorruptArchive("manifest illeggibile per %r: %s" % (name, e)) from None
        if m.get("version") != VERSION:
            raise CorruptArchive("versione manifest %r" % m.get("version"))
        return m

    def refcounts(self) -> dict[str, int]:
        """hash -> numero di riferimenti (somma su tutti i manifest; un blocco ripetuto nello stesso file conta più
        volte)."""
        rc: dict[str, int] = {}
        for nm in self.names():
            for h, _ in self.manifest(nm)["blocks"]:
                rc[h] = rc.get(h, 0) + 1
        return rc

    def stored_blocks(self) -> dict[str, int]:
        out = {}
        bd = os.path.join(self.root, "blocks")
        for sub in os.listdir(bd):
            sp = os.path.join(bd, sub)
            if not os.path.isdir(sp):
                continue
            for f in os.listdir(sp):
                if f.endswith(".kvb"):
                    out[f[:-4]] = os.path.getsize(os.path.join(sp, f))
        return out

    def usage(self) -> dict:
        sb = self.stored_blocks()
        logical = sum(self.manifest(n)["size"] for n in self.names())
        stored = sum(sb.values())
        return {"manifests": len(self.names()), "blocks": len(sb), "stored_bytes": stored,
                "logical_bytes": logical, "ratio": (stored / logical) if logical else None}

    # ---------- scrittura ----------
    def _write_block(self, h: str, blob: bytes) -> int:
        """Scrive (tmp + fsync + rename) se assente. -> byte nuovi scritti (0 se già presente)."""
        path = self.block_path(h)
        if os.path.exists(path):
            return 0
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = "%s.tmp-%d-%d" % (path, os.getpid(), threading.get_ident())
        with open(tmp, "wb") as f:
            f.write(blob)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        return len(blob)

    def archive(self, src: str, name: str | None = None, delete_original: bool = False,
                should_abort=None, before_delete=None) -> dict:
        """Archivia `src`. Ordine (sicuro a interruzioni): blocchi (fsync) -> manifest temporaneo -> ricostruzione dal
        disco con sha256 == originale -> rename del manifest -> (se richiesto) cancellazione dell'originale.
        `should_abort()` è chiamata fra i blocchi (True = interrompi, originale intatto). `before_delete(st)` (stat
        dell'originale) può vietare la cancellazione restituendo False (es. Strata occupato)."""
        t0 = time.time()
        name = name or os.path.basename(src)
        self.manifest_path(name)
        st0 = os.stat(src)
        # il lock copre blocchi + verifica + manifest: gc non può togliere blocchi non ancora referenziati
        with self.lock():
            r = self._archive_locked(src, name, st0, t0, should_abort)
        deleted = False
        if delete_original:
            st1 = os.stat(src)
            same = (st1.st_size, st1.st_mtime_ns, st1.st_ino) == (st0.st_size, st0.st_mtime_ns, st0.st_ino)
            if same and (before_delete is None or before_delete(st1) is not False):
                os.remove(src)
                self._fsync_dir(os.path.dirname(os.path.abspath(src)))
                deleted = True
        r["deleted_original"] = deleted
        r["ms"] = round((time.time() - t0) * 1000)
        return r

    def _archive_locked(self, src, name, st0, t0, should_abort) -> dict:
        with open(src, "rb") as f:
            import mmap
            size = st0.st_size
            mm = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) if size else b""
            try:
                mv = memoryview(mm)
                blocks = split_blocks(mv)
                sha = hashlib.sha256()
                entries, new_bytes, new_blocks, dedup_bytes = [], 0, 0, 0
                kinds: dict[str, list[int]] = {}

                def work(b):
                    o, ln, kind, ts = b
                    raw = mv[o:o + ln]
                    h = bhash(raw)
                    with claim_mu:
                        if h in claimed or os.path.exists(self.block_path(h)):
                            return h, ln, kind, 0
                        claimed.add(h)            # stesso blocco due volte nel file: scritto una volta sola
                    codec = self.codecs.get(kind, CODEC_ZSTD)
                    return h, ln, kind, self._write_block(h, encode_block(raw, codec, ts, self.level))

                claimed, claim_mu = set(), threading.Lock()
                with self._pool() as ex:
                    W = self.threads * 4
                    futs = []
                    i = 0
                    while i < len(blocks) or futs:
                        while i < len(blocks) and len(futs) < W:
                            futs.append(ex.submit(work, blocks[i]))
                            i += 1
                        h, ln, kind, wrote = futs.pop(0).result()
                        o = blocks[len(entries)][0]
                        sha.update(mv[o:o + ln])          # sha256 dell'originale, in ordine
                        entries.append([h, ln])
                        k = kinds.setdefault(kind, [0, 0, 0])
                        k[0] += ln
                        k[1] += wrote
                        if wrote:
                            new_bytes += wrote
                            new_blocks += 1
                        else:
                            dedup_bytes += ln
                            k[2] += ln
                        if should_abort and should_abort():
                            for fu in futs:
                                fu.cancel()
                            raise Aborted("interrotto dopo %d blocchi su %d" % (len(entries), len(blocks)))
                del mv
            finally:
                if size:
                    try:
                        mm.close()
                    except BufferError:
                        pass                      # viste ancora vive dopo un'interruzione: la chiude il GC
        t_arch = time.time()
        man = {"version": VERSION, "name": name, "size": size, "sha256": sha.hexdigest(),
               "mtime": st0.st_mtime, "created": time.time(), "level": self.level,
               "blocks": entries,
               "stats": {"n_blocks": len(entries), "new_blocks": new_blocks, "new_stored_bytes": new_bytes,
                         "dedup_bytes": dedup_bytes,
                         "kinds": {k: {"raw": v[0], "new_stored": v[1], "dedup_raw": v[2]} for k, v in kinds.items()}}}
        mp = self.manifest_path(name)
        tmp = mp + ".tmp-%d" % os.getpid()
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(man, f, separators=(",", ":"))
            f.flush()
            os.fsync(f.fileno())
        try:
            vsha, vsize = self._verify_entries(entries)
            if vsha != man["sha256"] or vsize != size:
                raise CorruptArchive("verification failed for %s: sha256 of the reconstruction differs" % name)
            os.replace(tmp, mp)
            self._fsync_dir(os.path.dirname(mp))
        except BaseException:
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise
        t_ver = time.time()
        return {"name": name, "bytes": size, "new_stored_bytes": new_bytes, "dedup_bytes": dedup_bytes,
                "blocks": len(entries), "new_blocks": new_blocks, "sha256": man["sha256"],
                "archive_ms": round((t_arch - t0) * 1000), "verify_ms": round((t_ver - t_arch) * 1000)}

    @staticmethod
    def _fsync_dir(d):
        try:
            fd = os.open(d, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        except OSError:
            pass

    # ---------- lettura ----------
    def _read_block(self, idx: int, h: str, ln: int) -> bytes:
        try:
            with open(self.block_path(h), "rb") as f:
                blob = f.read()
        except FileNotFoundError:
            raise MissingBlock("block %d missing: %s (incomplete archive, reconstruction impossible)"
                               % (idx, h)) from None
        raw = decode_block(blob)
        if len(raw) != ln:
            raise CorruptArchive("block %d (%s): %d bytes instead of %d" % (idx, h, len(raw), ln))
        return raw

    def _iter_raw(self, entries, check_hash: bool = True):
        def rd(a):
            i, (h, ln) = a
            raw = self._read_block(i, h, ln)
            if check_hash and bhash(raw) != h:
                raise CorruptArchive("blocco %d: contenuto diverso dal suo hash %s" % (i, h))
            return raw
        with self._pool() as ex:
            W = self.threads * 4
            it = iter(enumerate(entries))
            futs = []
            for a in it:
                futs.append(ex.submit(rd, a))
                if len(futs) >= W:
                    break
            while futs:
                raw = futs.pop(0).result()
                a = next(it, None)
                if a is not None:
                    futs.append(ex.submit(rd, a))
                yield raw

    def _verify_entries(self, entries) -> tuple[str, int]:
        sha, n = hashlib.sha256(), 0
        for raw in self._iter_raw(entries):
            sha.update(raw)
            n += len(raw)
        return sha.hexdigest(), n

    def verify(self, name: str) -> bool:
        m = self.manifest(name)
        sha, n = self._verify_entries(m["blocks"])
        return sha == m["sha256"] and n == m["size"]

    def restore(self, name: str, dest: str) -> dict:
        """Ricostruisce `name` in `dest`: file temporaneo nella stessa cartella, sha256 verificato, fsync, rename
        atomico. Se manca un blocco: MissingBlock e nessun file lasciato."""
        t0 = time.time()
        m = self.manifest(name)
        d = os.path.dirname(os.path.abspath(dest)) or "."
        tmp = os.path.join(d, ".%s.kva-tmp-%d" % (os.path.basename(dest), os.getpid()))
        sha, n = hashlib.sha256(), 0
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "wb") as f:
                for raw in self._iter_raw(m["blocks"], check_hash=False):
                    sha.update(raw)
                    f.write(raw)
                    n += len(raw)
                f.flush()
                if sha.hexdigest() != m["sha256"] or n != m["size"]:
                    raise CorruptArchive("ricostruzione di %s: sha256/dimensione diversi dall'originale" % name)
                os.fsync(f.fileno())
            os.replace(tmp, dest)
            self._fsync_dir(d)
        except BaseException:
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise
        return {"name": name, "bytes": n, "ms": round((time.time() - t0) * 1000), "sha256": m["sha256"]}

    # ---------- rimozione / pulizia ----------
    def remove(self, name: str, gc: bool = True) -> dict:
        """Toglie il manifest; con gc, cancella i blocchi rimasti senza riferimenti."""
        with self.lock():
            try:
                os.remove(self.manifest_path(name))
            except FileNotFoundError:
                return {"name": name, "removed": False, "freed_bytes": 0, "freed_blocks": 0}
            r = self._gc_locked() if gc else {"freed_bytes": 0, "freed_blocks": 0}
        return {"name": name, "removed": True, **r}

    def gc(self) -> dict:
        with self.lock():
            return self._gc_locked()

    def _gc_locked(self) -> dict:
        live = self.refcounts()
        freed, nb = 0, 0
        bd = os.path.join(self.root, "blocks")
        for sub in os.listdir(bd):
            sp = os.path.join(bd, sub)
            if not os.path.isdir(sp):
                continue
            for f in os.listdir(sp):
                h = f.split(".", 1)[0]
                if f.endswith(".kvb") and h in live:
                    continue
                p = os.path.join(sp, f)
                try:
                    freed += os.path.getsize(p)
                    os.remove(p)
                    nb += 1
                except OSError:
                    pass
        return {"freed_bytes": freed, "freed_blocks": nb}


# --------------------------------------------------------------------------- integrazione nel proxy
def default_root(cfg) -> str:
    if getattr(cfg, "kv_archive_dir", ""):
        return cfg.kv_archive_dir
    if getattr(cfg, "slot_dir", ""):
        return os.path.join(os.path.dirname(os.path.abspath(cfg.slot_dir)), "kv-archive")
    return ""


def discard(cfg, name: str, journal=None) -> None:
    """Il proxy ha cancellato (o sostituito) il salvataggio `name`: anche la sua copia archiviata non serve più."""
    if not getattr(cfg, "kv_archive", False):
        return
    root = default_root(cfg)
    if not root or not os.path.exists(os.path.join(root, "manifests", name + ".kva")):
        return
    try:
        r = KvArchive(root, threads=1).remove(name)
        if journal is not None:
            journal.log("kv_archive_drop", file=name, freed_bytes=r.get("freed_bytes"))
    except Exception as e:  # noqa: BLE001
        if journal is not None:
            journal.log("kv_archive_error", step="drop", file=name, error=str(e)[:300])


class ArchivingUpstream:
    """Avvolge l'Upstream di base: prima di un restore di un file archiviato (assente nella slot_dir) lo ricostruisce
    nella slot_dir; dopo un save con lo stesso nome di un archivio, l'archivio vecchio si scarta."""

    def __init__(self, up, cfg, journal, arc: KvArchive):
        self.up, self.cfg, self.journal, self.arc = up, cfg, journal, arc

    def __getattr__(self, k):
        return getattr(self.up, k)

    def raw(self, *a, **kw):
        return self.up.raw(*a, **kw)

    def ensure_local(self, filename: str) -> dict | None:
        dest = os.path.join(self.cfg.slot_dir, filename)
        if os.path.exists(dest) or not self.arc.has(filename):
            return None
        try:
            r = self.arc.restore(filename, dest)
        except Exception as e:  # noqa: BLE001
            self.journal.log("kv_archive_error", step="unarchive", file=filename, error=str(e)[:300])
            raise
        st = self.arc.manifest(filename)["stats"]
        ev = self.journal.log("kv_unarchive", file=filename, bytes=r["bytes"], ms=r["ms"],
                              blocks=st.get("n_blocks"))
        return ev

    def slot(self, action: str, filename: str) -> dict:
        if action == "restore" and self.cfg.slot_dir:
            self.ensure_local(filename)
        r = self.up.slot(action, filename)
        if action == "save" and self.arc.has(filename):
            discard(self.cfg, filename, self.journal)
        return r


class Archiver:
    """Thread a priorità bassa: quando Strata è inattivo da `kv_archive_idle_s` archivia i salvataggi freddi della
    slot_dir (non toccati da `kv_archive_min_age_s`, non quello che Strata tiene ora, non l'ultimo autosalvataggio
    di una conversazione, non l'ancora corrente) e cancella l'originale solo dopo la verifica sha256."""

    def __init__(self, proxy, arc: KvArchive):
        self.proxy, self.arc = proxy, arc
        self.cfg, self.store, self.journal = proxy.cfg, proxy.store, proxy.journal
        self.stop_ev = threading.Event()
        self.th = None
        self.failed: dict[str, float] = {}

    def start(self):
        self.th = threading.Thread(target=self._loop, daemon=True, name="kv-archive")
        self.th.start()
        return self

    def stop(self):
        self.stop_ev.set()

    def _loop(self):
        _lower_priority()
        while not self.stop_ev.wait(self.cfg.kv_archive_poll_s):
            try:
                self.tick()
            except Exception as e:  # noqa: BLE001
                self.journal.log("kv_archive_error", step="tick", error=repr(e)[:300])

    # ---------- politica ----------
    def _status(self):
        from .engines import normalize_status
        try:
            up = self.proxy.up
            return normalize_status(getattr(up, "engine", None), up)
        except Exception:  # noqa: BLE001
            return None

    def _strata_idle(self, now) -> bool:
        """Inattivo = lock del proxy libero, /v1/status senza richieste in corso e contatore `requests` fermo da
        almeno kv_archive_idle_s (vale anche per client che non passano dal proxy)."""
        if self.proxy.lock.locked():
            self._seen = None
            return False
        s = self._status()
        if s is None or (s.get("activity") or {}).get("in_flight"):
            self._seen = None
            return False
        req = (s.get("activity") or {}).get("requests")
        seen = getattr(self, "_seen", None)
        if seen is None or seen[0] != req:
            self._seen = (req, now)
            return False
        return now - seen[1] >= self.cfg.kv_archive_idle_s

    def _protected(self) -> set[str]:
        """File che restano in chiaro: l'autosalvataggio più recente di ogni conversazione, l'ancora corrente di ogni
        (conv, seg) e il file che corrisponde a ciò che Strata tiene ora."""
        keep = set()
        try:
            seen = set()
            for r in self.store.autosaves(active_only=True):       # più recenti prima
                if r["conv"] not in seen:
                    seen.add(r["conv"])
                    keep.add(r["file"])
        except Exception:  # noqa: BLE001
            pass
        st = getattr(self.proxy.up, "state", None) or {}
        if st.get("conv"):
            pre = "%s-seg%d-" % (st["conv"], st.get("seg") or 0)
            try:
                keep |= {f for f in os.listdir(self.cfg.slot_dir) if f.startswith(pre)}
            except OSError:
                pass
        return keep

    def candidates(self, now: float | None = None) -> list[str]:
        now = now or time.time()
        sd = self.cfg.slot_dir
        if not sd or not os.path.isdir(sd):
            return []
        keep = self._protected()
        out = []
        for f in sorted(os.listdir(sd)):
            p = os.path.join(sd, f)
            if not f.endswith(".bin") or f.startswith(".") or f in keep:
                continue
            if now - self.failed.get(f, 0) < 3600:
                continue
            try:
                stt = os.stat(p)
            except OSError:
                continue
            if now - stt.st_mtime < self.cfg.kv_archive_min_age_s or stt.st_size < self.cfg.kv_archive_min_bytes:
                continue
            out.append(f)
        return out

    def tick(self, now: float | None = None):
        """Al massimo un file per giro. -> evento kv_archive o None."""
        now = now or time.time()
        if not self._strata_idle(now):
            return None
        cands = self.candidates(now)
        if not cands:
            return None
        f = cands[0]
        src = os.path.join(self.cfg.slot_dir, f)

        def abort():
            return self.stop_ev.is_set() or self.proxy.lock.locked()

        def before_delete(_st):
            # cancellazione solo a proxy fermo: nessuna richiesta (né restore) in corso in questo istante
            if not self.proxy.lock.acquire(blocking=False):
                return False
            self._held = True
            return True

        self._held = False
        try:
            r = self.arc.archive(src, f, delete_original=True, should_abort=abort, before_delete=before_delete)
        except Aborted as e:
            self.journal.log("kv_archive_skip", file=f, reason="busy", detail=str(e)[:200])
            return None
        except Exception as e:  # noqa: BLE001
            self.failed[f] = now
            self.journal.log("kv_archive_error", step="archive", file=f, error=str(e)[:300])
            return None
        finally:
            if self._held:
                self.proxy.lock.release()
                self._held = False
        return self.journal.log("kv_archive", file=f, bytes_before=r["bytes"], bytes_new=r["new_stored_bytes"],
                                bytes_dedup=r["dedup_bytes"], blocks=r["blocks"], new_blocks=r["new_blocks"],
                                ms=r["ms"], archive_ms=r["archive_ms"], verify_ms=r["verify_ms"],
                                deleted_original=r["deleted_original"])


def enable(proxy, start: bool = True):
    """Chiamata dal server quando cfg.kv_archive: avvolge l'upstream di base e avvia il thread."""
    cfg = proxy.cfg
    root = default_root(cfg)
    if not cfg.slot_dir or not root:
        proxy.journal.log("kv_archive_error", step="enable", error="serve slot_dir")
        return None
    arc = KvArchive(root, level=cfg.kv_archive_level, threads=cfg.kv_archive_threads, low_priority=True)
    up = proxy.up
    inner = getattr(up, "up", None) if up.__class__.__name__ == "Tracker" else None
    if inner is not None and not isinstance(inner, ArchivingUpstream):
        up.up = ArchivingUpstream(inner, cfg, proxy.journal, arc)
    elif inner is None and not isinstance(up, ArchivingUpstream):
        proxy.up = ArchivingUpstream(up, cfg, proxy.journal, arc)
    proxy.archiver = Archiver(proxy, arc)
    if start:
        proxy.archiver.start()
    return proxy.archiver


def _main(argv=None):
    """CLI: archive / restore / verify / ls / gc / usage (prove e manutenzione a mano)."""
    import argparse
    ap = argparse.ArgumentParser(prog="python -m ctxproxy.kvarchive")
    ap.add_argument("--root", required=True)
    ap.add_argument("--threads", type=int, default=4)
    ap.add_argument("--level", type=int, default=3)
    ap.add_argument("--codecs", choices=sorted(CODEC_SETS), default="mixed",
                    help="mixed (di serie) / full (riordino anche K/V) / plain (zstd semplice)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a1 = sub.add_parser("archive")
    a1.add_argument("files", nargs="+")
    a1.add_argument("--delete", action="store_true")
    a2 = sub.add_parser("restore")
    a2.add_argument("name")
    a2.add_argument("dest")
    a3 = sub.add_parser("verify")
    a3.add_argument("names", nargs="*")
    sub.add_parser("ls")
    sub.add_parser("gc")
    sub.add_parser("usage")
    a = ap.parse_args(argv)
    arc = KvArchive(a.root, level=a.level, threads=a.threads, codecs=CODEC_SETS[a.codecs])
    if a.cmd == "archive":
        for f in a.files:
            print(json.dumps(arc.archive(f, delete_original=a.delete)), flush=True)
    elif a.cmd == "restore":
        print(json.dumps(arc.restore(a.name, a.dest)))
    elif a.cmd == "verify":
        for n in a.names or arc.names():
            t = time.time()
            print(json.dumps({"name": n, "ok": arc.verify(n), "ms": round((time.time() - t) * 1000)}), flush=True)
    elif a.cmd == "ls":
        for n in arc.names():
            m = arc.manifest(n)
            print(json.dumps({"name": n, "size": m["size"], **{k: m["stats"][k] for k in
                                                                 ("n_blocks", "new_blocks", "new_stored_bytes")}}))
    elif a.cmd == "gc":
        print(json.dumps(arc.gc()))
    else:
        print(json.dumps(arc.usage()))


if __name__ == "__main__":
    _main()
