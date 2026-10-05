# Author: Maurizio Verde — LastReload
"""recall strutturato (docs/dev-notes/RECALL-RESULT.md): ricerca a passaggi, parole flessibili, struttura dai dati.

Nessun modello: solo SQLite/FTS5 e Python. Attivato da opzioni di Config (tutte spente di serie):
  recall_struct  risultati con intestazione (id, messaggio, segmento, ruolo, strumento, percorso, collegamento),
                 ricerca su PASSAGGI (finestre di ~700 caratteri con offset nel pezzo originale), modalità
                 timeline/first/neighbors/output/reasoning/tree, filtri role/seg/from/to, marcatura 'forte' (tutte le
                 parole della domanda nello stesso passaggio, o frase esatta), suggerimenti se non trova niente;
  recall_multi   `queries: [...]`: più varianti in una chiamata (fusione per rango + deduplica);
  recall_flex    indice normalizzato: identificatori spezzati (camelCase, snake_case, kebab), minuscole, radice
                 leggera comune a italiano e inglese (stessa funzione per indice e domanda); prima un tentativo
                 stretto (AND per gruppi), poi largo (OR).
Gli indici dei passaggi si costruiscono pigramente alla prima ricerca e poi in modo incrementale (tabella
passages_done): niente lavoro sul percorso di scrittura delle richieste, e funziona anche su archivi esistenti.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3

P_SCHEMA = """
CREATE TABLE IF NOT EXISTS passages(pid INTEGER PRIMARY KEY, rid TEXT, conv TEXT, idx INT, role TEXT, off INT,
    len INT);
CREATE INDEX IF NOT EXISTS passages_rid ON passages(rid);
CREATE INDEX IF NOT EXISTS passages_conv ON passages(conv, idx);
CREATE TABLE IF NOT EXISTS passages_done(rid TEXT, kind TEXT, PRIMARY KEY(rid, kind));
CREATE VIRTUAL TABLE IF NOT EXISTS p_raw USING fts5(t, content='', tokenize='unicode61 remove_diacritics 2');
CREATE VIRTUAL TABLE IF NOT EXISTS p_norm USING fts5(t, content='', tokenize='unicode61 remove_diacritics 2');
"""
WIN, STEP = 700, 550           # finestra e passo dei passaggi (caratteri)
SHOW_PAD = 160                 # contesto mostrato attorno al passaggio
MAX_SHOW = 1100                # caratteri massimi di estratto per risultato

STOP = set("""il lo la i gli le un uno una di da in con su per tra fra e ed o ma che chi cosa come quando dove
quale quali quanto quanta quanti quante del dello della dei degli delle al allo alla ai agli alle dal dalla dai dalle
nel nello nella nei negli nelle sul sulla sui sulle è era erano sono sei hai avevi aveva ho ha abbiamo hanno c ci
si se non più anche poi prima dopo questo questa quello quella the a an of to in on for and or is was were are be
been what which who how when where did do does it its this that these those with from at by as into than then""".split())

_WORD = re.compile(r"\w+")
_CAMEL = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+")


def stem(w: str) -> str:
    """Radice leggera, identica per indice e domanda, per italiano e inglese insieme: -ing/-ed, -s finale, vocale
    finale. Non è un vero stemmer (Porter è solo inglese): serve a far combaciare starts/start, ondata/ondate,
    decided/decide, virgole/virgola. Numeri e parole corte restano come sono."""
    if len(w) <= 3 or not w.isalpha():
        return w
    for suf in ("ing", "ed"):
        if w.endswith(suf) and len(w) - len(suf) >= 4:
            w = w[:-len(suf)]
            break
    if w.endswith("s") and not w.endswith("ss") and len(w) > 4:
        w = w[:-1]
    if w[-1] in "aeiou\u00e0\u00e8\u00e9\u00ec\u00f2\u00f9" and len(w) > 3:
        w = w[:-1]
    return w


def ident_parts(tok: str) -> list[str]:
    """startGold -> [start, gold]; snake_case -> [snake, case]; HTTPServer -> [http, server]; [] se non composto."""
    parts = []
    for p in tok.split("_"):
        parts.extend(_CAMEL.findall(p) or ([p] if p else []))
    parts = [p.lower() for p in parts if p]
    return parts if len(parts) > 1 else []


def norm_tokens(text: str) -> list[list[str]]:
    """Per ogni parola del testo: [forma intera normalizzata, parti normalizzate...]."""
    out = []
    for tok in _WORD.findall(text or ""):
        whole = stem(tok.lower())
        parts = [stem(p) for p in ident_parts(tok)]
        out.append([whole] + [p for p in parts if p != whole])
    return out


def norm_text(text: str) -> str:
    return " ".join(t for grp in norm_tokens(text) for t in grp)


def raw_tokens(text: str) -> list[list[str]]:
    return [[t.lower()] for t in _WORD.findall(text or "")]


def windows(text: str) -> list[tuple[int, int]]:
    """Finestre (off, len) di ~WIN caratteri, passo STEP, allineate a un a-capo vicino quando c'è."""
    n = len(text)
    if n <= WIN:
        return [(0, n)]
    out, off = [], 0
    while off < n:
        end = min(n, off + WIN)
        if end < n:
            nl = text.rfind("\n", off + WIN // 2, end)
            if nl > 0:
                end = nl + 1
        out.append((off, end - off))
        if end >= n:
            break
        nxt = off + STEP if end - off > STEP else end
        nl = text.find("\n", nxt - 80, nxt)
        off = nl + 1 if nl > off else nxt
    return out


def _q(t: str) -> str:
    return '"%s"' % t.replace('"', "")


def fts_queries(query: str, flex: bool) -> tuple[str, str, list[str]]:
    """(stretta, larga, termini). Stretta: AND dei gruppi (un identificatore composto vale come intero OPPURE tutte le
    sue parti); larga: OR di tutti i termini. Parole vuote tolte se resta almeno un termine."""
    groups = norm_tokens(query) if flex else raw_tokens(query)
    seen, gs = set(), []
    for g in groups:
        key = tuple(g)
        if key in seen or not g[0]:
            continue
        seen.add(key)
        gs.append(g)
    content = [g for g in gs if g[0] not in STOP and (len(g[0]) > 1 or g[0].isdigit())]
    gs = content or gs
    if not gs:
        return "", "", []
    strict = []
    for g in gs:
        if len(g) > 1:
            strict.append("(%s OR (%s))" % (_q(g[0]), " AND ".join(_q(p) for p in g[1:])))
        else:
            strict.append(_q(g[0]))
    terms = []
    for g in gs:
        for t in g:
            if t not in terms:
                terms.append(t)
    return " AND ".join(strict), " OR ".join(_q(t) for t in terms), terms


class PassageIndex:
    """Indice a passaggi sopra l'archivio di uno Store (stessa connessione e stesso lock)."""

    def __init__(self, store):
        self.st = store
        with store.lock:
            store.db.executescript(P_SCHEMA)

    def ensure(self, conv: str, flex: bool) -> int:
        """Indicizza i pezzi della conversazione non ancora divisi in passaggi. -> numero di pezzi nuovi."""
        kind = "norm" if flex else "raw"
        tbl = "p_norm" if flex else "p_raw"
        st = self.st
        with st.lock:
            rows = st.db.execute(
                "SELECT a.rid, a.idx, a.role, a.content FROM archive a WHERE a.conv=? AND NOT EXISTS "
                "(SELECT 1 FROM passages_done d WHERE d.rid=a.rid AND d.kind=?)", (conv, kind)).fetchall()
            if not rows:
                return 0
            for rid, idx, role, content in rows:
                content = content or ""
                have = st.db.execute("SELECT pid, off, len FROM passages WHERE rid=? ORDER BY off", (rid,)).fetchall()
                if not have:
                    have = []
                    for off, ln in windows(content):
                        cur = st.db.execute("INSERT INTO passages(rid, conv, idx, role, off, len) VALUES(?,?,?,?,?,?)",
                                            (rid, conv, idx, role, off, ln))
                        have.append((cur.lastrowid, off, ln))
                for pid, off, ln in have:
                    seg = content[off:off + ln]
                    st.db.execute("INSERT INTO %s(rowid, t) VALUES(?,?)" % tbl,
                                  (pid, norm_text(seg) if flex else seg))
                st.db.execute("INSERT OR IGNORE INTO passages_done VALUES(?,?)", (rid, kind))
            return len(rows)

    def search(self, conv: str, expr: str, flex: bool, max_idx: int, roles=None, lo=0, hi=None, limit=400):
        """-> [(pid, rid, idx, role, off, len, score)] con score = -bm25 (più alto = migliore)."""
        if not expr:
            return []
        tbl = "p_norm" if flex else "p_raw"
        sql = ("SELECT p.pid, p.rid, p.idx, p.role, p.off, p.len, -bm25(%s) AS s FROM %s JOIN passages p ON "
               "p.pid = %s.rowid WHERE %s MATCH ? AND p.conv=? AND p.idx < ? AND p.idx >= ?" % (tbl, tbl, tbl, tbl))
        args = [expr, conv, max_idx if hi is None else min(max_idx, hi + 1), lo]
        if roles:
            sql += " AND p.role IN (%s)" % ",".join("?" * len(roles))
            args += list(roles)
        sql += " ORDER BY s DESC, p.idx LIMIT ?"
        args.append(limit)
        with self.st.lock:
            try:
                return self.st.db.execute(sql, args).fetchall()
            except sqlite3.OperationalError:
                return []


# ---------------------------------------------------------------- struttura dai dati
class Structure:
    """Collegamenti ricavati dall'archivio, senza modello:
       - stesso messaggio (protocollo): risposta, ragionamento e argomenti di una chiamata hanno lo stesso indice;
       - chiamata -> uscita: tool_call_id registrato nel registro file (fileops), altrimenti ordine delle chiamate
         (k-esima riga degli argomenti -> k-esimo messaggio tool che segue): marcato 'per posizione'."""

    def __init__(self, store):
        self.st = store
        self._cache: dict[str, tuple[int, dict, dict]] = {}

    def calls(self, conv: str) -> tuple[dict, dict]:
        """-> (per rid di uscita: {name, path, cmd, args_rid, call_idx, link}, per rid di argomenti: [rid uscite])."""
        with self.st.lock:
            n = self.st.db.execute("SELECT count(*) FROM archive WHERE conv=?", (conv,)).fetchone()[0]
            c = self._cache.get(conv)
            if c and c[0] == n:
                return c[1], c[2]
            rows = self.st.db.execute("SELECT rid, idx, role, name, content FROM archive WHERE conv=? AND role IN "
                                      "('assistant-tool-args', 'tool') ORDER BY idx", (conv,)).fetchall()
            fops = self.st.db.execute("SELECT call_id, op, path, rid_args, rid_out FROM fileops WHERE conv=?",
                                      (conv,)).fetchall()
        by_out, by_args = {}, {}
        reg = {r[4]: r for r in fops}
        cur, k, calls = None, 0, []
        for rid, idx, role, name, content in rows:
            if role == "assistant-tool-args":
                cur, k, calls = (rid, idx), 0, []
                for line in (content or "").split("\n"):
                    nm, _, a = line.partition(" ")
                    try:
                        args = json.loads(a) if a.strip().startswith("{") else {}
                    except ValueError:
                        args = {}
                    args = args if isinstance(args, dict) else {}
                    calls.append((nm, str(args.get("path") or args.get("file_path") or ""),
                                  str(args.get("command") or args.get("cmd") or "")))
                continue
            info = {"name": "", "path": "", "cmd": "", "args_rid": None, "call_idx": None, "link": ""}
            if rid in reg:
                _, op, path, ra, _ = reg[rid]
                info.update(name=op, path=path, args_rid=ra, link="registrato (tool_call_id)")
                if cur:
                    info["call_idx"] = cur[1]
            elif cur and k < len(calls):
                nm, path, cmd = calls[k]
                info.update(name=nm, path=path, cmd=cmd, args_rid=cur[0], call_idx=cur[1], link="per posizione")
            if cur and k < len(calls) and not info["cmd"]:
                info["cmd"] = calls[k][2]
            if info["args_rid"]:
                by_args.setdefault(info["args_rid"], []).append(rid)
            k += 1
            by_out[rid] = info
        self._cache[conv] = (n, by_out, by_args)
        return by_out, by_args


def seg_bounds(store, conv: str) -> list[int]:
    with store.lock:
        return [c for c, in store.db.execute("SELECT cut_idx FROM segments WHERE conv=? ORDER BY seg", (conv,))]


def seg_of(cuts: list[int], idx: int) -> int:
    return sum(1 for c in cuts if c <= idx)


ROLE_IT = {"tool": "uscita di strumento", "assistant-reasoning": "ragionamento", "assistant-tool-args":
           "argomenti di chiamata", "user": "utente", "assistant": "risposta", "system": "sistema"}


class Recall2:
    """recall con le opzioni recall_struct / recall_multi / recall_flex."""

    def __init__(self, mgr):
        self.mgr, self.cfg, self.st, self.tc = mgr, mgr.cfg, mgr.store, mgr.tc
        self.pi = PassageIndex(self.st)
        self.sx = Structure(self.st)

    # ------------------------------------------------------------ helpers
    @property
    def rn(self) -> str:
        """Nome dello strumento di recall nella conversazione corrente (per le indicazioni nei risultati)."""
        return getattr(self.mgr, "rn", None) or "recall"

    def _row(self, rid, conv):
        r = self.st.get(rid, conv) or self.st.get(rid)
        return r  # (rid, conv, idx, role, name, content, tokens)

    def head(self, rid, idx, role, tokens, conv, cuts, extra="", strong=None, span=None, total=None):
        by_out, _ = self.sx.calls(conv)
        bits = ["id=%s" % rid, "messaggio %d" % (idx + 1), "segmento %d" % seg_of(cuts, idx), ROLE_IT.get(role, role)]
        info = by_out.get(rid)
        if role == "tool" and info:
            if info["name"]:
                bits.append("strumento %s" % info["name"])
            if info["path"]:
                bits.append("file %s" % info["path"])
            elif info["cmd"]:
                bits.append("comando %s" % _short(info["cmd"], 70))
            if info["args_rid"]:
                bits.append("chiamata id=%s (%s)" % (info["args_rid"], info["link"]))
        elif role == "assistant-tool-args":
            row = self._row(rid, conv)
            names = [ln.split(" ", 1)[0] for ln in (row[5] if row else "").split("\n") if ln.strip()]
            if names:
                bits.append("chiamate %s" % ",".join(names[:6]))
            paths = re.findall(r'"(?:path|file_path)"\s*:\s*"([^"]+)"', row[5] if row else "")
            if paths:
                bits.append("file %s" % ",".join(dict.fromkeys(paths[:4])))
        bits.append("%d token" % (tokens or 0))
        if span and total:
            bits.append("caratteri %d\u2013%d di %d" % (span[0], span[1], total))
        if strong is not None:
            bits.append("forte" if strong else "parziale")
        if extra:
            bits.append(extra)
        return "--- " + " \u00b7 ".join(bits)

    def excerpt(self, content: str, off: int, ln: int, terms: list[str]):
        a = max(0, off - SHOW_PAD)
        b = min(len(content), off + ln + SHOW_PAD)
        if b - a > MAX_SHOW:
            # centra sul primo termine trovato nel passaggio
            low = content[off:off + ln].lower()
            ps = [low.find(t) for t in terms if t and low.find(t) >= 0]
            c = off + (min(ps) if ps else 0)
            a = max(0, c - MAX_SHOW // 3)
            b = min(len(content), a + MAX_SHOW)
        return a, b, content[a:b]

    def fit(self, text, n):
        return self.mgr.fit_tokens(text, n)

    # ------------------------------------------------------------ ricerca
    def _filters(self, args, conv):
        roles = args.get("role")
        if isinstance(roles, str):
            roles = [r.strip() for r in roles.replace("|", ",").split(",") if r.strip()]
        alias = {"ragionamento": "assistant-reasoning", "reasoning": "assistant-reasoning", "argomenti":
                 "assistant-tool-args", "args": "assistant-tool-args", "uscita": "tool", "output": "tool",
                 "utente": "user", "risposta": "assistant"}
        roles = [alias.get(r, r) for r in roles or []] or None
        lo, hi = 0, None
        cuts = seg_bounds(self.st, conv)
        if args.get("seg") is not None and str(args.get("seg")).strip() != "":
            try:
                s = int(args.get("seg"))
                bounds = [0] + cuts
                lo = bounds[s] if s < len(bounds) else 10 ** 9
                hi = (bounds[s + 1] - 1) if s + 1 < len(bounds) else None
            except (TypeError, ValueError):
                pass
        for k, f in (("from", max), ("to", min)):
            v = args.get(k)
            if v not in (None, ""):
                try:
                    v = int(v) - 1
                    if k == "from":
                        lo = max(lo, v)
                    else:
                        hi = v if hi is None else min(hi, v)
                except (TypeError, ValueError):
                    pass
        return roles, lo, hi, cuts

    def search_one(self, conv, query, max_idx, roles, lo, hi):
        """-> lista ordinata di dict {rid, idx, role, off, len, score, strong} (miglior passaggio per pezzo)."""
        flex = bool(getattr(self.cfg, "recall_flex", False))
        self.pi.ensure(conv, flex)
        strict, wide, terms = fts_queries(query, flex)
        res = {}
        for pid, rid, idx, role, off, ln, s in self.pi.search(conv, wide, flex, max_idx, roles, lo, hi):
            if rid not in res or s > res[rid]["score"]:
                res[rid] = dict(rid=rid, idx=idx, role=role, off=off, len=ln, score=s, strong=False)
        if strict and strict != wide:
            for pid, rid, idx, role, off, ln, s in self.pi.search(conv, strict, flex, max_idx, roles, lo, hi):
                r = res.get(rid)
                if r is None or not r["strong"] or s > r["score"]:
                    res[rid] = dict(rid=rid, idx=idx, role=role, off=off, len=ln, score=s, strong=True)
        elif strict:
            for r in res.values():
                r["strong"] = True
        # frase esatta (percorsi, `chiave: valore`, simboli): sottostringa nel testo originale
        q = query.strip()
        if len(q) >= 3 and re.search(r"[^\w\s]", q):
            for rid, idx, role, name, content, tokens in self.st.search(conv, q, limit=40, max_idx=max_idx):
                if (roles and role not in roles) or idx < lo or (hi is not None and idx > hi):
                    continue
                p = content.lower().find(q.lower())
                r = res.get(rid)
                off = max(0, p - WIN // 3)
                if r is None:
                    res[rid] = dict(rid=rid, idx=idx, role=role, off=off, len=min(WIN, len(content) - off), score=0.0,
                                    strong=True)
                else:
                    r.update(strong=True, off=off, len=min(WIN, len(content) - off))
        out = sorted(res.values(), key=lambda r: (not r["strong"], -r["score"], r["idx"]))
        return out, terms

    def search_many(self, conv, queries, max_idx, roles, lo, hi):
        """Più varianti: fusione per rango (RRF, k=30) con priorità ai risultati forti; deduplica per pezzo."""
        lists, allterms, stats = [], [], []
        for q in queries:
            lst, terms = self.search_one(conv, q, max_idx, roles, lo, hi)
            lists.append(lst)
            stats.append((q, len(lst), sum(r["strong"] for r in lst)))
            allterms += [t for t in terms if t not in allterms]
        if len(lists) == 1:
            return lists[0], allterms, stats
        fused = {}
        for lst in lists:
            for rank, r in enumerate(lst):
                f = fused.get(r["rid"])
                sc = 1.0 / (30 + rank)
                if f is None:
                    fused[r["rid"]] = dict(r, rrf=sc)
                else:
                    f["rrf"] += sc
                    if r["strong"] and not f["strong"]:
                        f.update(off=r["off"], len=r["len"], strong=True)
        out = sorted(fused.values(), key=lambda r: (not r["strong"], -r["rrf"], r["idx"]))
        return out, allterms, stats

    # ------------------------------------------------------------ entrata
    def recall(self, conv, args, max_idx=None, meta=None) -> str:
        max_idx = 10 ** 9 if max_idx is None else max_idx
        mode = str(args.get("mode") or "").strip().lower()
        rid = str(args.get("id") or "").strip().removeprefix("recall:").removeprefix("id=")
        path = str(args.get("path") or "").strip()
        if mode == "tree":
            return self.tree(conv, args, max_idx)
        if rid and mode in ("neighbors", "vicini", "output", "uscita", "reasoning", "ragionamento", "call",
                            "chiamata"):
            return self.around(conv, rid, mode, args, max_idx, meta)
        if rid:
            return self.mgr.recall_legacy(conv, {"id": rid, "offset": args.get("offset")}, max_idx, meta,
                                          head_fn=self._id_head)
        if path:
            return self.file(conv, path, mode or "timeline", args, max_idx, meta)
        qs = args.get("queries")
        if isinstance(qs, str):
            qs = [qs]
        qs = [str(x).strip() for x in (qs or []) if str(x).strip()] if getattr(self.cfg, "recall_multi", False) \
            else []
        if str(args.get("query") or "").strip():
            qs = [str(args.get("query")).strip()] + [x for x in qs if x != str(args.get("query")).strip()]
        qs = qs[:6]
        if not qs:
            return ("recall: serve id, path, query%s oppure mode=tree" %
                    (" o queries" if getattr(self.cfg, "recall_multi", False) else ""))
        roles, lo, hi, cuts = self._filters(args, conv)
        hits, terms, stats = self.search_many(conv, qs, max_idx, roles, lo, hi)
        if not hits:
            return self.empty(conv, qs, terms, max_idx)
        order = str(args.get("order") or "").lower()
        if order in ("tempo", "time"):
            # le occorrenze più vecchie prima (prime versioni, primi esiti): forti in ordine di messaggio, poi parziali
            hits = sorted(hits, key=lambda h: (not h["strong"], h["idx"]))
        return self.render(conv, hits, qs, terms, stats, cuts, meta, by_time=order in ("tempo", "time"))

    def _id_head(self, r, off, chunk_len, total):
        conv = r[1]
        cuts = seg_bounds(self.st, conv)
        return self.head(r[0], r[2], r[3], r[6], conv, cuts, span=(off, off + chunk_len), total=total) + "\n"

    def render(self, conv, hits, qs, terms, stats, cuts, meta, by_time=False, limit=8):
        n_strong = sum(h["strong"] for h in hits)
        if len(qs) > 1:
            top = "[recall: %d risultati per %d varianti (%s); %d forti]" % (
                len(hits), len(qs), "; ".join("%r: %d" % (q, n) for q, n, _ in stats), n_strong)
        else:
            top = "[recall: %d risultati per %r; %d forti (tutte le parole nello stesso passaggio)]" % (
                len(hits), qs[0], n_strong)
        if not n_strong:
            top += ("\n[nessun pezzo contiene tutte le parole: risultati PARZIALI, verificali prima di usarli; "
                    "prova parole diverse o mode=timeline sul file]")
        budget = int(getattr(self.cfg, "recall_struct_max_tokens", 3500))
        out, used = [top], self.tc.count(top)
        shown, seen_txt, dups = [], {}, {}
        for h in hits:
            if len(shown) >= limit:
                break
            r = self._row(h["rid"], conv)
            if not r:
                continue
            content = r[5] or ""
            a, b, s = self.excerpt(content, h["off"], h["len"], terms)
            key = hashlib.sha1(s.strip().encode("utf-8", "replace")).hexdigest()
            if key in seen_txt:
                dups.setdefault(seen_txt[key], []).append(r[2] + 1)
                continue
            seen_txt[key] = h["rid"]
            shown.append((h, r, a, b, s))
        if by_time:
            shown.sort(key=lambda x: x[1][2])
        for rank, (h, r, a, b, s) in enumerate(shown):
            extra = ""
            if dups.get(h["rid"]):
                extra = "stesso testo anche nei messaggi %s" % ",".join(map(str, dups[h["rid"]][:6]))
            block = "\n%s\n%s" % (self.head(r[0], r[2], r[3], r[6], conv, cuts, extra, h["strong"], (a, b),
                                            len(r[5] or "")), s)
            t = self.tc.count(block)
            if used + t > budget:
                if rank < 2 and budget - used > 200:
                    block, t = self.fit(block, budget - used), budget - used
                else:
                    out.append("\n[altri %d risultati non mostrati: affina la query o usa i filtri role/seg/from/to]"
                               % (len(shown) - rank))
                    break
            out.append(block)
            used += t
            if meta is not None:
                meta.append({"rid": r[0], "idx": r[2], "rank": rank, "tokens": r[6], "chars": b - a,
                             "strong": h["strong"]})
        if len(shown) > 1 and not by_time:
            out.append("\n[in ordine di messaggio: %s]" % ", ".join(
                "%d%s" % (x[1][2] + 1, "" if x[0]["strong"] else "?") for x in sorted(shown, key=lambda x: x[1][2])))
        return "\n".join(out)

    def empty(self, conv, qs, terms, max_idx) -> str:
        flex = bool(getattr(self.cfg, "recall_flex", False))
        with_hits, without = [], []
        for t in terms[:12]:
            n = len(self.pi.search(conv, _q(t), flex, max_idx, limit=50))
            (with_hits if n else without).append("%s(%d)" % (t, n) if n else t)
        files = []
        with self.st.lock:
            for p, in self.st.db.execute("SELECT path FROM fileops WHERE conv=? AND idx<? AND op IN ('write','edit') "
                                         "GROUP BY path ORDER BY count(*) DESC LIMIT 6", (conv, max_idx)):
                files.append(p.rsplit("/", 2)[-2:] and "/".join(p.rsplit("/", 2)[-2:]))
        s = ["recall: nessun risultato per %s." % ", ".join(repr(q) for q in qs), "Suggerimenti:"]
        if with_hits:
            s.append("- parole con risultati da sole: %s; senza: %s" % (", ".join(with_hits),
                                                                         ", ".join(without) or "-"))
        else:
            s.append("- nessuna di queste parole compare: prova sinonimi, l'altra lingua (italiano/inglese) o i nomi "
                     "usati nel codice")
        if files:
            s.append("- file modificati: %s \u2192 %s path=<file> mode=timeline (o mode=first per la "
                     "prima scrittura)" % (", ".join(files), self.rn))
        s.append("- mappa della sessione per turni: %s mode=tree" % self.rn)
        s.append("Se l'informazione non c'è, dillo: non ricostruirla.")
        return "\n".join(s)

    # ------------------------------------------------------------ file
    def file(self, conv, path, mode, args, max_idx, meta) -> str:
        ops = self.st.fileops(conv, path, max_idx=max_idx)
        if not ops:
            return "recall: nessuna operazione registrata sul file %r (prova query=%r)" % (path, path.rsplit("/", 1)[-1])
        cuts = seg_bounds(self.st, conv)
        q = str(args.get("query") or "").strip()
        flex = bool(getattr(self.cfg, "recall_flex", False))
        budget = int(getattr(self.cfg, "recall_struct_max_tokens", 3500))
        writes = [o for o in ops if o[1] in ("write", "edit", "create", "multiedit")]
        if mode in ("first", "prima"):
            first = next((o for o in ops if o[1] in ("write", "create")), writes[0] if writes else ops[0])
            idx, op, p2, esito, ra, ro, hd = first
            rarg = self._row(ra, conv)
            if rarg:
                idx = rarg[2]            # il registro file usa l'indice dell'uscita: il turno è quello degli argomenti
            out = ["[recall: PRIMA scrittura di %r: messaggio %d (segmento %d), %s, esito %s. Collegamenti: argomenti "
                   "id=%s e uscita id=%s (registrati, tool_call_id); ragionamento dello stesso messaggio id=%s "
                   "(stesso turno: ciò che è SCRITTO negli argomenti vale più di ciò che è CONSIDERATO nel "
                   "ragionamento)]" % (p2, idx + 1, seg_of(cuts, idx), op, esito, ra, ro, "t?")]
            # ragionamento dello stesso messaggio
            with self.st.lock:
                tr = self.st.db.execute("SELECT rid FROM archive WHERE conv=? AND idx=? AND role='assistant-reasoning'",
                                        (conv, idx)).fetchone()
            out[0] = out[0].replace("id=t?", "id=%s" % tr[0] if tr else "(nessuno)")
            used = self.tc.count(out[0])
            for rid, role in ((ra, "assistant-tool-args"), (tr[0] if tr else None, "assistant-reasoning")):
                if not rid:
                    continue
                r = self._row(rid, conv)
                if not r:
                    continue
                content = r[5] or ""
                spans = []
                if q:
                    self.pi.ensure(conv, flex)
                    strict, wide, terms = fts_queries(q, flex)
                    found = self.pi.search(conv, strict or wide, flex, idx + 1, lo=idx) or \
                        self.pi.search(conv, wide, flex, idx + 1, lo=idx)
                    spans = [(off, ln) for pid, r2, i2, ro2, off, ln, s in found if r2 == rid][:3]
                    spans.sort()
                    if not spans:
                        continue
                else:
                    terms = []
                    if role == "assistant-tool-args":
                        p0 = content.find('"%s"' % p2)
                        p0 = content.rfind("\n", 0, p0) + 1 if p0 > 0 else 0
                        spans = [(p0, min(len(content) - p0, 4000))]
                    else:
                        stem_name = p2.rsplit("/", 1)[-1].split(".")[0].lower()
                        low = content.lower()
                        hits = [m.start() for m in re.finditer(re.escape(stem_name), low)][:2] if stem_name else []
                        spans = [(max(0, h - 300), 700) for h in hits] or [(0, min(len(content), 900))]
                for off, ln in spans:
                    a, b, s = self.excerpt(content, off, ln, terms) if q else (off, off + ln, content[off:off + ln])
                    block = "\n%s\n%s" % (self.head(rid, r[2], r[3], r[6], conv, cuts, span=(a, b),
                                                    total=len(content)), s)
                    t = self.tc.count(block)
                    if used + t > budget:
                        block, t = self.fit(block, max(200, budget - used)), max(200, budget - used)
                    out.append(block)
                    used += t
                    if meta is not None:
                        meta.append({"rid": rid, "idx": r[2], "rank": len(meta), "tokens": r[6], "chars": b - a})
                    if used >= budget:
                        break
            if q and len(out) == 1:
                out.append("\n(nessun passaggio con %r nella prima scrittura né nel suo ragionamento: usa "
                           "mode=timeline con query)" % q)
            return "\n".join(out)
        # timeline
        out = ["[recall: cronologia di %r: %d operazioni (%d scritture/modifiche), in ordine; collegamenti "
               "argomenti/uscita registrati (tool_call_id). Per lo stato attuale rileggi il file]" % (
                   path, len(ops), len(writes))]
        terms = []
        hits_by_rid = {}
        if q:
            self.pi.ensure(conv, flex)
            strict, wide, terms = fts_queries(q, flex)
            rids = {o[4] for o in ops} | {o[5] for o in ops}
            for expr in (wide,):
                for pid, r2, i2, ro2, off, ln, s in self.pi.search(conv, expr, flex, max_idx, limit=5000):
                    if r2 in rids:
                        hits_by_rid.setdefault(r2, []).append((off, ln))
        qwords = [w.lower() for w in _WORD.findall(q)] if q else []
        used = self.tc.count(out[0])
        first_w = next((o[0] for o in ops if o[1] in ("write", "create")), None)
        skipped = 0
        for k, (idx, op, p2, esito, ra, ro, hd) in enumerate(ops):
            tag = " [PRIMA SCRITTURA]" if idx == first_w and op in ("write", "create") else ""
            hit_rid = ra if ra in hits_by_rid else (ro if ro in hits_by_rid else None)
            if q and not hit_rid and op == "read":
                skipped += 1
                continue
            line = "- messaggio %d (segmento %d): %s%s \u2014 %s \u00b7 argomenti id=%s \u00b7 uscita id=%s" % (
                idx + 1, seg_of(cuts, idx), op, tag, esito, ra, ro)
            if op in ("read",) and not q:
                line += " \u00b7 " + _short(hd, 60)
            snippet = ""
            if hit_rid:
                r = self._row(hit_rid, conv)
                if r:
                    s = best_lines(r[5] or "", hits_by_rid[hit_rid], qwords, terms, 2 if op != "read" else 1)
                    snippet = "\n    \u2502 " + s.replace("\n", "\n    \u2502 ")
                    if meta is not None:
                        meta.append({"rid": hit_rid, "idx": idx, "rank": k, "tokens": r[6], "chars": len(s)})
            elif op in ("edit", "multiedit"):
                r = self._row(ra, conv)
                if r:
                    snippet = "\n    \u2502 " + _short(_edit_summary(r[5] or ""), 260)
            elif q:
                line += " \u00b7 (senza %r)" % q
            if meta is not None and not snippet:
                meta.append({"rid": ro, "idx": idx, "rank": k, "tokens": 0, "path": p2})
            t = self.tc.count(line + snippet)
            if used + t > budget:
                out.append("[altre %d operazioni non mostrate: usa from/to (numeri di messaggio) o query]" %
                           (len(ops) - k))
                break
            out.append(line + snippet)
            used += t
        if skipped:
            out.append("[%d letture senza %r omesse]" % (skipped, q))
        if q and not hits_by_rid:
            out.append("(nessuna operazione su questo file contiene tutte le parole di %r)" % q)
        return "\n".join(out)

    # ------------------------------------------------------------ attorno a un id
    def around(self, conv, rid, mode, args, max_idx, meta) -> str:
        r = self._row(rid, conv)
        if not r:
            return "recall: id %s non trovato" % rid
        conv = r[1]
        cuts = seg_bounds(self.st, conv)
        idx = r[2]
        by_out, by_args = self.sx.calls(conv)
        if mode in ("output", "uscita"):
            outs = by_args.get(rid, [])
            if r[3] == "tool":
                info = by_out.get(rid) or {}
                return ("[recall: l'uscita id=%s viene dalla chiamata id=%s del messaggio %s (%s)] -> %s "
                        "id=%s" % (rid, info.get("args_rid"), (info.get("call_idx") or 0) + 1, info.get("link") or
                                   "non collegata", self.rn, info.get("args_rid")))
            if not outs:
                return "recall: nessuna uscita collegata agli argomenti id=%s" % rid
            lines = ["[recall: uscite delle chiamate id=%s (messaggio %d)]" % (rid, idx + 1)]
            for o in outs:
                ro = self._row(o, conv)
                if ro:
                    info = by_out.get(o) or {}
                    lines.append("%s\n%s" % (self.head(o, ro[2], ro[3], ro[6], conv, cuts), _short(ro[5] or "", 700)))
                    if meta is not None:
                        meta.append({"rid": o, "idx": ro[2], "rank": len(meta), "tokens": ro[6]})
            return self.fit("\n".join(lines), int(getattr(self.cfg, "recall_struct_max_tokens", 3500)))
        if mode in ("reasoning", "ragionamento", "call", "chiamata"):
            want = "assistant-reasoning" if mode in ("reasoning", "ragionamento") else "assistant-tool-args"
            src_idx = idx
            if r[3] == "tool":
                src_idx = (by_out.get(rid) or {}).get("call_idx")
            with self.st.lock:
                tr = self.st.db.execute("SELECT rid, idx, role, content, tokens FROM archive WHERE conv=? AND idx=? "
                                        "AND role=?", (conv, src_idx, want)).fetchone() if src_idx is not None else None
            if not tr:
                return "recall: nessun %s nello stesso messaggio di id=%s" % (ROLE_IT[want], rid)
            if meta is not None:
                meta.append({"rid": tr[0], "idx": tr[1], "rank": 0, "tokens": tr[4]})
            return self.fit("[recall: %s dello stesso messaggio %d (collegamento di protocollo: stesso turno)]\n%s\n%s"
                            % (ROLE_IT[want], tr[1] + 1, self.head(tr[0], tr[1], tr[2], tr[4], conv, cuts), tr[3]),
                            int(getattr(self.cfg, "recall_struct_max_tokens", 3500)))
        # vicini
        try:
            n = max(1, min(6, int(args.get("n") or 2)))
        except (TypeError, ValueError):
            n = 2
        with self.st.lock:
            rows = self.st.db.execute("SELECT rid, idx, role, content, tokens FROM archive WHERE conv=? AND idx BETWEEN "
                                      "? AND ? AND idx < ? ORDER BY idx, CASE role WHEN 'assistant-reasoning' THEN 0 "
                                      "WHEN 'assistant' THEN 1 WHEN 'assistant-tool-args' THEN 2 ELSE 3 END",
                                      (conv, idx - n, idx + n, max_idx)).fetchall()
        lines = ["[recall: messaggi vicini a id=%s (messaggio %d, \u00b1%d): adiacenza, NON un collegamento causale]"
                 % (rid, idx + 1, n)]
        for rr, i2, role, content, tok in rows:
            mark = " \u25c0" if rr == rid else ""
            lines.append("- %s%s: %s" % (self.head(rr, i2, role, tok, conv, cuts)[4:], mark,
                                         _short(content or "", 220 if rr != rid else 500)))
            if meta is not None:
                meta.append({"rid": rr, "idx": i2, "rank": len(meta), "tokens": tok})
        return self.fit("\n".join(lines), int(getattr(self.cfg, "recall_struct_max_tokens", 3500)))

    # ------------------------------------------------------------ indice ad albero
    def tree(self, conv, args, max_idx) -> str:
        """segmento -> turni (da un messaggio dell'utente al successivo): messaggio dell'utente, file toccati,
        comandi ed esiti. Solo dati, nessun riassunto."""
        cuts = seg_bounds(self.st, conv)
        roles_lo, lo, hi, _ = self._filters(args, conv)
        hi = max_idx - 1 if hi is None else min(hi, max_idx - 1)
        with self.st.lock:
            users = self.st.db.execute("SELECT idx, content FROM archive WHERE conv=? AND role='user' AND idx BETWEEN ? "
                                       "AND ? ORDER BY idx", (conv, lo, hi)).fetchall()
            fops = self.st.db.execute("SELECT idx, op, path, outcome FROM fileops WHERE conv=? AND idx BETWEEN ? AND ? "
                                      "ORDER BY idx", (conv, lo, hi)).fetchall()
        by_out, _ = self.sx.calls(conv)
        with self.st.lock:
            tools = self.st.db.execute("SELECT rid, idx, content FROM archive WHERE conv=? AND role='tool' AND idx "
                                       "BETWEEN ? AND ? ORDER BY idx", (conv, lo, hi)).fetchall()
        human = [(i, c) for i, c in users if not (c or "").lstrip().startswith("<background-task")]
        starts = [i for i, _ in human] or [lo]
        if starts[0] > lo:
            starts = [lo] + starts
        out = ["[recall: mappa della sessione per segmento e turno (messaggi %d\u2013%d); dati dell'archivio, nessun "
               "riassunto. Dettagli: %s from=<msg> to=<msg> query=..., o path=<file> mode=timeline]"
               % (lo + 1, hi + 1, self.rn)]
        cur_seg = None
        from .core import outcome
        for k, s in enumerate(starts):
            e = (starts[k + 1] - 1) if k + 1 < len(starts) else hi
            sg = seg_of(cuts, s)
            if sg != cur_seg:
                out.append("segmento %d" % sg)
                cur_seg = sg
            utext = next((c for i, c in human if i == s), "")
            files = {}
            for i, op, p, oc in fops:
                if s <= i <= e and op != "read":
                    key = p.rsplit("/", 2)[-2:]
                    f = files.setdefault("/".join(key), [0, 0])
                    f[0] += 1
                    f[1] += oc == "fallito"
            ncmd = nfail = 0
            fails = []
            for rid, i, content in tools:
                if s <= i <= e:
                    info = by_out.get(rid) or {}
                    if info.get("name") == "bash":
                        ncmd += 1
                        if outcome("bash", content or "") == "fallito":
                            nfail += 1
                            if len(fails) < 2:
                                fails.append(i + 1)
            line = "  turno msg %d\u2013%d" % (s + 1, e + 1)
            if utext:
                line += " \u00b7 utente: %s" % _short(utext, 90)
            if files:
                line += " \u00b7 file: %s" % ", ".join("%s\u00d7%d%s" % (p, n, " (%d falliti)" % f if f else "")
                                                      for p, (n, f) in list(files.items())[:6])
            if ncmd:
                line += " \u00b7 comandi %d (falliti %d%s)" % (ncmd, nfail, (": msg " + ",".join(map(str, fails)))
                                                                if fails else "")
            out.append(line)
        return self.fit("\n".join(out), int(getattr(self.cfg, "recall_struct_max_tokens", 3500)))


def best_lines(content: str, spans, qwords, terms, k=2, width=230) -> str:
    """Le k righe (a-capo veri o \\n letterali dei JSON) con più parole della domanda, dentro i passaggi trovati;
    ciascuna con un po' di contesto. Le parole contano sia intere (minuscole) sia come radici normalizzate."""
    cands = []
    wres = [re.compile(r"(?<![A-Za-z0-9_])%s(?![A-Za-z0-9_])" % re.escape(w)) for w in qwords]
    for off, ln in spans:
        seg = content[off:off + ln + 300]
        parts = re.split(r"(\n|\\n)", seg)
        lines, pos = [], 0
        for part in parts:
            if part not in ("\n", "\\n"):
                lines.append((pos, part))
            pos += len(part)
        for j, (p0, line) in enumerate(lines):
            win = " ".join(x for _, x in lines[j:j + 3])
            low = win.lower()
            sc = sum(2 for w in wres if w.search(low))
            if not sc:
                continue
            ntok = set(t for g in norm_tokens(win) for t in g)
            sc += sum(1 for t in terms if t in ntok)
            # preferisci le finestre che INIZIANO con una parola cercata
            sc += 0.5 * any(w.search(line.lower()) for w in wres)
            span_len = sum(len(x) + 1 for _, x in lines[j:j + 3])
            cands.append((sc, off + p0, span_len))
    if not cands:
        off, ln = spans[0]
        return _short(content[off:off + ln], width * 2)
    cands.sort(key=lambda c: (-c[0], c[1]))
    picked = []
    for sc, p, ln in cands:
        if any(abs(p - q) < width for q, _ in picked):
            continue
        picked.append((p, ln))
        if len(picked) >= k:
            break
    out = []
    for p, ln in sorted(picked):
        a = max(0, p - 20)
        b = min(len(content), p + min(ln, width + 120))
        out.append(_short(content[a:b].replace("\\n", "\n").replace('\\"', '"'), width + 120))
    return "\n".join(out)


def _short(s: str, n: int) -> str:
    s = re.sub(r"\s+", " ", s or "").strip()
    return s if len(s) <= n else s[:n - 1] + "\u2026"


def _edit_summary(args_text: str) -> str:
    """Prima riga di newText delle modifiche (per la cronologia)."""
    m = re.findall(r'"newText"\s*:\s*"((?:[^"\\]|\\.){0,200})', args_text)
    if m:
        return " | ".join(x.replace("\\n", " ").replace('\\"', '"') for x in m[:3])
    return args_text[:200]


def recall_tool(cfg, base: dict) -> dict:
    """Definizione dello strumento: quella di serie, o quella estesa se recall_struct/recall_multi sono attivi."""
    if not (getattr(cfg, "recall_struct", False) or getattr(cfg, "recall_multi", False)):
        return base
    f = dict(base["function"])
    props = dict(f["parameters"]["properties"])
    desc = f["description"]
    if getattr(cfg, "recall_multi", False):
        props["queries"] = {"type": "array", "items": {"type": "string"},
                            "description": "più varianti della ricerca in UNA chiamata (sinonimi, italiano/inglese, "
                                           "nomi nel codice): risultati fusi e senza doppioni"}
    if getattr(cfg, "recall_struct", False):
        props["mode"] = {"type": "string", "enum": ["timeline", "first", "neighbors", "output", "reasoning", "tree"],
                         "description": "con path: timeline (cronologia del file) o first (prima scrittura + "
                                        "ragionamento dello stesso turno); con id: neighbors (messaggi vicini), output "
                                        "(uscita di una chiamata), reasoning (ragionamento dello stesso turno); tree: "
                                        "mappa della sessione per turni"}
        props["order"] = {"type": "string", "enum": ["pertinenza", "tempo"],
                          "description": "tempo: le occorrenze più VECCHIE prima (prima versione, primo esito)"}
        props["role"] = {"type": "string", "description": "filtro: user, assistant, reasoning, args, tool"}
        props["seg"] = {"type": "integer", "description": "filtro: numero di segmento"}
        props["from"] = {"type": "integer", "description": "filtro: dal messaggio numero"}
        props["to"] = {"type": "integer", "description": "filtro: fino al messaggio numero"}
        props["n"] = {"type": "integer", "description": "con mode=neighbors: quanti messaggi prima e dopo"}
        desc += (" Per la PRIMA versione di qualcosa usa path=<file> mode=first (o mode=timeline con query) e cita "
                 "id e messaggio; distingui i valori SCRITTI (argomenti/uscite) da quelli solo CONSIDERATI nel "
                 "ragionamento. I risultati 'forte' contengono tutte le parole cercate; 'parziale' no.")
    f["parameters"] = dict(f["parameters"], properties=props)
    f["description"] = desc
    return {"type": "function", "function": f}
