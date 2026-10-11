#!/usr/bin/env python3
# Author: Maurizio Verde — LastReload (conferenza dell'archivio freddo)
"""Conferma che il `kv_archive` sta lavorando e quanto fa risparmiare.

Sole LETTURE: non archivia, non cancella, non tocca il motore.

Fotografa tre cose e le mette a confronto:

  sessions/     i .bin dello stato salvato (bytes, eta del file)
  kv-archive/   blocks/*.kvb + manifests/*.kva (usage, blocchi condivisi per deduplica)
  condizioni    cosa chiede l'archiviatore (ctxproxy/core.py) e quanto manca perche' scatti

Le condizioni per archiviare sono TUTTE e tre insieme:

  motore fermo da >= kv_archive_idle_s      (default 600 s = 10 min)
  file di stato fermo da >= kv_archive_min_age_s (default 1800 s = 30 min)
  file di stato >= kv_archive_min_bytes     (default 64 MiB)

e l'archiviatore si sveglia ogni kv_archive_poll_s (default 60 s).

Il contatore di fermata del motore si AZZERA a ogni richiesta: una conversazione in corso
(il cliente che chiama strumenti) impedisce l'archiviazione. E' il motivo per cui la
conferenza si fa a motore lasciato stare.

Uso:
    .venv314/bin/python tools/check_kv_archive.py              # fotografia adesso
    .venv314/bin/python tools/check_kv_archive.py --watch      # segue finche' archivia
    .venv314/bin/python tools/check_kv_archive.py --restore-check
                                                              # prova che il restore ricostruisce
                                                                un .bin byte-identico (sha256)
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
import types
import urllib.request

HERE = os.path.abspath(os.path.dirname(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from ctxproxy.kvarchive import KvArchive, default_root  # noqa: E402


def human(n):
    if n is None:
        return "-"
    n = float(n)
    for unit, div in (("GB", 1024 ** 3), ("MB", 1024 ** 2), ("KB", 1024)):
        if n >= div:
            return "%.2f %s" % (n / div, unit)
    return "%d B" % n


def hhmm(ts):
    return time.strftime("%H:%M:%S", time.localtime(ts))


def read_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def engine_status(upstream, timeout=8.0):
    """/v1/status del motore: requests, in_flight, started. None se non risponde."""
    try:
        with urllib.request.urlopen(upstream.rstrip("/") + "/v1/status", timeout=timeout) as r:
            st = json.loads(r.read().decode())
    except Exception as e:
        return {"error": str(e)}
    act = st.get("activity") or {}
    return {"requests": act.get("requests"), "in_flight": act.get("in_flight"),
            "started": st.get("started"), "loaded": st.get("loaded")}


def snapshot_sessions(slot_dir):
    out = []
    if not slot_dir or not os.path.isdir(slot_dir):
        return out
    now = time.time()
    for f in sorted(os.listdir(slot_dir)):
        p = os.path.join(slot_dir, f)
        if not os.path.isfile(p):
            continue
        st = os.stat(p)
        out.append({"name": f, "bytes": st.st_size, "mtime": st.st_mtime,
                    "age_s": round(now - st.st_mtime)})
    return out


def snapshot_archive(arc):
    """usage() della libreria + blocchi condivisi fra manifest (deduplica)."""
    if not os.path.isdir(arc.root):
        return {"root": arc.root, "exists": False}
    u = arc.usage()
    rc = arc.refcounts()
    stored = arc.stored_blocks()
    shared = {h: c for h, c in rc.items() if c > 1}
    shared_bytes = sum(stored.get(h, 0) for h in shared)
    logical_shared = sum(c - 1 for c in shared.values())
    return {"root": arc.root, "exists": True, "manifests": u["manifests"],
            "blocks": u["blocks"], "stored_bytes": u["stored_bytes"],
            "logical_bytes": u["logical_bytes"], "ratio": u["ratio"],
            "shared_blocks": len(shared), "shared_stored_bytes": shared_bytes,
            "reuses": logical_shared, "names": arc.names()}


def verify_all(arc, names=None):
    out = []
    for n in (names or arc.names()):
        t = time.time()
        try:
            ok = arc.verify(n)
        except Exception as e:
            ok = False
            out.append({"name": n, "ok": False, "error": str(e)[:200]})
            continue
        out.append({"name": n, "ok": ok, "ms": round((time.time() - t) * 1000)})
    return out


def restore_check(arc, dest_dir):
    """Prova il restore: ricostruisce ogni archivio e confronta sha256 con il manifest."""
    out = []
    os.makedirs(dest_dir, exist_ok=True)
    for n in arc.names():
        m = arc.manifest(n)
        dest = os.path.join(dest_dir, n + ".bin")
        try:
            r = arc.restore(n, dest)
            sha = hashlib.sha256()
            with open(dest, "rb") as f:
                for chunk in iter(lambda: f.read(1 << 22), b""):
                    sha.update(chunk)
            out.append({"name": n, "bytes": r["bytes"], "ms": r["ms"],
                        "sha256_ok": sha.hexdigest() == m["sha256"],
                        "sha256": m["sha256"][:16]})
        except Exception as e:
            out.append({"name": n, "error": str(e)[:200]})
        finally:
            try:
                os.remove(dest)
            except OSError:
                pass
    return out


def conditions(cfg, sessions, eng, idle_seen_s):
    """Cosa chiede l'archiviatore e quanto manca. idle_seen_s: secondi di motore fermo osservati."""
    need_idle = float(getattr(cfg, "kv_archive_idle_s", 600.0))
    need_age = float(getattr(cfg, "kv_archive_min_age_s", 1800.0))
    need_bytes = int(getattr(cfg, "kv_archive_min_bytes", 64 * 2 ** 20))
    rows = []
    for s in sessions:
        ok_age = s["age_s"] >= need_age
        ok_size = s["bytes"] >= need_bytes
        ok_eng = (idle_seen_s or 0) >= need_idle and not eng.get("in_flight")
        rows.append({"name": s["name"], "bytes": s["bytes"], "age_s": s["age_s"],
                     "size_ok": ok_size, "age_ok": ok_age, "engine_ok": ok_eng,
                     "ready": ok_age and ok_size and ok_eng,
                     "eta_age_s": max(0, round(need_age - s["age_s"])),
                     "eta_idle_s": max(0, round(need_idle - (idle_seen_s or 0)))})
    return rows, {"idle_s": need_idle, "min_age_s": need_age, "min_bytes": need_bytes,
                  "poll_s": float(getattr(cfg, "kv_archive_poll_s", 60.0))}


def report(sessions, arch, rows, need, eng, idle_seen_s, arc):
    print("== sessions/ (%s) ==" % hhmm(time.time()))
    if not sessions:
        print("  nessun .bin: lo stato salvato non c'e' (autosave non ha ancora salvato)")
    tot = 0
    for s in sessions:
        tot += s["bytes"]
        print("  %-46s %12s  eta %5ss" % (s["name"], human(s["bytes"]), s["age_s"]))
    print("  totale %s in %d file" % (human(tot), len(sessions)))

    print("\n== kv-archive/ (%s) ==" % arch["root"])
    if not arch.get("exists"):
        print("  la cartella non esiste ancora: l'archiviatore non ha mai archiviato")
    elif arch["manifests"] == 0:
        print("  vuota: %d blocchi, nessun manifest - l'archiviatore non ha ancora archiviato nulla"
              % arch["blocks"])
    else:
        print("  manifest %d, blocchi %d" % (arch["manifests"], arch["blocks"]))
        print("  logico %s  archiviato %s  ratio %s"
              % (human(arch["logical_bytes"]), human(arch["stored_bytes"]),
                 ("%.1f%%" % (arch["ratio"] * 100)) if arch["ratio"] else "-"))
        if arch["logical_bytes"]:
            saved = arch["logical_bytes"] - arch["stored_bytes"]
            print("  risparmio %s (%.1f%%)" % (human(saved), 100.0 * saved / arch["logical_bytes"]))
        print("  blocchi condivisi per deduplica: %d (%s gia pagati una sola volta, %d riutilizzi)"
              % (arch["shared_blocks"], human(arch["shared_stored_bytes"]), arch["reuses"]))
        for n in arch["names"]:
            print("    %s" % n)

    print("\n== perche' non archivia (condizioni di ctxproxy/core.py) ==")
    print("  motore fermo >= %ss, file fermo >= %ss, file >= %s, risveglio ogni %ss"
          % (int(need["idle_s"]), int(need["min_age_s"]), human(need["min_bytes"]), int(need["poll_s"])))
    print("  motore: requests=%s in_flight=%s; fermo osservato da %ss"
          % (eng.get("requests"), eng.get("in_flight"), int(idle_seen_s or 0)))
    if eng.get("error"):
        print("  motore non raggiungibile: %s" % eng["error"])
    for r in rows:
        print("  %-46s size_ok=%s age_ok=%s engine_ok=%s -> %s"
              % (r["name"], r["size_ok"], r["age_ok"], r["engine_ok"],
                 "PRONTO" if r["ready"] else "manca eta %ss, motore %ss"
                 % (r["eta_age_s"], r["eta_idle_s"])))


def main() -> int:
    ap = argparse.ArgumentParser(description="conferenza dell'archivio freddo kv_archive")
    ap.add_argument("--config", default=os.path.join(ROOT, "config.json"))
    ap.add_argument("--slot-dir", default="", help="default: dal config.json")
    ap.add_argument("--root", default="", help="default: <padre di slot_dir>/kv-archive")
    ap.add_argument("--upstream", default="http://127.0.0.1:8080")
    ap.add_argument("--watch", action="store_true", help="segue finche' appare un manifest")
    ap.add_argument("--interval", type=float, default=60.0, help="poll del --watch (default 60 s)")
    ap.add_argument("--duration", type=float, default=3600.0, help="max secondi del --watch (default 3600)")
    ap.add_argument("--restore-check", action="store_true",
                    help="ricostruisce ogni archivio e confronta sha256 (scrive in /tmp)")
    ap.add_argument("--json", action="store_true", help="stampa anche il JSON riassuntivo")
    a = ap.parse_args()

    raw = read_json(a.config) if os.path.exists(a.config) else {}
    slot_dir = a.slot_dir or raw.get("slot_dir", "")
    cfg = types.SimpleNamespace(
        slot_dir=slot_dir,
        kv_archive_dir=raw.get("kv_archive_dir", ""),
        kv_archive=bool(raw.get("kv_archive", False)),
        kv_archive_idle_s=raw.get("kv_archive_idle_s", 600.0),
        kv_archive_min_age_s=raw.get("kv_archive_min_age_s", 1800.0),
        kv_archive_min_bytes=raw.get("kv_archive_min_bytes", 64 * 2 ** 20),
        kv_archive_poll_s=raw.get("kv_archive_poll_s", 60.0))
    root = a.root or default_root(cfg)
    if not slot_dir:
        print("FALTA slot_dir (config.json senza slot_dir e senza --slot-dir): niente archivio")
        return 2
    print("config %s | kv_archive=%s | slot_dir=%s | root=%s" % (a.config, cfg.kv_archive, slot_dir, root))
    if not cfg.kv_archive:
        print("NOTA: nel config kv_archive e' spento. L'archiviatore non gira: la conferenza e' solo lo stato.")

    arc = KvArchive(root)
    eng0 = engine_status(a.upstream)
    sessions = snapshot_sessions(slot_dir)
    arch = snapshot_archive(arc)
    rows, need = conditions(cfg, sessions, eng0, 0.0)
    report(sessions, arch, rows, need, eng0, 0.0, arc)

    if not a.watch:
        if a.restore_check:
            print("\n== restore-check (ricostruzione + sha256) ==")
            for r in restore_check(arc, "/tmp/kva-restore-check"):
                print("  ", json.dumps(r, ensure_ascii=False))
        if a.json:
            print("\n" + json.dumps({"sessions": sessions, "archive": arch, "conditions": rows,
                                     "need": need}, ensure_ascii=False, indent=2))
        return 0

    print("\n--watch: motore fermo da %ss; un solo uso del motore azzera il conteggio\n"
          % int(0))
    last_req, idle_since = eng0.get("requests"), time.time()
    seen = set(arch["names"]) if arch.get("exists") else set()
    t_end = time.time() + a.duration
    while time.time() < t_end:
        time.sleep(a.interval)
        eng = engine_status(a.upstream)
        now = time.time()
        if eng.get("requests") != last_req:
            last_req, idle_since = eng.get("requests"), now
        idle_seen = now - idle_since
        sessions = snapshot_sessions(slot_dir)
        arch = snapshot_archive(arc)
        rows, need = conditions(cfg, sessions, eng, idle_seen)
        new = [n for n in arch["names"] if n not in seen]
        if new:
            print("== %s: ARCHIVIATO %s ==" % (hhmm(now), ", ".join(new)))
            report(sessions, arch, rows, need, eng, idle_seen, arc)
            if a.restore_check:
                print("\n== restore-check (ricostruzione + sha256) ==")
                for r in restore_check(arc, "/tmp/kva-restore-check"):
                    print("  ", json.dumps(r, ensure_ascii=False))
            if a.json:
                print(json.dumps({"sessions": sessions, "archive": arch}, ensure_ascii=False, indent=2))
            return 0
        ready = [r for r in rows if r["ready"]]
        print("%s  motore fermo %3.0fs  in_flight=%s  manifest=%d  %s"
              % (hhmm(now), idle_seen, eng.get("in_flight"), arch["manifests"],
                 ("PRONTI: " + ", ".join(r["name"] for r in ready)) if ready else
                 "mancano eta/motore"))
    print("--watch terminato senza archiviazione (motore mai rimasto fermo abbastanza)")
    return 1


if __name__ == "__main__":
    sys.exit(main())
