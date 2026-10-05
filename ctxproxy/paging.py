# Author: Maurizio Verde — LastReload
"""Strumenti su richiesta, ricevute tipizzate, query del richiamo automatico (card t_00c5aef6, PAGING-RESULT.md).

Tutto qui è DETERMINISTICO: stesso ingresso -> stesso testo, così il prompt fisico resta identico fra richieste e la
cache a prefisso di Strata continua a valere.

1. Strumenti su richiesta (`tools_paging`, spento di serie)
   Il template di Strata (chat_template.jinja) rende le definizioni SOLO nel blocco <tools> del primo messaggio di
   sistema, cioè in testa al prompt: cambiare l'elenco a metà conversazione riscrive tutto (rilettura completa). Un
   messaggio di sistema tardivo non è ammesso (il template solleva "System message must be at the beginning";
   frontend.py lo trasforma in user). Quindi:
     - elenco strumenti FISSO per tutta la conversazione: strumenti di base del client + recall + tools (nomi 0.2.0;
       strata_recall + strata_tools nelle conversazioni nate prima) — la descrizione di tools elenca i caricabili;
     - le definizioni complete arrivano come RISULTATO di tools, in coda (role tool -> <tool_response> dentro
       un turno user): si aggiungono, non riscrivono nulla; il proxy le ricorda come eventi interni (tabella inserts,
       come recall), quindi alla richiesta dopo sono reinserite identiche;
     - "caricati" = strumenti le cui definizioni sono presenti nel prompt fisico corrente (si rilegge dal prompt, così
       dopo un cambio di segmento che taglia quel risultato tornano non caricati);
     - chiamata diretta a uno strumento non caricato: il proxy NON la passa al client, risponde con la definizione e
       chiede di ripetere la chiamata (giro interno, registrato come `tool_not_loaded`).
2. Ricevute tipizzate: al nascondimento, il risultato di uno strumento diventa una ricevuta breve secondo il tipo
   (scrittura/modifica, shell riuscita, shell fallita, test, lettura). Sempre e solo nel RISULTATO dello strumento.
3. Query del richiamo automatico: parole della richiesta corrente + file toccati di recente + identificatori + errori
   recenti + ultimo comando; la ricerca (FTS5/bm25) è in core.Manager.auto_recall.
"""
from __future__ import annotations

import hashlib
import json
import re

TOOLS_NAME = "tools"                 # 0.2.0 (era strata_tools: resta per le conversazioni nate prima, vedi core.py)
LEGACY_TOOLS_NAME = "strata_tools"
TOOLS_HEAD_TPL = "[%s: definizioni caricate: "
TOOLS_HEAD = TOOLS_HEAD_TPL % TOOLS_NAME
_TOOLS_HEAD_RE = re.compile(r"^\[([A-Za-z0-9_.-]+): definizioni caricate: ([^\]]*)\]", re.M)


def is_tools_result(text: str) -> bool:
    """Risultato del caricatore di strumenti (con qualunque nome: tools, strata_tools, alternativo)."""
    return isinstance(text, str) and _TOOLS_HEAD_RE.match(text) is not None
AUTO_HEAD = "[gestore del contesto: richiamo automatico]"


# ---------------------------------------------------------------- strumenti su richiesta
def tool_name(t: dict) -> str:
    f = t.get("function") if isinstance(t, dict) and isinstance(t.get("function"), dict) else t
    return (f or {}).get("name") or ""


def tool_desc(t: dict) -> str:
    f = t.get("function") if isinstance(t, dict) and isinstance(t.get("function"), dict) else t
    return (f or {}).get("description") or ""


def category(name: str) -> str:
    """Categoria dal prefisso del nome: github_list_issues -> github, codebase-memory_x -> codebase-memory."""
    m = re.match(r"([A-Za-z0-9]+(?:-[A-Za-z0-9]+)*)[_]", name)
    return m.group(1).lower() if m else "altri"


def _words(s: str) -> list[str]:
    return [w for w in re.split(r"[^0-9a-zà-ù]+", (s or "").lower()) if len(w) > 1]


class ToolPager:
    """Divide gli strumenti del client in base (sempre nel prompt) e catalogo (caricabili con lo strumento tools)."""

    def __init__(self, core: tuple | list | set):
        self.core = set(core or ())
        self._tool_cache: dict[str, dict] = {}

    def split(self, tools: list | None) -> tuple[list, dict]:
        base, catalog = [], {}
        for t in tools or []:
            n = tool_name(t)
            if n in self.core or not n:
                base.append(t)
            else:
                catalog[n] = t
        return base, catalog

    def tools_tool(self, catalog: dict, name: str = TOOLS_NAME) -> dict:
        """Definizione dello strumento tools: funzione pura del nome e dei NOMI del catalogo (stabile fra richieste)."""
        groups: dict[str, list[str]] = {}
        for n in catalog:
            groups.setdefault(category(n), []).append(n)
        key = json.dumps([name] + sorted((k, sorted(v)) for k, v in groups.items()))
        t = self._tool_cache.get(key)
        if t is not None:
            return t
        multi = sorted(k for k, v in groups.items() if len(v) > 1)
        single = sorted(n for k, v in groups.items() if len(v) == 1 for n in v)
        listing = "; ".join("%s: %s" % (k, ", ".join(sorted(groups[k]))) for k in multi)
        if single:
            listing += ("; " if listing else "") + "altri: " + ", ".join(single)
        t = {"type": "function", "function": {
            "name": name,
            "description": (
                "Carica la definizione completa di strumenti che NON sono nell'elenco qui sopra. Per risparmiare "
                "spazio il prompt contiene solo gli strumenti di base; gli altri esistono e funzionano normalmente, "
                "ma prima di chiamarli devi caricarne la definizione con questo strumento (eseguito dal server, costa "
                "poco). Dopo il caricamento chiamali come gli altri. Caricabili: " + listing + "."),
            "parameters": {"type": "object", "properties": {
                "query": {"type": "string", "description": "cosa vuoi fare o parole chiave (es. 'lista todo', "
                                                            "'processo in background')"},
                "names": {"type": "array", "items": {"type": "string"},
                          "description": "nomi esatti degli strumenti da caricare (1-5)"}}}}}
        if len(self._tool_cache) > 50:
            self._tool_cache.clear()
        self._tool_cache[key] = t
        return t

    @staticmethod
    def loaded_in(phys: list) -> set[str]:
        """Strumenti le cui definizioni sono nel prompt fisico (risultati del caricatore presenti)."""
        out = set()
        for m in phys or []:
            if m.get("role") != "tool":
                continue
            c = m.get("content")
            if isinstance(c, str):
                g = _TOOLS_HEAD_RE.match(c)
                if g:
                    out.update(x.strip() for x in g.group(2).split(",") if x.strip())
        return out

    @staticmethod
    def search(catalog: dict, query: str = "", names=None, limit: int = 5) -> list[str]:
        """Nomi ordinati per pertinenza (deterministico): nome esatto, parti del nome, categoria, descrizione."""
        if isinstance(names, str):
            names = [names]
        exact = [n for n in (names or []) if n in catalog][:limit]
        if names and len(exact) == len(names):
            return exact                    # tutti chiesti per nome ed esistenti: solo quelli, niente extra
        q = _words(query) + [w for n in (names or []) if n not in catalog for w in _words(n)]
        qset = set(q)
        scored = []
        for n, t in catalog.items():
            if n in exact:
                continue
            nw = set(_words(n.replace("_", " ").replace("-", " ")))
            dw = set(_words(tool_desc(t)))
            s = 0.0
            if (query or "").strip().lower() == n.lower():
                s += 20
            s += 4 * len(qset & nw) + 2 * (category(n) in qset) + 1.0 * len(qset & dw)
            s += sum(1.5 for w in qset if len(w) > 3 and w in n.lower() and w not in nw)
            if s > 0:
                scored.append((-s, n))
        scored.sort()
        return (exact + [n for _, n in scored])[:limit]

    @staticmethod
    def result(catalog: dict, names: list[str], loaded: set, query: str = "", note: str = "",
               name: str = TOOLS_NAME) -> str:
        """Testo del risultato dello strumento tools. La prima riga (TOOLS_HEAD) dice quali definizioni contiene: è ciò che
        loaded_in() rilegge. Strumenti già caricati: non si ripetono."""
        new = [n for n in names if n not in loaded]
        again = [n for n in names if n in loaded]
        if not names:
            return ("[%s: nessuno strumento trovato per %r. Caricabili: %s]"
                    % (name, query, ", ".join(sorted(catalog))))
        lines = [TOOLS_HEAD_TPL % name + ", ".join(new) + "]"]
        if note:
            lines.append(note)
        if again:
            lines.append("Già caricati più sopra (usali direttamente): %s." % ", ".join(again))
        if new:
            lines.append("Ora puoi chiamare questi strumenti normalmente. Definizioni complete:")
            for n in new:
                lines.append(json.dumps(catalog[n], ensure_ascii=False))
        return "\n".join(lines)


# ---------------------------------------------------------------- ricevute tipizzate
_EXIT = re.compile(r"(?:Command )?exited with code (\d+)|exit(?:ed)? (?:status|code)[:= ]+(\d+)", re.I)
_TEST_CMD = re.compile(
    r"(?:^|[\s/;&|(])(?:npm (?:run )?test|pnpm (?:run )?test|yarn test|node --test|pytest|py\.test|vitest|jest|"
    r"mocha|go test|cargo test|ctest|python3? -m unittest|tap)\b|[\w./-]*\btests?[\w.-]*\.(?:m?js|cjs|ts|py|sh)\b",
    re.I)
_COUNTS = [
    (re.compile(r"^#\s*pass\s+(\d+)", re.M), "pass"), (re.compile(r"^#\s*fail\s+(\d+)", re.M), "fail"),
    (re.compile(r"\b(\d+)\s+(?:passed|passing|pass\b|ok\b)", re.I), "pass"),
    (re.compile(r"\b(\d+)\s+(?:failed|failing|fail\b|errors?\b)", re.I), "fail"),
    (re.compile(r"^Ran (\d+) tests?", re.M), "ran"),
    (re.compile(r"FAILED \((?:failures|errors)=(\d+)", re.M), "fail"),
]
_FAILED_NAMES = [re.compile(r"^not ok \d+ - (.+)$", re.M), re.compile(r"^FAILED ([^\s]+)", re.M),
                 re.compile(r"^\s*[✗✖×]\s+(.+)$", re.M), re.compile(r"^(?:FAIL|ERROR):?\s+(\S.*)$", re.M)]
_ERRLINE = re.compile(r"Error|error|ERR!|Traceback|failed|FAIL|not found|No such file|denied|Cannot|cannot|"
                      r"Unexpected|undefined|exception", re.M)


def _q(s: str, n: int = 160) -> str:
    t = " ".join((s or "").split())
    t = t.replace("\u00ab", "\"").replace("\u00bb", "\"")
    return t[:n] + ("…" if len(t) > n else "")


def sha12(s: str) -> str:
    return hashlib.sha256((s or "").encode("utf-8", "surrogatepass")).hexdigest()[:12]


def exit_code(text: str):
    m = None
    for m in _EXIT.finditer(text or ""):
        pass
    if not m:
        return None
    return int(m.group(1) or m.group(2))


def test_summary(cmd: str, text: str) -> dict | None:
    """Riconosce un'esecuzione di test: comando tipico o righe di riepilogo (pass/fail). None se non è un test."""
    counts: dict[str, int] = {}
    for rx, k in _COUNTS:
        for m in rx.finditer(text or ""):
            counts[k] = int(m.group(1))     # l'ultimo riepilogo vince (di solito è quello finale)
    is_cmd = bool(_TEST_CMD.search(cmd or ""))
    if not is_cmd and not ({"pass", "fail"} & set(counts) and ("pass" in counts or "ran" in counts)):
        return None
    names = []
    for rx in _FAILED_NAMES:
        for m in rx.finditer(text or ""):
            nm = _q(m.group(1), 60)
            if nm not in names:
                names.append(nm)
    if not counts and not names and not is_cmd:
        return None
    return {"pass": counts.get("pass", counts.get("ran", 0) - counts.get("fail", 0) if "ran" in counts else None),
            "fail": counts.get("fail"), "failed_names": names[:6], "n_failed_names": len(names)}


def last_lines(text: str, n: int = 4, max_chars: int = 320) -> str:
    lines = [l.strip() for l in (text or "").splitlines() if l.strip()]
    lines = [l for l in lines if not _EXIT.search(l)]
    err = [l for l in lines if _ERRLINE.search(l)]
    pick = (err if err else lines)
    # errori identici ripetuti (stesso messaggio stampato più volte): uno solo, con il numero di ripetizioni
    uniq: list[list] = []
    for l in pick:
        if uniq and uniq[-1][0] == l:
            uniq[-1][1] += 1
        elif any(u[0] == l for u in uniq):
            next(u for u in uniq if u[0] == l)[1] += 1
        else:
            uniq.append([l, 1])
    pick = uniq[-n:]
    s = " ⏎ ".join(_q(l, 140) + (" (×%d)" % k if k > 1 else "") for l, k in pick)
    return s[:max_chars] + ("…" if len(s) > max_chars else "")


# ---------------------------------------------------------------- guardia in uscita
GUARD_NAMES = ("write", "edit", "bash")
_GUARD_RE = re.compile(r"\u27eactx-archive id=[rta][0-9a-f]{12}|\u27eb Ricevuta: |"
                       r"\[gestore del contesto: richiamo automatico\] Pezzi|nascosta per spazio \(\d+ token\)")
GUARD_MSG = ("[gestore del contesto: chiamata NON eseguita. I suoi argomenti contengono il testo di una ricevuta o di "
             "un segnaposto del gestore del contesto (es. \u27eactx-archive \u2026\u27eb, \"nascosta per spazio\"): "
             "non è contenuto vero del file. Rileggi il file (read) o recupera il testo esatto con {rn}, "
             "poi ripeti la chiamata con il contenuto vero.]")


def receipt_contaminated(name: str | None, args) -> bool:
    """Una chiamata con effetti (write/edit/bash) che contiene una ricevuta/segnaposto copiato: va bloccata."""
    if name not in GUARD_NAMES or not args:
        return False
    s = args if isinstance(args, str) else json.dumps(args, ensure_ascii=False)
    try:   # gli argomenti arrivano come JSON: i caratteri non ASCII possono essere \\uXXXX
        s = s + "\n" + json.dumps(json.loads(s), ensure_ascii=False)
    except (ValueError, TypeError):
        pass
    return bool(_GUARD_RE.search(s))


def receipt_kind(info: dict | None, text: str, failed: bool) -> str:
    if not info:
        return "altro"
    name = info.get("name")
    if name == "write":
        return "scrittura"
    if name == "edit":
        return "modifica"
    if name == "read":
        return "lettura"
    if name == "bash" or info.get("cmd"):
        if test_summary(info.get("cmd") or "", text) is not None:
            return "test"
        # pi (e i client simili) aggiungono "Command exited with code N" solo se N != 0: il codice è l'esito.
        # Senza codice si guarda solo la fine dell'uscita (un "Error" nel mezzo di un grep non è un fallimento).
        code = exit_code(text)
        if code is not None:
            return "shell_errore" if code != 0 else "shell_ok"
        tail = "\n".join((text or "").strip().splitlines()[-3:])
        return "shell_errore" if re.search(r"command not found|No such file|Traceback|^\w*Error\b", tail, re.M) \
            else "shell_ok"
    return "altro"


def typed_receipt(info: dict | None, text: str, failed: bool, rid_args: str | None = None,
                  rn: str = "recall") -> tuple[str, str]:
    """-> (tipo, testo della ricevuta) per un'uscita nascosta. Funzione pura di (chiamata, uscita, esito)."""
    kind = receipt_kind(info, text, failed)
    args = (info or {}).get("args") or {}
    if kind in ("scrittura", "modifica"):
        esito = "FALLITO (contenuto NON applicato)" if failed else "riuscito"
        if kind == "scrittura":
            c = args.get("content")
            c = c if isinstance(c, str) else ""
            s = "scrittura: %s, %d byte, sha256 %s del contenuto scritto" % (esito, len(c.encode("utf-8")), sha12(c))
        else:
            ed = args.get("edits")
            nb = len(ed) if isinstance(ed, list) else (1 if args.get("oldText") or args.get("old_string") else 0)
            s = "modifica: %s, %d blocchi" % (esito, nb)
        if rid_args:
            s += ", argomenti originali: %s id=%s" % (rn, rid_args)
        if failed:
            s += ". Errore: \u00ab%s\u00bb" % last_lines(text, 2, 200)
        return kind, s
    if kind == "lettura":
        off, lim = args.get("offset"), args.get("limit")
        rng = ("righe %s\u2013%s" % (off or 1, (int(off or 1) + int(lim) - 1) if isinstance(lim, int) else "fine")
               if (off or lim) else "file intero")
        n = len((text or "").splitlines())
        if failed:
            return kind, "lettura FALLITA (%s): \u00ab%s\u00bb" % (rng, last_lines(text, 2, 200))
        return kind, "lettura: %s, %d righe restituite, sha256 %s del testo letto" % (rng, n, sha12(text))
    if kind == "test":
        t = test_summary((info or {}).get("cmd") or "", text) or {}
        code = exit_code(text)
        parts = ["codice d'uscita %s" % (code if code is not None else 0)]
        if t.get("pass") is not None:
            parts.append("passati %d" % t["pass"])
        if t.get("fail") is not None:
            parts.append("falliti %d" % t["fail"])
        s = "test: " + ", ".join(parts)
        if t.get("failed_names"):
            s += " (%s%s)" % (", ".join(t["failed_names"]),
                              ", \u2026" if t["n_failed_names"] > len(t["failed_names"]) else "")
        elif code not in (None, 0):
            s += ". Ultime righe: \u00ab%s\u00bb" % last_lines(text, 3, 240)
        return kind, s
    if kind == "shell_errore":
        code = exit_code(text)
        return kind, "comando FALLITO: codice d'uscita %s. Ultime righe: \u00ab%s\u00bb" % (
            code if code is not None else "non indicato", last_lines(text))
    if kind == "shell_ok":
        lines = [l for l in (text or "").splitlines() if l.strip()]
        last = _q(lines[-1], 120) if lines else ""
        return kind, "comando riuscito: codice d'uscita 0, %d righe di uscita%s" % (
            len(lines), (". Ultima riga: \u00ab%s\u00bb" % last) if last else "")
    return kind, ""


# ---------------------------------------------------------------- query del richiamo automatico
_STOP = set("""il lo la i gli le un una uno di da in con su per tra fra e o ma se che chi cosa come dove quando perché
perche non si ci ne mi ti vi del della dei delle degli al alla ai alle dal dalla nel nella nei sul sulla questo questa
questi quello quella sono sei era hai ho ha abbiamo avete hanno fare fai fatto qual quale quali quanto quanti tutto
tutti anche ancora poi più piu già gia solo molto ora qui qua lì li là sia essere stato stata del dello cui allora
the a an of to in on for and or but is are was were be been it this that these those with as at by from you your we
our they their what which who how why when where not no yes do does did have has had can could should would will
domanda verifica conversazione rispondi risposta breve precisa esatti valori sai dillo comandi file progetto
eseguire leggere contesto recuperare puoi ciò fin""".split())
IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*[A-Z_][A-Za-z0-9_]*|[\w.-]+\.[a-z]{1,5}\b|\d[\d.,:]{2,}")


def query_terms(user_text: str, recent_files: list[str], errors: list[str], last_cmd: str,
                max_terms: int = 24) -> list[str]:
    """Termini della query, in ordine di priorità, senza duplicati: identificatori e parole della richiesta, nomi dei
    file recenti, parole degli errori recenti, ultimo comando."""
    out: list[str] = []

    def add(ws):
        for w in ws:
            w = w.strip("._-:,").lower()
            if len(w) < 3 or w in _STOP or w in out:
                continue
            out.append(w)

    ut = user_text or ""
    add(re.findall(r"\w+", " ".join(IDENT_RE.findall(ut))))
    add(re.findall(r"\w+", ut))
    for f in recent_files:
        add(re.findall(r"\w+", f.rsplit("/", 1)[-1]))
    for e in errors:
        add(re.findall(r"[A-Za-z_]\w+", e)[:6])
    add(re.findall(r"[A-Za-z_][\w./-]+", last_cmd or "")[:4])
    return out[:max_terms]
