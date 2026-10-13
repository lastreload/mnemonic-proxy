"""Selettore a sotto-blocco foglia per recall (opzione recall_leaf_select, spenta di serie). Solo libreria standard.

Problema: in un output strutturato con molti record simili (yaml/toml/xml/json/tabelle) il recall consegna
una pagina con decine di record dello stesso tipo e il modello copia il valore del record accanto ("gemello"), oppure la
pagina non arriva al record giusto. Qui il testo originale viene diviso in SOTTO-BLOCCHI FOGLIA (il più piccolo gruppo
di righe che forma un record: chiave + figli scalari, sezione toml, elemento xml con figli semplici, oggetto json con
membri scalari, riga di tabella) e per ognuno si conosce il percorso degli antenati (le sole righe-chiave, senza
fratelli). La domanda (query esplicita e/o ultima domanda dell'utente) sceglie le foglie con BM25 su foglia + percorso
(+ identificatori esatti come `orders-db`, `T-0447`); si consegnano le righe-chiave degli antenati + le foglie, verbatim
dall'originale, nell'ordine dell'originale, con '⋮' per le righe saltate, e una riga finale che dice come leggere il
resto (id, offset, intervallo).

Regole di prudenza:
- parse incerto (formato non riconosciuto, parse di controllo fallito, righe non conformi) -> None = comportamento
  attuale del proxy;
- più foglie solo se la domanda non le distingue: una foglia entra se le parole della domanda che contiene includono
  tutte quelle della migliore (domanda plurale o ambigua); più di MAX_BLOCKS foglie equivalenti -> None;
- nessuna parola discriminante trovata -> None;
- l'estratto (intestazione e coda comprese) deve stare nel budget in token; altrimenti si tolgono le foglie meno
  pertinenti con un avviso, e se non entra neanche la migliore -> None.
"""
from __future__ import annotations

import csv
import io
import json
import math
import re

MAX_BLOCKS = 10
GAP = "\u22ee"

_STOP = set("""
il lo la i gli le un uno una di del dello della dei degli delle da dal dallo dalla dai dagli dalle in nel nello nella
nei negli nelle su sul sullo sulla sui sugli sulle con per tra fra a al allo alla ai agli alle e ed o che chi cui non
come dove quando quale quali qual quanto quanti è sono era essere ha hanno ho mi ti si ci vi ne questo questa quello
quella dell all nell sull dall eseguire comandi leggere file files
the a an of to in on at by for from with and or is are was were be been what which who whom whose how where when
this that these those it its do does did not no run commands read please tell me give value
""".split())

_FILE_EXT = {"yaml", "yml", "json", "toml", "xml", "csv", "md", "txt", "log", "py", "ini", "conf", "cfg", "tsv",
             "html", "js", "ts", "sh"}


# piccolo lessico esplicito it/en: parole equivalenti contano come lo stesso termine (prima = canone)
_LEX_GROUPS = [
    ("porta", "porte", "port", "ports"), ("replica", "repliche", "replicas"),
    ("primario", "primaria", "primary"), ("riserva", "backup", "standby"),
    ("fallito", "fallita", "falliti", "fallite", "fallimento", "failed", "failing", "failure", "fail"),
    ("saltato", "saltata", "saltati", "skipped"), ("rifiutato", "rifiutata", "rifiutate", "rifiutati", "refused",
                                                   "rejected"),
    ("errore", "errori", "error", "errors"), ("soglia", "threshold"), ("proprietario", "owner"),
    ("nodo", "node"), ("servizio", "servizi", "service", "services"), ("firma", "signature"),
    ("inserimenti", "insertions", "insertion"), ("transazione", "transaction", "txn"),
]
_CANON = {w: g[0] for g in _LEX_GROUPS for w in g}
# rami che si escludono: la domanda che nomina uno e non l'altro penalizza l'altro (canoni)
_OPP = [("primario", "replica"), ("primario", "riserva")]
_OPP_PEN = 0.35
_NEAR = 0.9          # foglia con termini diversi ma punteggio >= 90% della migliore: seconda interpretazione


# ------------------------------------------------------------------------------------------------ parole
def words(text: str) -> list[str]:
    return [_CANON.get(w, w) for w in re.findall(r"[0-9a-zà-öø-ÿ]+", text.lower()) if len(w) >= 2 or w.isdigit()]


def question_terms(text: str) -> tuple[list[str], list[str]]:
    """(parole, identificatori). Identificatori: token con - _ . : / o lettere+cifre (orders-db, T-0447, soglia_bar);
    i nomi di file (servizi.yaml) non contano."""
    idents = []
    for m in re.finditer(r"[A-Za-z0-9][\w.\-:/]*[\w]", text or ""):
        t = m.group(0)
        if len(t) < 3:
            continue
        if not (re.search(r"[-_.:/]", t) or (re.search(r"\d", t) and re.search(r"[A-Za-z]", t))
                or re.search(r"[a-z][A-Z]", t)):           # camelCase (rateCeilingKbps, CrashLoopBackOff)
            continue
        if "." in t and t.rsplit(".", 1)[-1].lower() in _FILE_EXT:
            continue
        if t.lower() not in idents:
            idents.append(t.lower())
    ws = []
    for w in words(text or ""):
        if w in _STOP or w in ws:
            continue
        ws.append(w)
    # le parole dei nomi di file non contano (servizi.yaml -> servizi)
    for m in re.finditer(r"[\w\-]+\.(\w{1,5})\b", text or ""):
        if m.group(1).lower() in _FILE_EXT:
            for w in words(m.group(0)):
                if w in ws and w not in words(re.sub(re.escape(m.group(0)), " ", text or "")):
                    ws.remove(w)
    return ws, idents


def _ident_re(ident: str):
    return re.compile(r"(?<![\w\-.])%s(?![\w\-])" % re.escape(ident), re.I)


# ------------------------------------------------------------------------------------------------ formati
class Unit:
    __slots__ = ("lines", "anc", "kind")

    def __init__(self, lines, anc, kind=""):
        self.lines = sorted(set(lines))
        self.anc = sorted(set(anc) - set(lines))
        self.kind = kind


def _ind(s: str) -> int:
    return len(s) - len(s.lstrip(" "))


def _blank_or_comment(s: str, c="#") -> bool:
    t = s.strip()
    return not t or t.startswith(c)


def parse_yaml(lines: list[str]):
    """Mappe annidate per indentazione (spazi), liste '- '. Niente flow style, ancore, blocchi | >, tab."""
    body = [i for i, l in enumerate(lines) if not _blank_or_comment(l)]
    if len(body) < 4:
        return None
    key_re = re.compile(r"^ *(?:- +)?[^\s:#][^:#]*?:(?: +[^#].*)?$|^ *(?:- +)?[^\s:#][^:#]*?:$")
    item_re = re.compile(r"^ *- +\S")
    ok = 0
    for i in body:
        l = lines[i].rstrip()
        if "\t" in lines[i][:_ind(lines[i]) + 1]:
            return None
        if re.search(r":\s*[|>][-+]?\s*$", l) or re.search(r"(^|\s)[&*]\w", l) or l.strip().startswith(("{", "[")):
            return None
        if key_re.match(l) or item_re.match(l):
            ok += 1
    if ok < 0.95 * len(body):
        return None
    if not any(lines[i].rstrip().endswith(":") for i in body):
        return None
    # albero: ogni riga ha un genitore = la riga precedente con indentazione minore che apre un blocco (finisce con ':'
    # o è un elemento di lista '- chiave:' / '- chiave: valore' con figli)
    eff = {}
    for i in body:
        l = lines[i]
        m = re.match(r"^( *)- +", l)
        eff[i] = (len(m.group(0)) if m else _ind(l))   # indentazione del contenuto (dopo '- ')
    parent = {}
    stack = []    # (ind, riga)
    for i in body:
        ind = _ind(lines[i])
        while stack and stack[-1][0] >= ind:
            # un '- ' alla stessa indentazione della chiave madre è un figlio (stile yaml compatto)
            if stack[-1][0] == ind and re.match(r"^ *- ", lines[i]) and lines[stack[-1][1]].rstrip().endswith(":") \
                    and not re.match(r"^ *- ", lines[stack[-1][1]]):
                break
            stack.pop()
        parent[i] = stack[-1][1] if stack else None
        if lines[i].rstrip().endswith(":") or re.match(r"^ *- +\S", lines[i]):
            stack.append((ind, i))
        # contenuto di un elemento '- k: v': le righe successive più indentate sono suoi figli
    children = {}
    for i in body:
        children.setdefault(parent[i], []).append(i)

    def opens(i):
        return lines[i].rstrip().endswith(":") or (re.match(r"^ *- +\S", lines[i]) and children.get(i))

    def is_scalar(i):
        return not opens(i)

    units = []
    for p in [None] + body:
        if p is not None and not opens(p):
            continue
        sc = [c for c in children.get(p, []) if is_scalar(c)]
        if p is not None and re.match(r"^ *- +\S", lines[p]) and not lines[p].rstrip().endswith(":"):
            sc = [p] + sc              # '- nome: x' apre un record: la riga stessa è un campo
            own = []
        else:
            own = [p] if p is not None else []
        if not sc:
            continue
        anc, q = [], parent.get(p) if p is not None else None
        while q is not None:
            anc.append(q)
            q = parent.get(q)
        units.append(Unit(own + sc, anc, "yaml"))
    return units if len(units) >= 2 else None


def parse_toml(lines: list[str], text: str):
    try:
        import tomllib
    except ImportError:   # pragma: no cover (Python < 3.11)
        return None
    try:
        tomllib.loads(text)
    except Exception:  # noqa: BLE001
        return None
    if '"""' in text or "'''" in text:
        return None
    hdr = re.compile(r"^\s*\[\[?\s*([^\]]+?)\s*\]\]?\s*(#.*)?$")
    kv = re.compile(r"^\s*[\w\-.\"' ]+\s*=\s*\S")
    heads = [i for i, l in enumerate(lines) if hdr.match(l)]
    if len(heads) < 2:
        return None
    units, cur, cur_lines = [], None, []

    def flush():
        if cur_lines or cur is not None:
            sc = [k for k in cur_lines if kv.match(lines[k])]
            if sc:
                own = [cur] if cur is not None else []
                anc = []
                if cur is not None:
                    name = hdr.match(lines[cur]).group(1).strip()
                    parts = name.split(".")
                    for n in range(1, len(parts)):
                        h = ".".join(parts[:n])
                        cand = [k for k in heads if k < cur and hdr.match(lines[k]).group(1).strip() == h]
                        if cand:
                            anc.append(cand[-1])
                units.append(Unit(own + sc, anc, "toml"))

    for i, l in enumerate(lines):
        if hdr.match(l):
            flush()
            cur, cur_lines = i, []
        elif not _blank_or_comment(l):
            if not kv.match(l) and not l.strip().startswith(("]", "\"", "'")) and not re.match(r"^\s*[\w\-.]+,?$", l):
                return None
            cur_lines.append(i)
    flush()
    return units if len(units) >= 2 else None


def parse_xml(lines: list[str], text: str):
    import xml.etree.ElementTree as ET
    try:
        ET.fromstring(re.sub(r"^\s*<\?xml[^>]*\?>", "", text.strip()))
    except Exception:  # noqa: BLE001
        return None
    tag = re.compile(r"<(/?)([A-Za-z_][\w.\-:]*)([^>]*?)(/?)>")
    stack = []           # [tag, riga apertura, figli(list), righe semplici(list), complesso(bool)]
    units = []
    root_seen = False
    for k, l in enumerate(lines):
        s = l.strip()
        if not s or s.startswith(("<?", "<!--")) and s.endswith(("?>", "-->")):
            continue
        toks = list(tag.finditer(l))
        if not toks:
            if stack:          # testo di un elemento su più righe: riga semplice del genitore
                stack[-1][3].append(k)
                continue
            return None
        opens = [t for t in toks if not t.group(1) and not t.group(4)]
        closes = [t for t in toks if t.group(1)]
        selfc = [t for t in toks if t.group(4)]
        if len(opens) == 1 and len(closes) == 1 and opens[0].group(2) == closes[0].group(2) and not selfc:
            # <a>x</a> su una riga: campo del genitore (o record se ha attributi)
            if not stack:
                return None
            if len(re.findall(r"\w+=", opens[0].group(3))) >= 2:
                units.append(Unit([k], [x[1] for x in stack], "xml"))
                stack[-1][4] = True
            else:
                stack[-1][3].append(k)
            continue
        if not opens and not closes and len(selfc) == 1:
            if not stack:
                return None
            if len(re.findall(r"\w+=", selfc[0].group(3))) >= 1:
                units.append(Unit([k], [x[1] for x in stack], "xml"))
                stack[-1][4] = True
            else:
                stack[-1][3].append(k)
            continue
        if len(opens) == 1 and not closes and not selfc:
            if stack:
                stack[-1][4] = True
            elif root_seen:
                return None
            root_seen = True
            stack.append([opens[0].group(2), k, [], [], False])
            continue
        if len(closes) == 1 and not opens and not selfc:
            if not stack or stack[-1][0] != closes[0].group(2):
                return None
            t, a, _, simple, complex_ = stack.pop()
            if simple:
                if not complex_:
                    units.append(Unit(list(range(a, k + 1)), [x[1] for x in stack], "xml"))
                else:
                    units.append(Unit([a] + simple, [x[1] for x in stack], "xml"))
            continue
        return None          # più tag misti su una riga: incerto
    if stack:
        return None
    return units if len(units) >= 2 else None


def parse_json(lines: list[str], text: str):
    try:
        json.loads(text)
    except ValueError:
        return None
    if len(lines) < 4:
        return None
    stack = []      # [riga apertura, righe scalari, complesso]
    units = []
    for k, l in enumerate(lines):
        s = l.strip()
        if not s:
            continue
        o = s.count("{") + s.count("[")
        c = s.count("}") + s.count("]")
        if '"' in s:
            # le parentesi dentro le stringhe non contano
            ss = re.sub(r'"(?:[^"\\]|\\.)*"', '""', s)
            o, c = ss.count("{") + ss.count("["), ss.count("}") + ss.count("]")
        if o == 0 and c == 0:
            if not stack:
                return None
            stack[-1][1].append(k)
            continue
        if o > 0 and o == c:
            # contenitore aperto e chiuso sulla riga: record (oggetto) o campo (lista di scalari)
            if not stack:
                return None
            if "{" in s and ":" in re.sub(r'"(?:[^"\\]|\\.)*"', '""', s.split("{", 1)[1]) or s.count('":') >= 2:
                units.append(Unit([k], _json_anc(stack, lines), "json"))
                stack[-1][2] = True
            else:
                stack[-1][1].append(k)
            continue
        if o == 1 and c == 0:
            if stack:
                stack[-1][2] = True
            stack.append([k, [], False])
            continue
        if c == 1 and o == 0 or (c == 1 and o == 1 and re.match(r"^\s*[}\]]\s*,?\s*[{\[]\s*$", s)):
            if not stack:
                return None
            a, sc, cx = stack.pop()
            if sc:
                if not cx:
                    units.append(Unit(list(range(a, k + 1)), _json_anc(stack, lines), "json"))
                else:
                    units.append(Unit([a] + sc, _json_anc(stack, lines), "json"))
            if o == 1:          # '}, {' : chiude e riapre
                if stack:
                    stack[-1][2] = True
                stack.append([k, [], False])
            continue
        return None
    if stack:
        return None
    return units if len(units) >= 2 else None


_JSON_IDK = re.compile(r'^\s*"(id|name|key|uid|uuid|code|slug|sku|title|[a-z_]*_id|[a-z_]*_name)"\s*:\s*[^\[{\s]')


def _json_anc(stack, lines):
    """Antenati di un record json: righe di apertura dei contenitori + il membro identificante ("id", "name", ...)
    già visto di ciascun oggetto antenato (es. `"id": "acct-77qz"` è fratello di "billing", non suo antenato)."""
    anc = []
    for a, sc, _ in stack:
        anc.append(a)
        idl = next((k for k in sc if _JSON_IDK.match(lines[k])), None)
        if idl is not None:
            anc.append(idl)
    return anc


def parse_mdtab(lines: list[str]):
    sep = [i for i, l in enumerate(lines) if re.match(r"^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?\s*$", l)]
    if not sep:
        return None
    units = []
    for s in sep:
        h = s - 1
        if h < 0 or lines[h].count("|") < 2:
            continue
        ncol = lines[h].strip().strip("|").count("|")
        k = s + 1
        while k < len(lines) and lines[k].strip().startswith("|"):
            if lines[k].strip().strip("|").count("|") != ncol:
                return None
            units.append(Unit([k], [h, s], "mdtab"))
            k += 1
    return units if len(units) >= 2 else None


def parse_coltable(lines: list[str]):
    """Tabelle a colonne allineate (docker ps, kubectl get, ps): intestazione in maiuscolo, colonne separate da >= 2
    spazi, righe con lo stesso numero (o più, per l'ultima colonna) di campi."""
    body = [i for i, l in enumerate(lines) if l.strip()]
    if len(body) < 3:
        return None
    h = body[0]
    cols = re.split(r"\s{2,}", lines[h].strip())
    if len(cols) < 3 or not all(re.fullmatch(r"[A-Z][A-Z0-9 _()/.%-]*", c) for c in cols):
        return None
    units = []
    for k in body[1:]:
        n = len(re.split(r"\s{2,}", lines[k].strip()))
        if n < len(cols) - 1:
            return None
        units.append(Unit([k], [h], "table"))
    return units if len(units) >= 2 else None


def parse_csv(lines: list[str], text: str):
    body = [i for i, l in enumerate(lines) if l.strip()]
    if len(body) < 3:
        return None
    try:
        rows = list(csv.reader(io.StringIO("\n".join(lines[i] for i in body))))
    except csv.Error:
        return None
    if len(rows) != len(body):
        return None              # campi su più righe: incerto
    n = len(rows[0])
    if n < 2 or any(len(r) != n for r in rows):
        return None
    if any(re.fullmatch(r"-?[\d.]+", c.strip()) for c in rows[0]):
        return None              # niente intestazione
    return [Unit([k], [body[0]], "csv") for k in body[1:]]


_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def parse_diff(lines: list[str]):
    """diff unificato (git diff): unità = hunk (da '@@' alla fine dei suoi conteggi), antenati = intestazione del
    file (diff --git, ---, +++). Conteggi del hunk che non tornano o righe estranee -> None."""
    if not any(l.startswith("@@ ") for l in lines) or not any(l.startswith("+++ ") for l in lines):
        return None
    units, hdr, k, n = [], [], 0, len(lines)
    while k < n:
        l = lines[k]
        if l.startswith("diff --git "):
            hdr = [k]
            k += 1
            continue
        if l.startswith(("index ", "new file mode", "deleted file mode", "similarity ", "rename ", "old mode",
                         "new mode", "Binary files")):
            k += 1
            continue
        if l.startswith("--- ") and k + 1 < n and lines[k + 1].startswith("+++ "):
            hdr = [h for h in hdr if lines[h].startswith("diff --git")] + [k, k + 1]
            k += 2
            continue
        m = _HUNK.match(l)
        if m:
            if not hdr:
                return None
            old_n, new_n = int(m.group(2) or 1), int(m.group(4) or 1)
            a, k = k, k + 1
            while k < n and (old_n > 0 or new_n > 0):
                t = lines[k]
                c = t[:1]
                if c == "\\":
                    pass
                elif c == " " or t == "":
                    old_n, new_n = old_n - 1, new_n - 1
                elif c == "-":
                    old_n -= 1
                elif c == "+":
                    new_n -= 1
                else:
                    return None
                k += 1
            if old_n != 0 or new_n != 0:
                return None
            while k < n and lines[k].startswith("\\"):
                k += 1
            units.append(Unit(list(range(a, k)), hdr, "diff"))
            continue
        if not l.strip():
            k += 1
            continue
        return None
    return units if len(units) >= 2 else None


def parse_gitlog(lines: list[str]):
    """git log (anche --stat): unità = un commit intero (dalla riga 'commit <sha>' alla successiva)."""
    starts = [i for i, l in enumerate(lines) if re.match(r"^commit [0-9a-f]{7,40}\b", l)]
    first = next((i for i, l in enumerate(lines) if l.strip()), None)
    if len(starts) < 2 or first != starts[0]:
        return None
    units = []
    for j, a in enumerate(starts):
        b = starts[j + 1] if j + 1 < len(starts) else len(lines)
        while b > a + 1 and not lines[b - 1].strip():
            b -= 1
        units.append(Unit(list(range(a, b)), [], "gitlog"))
    return units


_PYT = re.compile(r"^\S+::\S+.*\b(PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)\b|^(FAILED|ERROR) \S+::")


def parse_pytest(lines: list[str]):
    """rapporto pytest: unità = una riga (esito di un test, riassunto) o un blocco '____ nome ____' della sezione
    dei fallimenti (fino al successivo '____'/'===')."""
    if sum(1 for l in lines if _PYT.search(l)) < 3:
        return None
    units, k, n = [], 0, len(lines)
    while k < n:
        l = lines[k]
        if re.match(r"^_{3,} .+ _{3,}$", l):
            a, k = k, k + 1
            while k < n and not re.match(r"^(_{3,}|={3,})", lines[k]):
                k += 1
            units.append(Unit(list(range(a, k)), [], "pytest"))
            continue
        if l.strip():
            units.append(Unit([k], [], "pytest"))
        k += 1
    return units if len(units) >= 2 else None


def parse_py(lines: list[str], text: str):
    """sorgente Python (ast della libreria standard): unità = funzione/classe di primo livello e metodi (dal primo
    decoratore all'ultima riga); antenato di un metodo = la riga 'class'. Codice non valido -> None."""
    import ast
    if not re.search(r"^(async\s+def|def|class)\s+\w", text, re.M):
        return None
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        return None
    units = []

    def span(node):
        a = min([node.lineno] + [d.lineno for d in getattr(node, "decorator_list", [])]) - 1
        return a, node.end_lineno - 1

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            a, b = span(node)
            units.append(Unit(list(range(a, b + 1)), [], "py"))
        elif isinstance(node, ast.ClassDef):
            a, b = span(node)
            meths = [m for m in node.body if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))]
            if not meths:
                units.append(Unit(list(range(a, b + 1)), [], "py"))
                continue
            first = span(meths[0])[0]
            if first > a + 1:
                units.append(Unit(list(range(a, first)), [], "py"))    # intestazione + attributi della classe
            for m in meths:
                ma, mb = span(m)
                units.append(Unit(list(range(ma, mb + 1)), [node.lineno - 1], "py"))
    return units if len(units) >= 2 else None


_TS = re.compile(r"^\[?(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}|\d{2}:\d{2}:\d{2}|[A-Z][a-z]{2} +\d+ \d{2}:\d{2}:\d{2})")


def parse_log(lines: list[str]):
    """log con marca temporale a inizio riga (>= 80% delle righe non vuote): unità = evento (riga + continuazioni)."""
    body = [i for i, l in enumerate(lines) if l.strip()]
    if len(body) < 4:
        return None
    ts = [i for i in body if _TS.match(lines[i])]
    if len(ts) < 0.8 * len(body) or ts[0] != body[0]:
        return None
    units = []
    for j, a in enumerate(ts):
        b = ts[j + 1] if j + 1 < len(ts) else len(lines)
        units.append(Unit([k for k in range(a, b) if lines[k].strip()], [], "log"))
    return units


def parse(text: str):
    """-> (formato, righe, unità) oppure None se il formato non è riconosciuto con certezza."""
    if not text or len(text) > 2_000_000:
        return None
    lines = text.split("\n")
    s = text.lstrip()
    if s.startswith(("{", "[")):
        u = parse_json(lines, text)
        return ("json", lines, u) if u else None
    if s.startswith("<"):
        u = parse_xml(lines, text)
        return ("xml", lines, u) if u else None
    for name, fn in (("toml", lambda: parse_toml(lines, text)), ("mdtab", lambda: parse_mdtab(lines)),
                     ("table", lambda: parse_coltable(lines)), ("csv", lambda: parse_csv(lines, text)),
                     ("yaml", lambda: parse_yaml(lines)), ("diff", lambda: parse_diff(lines)),
                     ("gitlog", lambda: parse_gitlog(lines)), ("pytest", lambda: parse_pytest(lines)),
                     ("log", lambda: parse_log(lines)), ("py", lambda: parse_py(lines, text))):
        u = fn()
        if u:
            return name, lines, u
    return None


# ------------------------------------------------------------------------------------------------ scelta
def _rank(units, lines, ws, idents):
    """BM25 (k1 1.2, b 0.75) su foglia + antenati; identificatori esatti come termini a peso 2.
    Contraddizioni: se la domanda nomina un ramo (replica) e non il suo opposto (primario), le
    foglie il cui percorso di chiavi (antenati + prima riga) nomina l'opposto valgono _OPP_PEN del punteggio.
    -> lista (punteggio, insieme dei termini trovati, indice unità) ordinata."""
    docs = []
    for u in units:
        t = "\n".join(lines[k] for k in u.anc + u.lines)
        docs.append((t, words(t)))
    N = len(docs)
    avg = sum(len(d[1]) for d in docs) / max(N, 1) or 1.0
    wq = set(ws)
    against = {b for a, b in _OPP if a in wq and b not in wq} | {a for a, b in _OPP if b in wq and a not in wq}
    tf = []
    for t, ws_d in docs:
        c = {}
        for w in ws_d:
            c[w] = c.get(w, 0) + 1
        for idt in idents:
            n = len(_ident_re(idt).findall(t))
            if n:
                c["\x00" + idt] = n
        tf.append(c)
    terms = list(ws) + ["\x00" + i for i in idents]
    df = {q: sum(1 for c in tf if q in c) for q in terms}
    out = []
    for j, c in enumerate(tf):
        sc, got = 0.0, set()
        L = len(docs[j][1])
        for q in terms:
            f = c.get(q, 0)
            if not f:
                continue
            idf = math.log(1 + (N - df[q] + 0.5) / (df[q] + 0.5))
            w = 2.0 if q.startswith("\x00") else 1.0
            sc += w * idf * f * 2.2 / (f + 1.2 * (0.25 + 0.75 * L / avg))
            got.add(q)
        if against and sc > 0:
            u = units[j]
            path = set(words("\n".join(lines[k] for k in u.anc + u.lines[:1])))
            if path & against:
                sc *= _OPP_PEN
        out.append((sc, got, j))
    # copertura congiunta nello stesso record: prima le foglie che contengono più termini della
    # domanda che non stanno in più di metà delle foglie, poi il punteggio BM25
    cov = {q for q in terms if df[q] <= N / 2}
    out.sort(key=lambda x: (-len(x[1] & cov), -x[0], x[2]))
    return out, df, N


def choose(units, lines, text_q: str, strict: bool = False):
    """Foglie scelte per un testo-guida (query o domanda). -> (indici, motivo) ; indici vuoti = nessuna scelta.
    strict (guida = sola domanda dell'utente, recall senza query: la domanda può non essere il
    bisogno del modello): si sceglie solo con corrispondenza forte nella foglia migliore = almeno due termini della
    domanda (di cui uno discriminante), oppure un identificatore esatto o una parola rara (<= 3 foglie)
    discriminanti."""
    ws, idents = question_terms(text_q)
    if not ws and not idents:
        return [], "nessuna parola"
    ranked, df, N = _rank(units, lines, ws, idents)
    best = ranked[0]
    if best[0] <= 0 or not best[1]:
        return [], "nessuna parola trovata"
    # almeno un termine trovato deve distinguere (non presente in quasi tutte le foglie)
    disc = [q for q in best[1] if df[q] <= max(MAX_BLOCKS, N // 4)]
    if not disc:
        return [], "nessuna parola discriminante"
    if strict and not (len(best[1]) >= 2 or any(q.startswith("\x00") or df[q] <= 3 for q in disc)):
        return [], "corrispondenza debole"
    eq = [r for r in ranked if r[0] > 0 and r[1] >= best[1]]
    if len(eq) > MAX_BLOCKS:
        return [], "%d blocchi equivalenti" % len(eq)
    # due interpretazioni plausibili: un'altra foglia con termini DIVERSI (non un sottoinsieme
    # di quelli della migliore), la stessa copertura e punteggio quasi pari si mostra anch'essa, con i suoi antenati
    cov = {q for q in df if df[q] <= N / 2}
    for r in ranked[1:]:
        if len(eq) >= MAX_BLOCKS or len(r[1] & cov) < len(best[1] & cov):
            break
        if r not in eq and r[0] >= _NEAR * best[0] and not r[1] <= best[1]:
            eq.append(r)
    return [r[2] for r in eq], ""


def select(text: str, question: str = "", query: str = ""):
    """-> dict(fmt, lines, units, picked=[indici in ordine di pertinenza], guides=[...]) oppure None."""
    p = parse(text)
    if p is None:
        return None
    fmt, lines, units = p
    query, question = (query or "").strip(), (question or "").strip()
    notes = []
    # 1) query esplicita che individua UN blocco; 2) query + domanda insieme (la domanda distingue fra i blocchi che
    # la query non separa, es. lo stesso id in due sezioni); 3) query da sola con più blocchi; senza query: domanda.
    tries = []
    if query:
        ixq, why = choose(units, lines, query)
        if why:
            notes.append(("query", why))
        if len(ixq) == 1:
            ixs = choose(units, lines, question, strict=True)[0] if question else []
            if ixs and ixq[0] not in ixs and len(ixs) <= 2:
                # query e domanda indicano blocchi diversi (es. query 'AssertionError' -> riga E, domanda 'quale test
                # è fallito' -> riga FAILED): due interpretazioni plausibili, si mostrano entrambe
                tries.append((ixq + ixs, ["query", "question"]))
                notes.append(("query/question", "blocchi diversi: entrambi"))
            tries.append((ixq, ["query"]))
        if question:
            ixb, why = choose(units, lines, query + " \n " + question)
            if why:
                notes.append(("query+question", why))
            if ixb:
                tries.append((ixb, ["query", "question"]))
        if ixq:
            tries.append((ixq, ["query"]))
    elif question:
        ix, why = choose(units, lines, question, strict=True)
        if why:
            notes.append(("question", why))
        if ix:
            tries.append((ix, ["question"]))
    if not tries:
        return None
    picked, guides = tries[0]
    return dict(fmt=fmt, lines=lines, units=units, picked=picked[:MAX_BLOCKS], guides=guides, notes=notes)


# ------------------------------------------------------------------------------------------------ testo
def _keep_text(lines, keep):
    out, prev = [], None
    for k in keep:
        if prev is not None and k != prev + 1:
            out.append(GAP)
        out.append(lines[k])
        prev = k
    return "\n".join(out)


def _short(s, n):
    s = re.sub(r"\s+", " ", s or "").strip()
    return s if len(s) <= n else s[:n - 1] + "\u2026"


def render(sel, text: str, rid: str, where: str, count, budget: int | None, rn: str = "recall", lang: str = "it",
           question: str = "", query: str = ""):
    """Estratto pronto per il modello, entro budget token (count(testo) -> int). None se non entra la foglia migliore.
    where: descrizione del pezzo (es. 'messaggio 8, uscita di strumento bash')."""
    en = lang != "it"
    lines, units = sel["lines"], sel["units"]
    starts, pos = [], 0
    for l in lines:
        starts.append(pos)
        pos += len(l) + 1
    picked = list(sel["picked"])
    g = []
    if "query" in sel["guides"]:
        g.append(("query %r" % query) if en else ("query %r" % query))
    if "question" in sel["guides"]:
        g.append(("last user question \u00ab%s\u00bb" if en else "ultima domanda dell'utente \u00ab%s\u00bb")
                 % _short(question, 140))
    fmt = sel["fmt"]

    def build(ix, dropped, level=0):
        keep = sorted({k for i in ix for k in units[i].anc + units[i].lines})
        if level >= 1:
            return build_compact(ix, dropped, keep, level), keep
        if en:
            head = ("[recall:%s \u2014 %s: EXCERPT chosen by %s \u2014 %d block%s of %d (%s), whole lines verbatim from "
                    "the original with only the key lines of their parents; '%s' = lines skipped]") % (
                rid, where, " and ".join(g), len(ix), "" if len(ix) == 1 else "s", len(units), fmt, GAP)
        else:
            head = ("[recall:%s \u2014 %s: ESTRATTO scelto da %s \u2014 %d blocc%s su %d (%s), righe intere verbatim "
                    "dall'originale con le sole righe-chiave dei genitori; '%s' = righe saltate]") % (
                rid, where, " e ".join(g), len(ix), "o" if len(ix) == 1 else "hi", len(units), fmt, GAP)
        blocks = sorted(ix, key=lambda i: units[i].lines[0])
        loc = ", ".join("%s %d offset=%d" % ("line" if en else "riga", units[i].lines[0] + 1, starts[units[i].lines[0]])
                        for i in blocks)
        # parti omesse: intervalli di righe con l'offset per leggerle
        gaps, prev = [], -1
        for k in keep + [len(lines)]:
            if k > prev + 1:
                a0, b0 = prev + 1, k - 1
                while b0 >= a0 and b0 == len(lines) - 1 and not lines[b0]:
                    b0 -= 1             # riga vuota finale del testo: non è una parte omessa
                if b0 >= a0:
                    gaps.append((a0, b0))
            prev = k
        om = ", ".join("%d\u2013%d offset=%d" % (a0 + 1, b0 + 1, starts[a0]) for a0, b0 in gaps[:6])
        if len(gaps) > 6:
            om += ", \u2026"
        if en:
            tail = ("[whole text: %s id=%s offset=0 (lines 1\u2013%d, %d characters); blocks above: %s%s; omitted "
                    "lines: %s]") % (rn, rid, len(lines), len(text), loc,
                                     ("; %d more equivalent blocks not shown for space" % dropped) if dropped else "",
                                     om or "none")
        else:
            tail = ("[testo intero: %s id=%s offset=0 (righe 1\u2013%d, %d caratteri); blocchi qui sopra: %s%s; righe "
                    "omesse: %s]") % (rn, rid, len(lines), len(text), loc,
                                      ("; altri %d blocchi equivalenti non mostrati per spazio" % dropped) if dropped
                                      else "", om or "nessuna")
        return head + "\n" + _keep_text(lines, keep) + "\n" + tail, keep

    def build_compact(ix, dropped, keep, level):
        # intestazione/coda corte (stesse righe verbatim) quando l'estratto pieno non entra nel budget per
        # pochi token; prima si accorcia la cornice, solo dopo si tolgono blocchi. Senza questo il proxy tornava al
        # recall di serie, la cui pagina può finire a metà riga (fedeltà).
        nb = len(ix)
        if en:
            head = "[recall:%s \u2014 %s: EXCERPT (%d block%s of %d, %s), verbatim lines; '%s' = skipped]" % (
                rid, where if level == 1 else "", nb, "" if nb == 1 else "s", len(units), fmt, GAP)
            tail = "[whole text: %s id=%s offset=0 (%d lines)%s]" % (
                rn, rid, len(lines), ("; %d more blocks not shown" % dropped) if dropped else "")
        else:
            head = "[recall:%s \u2014 %s: ESTRATTO (%d blocc%s su %d, %s), righe verbatim; '%s' = saltate]" % (
                rid, where if level == 1 else "", nb, "o" if nb == 1 else "hi", len(units), fmt, GAP)
            tail = "[testo intero: %s id=%s offset=0 (%d righe)%s]" % (
                rn, rid, len(lines), ("; altri %d blocchi non mostrati" % dropped) if dropped else "")
        if level >= 2:
            head = head.replace(" \u2014 : ", ": ")
        return head + "\n" + _keep_text(lines, keep) + "\n" + tail

    ix = picked
    while ix:
        for level in (0, 1, 2):
            out, keep = build(ix, len(picked) - len(ix), level)
            if budget is None or count(out) <= budget:
                return out, keep, ix
        ix = ix[:-1]
    return None


def faithful(text: str, keep) -> bool:
    """Controllo automatico: ogni riga consegnata == originale[a:b] con a = offset in caratteri
    Python della riga, b = a + lunghezza (Unicode e '\\r' dei CRLF compresi, nessuna normalizzazione)."""
    lines = text.split("\n")
    starts, pos = [], 0
    for l in lines:
        starts.append(pos)
        pos += len(l) + 1
    return all(0 <= k < len(lines) and text[starts[k]:starts[k] + len(lines[k])] == lines[k]
               and "\n" not in lines[k] for k in keep)


def leaf_recall(text: str, question: str, query: str, rid: str, where: str, count, budget, rn="recall", lang="it"):
    """Entrata unica per il proxy. -> (testo, info) oppure (None, motivo)."""
    sel = select(text, question=question, query=query)
    if sel is None:
        return None, "nessuna scelta"
    r = render(sel, text, rid, where, count, budget, rn=rn, lang=lang, question=question, query=query)
    if r is None:
        return None, "oltre il budget"
    out, keep, ix = r
    if not faithful(text, keep):
        return None, "fedeltà"
    return out, dict(fmt=sel["fmt"], blocks=len(ix), units=len(sel["units"]), guides=sel["guides"],
                     lines=[k + 1 for k in keep], notes=sel["notes"])
