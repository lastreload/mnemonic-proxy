# Author: Maurizio Verde — LastReload
"""Offline, schema-driven conversation deletion. No engine or model is contacted."""
from __future__ import annotations

import argparse
from contextlib import contextmanager, nullcontext
import datetime as dt
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import sys
import tempfile

from . import compat
from .datalock import DataLock
from .kvarchive import KvaError, KvArchive, default_root

INDEX = ("passages", "passages_done", "p_raw", "p_norm")


def ident(name):
    return '"' + name.replace('"', '""') + '"'


def schema(db):
    return {name: [r[1] for r in db.execute("PRAGMA table_info(%s)" % ident(name))]
            for name, in db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}


def state_name(name, conv):
    # All segment/anchor/autosave variants, including historical names. No glob interpolation.
    return re.fullmatch(re.escape(conv) + r"-seg\d+-[^/\\]+\.bin", name) is not None


def belongs(obj, conv):
    """Journal events can name only a state file, or contain nested autorestore/tracker metadata."""
    if isinstance(obj, dict):
        return any(belongs(k, conv) or belongs(v, conv) for k, v in obj.items())
    if isinstance(obj, list):
        return any(belongs(v, conv) for v in obj)
    return isinstance(obj, str) and (obj == conv or state_name(obj, conv))


@contextmanager
def snapshot(path):
    # Even mode=ro can create -wal/-shm on the source. Read a private copy including committed WAL instead.
    if not path.is_file():
        raise ValueError("archive does not exist: %s" % path)
    with tempfile.TemporaryDirectory(prefix="mnemonic-forget-read-") as tmp:
        copy = Path(tmp) / "archive.sqlite"
        shutil.copyfile(path, copy)
        wal = Path(str(path) + "-wal")
        if wal.exists():
            shutil.copyfile(wal, Path(str(copy) + "-wal"))
        db = sqlite3.connect(copy)
        try:
            yield db
        finally:
            db.close()


def kv_keys(db, tables, conv):
    if "kv" not in tables:
        return []
    keys = []
    for k, v in db.execute("SELECT k, v FROM kv"):
        # tool_names:, prompt_language:, response_clamp_warned:, and future per-conversation namespaces.
        match = k.endswith(":" + conv)
        try:
            match = match or belongs(json.loads(v), conv)
        except (ValueError, TypeError):
            pass
        if match:
            keys.append(k)
    return keys


def table_size(db, table, cols, conv):
    expr = "+".join("coalesce(length(CAST(%s AS BLOB)),0)" % ident(c) for c in cols)
    return db.execute("SELECT count(*), coalesce(sum(%s),0) FROM %s WHERE conv=?" %
                      (expr, ident(table)), (conv,)).fetchone()


def conversation_list(db):
    tables = {t: cols for t, cols in schema(db).items() if "conv" in cols}
    ids = set()
    for t in tables:
        ids.update(r[0] for r in db.execute("SELECT DISTINCT conv FROM %s WHERE conv IS NOT NULL" % ident(t)))
    print("id\trows\tlogical_bytes\tfirst_timestamp\tlast_timestamp")
    for conv in sorted(ids):
        rows = size = 0
        timestamps = []
        for t, cols in tables.items():
            n, b = table_size(db, t, cols, conv)
            rows += n
            size += b
            for col in ("created", "last_used"):
                if col in cols:
                    lo, hi = db.execute("SELECT min(%s),max(%s) FROM %s WHERE conv=?" %
                                        (ident(col), ident(col), ident(t)), (conv,)).fetchone()
                    timestamps += [x for x in (lo, hi) if x is not None]
        def stamp(ts):
            return dt.datetime.fromtimestamp(ts, dt.timezone.utc).isoformat() if ts is not None else "-"
        print("%s\t%d\t%d\t%s\t%s" % (conv, rows, size, stamp(min(timestamps) if timestamps else None),
                                            stamp(max(timestamps) if timestamps else None)))
    print("Sizes are logical SQLite row payloads (including derived index metadata), not allocated pages.")


def journal_lines(path, conv):
    if path.exists():
        with path.open("rb") as f:
            for number, line in enumerate(f, 1):
                try:
                    obj = json.loads(line)
                except (ValueError, UnicodeError) as e:
                    raise ValueError("%s:%d: invalid journal JSON; repair it before forgetting: %s" %
                                     (path, number, e)) from e
                yield line, belongs(obj, conv)


def plan(db, data, conv, slot, arc, live_roots):
    tables = schema(db)
    rows = {t: db.execute("SELECT count(*) FROM %s WHERE conv=?" % ident(t), (conv,)).fetchone()[0]
            for t, cols in tables.items() if "conv" in cols}
    keys = kv_keys(db, tables, conv)
    dropped = {t: db.execute("SELECT count(*) FROM %s" % ident(t)).fetchone()[0] for t in INDEX if t in tables}
    files = []
    if slot and Path(slot).exists():
        files += [p for p in Path(slot).iterdir() if state_name(p.name, conv)]
    if not slot and any(rows.get(t, 0) for t in ("anchors", "autosaves")):
        raise ValueError("saved-state records exist but slot_dir is unknown; pass --slot-dir or --config")
    manifests, garbage = [], []
    if arc:
        names = arc.names()
        live_blocks = set()
        for name in names:
            m = arc.manifest(name)  # Validate ALL manifests before any deletion or gc.
            if state_name(name, conv):
                manifests.append(Path(arc.manifest_path(name)))
            else:
                live_blocks.update(h for h, _ in m["blocks"])
        for sub in (Path(arc.root) / "blocks").iterdir():
            if sub.is_symlink():
                raise ValueError("refusing symlink in archive blocks: %s" % sub)
            if sub.is_dir():
                for p in sub.iterdir():
                    if not (p.name.endswith(".kvb") and p.name.split(".", 1)[0] in live_blocks):
                        garbage.append(p)
    files += manifests + garbage
    for root in live_roots:
        # Segment saves also write the full physical prompt to <data_dir>/<state>.bin.json.
        if root.exists():
            files += [p for p in root.iterdir() if p.name.endswith(".json") and state_name(p.name[:-5], conv)]
    files = sorted(set(files))
    for p in files:
        if p.is_symlink() or not p.is_file():
            raise ValueError("refusing non-regular state/archive file: %s" % p)
    journal = data / "journal.jsonl"
    jrows = jbytes = 0
    for line, remove in journal_lines(journal, conv):
        if remove:
            jrows += 1
            jbytes += len(line)
    rewrites = {}
    for root in live_roots:
        live = root / "live" / "last_request.json"
        if live.exists() and live.stat().st_size:
            obj = json.loads(live.read_bytes())
            if isinstance(obj, dict) and obj.get("conv") == conv:
                rewrites[live] = b""
        owners = root / "router-slot-models.json"
        if owners.exists():
            obj = json.loads(owners.read_bytes())
            kept = {k: v for k, v in obj.items() if not state_name(k, conv)}
            if len(kept) != len(obj):
                rewrites[owners] = json.dumps(kept, ensure_ascii=False, indent=0).encode("utf-8")
    if not (sum(rows.values()) or keys or any(p not in garbage for p in files) or jrows or rewrites):
        raise ValueError("conversation not found: %s" % conv)
    return dict(rows=rows, keys=keys, dropped=dropped, files=files, manifests=manifests, garbage=garbage,
                journal=journal, journal_rows=jrows, journal_bytes=jbytes, rewrites=rewrites)


@contextmanager
def atomic_writer(path):
    fd, tmp = tempfile.mkstemp(prefix=".forget-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as f:
            yield f
            f.flush()
            os.fsync(f.fileno())
        # Keep the original access permissions; the temporary file is private until replacement.
        os.chmod(tmp, path.stat().st_mode & 0o777)
        compat.replace(tmp, str(path))
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def report(p, conv, dry_run):
    print("%s conversation %s" % ("Would forget" if dry_run else "Forgetting", conv))
    for t, n in sorted(p["rows"].items()):
        print("  table %s: %d rows%s" % (t, n, " (conversation match; full index dropped below)" if t in INDEX else ""))
    print("  kv: %d entries (%s)" % (len(p["keys"]), ", ".join(p["keys"]) or "none"))
    print("  structured recall index: drop %s (rebuilt lazily for surviving conversations)" %
          (", ".join("%s=%d rows" % x for x in p["dropped"].items()) or "none"))
    for path in p["files"]:
        print("  remove %s: %d bytes" % (path, path.stat().st_size))
    print("  journal: %d lines, %d bytes removed (atomic rewrite)" % (p["journal_rows"], p["journal_bytes"]))
    for path, body in p["rewrites"].items():
        print("  rewrite %s: %d -> %d bytes" % (path, path.stat().st_size, len(body)))
    print("  total: %d conversation rows, %d files, %d file bytes; %d manifests, %d gc files" %
          (sum(p["rows"].values()), len(p["files"]), sum(x.stat().st_size for x in p["files"]),
           len(p["manifests"]), len(p["garbage"])))
    print("  SQLite: rebuild archive_fts, secure_delete, VACUUM and truncate WAL (reclaimed bytes depend on layout)")


def apply(dbpath, p, conv, arc):
    errors = []
    db = sqlite3.connect(dbpath)
    try:
        db.execute("PRAGMA secure_delete=ON")
        with db:
            for table in p["rows"]:
                db.execute("DELETE FROM %s WHERE conv=?" % ident(table), (conv,))
            db.executemany("DELETE FROM kv WHERE k=?", [(k,) for k in p["keys"]])
            for table in p["dropped"]:
                db.execute("DROP TABLE %s" % ident(table))
            if "archive_fts" in schema(db):
                db.execute("INSERT INTO archive_fts(archive_fts) VALUES('rebuild')")
        db.execute("VACUUM")
        result = db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        if result[0]:
            raise ValueError("SQLite WAL checkpoint is busy; an external reader must close the database")
    finally:
        db.close()

    def attempt(label, fn):
        try:
            fn()
        except (OSError, ValueError, sqlite3.Error, KvaError) as e:
            errors.append("%s: %s" % (label, e))

    for path in p["files"]:
        if path not in p["garbage"]:
            attempt(str(path), path.unlink)
    if arc:
        attempt("kv-archive gc", lambda: arc._gc_locked(strict=True))
    if p["journal_rows"]:
        def rewrite_journal():
            with atomic_writer(p["journal"]) as f:
                for line, remove in journal_lines(p["journal"], conv):
                    if not remove:
                        f.write(line)
        attempt(str(p["journal"]), rewrite_journal)
    for path, body in p["rewrites"].items():
        def rewrite(path=path, body=body):
            with atomic_writer(path) as f:
                f.write(body)
        attempt(str(path), rewrite)
    for path in p["files"]:
        if path.exists():
            errors.append("not removed: %s" % path)
    return errors


def build_forget_parser():
    ap = argparse.ArgumentParser(prog="mnemonic-proxy forget", description=__doc__)
    ap.add_argument("--data", required=True, help="proxy data directory")
    group = ap.add_mutually_exclusive_group(required=True)
    group.add_argument("--conv", help="conversation id to remove")
    group.add_argument("--list", action="store_true", help="list ids, logical sizes and UTC timestamps")
    ap.add_argument("--slot-dir", help="saved engine state directory (overrides config)")
    ap.add_argument("--kv-archive", help="cold archive directory (overrides config)")
    ap.add_argument("--config", help="proxy JSON config; default: MNEMONIC_CONFIG")
    ap.add_argument("--dry-run", action="store_true", help="report without changing stored data")
    return ap


def main(argv=None):
    from .server import load_config
    ap = build_forget_parser()
    a = ap.parse_args(argv)
    a.engine = None
    a.config = a.config or os.environ.get("MNEMONIC_CONFIG")   # forget only: the server itself needs --config
    if a.list and a.dry_run:
        ap.error("--dry-run requires --conv")
    if a.conv is not None and not re.fullmatch(r"[A-Za-z0-9_-]+", a.conv):
        ap.error("invalid conversation id (expected letters, digits, '_' or '-')")
    try:
        _, cfg = load_config(a)
        source = a.config or os.environ.get("MNEMONIC_CONFIG") or "Config defaults"
        slot = os.path.abspath(os.path.expanduser(a.slot_dir)) if a.slot_dir else cfg.slot_dir
        if a.slot_dir:
            cfg.slot_dir = slot
        root = os.path.abspath(os.path.expanduser(a.kv_archive)) if a.kv_archive else default_root(cfg)
        print("slot_dir: %s (source: %s)" % (slot or "not configured", "--slot-dir" if a.slot_dir else source))
        root_source = "--kv-archive" if a.kv_archive else source if cfg.kv_archive_dir else "derived from slot_dir"
        print("kv_archive: %s (source: %s)" % (root or "not configured", root_source))
        data = Path(a.data).resolve()
        live_roots = {data, Path(cfg.data_dir).expanduser().resolve()} if cfg.data_dir else {data}
        with DataLock(data):
            arc = KvArchive(root, metadata_only=True) if root and Path(root).exists() else None
            # A dry run never creates an archive lock or directories. Existing lock files are safe to flock.
            lock = arc.lock() if arc and (not a.dry_run and not a.list or (Path(root) / ".lock").exists()) else nullcontext()
            with lock, snapshot(data / "archive.sqlite") as db:
                if a.list:
                    conversation_list(db)
                    return 0
                p = plan(db, data, a.conv, slot, arc, live_roots)
                report(p, a.conv, a.dry_run)
                if a.dry_run:
                    print("DRY RUN: no stored data changed.")
                    return 0
                errors = apply(data / "archive.sqlite", p, a.conv, arc)
                if errors:
                    for error in errors:
                        print("ERROR: " + error, file=sys.stderr)
                    print("INCOMPLETE: some data could not be removed; fix the errors and retry.", file=sys.stderr)
                    return 1
                print("Forgot %s. FTS rebuilt, SQLite vacuumed, state removed and journal filtered." % a.conv)
                return 0
    except (OSError, ValueError, sqlite3.Error, KvaError) as e:
        print("forget: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
