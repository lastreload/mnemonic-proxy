#!/usr/bin/env python3
"""Impatto storico del filtro `idx < max_idx` del recall, in SOLA LETTURA.

Per ogni giornale con il suo archivio: ogni chiamata `recall` registrata viene riclassificata
confrontando l'indice del blocco chiesto con l'indice dell'ultimo messaggio UMANO della
conversazione (lo stesso `is_human_user` del proxy). `idx >= ultimo_umano` = la chiamata non
poteva riuscire su 0.3.23, qualunque fosse il testo in archivio.

Nessuna scrittura: gli archivi sono aperti in sola lettura (file: ... ?mode=ro&immutable=1).
Uso:  python3 tools/measure_recall_filter.py <nome> <journal.jsonl> <archive.sqlite> [...]
Scrive il dettaglio in impact.json nella cartella corrente.
"""
from __future__ import annotations

import json
import sqlite3
import sys


def is_human_user(role: str, content: str) -> bool:
    if role != "user":
        return False
    t = (content or "").strip()
    return not (t.startswith("<tool_response>") and t.endswith("</tool_response>"))


def open_ro(path: str):
    return sqlite3.connect("file:%s?mode=ro" % path, uri=True)


def users_of(db, conv: str) -> list[tuple[int, str]]:
    """Messaggi umani della conversazione: (indice, testo). L'archivio cresce a ogni richiesta, quindi
    l'indice giusto per una singola chiamata recall NON è l'ultimo della conversazione ma quello del
    messaggio dell'utente registrato in quell'evento (`last_user`, troncato a 300 caratteri dal giornale)."""
    rows = db.execute("SELECT idx, content FROM archive WHERE conv=? AND role='user' ORDER BY idx", (conv,)).fetchall()
    return [(i, c or "") for i, c in rows if is_human_user("user", c)]


def measure(name: str, journal: str, archive: str) -> dict:
    ev = []
    with open(journal, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    ev.append(json.loads(line))
                except ValueError:
                    pass
    db = open_ro(archive)
    rows = {rid: (conv, idx, role, len(content or ""))
            for rid, conv, idx, role, content in db.execute("SELECT rid, conv, idx, role, content FROM archive")}
    lu_cache: dict[str, int | None] = {}
    out = {"name": name, "recall_events": 0, "by_id": 0, "by_query": 0, "by_path": 0,
           "id_blocked": 0, "id_unknown": 0, "id_ok": 0, "id_empty": 0,
           "query_blocked_only": 0, "path_blocked_only": 0, "query_empty": 0, "path_empty": 0,
           "lu_unmatched": 0, "masks": 0, "mask_emergency": 0, "mask_ids": 0, "blocked_ids": []}
    for e in ev:
        if e.get("event") == "mask":
            out["masks"] += 1
            out["mask_ids"] += int(e.get("count") or 0)
            if e.get("emergency"):
                out["mask_emergency"] += 1
            continue
        if e.get("event") != "recall":
            continue
        out["recall_events"] += 1
        conv = e.get("conv")
        if conv not in lu_cache:
            lu_cache[conv] = users_of(db, conv)
        # indice ESATTO del max_idx usato in quella chiamata: il messaggio dell'utente registrato nell'evento
        lu_text = (e.get("last_user") or "").strip()
        cand = [i for i, c in lu_cache[conv] if lu_text and c.strip().startswith(lu_text[:200])]
        lu = max(cand) if cand else (max([i for i, _ in lu_cache[conv]], default=None) if not lu_text else None)
        if lu is None:
            out["lu_unmatched"] += 1
            continue
        try:
            a = json.loads(e.get("args") or "{}")
        except ValueError:
            a = {}
        if not isinstance(a, dict):
            a = {}
        rid = str(a.get("id") or "").strip().removeprefix("recall:").removeprefix("id=")
        q = str(a.get("query") or "").strip() or (a.get("queries") or [""])[0]
        path = str(a.get("path") or "").strip()
        empty = not (e.get("results") or [])
        if rid:
            out["by_id"] += 1
            out["id_empty"] += bool(empty)
            r = rows.get(rid)
            if r is None or r[0] != conv:
                out["id_unknown"] += 1
            elif lu is not None and r[1] >= lu:
                out["id_blocked"] += 1
                out["blocked_ids"].append({"rid": rid, "idx": r[1], "last_user": lu, "role": r[2],
                                           "chars": r[3], "args": e.get("args"), "results": len(e.get("results") or [])})
            else:
                out["id_ok"] += 1
        elif path:
            out["by_path"] += 1
            out["path_empty"] += bool(empty)
            if empty and lu is not None:
                p = path.lower()
                n_after = db.execute(
                    "SELECT count(*) FROM fileops WHERE conv=? AND idx >= ? AND (lower(path)=? OR lower(path) LIKE ?)",
                    (conv, lu, p, "%" + p)).fetchone()[0]
                n_before = db.execute(
                    "SELECT count(*) FROM fileops WHERE conv=? AND idx < ? AND (lower(path)=? OR lower(path) LIKE ?)",
                    (conv, lu, p, "%" + p)).fetchone()[0]
                if n_after and not n_before:
                    out["path_blocked_only"] += 1
        elif q:
            out["by_query"] += 1
            out["query_empty"] += bool(empty)
            if empty and lu is not None:
                n_after = db.execute("SELECT count(*) FROM archive WHERE conv=? AND idx >= ? AND "
                                     "instr(lower(content), lower(?)) > 0", (conv, lu, q)).fetchone()[0]
                n_before = db.execute("SELECT count(*) FROM archive WHERE conv=? AND idx < ? AND "
                                      "instr(lower(content), lower(?)) > 0", (conv, lu, q)).fetchone()[0]
                if n_after and not n_before:
                    out["query_blocked_only"] += 1
    db.close()
    return out


def main(argv):
    if len(argv) < 4 or (len(argv) - 1) % 3:
        print(__doc__)
        return 2
    res = []
    for i in range(1, len(argv), 3):
        res.append(measure(argv[i], argv[i + 1], argv[i + 2]))
    tot = {k: 0 for k in res[0] if isinstance(res[0][k], int)}
    for r in res:
        for k in tot:
            tot[k] += r[k]
        print("%-28s recall=%-4d id=%-4d (bloccati=%-3d sconosciuti=%-3d ok=%-4d vuoti=%-3d)  "
              "query=%-4d (vuote=%-3d solo-dopo=%-2d)  path=%-3d (vuoti=%-2d solo-dopo=%-2d)  "
              "mask=%-4d (emergenza=%-3d id=%d)"
              % (r["name"], r["recall_events"], r["by_id"], r["id_blocked"], r["id_unknown"], r["id_ok"],
                 r["id_empty"], r["by_query"], r["query_empty"], r["query_blocked_only"], r["by_path"],
                 r["path_empty"], r["path_blocked_only"], r["masks"], r["mask_emergency"], r["mask_ids"]))
    print("-" * 150)
    print("TOTALE  recall=%d  id=%d  id bloccati dal filtro=%d  id sconosciuti=%d  query vuote solo-dopo=%d  "
          "path vuoti solo-dopo=%d  mask=%d (emergenza=%d, %d id mascherati)"
          % (tot["recall_events"], tot["by_id"], tot["id_blocked"], tot["id_unknown"],
             tot["query_blocked_only"], tot["path_blocked_only"], tot["masks"], tot["mask_emergency"],
             tot["mask_ids"]))
    with open("impact.json", "w", encoding="utf-8") as f:
        json.dump({"sources": res, "total": tot}, f, ensure_ascii=False, indent=1)
    print("dettaglio: impact.json")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
