# Author: Maurizio Verde — LastReload
"""Nucleo del proxy "contesto virtuale": catena di hash, archivio, masking a pacchetti, segmenti, recall.

Niente HTTP qui: il server (server.py) e il rigioco a secco (replay_dry.py) usano lo stesso codice.

Idee chiave
- La verità è la storia che manda il client. Ogni messaggio ha un hash a catena h_i = H(h_{i-1} || canon(m_i)),
  quindi h_i identifica "questo messaggio in questa posizione dopo questa storia".
- Le decisioni (maschera di un'uscita tool, segmento, eventi interni di recall) sono chiavate da h_i: una volta
  prese si riapplicano identiche a ogni richiesta successiva -> prompt fisico stabile -> la cache a prefisso di
  Strata riusa tutto. Se il client modifica un messaggio vecchio, gli h_i successivi cambiano e le decisioni
  dopo quel punto semplicemente non valgono più (ramo nuovo).
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import re
import sqlite3
import threading
import time
import uuid

from .paging import AUTO_HEAD, LEGACY_TOOLS_NAME, TOOLS_NAME, ToolPager, is_tools_result, query_terms, typed_receipt
from .render import message_piece, render_pieces, template_kwargs
from .tooldefs import ToolShortener

# Nomi degli strumenti del proxy (0.2.0: neutri rispetto al motore). Le conversazioni nate prima della 0.2.0 tengono i
# nomi vecchi (strata_recall / strata_tools): il nome entra nel prefisso stabile del prompt (elenco strumenti e
# segnaposti) e cambiarlo invaliderebbe cache e salvataggi. Le chiamate con il nome nuovo o con quello vecchio sono
# sempre risolte dal proxy (alias in ingresso), salvo che il client dichiari lui uno strumento con quel nome.
RECALL_NAME = "recall"
LEGACY_RECALL_NAME = "strata_recall"
RECALL_ALTERNATES = ("history_recall", "archive_recall", "ctx_recall")
TOOLS_ALTERNATES = ("load_tools", "tool_catalog", "ctx_tools")
NAMES_KEY = "tool_names:"       # kv: nomi scelti per conversazione (json {"recall": ..., "tools": ...})
PLACEHOLDER = "[uscita strumento omessa: {n} token \u2014 recall:{rid}]"   # formato vecchio (solo riconoscimento)
THINK_PLACEHOLDER = "[ragionamento omesso: {n} token \u2014 recall:{rid}]"
# Segnaposto con provenienza (NEXT.md punto 10, GPT-ANSWER3): solo nei RISULTATI degli strumenti (il modello li legge
# ma non li scrive), con chiamata/file di origine, inizio vero dell'uscita e istruzione esplicita.
OUT_PLACEHOLDER = ("[gestore del contesto: uscita di {origin} nascosta per spazio ({n} token). Inizio: \u00ab{head}\u00bb. "
                   "Testo esatto: {rn} id={rid}. Per lo stato ATTUALE rileggi il file o riesegui il comando.]")
OUT_PREFIX = "[gestore del contesto: uscita di "
# Ricevuta tipizzata (card t_00c5aef6, ricerca §5 e §19; delimitatore fuori vocabolario come da RICERCA-3): al posto
# dell'«Inizio» una ricevuta secondo il tipo (scrittura/modifica, shell ok/errore, test, lettura). Solo nel RISULTATO
# dello strumento. Il delimitatore ⟪ctx-archive …⟫ non compare nel codice vero: la guardia in uscita (server.py) blocca
# le chiamate con effetti che lo contengono.
RECEIPT_OPEN = "\u27eactx-archive"
OUT_RECEIPT = ("\u27eactx-archive id={rid} \u00b7 uscita di {origin} \u00b7 questo agente \u00b7 nascosta per spazio "
               "({n} token)\u27eb Ricevuta: {receipt}. Testo esatto: {rn} id={rid}. Per lo stato ATTUALE "
               "rileggi il file o riesegui il comando.")
DROP_NOTE = ("\n\n[gestore del contesto: scambio \u00abusa e getta\u00bb concluso e nascosto ({n} token). Effetti: {fx}. "
             "Testo completo: {rn} id={rid}.]")
PIN_HEAD = "[strata-context: punti fermi]"
NOTES_MARK = "[strata-context: richiesta note di passaggio]"
SEG_MARK = "[strata-context: segmento {seg}]"

def recall_tool_def(name: str = RECALL_NAME) -> dict:
    """Definizione dello strumento di recall col nome della conversazione (funzione pura del nome). Conversazioni
    nate prima della 0.2.0 (nome strata_recall): testo italiano di allora, byte per byte (prefisso invariato); nomi
    nuovi: testo inglese."""
    if name != LEGACY_RECALL_NAME:
        return {"type": "function", "function": {
            "name": name,
            "description": (
                "Retrieve the ORIGINAL, exact text of a tool output or message that the context manager removed "
                "from the prompt to save space. Use `id` when you see '" + name + " id=<id>' or an id in the "
                "archive index; use `query` to search (words, path, function name, error message) across "
                "everything archived; use `path` for the history of operations on a file (reads, writes, edits, "
                "with outcome and id). Runs on the server and is cheap: use it instead of guessing or re-running "
                "commands. Results and receipts may be in Italian."),
            "parameters": {"type": "object", "properties": {
                "id": {"type": "string", "description": "id of the archived block, e.g. r1a2b3c4d5e6"},
                "query": {"type": "string", "description": "text to search in the archive (words or substring)"},
                "path": {"type": "string", "description": "path (or name) of a file: history of operations"},
                "offset": {"type": "integer", "description": "for long texts: character offset to continue from"},
            }}}}
    return {
    "type": "function",
    "function": {
        "name": name,
        "description": (
            "Recupera il testo ORIGINALE ed esatto di un'uscita di strumento o di un messaggio che il gestore del "
            "contesto ha tolto dal prompt per fare spazio. Usa `id` quando vedi '" + name + " id=<id>' o un "
            "id nell'indice dell'archivio; usa `query` per cercare (parole, percorso, nome di funzione, messaggio "
            "d'errore) fra tutto ciò che è stato archiviato; usa `path` per la storia delle operazioni su un file "
            "(letture, scritture, modifiche, con esito e id). Eseguito dal server, costa poco: usalo invece di "
            "indovinare o rieseguire comandi."),
        "parameters": {
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "id del blocco archiviato, es. r1a2b3c4d5e6"},
                "query": {"type": "string", "description": "testo da cercare nell'archivio (parole o sottostringa)"},
                "path": {"type": "string", "description": "percorso (o nome) di un file: storia delle operazioni"},
                "offset": {"type": "integer", "description": "per testi lunghi: carattere da cui proseguire"},
            },
        },
    },
    }


RECALL_TOOL = recall_tool_def(RECALL_NAME)

NOTES_INSTRUCTION = NOTES_MARK + """
Il contesto sta per essere compattato: i messaggi più vecchi verranno archiviati (recuperabili con {rn}) e
dopo questo punto vedrai solo queste note più la parte recente della conversazione. Scrivi ORA le note di passaggio,
brevi (al massimo ~2500 token), fattuali, con questa struttura fissa:

## Obiettivo (quasi letterale, come chiesto dall'utente)
## Vincoli e preferenze espliciti dell'utente
## Decisioni prese (cosa, perché, alternative scartate)
## Stato attuale (file toccati, comandi eseguiti, risultati, numeri)
## Problemi aperti / prossimi passi
## Fatti puntuali da non perdere (percorsi, valori, nomi, id recall:<id> quando utili)

Non chiamare strumenti. Non inventare: se non sei sicuro, scrivi l'id recall da consultare."""


@dataclasses.dataclass
class Config:
    window: int = 131072            # W: finestra fisica di Strata (128K)
    # livello 1: masking
    mask_enabled: bool = True
    mask_trigger: int = 80000       # si maschera (a pacchetto) solo quando il prompt fisico supera questa soglia
    mask_target: int = 40000        # ...e si maschera dal più vecchio finché si scende sotto questa (isteresi)
    min_mask_tokens: int = 200      # uscite più corte restano sempre in chiaro
    keep_recent_tokens: int = 16000 # coda protetta (mai mascherata)
    min_age_turns: int = 2          # un'uscita è mascherabile solo dopo almeno N messaggi assistant successivi
    min_batch_tokens: int = 16000   # un pacchetto parte solo se libera almeno tanti token (niente micro-pacchetti)
    # ancora di masking (LIVE-READY §1): dopo ogni pacchetto il proxy legge il prompt fisico fino alla "frontiera"
    # (primo messaggio che un pacchetto futuro potrà ancora cambiare) e lo salva con /slots save; al pacchetto
    # successivo lo ripristina, così Strata rilegge solo dalla frontiera e non da 16.384 (unico checkpoint rimasto)
    mask_anchor: bool = False
    anchor_min_tokens: int = 24000  # ancora utile solo se la frontiera è ben oltre il checkpoint radice di Strata
    slot_dir: str = ""              # cartella dei file slot di Strata (stesso host): pulizia delle ancore superate
    mask_reasoning: bool = False    # maschera anche il ragionamento (reasoning_content) dei turni assistant vecchi
    mask_tool_args: bool = False    # maschera anche gli argomenti grandi delle tool call vecchie (es. write di file)
    # livello 2: segmenti
    segments_enabled: bool = True
    reserve: int = 8192             # riserva per operazioni (recall, note) nel controllo di finestra
    default_response: int = 16384   # se la richiesta non dichiara max_tokens
    response_floor: int = 0         # risposta minima assunta nel controllo di finestra (rigiochi con max_tokens piccolo)
    data_dir: str = ""              # se impostato e slot_save: salva qui il prompt fisico di A (per il consulto)
    tail_max: int = 24000           # coda tenuta in B (token stimati)
    notes_max_tokens: int = 8192
    index_max_tokens: int = 2000
    keep_first_user: bool = True
    slot_save: bool = False         # SAVE di A via /slots/0?action=save prima dello switch
    seal_experimental: bool = False # "trucco del sigillo" (CLAUDE-ANSWER2 §1.2-a) prima del SAVE
    # recall
    inject_recall: bool = True
    recall_max_chars: int = 24000
    recall_max_tokens: int = 6000   # tetto in TOKEN per risultato recall (il limite in caratteri non basta)
    max_recall_rounds: int = 4
    # nomi degli strumenti del proxy per le conversazioni NUOVE (quelle nate prima della 0.2.0 tengono strata_recall /
    # strata_tools); se il client dichiara già uno strumento con lo stesso nome se ne usa uno alternativo
    recall_tool_name: str = RECALL_NAME
    tools_tool_name: str = TOOLS_NAME
    # recall strutturato (recall2.py, RECALL-RESULT.md): tutto spento di serie
    recall_struct: bool = False     # passaggi, intestazioni con collegamenti, mode timeline/first/tree..., filtri
    recall_multi: bool = False      # queries: [...] in una sola chiamata (fusione per rango)
    recall_flex: bool = False       # indice normalizzato (identificatori spezzati, radice leggera it/en)
    recall_struct_max_tokens: int = 3500  # tetto in token del risultato strutturato
    # definizioni strumenti accorciate (tooldefs.py, TOOLDEFS-RESULT.md): 0 = spento (default). Testi deterministici
    # e stabili fra richieste; strumenti in tooldefs_keep mai toccati (recall è del proxy: già corto)
    tooldefs_desc_max: int = 0      # caratteri massimi della description di uno strumento
    tooldefs_param_max: int = 0     # caratteri massimi della description di ogni parametro (anche annidati)
    tooldefs_keep: tuple = (RECALL_NAME, LEGACY_RECALL_NAME, "read", "bash", "edit", "write", "grep", "find", "ls")  # usati sempre, corti
    # strumenti su richiesta (card t_00c5aef6, paging.py): nel prompt solo tools_core + recall + tools;
    # gli altri si caricano con lo strumento tools (definizione nel risultato, in coda: il prefisso non cambia)
    tools_paging: bool = False
    tools_core: tuple = ("read", "bash", "edit", "write", "grep", "find", "ls")
    tools_load_max: int = 5
    # ricevute tipizzate al posto dell'«Inizio» nei segnaposti delle uscite nascoste
    typed_receipts: bool = True
    receipt_guard: bool = True           # blocca write/edit/bash che contengono ricevute/segnaposti copiati
    # richiamo automatico bm25 (ricerca §6-8): a ogni nuova richiesta dell'utente, pezzi NASCOSTI pertinenti in coda
    auto_recall: bool = False
    auto_recall_on_error: bool = False   # anche dopo un risultato di strumento fallito
    auto_recall_k: int = 4               # pezzi massimi
    auto_recall_max_tokens: int = 4000   # tetto del messaggio iniettato
    auto_recall_piece_tokens: int = 1200 # tetto per pezzo
    auto_recall_min_score: float = 8.0   # soglia su -bm25 (FTS5)
    auto_recall_min_terms: int = 2       # termini distinti della query presenti nel pezzo (esclusi i comuni)
    auto_recall_common_frac: float = 0.4 # termine comune se compare in più di questa quota dei pezzi trovati
    auto_recall_max_args: int = 1        # argomenti di chiamata al massimo per iniezione
    auto_recall_hint: bool = False       # indizio (una riga con id/messaggi) al posto dei pezzi inseriti
    auto_recall_hint_k: int = 5          # candidati citati nell'indizio
    # punti fermi / usa e getta (NEXT.md 11-12)
    pins_max_tokens: int = 8192     # tetto del blocco "Punti fermi" copiato al cambio di segmento
    # stima token
    chars_per_token: float = 3.5
    msg_overhead: int = 6
    image_tokens: int = 1024
    reasoning_last_turn_only: bool = False  # template Strata: preserve_thinking indefinito => tiene tutto
    # salvataggio/ripristino automatico del segmento attivo (AUTOSAVE-RESULT.md)
    autosave: bool = False          # salva lo stato di Strata quando la conversazione che lo occupa resta ferma
    autosave_idle_s: float = 180.0  # ...per almeno tanti secondi (e Strata è libero)
    autosave_min_tokens: int = 16384  # sotto questa soglia rileggere costa poco: niente file
    autosave_keep: int = 10         # file di autosalvataggio tenuti (i più recenti)
    autosave_max_gb: float = 25.0   # tetto di disco per i file di autosalvataggio
    autosave_min_free_gb: float = 8.0  # niente salvataggio se sul disco degli slot resta meno di così
    autosave_poll_s: float = 10.0
    autorestore: bool = True        # (con autosave) restore prima di inoltrare se Strata non ha già il prefisso
    autorestore_min_gain: int = 8192  # token di prefisso in più rispetto a quanto Strata ha già, per fare restore
    # archivio freddo dei salvataggi (kvarchive.py): blocchi deduplicati + zstd, originale cancellato solo dopo
    # ricostruzione verificata (sha256); restore di un file archiviato = ricostruzione prima
    kv_archive: bool = False
    kv_archive_dir: str = ""        # vuoto = <cartella madre di slot_dir>/kv-archive
    kv_archive_idle_s: float = 600.0  # Strata fermo (requests invariato, nessuna in_flight) da almeno tanto
    kv_archive_min_age_s: float = 1800.0  # file non modificato da almeno tanto
    kv_archive_min_bytes: int = 64 * 2 ** 20
    kv_archive_poll_s: float = 60.0
    kv_archive_level: int = 3
    kv_archive_threads: int = 4
    # osservabilità
    live_dump: bool = False         # scrive data_dir/live/last_request.json (prompt fisico + risposta) per la dashboard
    # motore (engines.py): auto = rilevato all'avvio; strata | llama.cpp | openai lo forzano
    engine: str = "auto"
    slot_id: int = 0                # slot del motore (llama-server con --parallel N)
    engine_tokenize: bool = False   # conteggio token esatto via /tokenize del motore (llama.cpp), se non c'è --tokenizer
    # ds4-server: ds4 rimette nel prompt le tool call campionate prese per id (exact DSML replay), quindi
    # mask_tool_args non serve e viene spento; false solo se ds4-server gira con --disable-exact-dsml-tool-replay
    ds4_exact_tool_replay: bool = True
    # allineamento dei pacchetti di masking ai checkpoint su disco del motore (ds4: --kv-cache-continued-interval-
    # tokens arrotondato a --kv-cache-boundary-align-tokens, di serie 10240). 0 = spento. Con N > 0 il primo
    # messaggio cambiato da un pacchetto viene scelto, fra i primi candidati, in modo da cadere poco dopo un
    # multiplo di N token (meno token da rileggere dopo l'ultimo checkpoint). Ha senso solo con --tokenizer esatto.
    checkpoint_align_tokens: int = 0
    checkpoint_align_slack: float = 0.25   # spreco accettato: frazione di N dopo il multiplo

    @classmethod
    def from_dict(cls, d: dict) -> "Config":
        names = {f.name for f in dataclasses.fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in names})


def H(*parts: str) -> str:
    h = hashlib.sha256()
    for p in parts:
        b = p.encode("utf-8", "surrogatepass")
        h.update(len(b).to_bytes(8, "little"))  # codifica senza ambiguità di concatenazione
        h.update(b)
    return h.hexdigest()


def content_text(content) -> str:
    """Testo di un campo content OpenAI (stringa o lista di parti). Le immagini diventano un segnaposto con hash."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    out = []
    for p in content:
        if isinstance(p, str):
            out.append(p)
        elif isinstance(p, dict):
            t = p.get("type")
            if t in ("text", "input_text", "output_text"):
                out.append(p.get("text") or "")
            elif t in ("image_url", "input_image", "image"):
                url = p.get("image_url")
                url = url.get("url") if isinstance(url, dict) else (url or p.get("data") or "")
                out.append("<immagine:%s>" % H(str(url))[:16])
    return "\n".join(out)


def n_images(content) -> int:
    if isinstance(content, list):
        return sum(1 for p in content if isinstance(p, dict) and p.get("type") in ("image_url", "input_image", "image"))
    return 0


def _args(a):
    if isinstance(a, str):
        try:
            return json.loads(a)
        except ValueError:
            return a
    return a


def canon(m: dict) -> str:
    """Forma canonica di un messaggio per la catena di hash. reasoning_content escluso (molti client non lo
    rimandano identico); ordine delle chiavi JSON degli argomenti normalizzato; \\r\\n e spazi in coda normalizzati."""
    d = {"role": m.get("role"), "content": content_text(m.get("content")).replace("\r\n", "\n").rstrip()}
    if m.get("tool_calls"):
        d["tool_calls"] = [{"id": c.get("id"), "name": (c.get("function") or {}).get("name"),
                            "arguments": _args((c.get("function") or {}).get("arguments"))} for c in m["tool_calls"]]
    if m.get("tool_call_id"):
        d["tool_call_id"] = m["tool_call_id"]
    if m.get("name"):
        d["name"] = m["name"]
    return json.dumps(d, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def chain(messages: list, tools: list | None) -> list[str]:
    h = H("tools", json.dumps(tools or [], sort_keys=True, ensure_ascii=False))
    out = []
    for m in messages:
        h = H(h, canon(m))
        out.append(h)
    return out


def rid_for(idx: int, m: dict) -> str:
    return "r" + H(str(idx), canon(m))[:12]


def tid_for(idx: int, m: dict) -> str:
    """id d'archivio del ragionamento di un messaggio assistant."""
    return "t" + H("think", str(idx), canon(m), m.get("reasoning_content") or "")[:12]


THINK_SUFFIX = ":think"
ARGS_SUFFIX = ":args"
DROP_SUFFIX = ":drop"
AUTO_SUFFIX = ":auto"


def aid_for(idx: int, m: dict) -> str:
    """id d'archivio degli argomenti delle tool call di un messaggio assistant."""
    return "a" + H("args", str(idx), canon(m))[:12]


def args_text(m: dict) -> str:
    out = []
    for c in m.get("tool_calls") or []:
        f = c.get("function") or {}
        a = f.get("arguments")
        out.append("%s %s" % (f.get("name"), a if isinstance(a, str) else json.dumps(a, ensure_ascii=False)))
    return "\n".join(out)


ARGS_KEEP_CHARS = 160
_FAKE_OMIT = re.compile(r"\[(?:contenuto|argomenti|uscita strumento|ragionamento) omess[oaie][^\]]*\]"
                        r"|\[gestore del contesto:[^\]]*\]")


def masked_calls(m: dict, rid: str, n: int) -> list:
    """Le tool call con i valori VOLUMINOSI accorciati a testo vero (prime righe + '…'), senza formule:
    nome, id, percorso e parametri piccoli restano. La nota con recall va nel RISULTATO dello strumento
    (vedi call_notes), che il modello legge ma non scrive. Fix live 4/10: i segnaposti negli argomenti
    venivano imitati dal modello nei suoi comandi nuovi."""
    out = []
    for c in m.get("tool_calls") or []:
        f = dict(c.get("function") or {})
        a = f.get("arguments")
        try:
            args = json.loads(a) if isinstance(a, str) else dict(a or {})
        except Exception:
            args = None
        if isinstance(args, dict):
            kept = {}
            for k, v in args.items():
                s = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
                kept[k] = (s[:ARGS_KEEP_CHARS].rstrip() + "\n…") if len(s) > 300 else v
            f["arguments"] = json.dumps(kept, ensure_ascii=False)
        else:
            s = a if isinstance(a, str) else json.dumps(a, ensure_ascii=False)
            f["arguments"] = s[:ARGS_KEEP_CHARS] + "…"
        out.append({**c, "function": f})
    return out


def call_notes(m: dict, rid: str) -> dict:
    """tool_call_id -> (nome, percorso, rid) delle chiamate i cui argomenti voluminosi vengono accorciati: build()
    antepone al risultato dello strumento una nota con operazione, file, ESITO (letto dal risultato) e recall."""
    notes = {}
    for c in m.get("tool_calls") or []:
        f = c.get("function") or {}
        a = f.get("arguments")
        try:
            args = json.loads(a) if isinstance(a, str) else dict(a or {})
        except Exception:
            args = {}
        big = [k for k, v in (args or {}).items()
               if len(v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)) > 300]
        if not big and isinstance(args, dict):
            continue
        path = (args or {}).get("path") or (args or {}).get("file_path") or ""
        notes[c.get("id")] = (f.get("name") or "", path, rid)
    return notes


def call_note_text(name: str, path: str, rid: str, result: str, rn: str = RECALL_NAME) -> str:
    """GPT-ANSWER3 §game.js: operazione, file, esito accertato o no, accesso allo storico, niente 'modifiche
    esterne' inventate."""
    esito = outcome(name, result)
    what = "%s%s" % (name, (" su " + path) if path else "")
    if esito == "riuscito":
        e = "esito: riuscito (vedi il risultato qui sotto)"
    elif esito == "fallito":
        e = "esito: FALLITO (vedi il risultato qui sotto): quel contenuto NON è stato applicato"
    else:
        e = "esito non verificato"
    return ("(nota del gestore del contesto: chiamata %s fatta da te in questa sessione, %s. Gli argomenti voluminosi "
            "sono accorciati nel prompt per spazio; testo originale: %s id=%s. Per lo stato attuale del "
            "file rileggilo; se differisce da ciò che ricordi, è per tue modifiche successive o per un esito "
            "fallito: nessuna modifica esterna è stata rilevata.)\n" % (what, e, rn, rid))


def is_human_user(m: dict) -> bool:
    if m.get("role") != "user":
        return False
    t = content_text(m.get("content")).strip()
    return not (t.startswith("<tool_response>") and t.endswith("</tool_response>"))


# ---------- 📌 punti fermi / 🗑 usa e getta (NEXT.md 11-12) ----------
_PIN_LINE = re.compile(r"^[ \t]*(?:\U0001F4CC|!!)[ \t]*(.+)$", re.M)
_PIN_BLOCK = re.compile(r"\[\[importante\]\](.*?)\[\[/importante\]\]", re.S | re.I)
_DROP_LINE = re.compile(r"^[ \t]*(?:\U0001F5D1\uFE0F?|~)(?!~)[ \t]*\S", re.M)


def pin_texts(text: str) -> list[str]:
    """Punti fermi scritti dall'utente: righe che iniziano con 📌 o !!, blocchi [[importante]]…[[/importante]]."""
    out = [m.group(1).strip() for m in _PIN_BLOCK.finditer(text or "") if m.group(1).strip()]
    rest = _PIN_BLOCK.sub("", text or "")
    out += [m.group(1).strip() for m in _PIN_LINE.finditer(rest) if m.group(1).strip()]
    return out


def is_disposable(text: str) -> bool:
    """Messaggio utente 'usa e getta': una riga che inizia con 🗑 o ~ (non ~~, che è markdown barrato)."""
    return bool(_DROP_LINE.search(text or ""))


def call_info(m: dict) -> dict:
    """tool_call_id -> {name, args(dict), path, cmd} per le chiamate di un messaggio assistant."""
    out = {}
    for c in m.get("tool_calls") or []:
        f = c.get("function") or {}
        a = _args(f.get("arguments"))
        a = a if isinstance(a, dict) else {}
        path = a.get("path") or a.get("file_path") or a.get("filePath") or ""
        cmd = a.get("command") or a.get("cmd") or ""
        out[c.get("id")] = {"name": f.get("name") or "?", "args": a, "path": str(path), "cmd": str(cmd)}
    return out


_FAIL = re.compile(r"RETRYABLE|not found in the current file|^Error|\bError:|Traceback|No such file|command not found|"
                   r"exit code [1-9]|exited with code [1-9]|ENOENT|EACCES", re.M)


def outcome(name: str, result: str, is_error: bool | None = None) -> str:
    """Esito di una chiamata dal testo del risultato: 'riuscito' | 'fallito' | 'non verificato'."""
    r = (result or "")[:600]
    if is_error or _FAIL.search(r):
        return "fallito"
    if re.match(r"\s*(Successfully|Created|Updated|Wrote|OK\b)", r):
        return "riuscito"
    return "riuscito" if name in ("read",) and r.strip() else "non verificato"


def origin_text(info: dict | None) -> str:
    """Descrizione breve dell'origine di un'uscita: «read src/game.js», «bash `npm test`»."""
    if not info:
        return "uno strumento"
    if info["path"]:
        return "%s %s" % (info["name"], info["path"])
    if info["cmd"]:
        c = " ".join(info["cmd"].split())
        return "%s `%s`" % (info["name"], c[:90] + ("…" if len(c) > 90 else ""))
    a = json.dumps(info["args"], ensure_ascii=False)
    return "%s %s" % (info["name"], a[:80] + ("…" if len(a) > 80 else ""))


def head_text(text: str, n: int = 140) -> str:
    t = " ".join((text or "").split())
    return t[:n] + ("…" if len(t) > n else "")


def receipt(info: dict | None, result: str, is_error=None) -> str:
    """Ricevuta di una riga degli effetti di una chiamata: «write src/a.js: riuscito»."""
    if not info:
        return "?"
    return "%s: %s" % (origin_text(info), outcome(info["name"], result, is_error))


class TokenCounter:
    """Stima token: tokenizer HF (tokenizers) se disponibile, altrimenti caratteri / chars_per_token.
    `scale` si calibra con i prompt_tokens reali restituiti da Strata."""

    def __init__(self, chars_per_token: float = 3.5, tokenizer_path: str | None = None):
        self.cpt = chars_per_token
        self.scale = 1.0
        self.tk = None
        self.kind = "chars/%.1f" % chars_per_token
        if tokenizer_path:
            try:
                self.tk = load_tokenizer(tokenizer_path)
                self.kind = "esatto:" + os.path.basename(os.path.abspath(tokenizer_path).rstrip("/"))
            except Exception as e:  # noqa: BLE001
                self.kind += " (tokenizer non caricato: %s)" % e
        self._cache: dict[str, int] = {}

    @property
    def exact(self) -> bool:
        return self.tk is not None

    def raw(self, text: str) -> int:
        if not text:
            return 0
        if self.tk is None:
            return int(len(text) / self.cpt + 0.5)
        key = H(text) if len(text) > 256 else text
        n = self._cache.get(key)
        if n is None:
            n = len(self.tk.encode(text, add_special_tokens=False).ids)
            if len(self._cache) > 200000:
                self._cache.clear()
            self._cache[key] = n
        return n

    def count(self, text: str) -> int:
        return int(self.raw(text) * self.scale + 0.5)

    def calibrate(self, estimated: int, real: int, alpha: float = 0.3) -> None:
        if self.tk is not None:
            return          # conteggio esatto (template + tokenizer del pack): nessuna scala da correggere
        if estimated > 1000 and real > 0:
            r = real / estimated
            self.scale = min(2.0, max(0.5, self.scale * ((1 - alpha) + alpha * r)))


def load_tokenizer(path: str):
    """Tokenizer HF (`tokenizers`) per il conteggio esatto. Accetta un tokenizer.json HF oppure la cartella
    tokenizer del pack di Strata (vocab.json + merges.txt + token_type.json + descrittore tokenizer.json, che NON è
    in formato HF): in quel caso lo costruisce (tools/make_hf_tokenizer.py, verificato identico a Strata)."""
    from tokenizers import Tokenizer  # type: ignore
    p = path
    if os.path.isfile(p):
        try:
            return Tokenizer.from_file(p)
        except Exception:  # noqa: BLE001  descrittore del pack: si costruisce dalla cartella
            p = os.path.dirname(os.path.abspath(p))
    if os.path.isdir(p) and os.path.exists(os.path.join(p, "vocab.json")):
        from .packtok import build
        return build(p)
    raise ValueError("tokenizer non riconosciuto: %s" % path)


class Journal:
    def __init__(self, path: str | None):
        self.path = path
        self.lock = threading.Lock()
        self.mem: list[dict] = []
        if path:
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)

    def log(self, event: str, **kw) -> dict:
        rec = {"ts": round(time.time(), 3), "event": event, **kw}
        with self.lock:
            self.mem.append(rec)
            if len(self.mem) > 5000:
                del self.mem[:1000]
            if self.path:
                with open(self.path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                    f.flush()
                    os.fsync(f.fileno())
        return rec


SCHEMA = """
CREATE TABLE IF NOT EXISTS archive(rid TEXT PRIMARY KEY, conv TEXT, idx INT, role TEXT, name TEXT,
    content TEXT, tokens INT, created REAL);
CREATE INDEX IF NOT EXISTS archive_conv ON archive(conv, idx);
CREATE TABLE IF NOT EXISTS chains(h TEXT PRIMARY KEY, conv TEXT, idx INT);
CREATE TABLE IF NOT EXISTS masks(h TEXT PRIMARY KEY, conv TEXT, idx INT, rid TEXT, tokens INT, created REAL);
CREATE TABLE IF NOT EXISTS segments(conv TEXT, seg INT, cut_idx INT, cut_h TEXT, notes_msg TEXT, kind TEXT,
    created REAL, PRIMARY KEY(conv, seg));
CREATE TABLE IF NOT EXISTS inserts(h TEXT PRIMARY KEY, conv TEXT, msgs TEXT, strip_content TEXT,
    strip_reasoning TEXT, created REAL);
CREATE TABLE IF NOT EXISTS anchors(conv TEXT, seg INT, idx INT, phash TEXT, file TEXT, tokens INT, created REAL,
    PRIMARY KEY(conv, seg));
CREATE TABLE IF NOT EXISTS pins(id INTEGER PRIMARY KEY AUTOINCREMENT, conv TEXT, idx INT, text TEXT, created REAL,
    active INT DEFAULT 1);
CREATE TABLE IF NOT EXISTS autosaves(file TEXT PRIMARY KEY, conv TEXT, seg INT, phash TEXT, plen INT,
    tokens INT, bytes INT, n_saved INT, created REAL, last_used REAL, deleted REAL);
CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS drops(conv TEXT, idx INT, created REAL, PRIMARY KEY(conv, idx));
CREATE TABLE IF NOT EXISTS fileops(conv TEXT, idx INT, call_id TEXT, op TEXT, path TEXT, outcome TEXT, rid_args TEXT,
    rid_out TEXT, head TEXT, PRIMARY KEY(conv, idx, call_id));
"""
# Ricerca (GPT-ANSWER3 §ricerca): indice FTS5 a contenuto esterno sull'archivio, aggiornato da trigger.
FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS archive_fts USING fts5(content, content='archive', content_rowid='rowid',
    tokenize='unicode61 remove_diacritics 2');
CREATE TRIGGER IF NOT EXISTS archive_ai AFTER INSERT ON archive BEGIN
    INSERT INTO archive_fts(rowid, content) VALUES (new.rowid, new.content); END;
"""
MIGRATIONS = ["ALTER TABLE masks ADD COLUMN saved INT", "ALTER TABLE segments ADD COLUMN pins_msg TEXT"]


class Store:
    """Archivio SQLite: testo originale (fonte di verità), mappa hash->conversazione, decisioni stabili."""

    def __init__(self, path: str = ":memory:"):
        if path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.db.execute("PRAGMA journal_mode=WAL" if path != ":memory:" else "PRAGMA journal_mode=MEMORY")
        self.db.executescript(SCHEMA)
        for sql in MIGRATIONS:
            try:
                self.db.execute(sql)
            except sqlite3.OperationalError:
                pass                      # colonna già presente
        self.fts = True
        try:
            self.db.executescript(FTS_SCHEMA)
            n_fts = self.db.execute("SELECT count(*) FROM archive_fts_docsize").fetchone()[0]
            n_arc = self.db.execute("SELECT count(*) FROM archive").fetchone()[0]
            if n_arc and n_fts < n_arc:   # archivio di una versione precedente: indicizza tutto una volta
                self.db.execute("INSERT INTO archive_fts(archive_fts) VALUES('rebuild')")
        except sqlite3.OperationalError:
            self.fts = False              # SQLite senza FTS5: resta la ricerca per sottostringa
        self.lock = threading.RLock()

    def _in(self, sql: str, keys: list, extra: tuple = ()):
        out = []
        for i in range(0, len(keys), 500):
            part = keys[i:i + 500]
            q = sql.replace("(?)", "(" + ",".join("?" * len(part)) + ")")
            out.extend(self.db.execute(q, (*extra, *part)).fetchall())
        return out

    def find_conv(self, hs: list[str]) -> tuple[str | None, int]:
        """Conversazione dell'ultimo hash noto della catena, e la sua posizione (-1 se nessuno)."""
        with self.lock:
            rows = self._in("SELECT h, conv, idx FROM chains WHERE h IN (?)", hs)
        if not rows:
            return None, -1
        best = max(rows, key=lambda r: r[2])
        return best[1], best[2]

    def add_chain(self, conv: str, hs: list[str]) -> None:
        with self.lock:
            self.db.executemany("INSERT OR IGNORE INTO chains VALUES(?,?,?)", [(h, conv, i) for i, h in enumerate(hs)])

    def has_successor(self, conv: str, idx: int) -> bool:
        with self.lock:
            return self.db.execute("SELECT 1 FROM chains WHERE conv=? AND idx=? LIMIT 1",
                                   (conv, idx + 1)).fetchone() is not None

    def archive_many(self, rows: list[tuple]) -> None:
        with self.lock:
            self.db.executemany("INSERT OR IGNORE INTO archive VALUES(?,?,?,?,?,?,?,?)", rows)

    def get(self, rid: str, conv: str | None = None):
        with self.lock:
            if conv:
                return self.db.execute("SELECT rid, conv, idx, role, name, content, tokens FROM archive "
                                       "WHERE rid=? AND conv=?", (rid, conv)).fetchone()
            return self.db.execute("SELECT rid, conv, idx, role, name, content, tokens FROM archive WHERE rid=?",
                                   (rid,)).fetchone()

    def search(self, conv: str, query: str, limit: int = 5, max_idx: int | None = None):
        """Sottostringa esatta (identificatori, percorsi), più recenti prima. max_idx: esclude i messaggi con indice
        >= max_idx (la domanda corrente e i suoi rami fratelli, che il modello vede già)."""
        with self.lock:
            return self.db.execute(
                "SELECT rid, idx, role, name, content, tokens FROM archive WHERE conv=? AND instr(lower(content), "
                "lower(?)) > 0 AND idx < ? ORDER BY idx DESC LIMIT ?",
                (conv, query, 10 ** 9 if max_idx is None else max_idx, limit)).fetchall()

    def search_fts(self, conv: str, query: str, limit: int = 20, max_idx: int | None = None):
        """FTS5 con ranking bm25 sulle parole della domanda (OR): i pezzi con più parole/più rare prima."""
        if not self.fts:
            return []
        terms = [t for t in re.findall(r"\w+", query or "") if len(t) > 1][:16]
        if not terms:
            return []
        q = " OR ".join('"%s"' % t.replace('"', "") for t in terms)
        with self.lock:
            try:
                return self.db.execute(
                    "SELECT a.rid, a.idx, a.role, a.name, a.content, a.tokens FROM archive_fts f "
                    "JOIN archive a ON a.rowid = f.rowid WHERE archive_fts MATCH ? AND a.conv=? AND a.idx < ? "
                    "ORDER BY bm25(archive_fts) LIMIT ?",
                    (q, conv, 10 ** 9 if max_idx is None else max_idx, limit)).fetchall()
            except sqlite3.OperationalError:
                return []

    def search_fts_scored(self, conv: str, terms: list[str], limit: int = 50, max_idx: int | None = None):
        """FTS5 OR dei termini dati -> righe (rid, idx, role, name, content, tokens, score) con score = -bm25 (più
        alto = più pertinente), ordinate per score e poi per idx (deterministico)."""
        if not self.fts or not terms:
            return []
        q = " OR ".join('"%s"' % t.replace('"', "") for t in terms if t)
        with self.lock:
            try:
                rows = self.db.execute(
                    "SELECT a.rid, a.idx, a.role, a.name, a.content, a.tokens, -bm25(archive_fts) AS s "
                    "FROM archive_fts f JOIN archive a ON a.rowid = f.rowid WHERE archive_fts MATCH ? AND a.conv=? "
                    "AND a.idx < ? ORDER BY s DESC, a.idx DESC LIMIT ?",
                    (q, conv, 10 ** 9 if max_idx is None else max_idx, limit)).fetchall()
            except sqlite3.OperationalError:
                return []
        return rows

    # ---------- punti fermi / usa e getta ----------
    def pins(self, conv: str, active_only: bool = True):
        with self.lock:
            return self.db.execute("SELECT id, idx, text, created, active FROM pins WHERE conv=?" +
                                   (" AND active=1" if active_only else "") + " ORDER BY idx, id", (conv,)).fetchall()

    def has_pin(self, conv: str, idx: int, text: str) -> bool:
        with self.lock:
            return self.db.execute("SELECT 1 FROM pins WHERE conv=? AND idx=? AND text=?",
                                   (conv, idx, text)).fetchone() is not None

    def add_pin(self, conv: str, idx: int, text: str) -> int:
        with self.lock:
            return self.db.execute("INSERT INTO pins(conv, idx, text, created, active) VALUES(?,?,?,?,1)",
                                   (conv, idx, text, time.time())).lastrowid

    def update_pin(self, pid: int, text: str | None = None, active: bool | None = None) -> bool:
        with self.lock:
            if text is not None:
                self.db.execute("UPDATE pins SET text=? WHERE id=?", (text, pid))
            if active is not None:
                self.db.execute("UPDATE pins SET active=? WHERE id=?", (1 if active else 0, pid))
            return self.db.execute("SELECT 1 FROM pins WHERE id=?", (pid,)).fetchone() is not None

    def drops(self, conv: str) -> set[int]:
        with self.lock:
            return {r[0] for r in self.db.execute("SELECT idx FROM drops WHERE conv=?", (conv,))}

    def add_drop(self, conv: str, idx: int) -> None:
        with self.lock:
            self.db.execute("INSERT OR IGNORE INTO drops VALUES(?,?,?)", (conv, idx, time.time()))

    def del_drop(self, conv: str, idx: int) -> None:
        with self.lock:
            self.db.execute("DELETE FROM drops WHERE conv=? AND idx=?", (conv, idx))

    def user_messages(self, conv: str):
        with self.lock:
            return self.db.execute("SELECT idx, content FROM archive WHERE conv=? AND role='user' ORDER BY idx",
                                   (conv,)).fetchall()

    def segment_pins(self, conv: str, seg: int) -> str | None:
        with self.lock:
            r = self.db.execute("SELECT pins_msg FROM segments WHERE conv=? AND seg=?", (conv, seg)).fetchone()
        return r[0] if r else None

    def set_segment_pins(self, conv: str, seg: int, pins_msg: str | None) -> None:
        with self.lock:
            self.db.execute("UPDATE segments SET pins_msg=? WHERE conv=? AND seg=?", (pins_msg, conv, seg))

    def del_masks(self, keys: list[str]) -> None:
        with self.lock:
            self._in("DELETE FROM masks WHERE h IN (?)", keys)

    def add_fileops(self, rows: list[tuple]) -> int:
        """rows: (conv, idx, call_id, op, path, outcome, rid_args, rid_out, head). -> righe nuove."""
        with self.lock:
            before = self.db.total_changes
            self.db.executemany("INSERT OR IGNORE INTO fileops VALUES(?,?,?,?,?,?,?,?,?)", rows)
            return self.db.total_changes - before

    def fileops(self, conv: str, path: str, max_idx: int | None = None):
        p = (path or "").strip()
        with self.lock:
            return self.db.execute(
                "SELECT idx, op, path, outcome, rid_args, rid_out, head FROM fileops WHERE conv=? AND idx < ? AND "
                "(path=? OR path LIKE ? OR ? LIKE '%' || path) ORDER BY idx",
                (conv, 10 ** 9 if max_idx is None else max_idx, p, "%" + p, p)).fetchall()

    def anchor(self, conv: str, seg: int):
        with self.lock:
            return self.db.execute("SELECT idx, phash, file, tokens FROM anchors WHERE conv=? AND seg=?",
                                   (conv, seg)).fetchone()

    def set_anchor(self, conv: str, seg: int, idx: int, phash: str, file: str, tokens: int) -> None:
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO anchors VALUES(?,?,?,?,?,?,?)",
                            (conv, seg, idx, phash, file, tokens, time.time()))

    # ---------- autosalvataggi (autosave.py) ----------
    AS_COLS = ("file", "conv", "seg", "phash", "plen", "tokens", "bytes", "n_saved", "created", "last_used", "deleted")

    def kv_get(self, k: str):
        with self.lock:
            r = self.db.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
        return r[0] if r else None

    def kv_set(self, k: str, v: str) -> None:
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO kv VALUES(?,?)", (k, v))

    def add_autosave(self, file, conv, seg, phash, plen, tokens, nbytes, n_saved) -> None:
        now = time.time()
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO autosaves VALUES(?,?,?,?,?,?,?,?,?,?,NULL)",
                            (file, conv, seg, phash, plen, tokens, nbytes, n_saved, now, now))

    def autosaves(self, active_only: bool = True) -> list[dict]:
        """Autosalvataggi, il più recente (creato o usato) per primo."""
        with self.lock:
            rows = self.db.execute("SELECT %s FROM autosaves %s ORDER BY max(created, coalesce(last_used, 0)) DESC"
                                   % (",".join(self.AS_COLS), "WHERE deleted IS NULL" if active_only else "")
                                   ).fetchall()
        return [dict(zip(self.AS_COLS, r)) for r in rows]

    def autosave_with_phash(self, phash: str):
        with self.lock:
            return self.db.execute("SELECT file FROM autosaves WHERE phash=? AND deleted IS NULL",
                                   (phash,)).fetchone()

    def autosave_state(self, file: str) -> dict | None:
        with self.lock:
            r = self.db.execute("SELECT phash, plen, tokens, conv, seg FROM autosaves WHERE file=?", (file,)).fetchone()
        return dict(zip(("phash", "plen", "tokens", "conv", "seg"), r)) if r else None

    def autosave_used(self, file: str) -> None:
        with self.lock:
            self.db.execute("UPDATE autosaves SET last_used=? WHERE file=?", (time.time(), file))

    def autosave_deleted(self, file: str) -> None:
        with self.lock:
            self.db.execute("UPDATE autosaves SET deleted=? WHERE file=?", (time.time(), file))

    def masks_for(self, hs: list[str]) -> dict[str, tuple[str, int]]:
        with self.lock:
            return {h: (rid, n) for h, rid, n in self._in("SELECT h, rid, tokens FROM masks WHERE h IN (?)", hs)}

    def masks_saved(self, hs: list[str]) -> int:
        with self.lock:
            r = self._in("SELECT coalesce(sum(saved), 0) FROM masks WHERE h IN (?)", hs)
        return sum(x[0] or 0 for x in r)

    def add_masks(self, rows: list[tuple]) -> None:
        """rows: (h, conv, idx, rid, tokens, created[, saved])"""
        with self.lock:
            self.db.executemany("INSERT OR IGNORE INTO masks(h, conv, idx, rid, tokens, created, saved) "
                                "VALUES(?,?,?,?,?,?,?)", [tuple(r) + (None,) * (7 - len(r)) for r in rows])

    def segments(self, conv: str):
        with self.lock:
            return self.db.execute("SELECT seg, cut_idx, cut_h, notes_msg, kind FROM segments WHERE conv=? "
                                   "ORDER BY seg DESC", (conv,)).fetchall()

    def add_segment(self, conv, seg, cut_idx, cut_h, notes_msg, kind) -> None:
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO segments(conv, seg, cut_idx, cut_h, notes_msg, kind, created) "
                            "VALUES(?,?,?,?,?,?,?)",
                            (conv, seg, cut_idx, cut_h, notes_msg, kind, time.time()))

    def inserts_for(self, hs: list[str]) -> dict[str, dict]:
        with self.lock:
            rows = self._in("SELECT h, msgs, strip_content, strip_reasoning FROM inserts WHERE h IN (?)", hs)
        return {h: {"msgs": json.loads(m), "strip_content": sc or "", "strip_reasoning": sr or ""}
                for h, m, sc, sr in rows}

    def add_insert(self, h, conv, msgs, strip_content="", strip_reasoning="") -> None:
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO inserts VALUES(?,?,?,?,?,?)",
                            (h, conv, json.dumps(msgs, ensure_ascii=False), strip_content, strip_reasoning,
                             time.time()))

    def has_conv(self, conv: str) -> bool:
        with self.lock:
            return self.db.execute("SELECT 1 FROM chains WHERE conv=? LIMIT 1", (conv,)).fetchone() is not None

    def stats(self, conv: str) -> dict:
        with self.lock:
            a = self.db.execute("SELECT count(*), coalesce(sum(tokens),0) FROM archive WHERE conv=?", (conv,)).fetchone()
            m = self.db.execute("SELECT count(*), coalesce(sum(tokens),0), coalesce(sum(saved),0) FROM masks "
                                "WHERE conv=?", (conv,)).fetchone()
        return {"archived": a[0], "archived_tokens": a[1], "masked": m[0], "masked_tokens": m[1],
                "masked_saved_est": m[2], "segments": len(self.segments(conv))}


@dataclasses.dataclass
class Prepared:
    conv: str
    messages: list            # prompt fisico (lista messaggi OpenAI da mandare a Strata)
    tools: list | None
    hs: list[str]
    seg: int                  # 0 = nessun segmento
    est_tokens: int           # stima del prompt fisico
    virtual_tokens: int       # stima della storia completa del client (senza masking/segmenti)
    events: list[dict]
    masked: int
    masked_tokens: int            # token ARCHIVIATI dei pezzi nascosti (testo originale)
    masked_saved: int = 0         # riduzione fisica misurata (somma dei Δ dei pacchetti)
    catalog: dict = dataclasses.field(default_factory=dict)      # strumenti caricabili con lo strumento tools
    invalidated: dict = dataclasses.field(default_factory=dict)  # token di cache invalidati e causa
    recall_name: str = RECALL_NAME     # nomi degli strumenti del proxy in questa conversazione
    tools_name: str = TOOLS_NAME
    recall_aliases: frozenset = frozenset((RECALL_NAME, LEGACY_RECALL_NAME))  # chiamate risolte come recall
    tools_aliases: frozenset = frozenset((TOOLS_NAME, LEGACY_TOOLS_NAME))


class Manager:
    """Decide e applica masking/segmenti; esegue recall. `upstream` (facoltativo) serve solo per le note,
    il sigillo e il SAVE: oggetto con .chat(body)->dict e .slot(action, filename)->dict."""

    def __init__(self, cfg: Config, store: Store, counter: TokenCounter, journal: Journal):
        self.cfg, self.store, self.tc, self.journal = cfg, store, counter, journal
        self.kw: dict = {}        # kwargs del template della richiesta corrente (reasoning_effort, ...)
        self._fileops_seen: dict[str, int] = {}
        self.shorten_tools = ToolShortener(cfg.tooldefs_desc_max, cfg.tooldefs_param_max, cfg.tooldefs_keep)
        self.pager = ToolPager(tuple(cfg.tools_core) + (RECALL_NAME, LEGACY_RECALL_NAME, TOOLS_NAME, LEGACY_TOOLS_NAME,
                                                        cfg.recall_tool_name, cfg.tools_tool_name))
        # nomi degli strumenti del proxy nella conversazione corrente (impostati da prepare)
        self.rn, self.tn = cfg.recall_tool_name or RECALL_NAME, cfg.tools_tool_name or TOOLS_NAME
        self._last_phys: dict = {}   # conv -> ultimo prompt fisico (misura dei token di cache invalidati)

    # ---------- stima ----------
    def piece_tokens(self, text: str) -> int:
        from .render import VISION
        return self.tc.raw(text.replace(VISION, "")) + text.count(VISION) * self.cfg.image_tokens

    def msg_tokens(self, m: dict, keep_reasoning: bool = True) -> int:
        if self.tc.exact:
            try:
                return self.piece_tokens(message_piece(m, keep_thinking=keep_reasoning))
            except ValueError:
                pass
        n = self.cfg.msg_overhead + self.tc.count(content_text(m.get("content")))
        n += n_images(m.get("content")) * self.cfg.image_tokens
        for c in m.get("tool_calls") or []:
            f = c.get("function") or {}
            a = f.get("arguments")
            n += 8 + self.tc.count((f.get("name") or "") + (a if isinstance(a, str) else json.dumps(a, ensure_ascii=False)))
        if keep_reasoning and isinstance(m.get("reasoning_content"), str):
            n += self.tc.count(m["reasoning_content"]) + 4
        return n

    def tools_tokens(self, tools) -> int:
        return self.tc.count(json.dumps(tools, ensure_ascii=False)) if tools else 0

    def estimate(self, msgs: list, tools) -> tuple[int, list[int]]:
        """-> (totale, token per messaggio). Con il tokenizer del pack: conteggio ESATTO del prompt che Strata
        tokenizza (render_pieces = template Jinja vero, verificato su 421 richieste di nibble2)."""
        if self.tc.exact:
            try:
                per, fixed = [0] * len(msgs), 0
                for i, t in render_pieces(msgs, tools, self.kw):
                    n = self.piece_tokens(t)
                    if i is None:
                        fixed += n
                    else:
                        per[i] += n
                return fixed + sum(per), per
            except ValueError:
                pass
        last_user = max((i for i, m in enumerate(msgs) if is_human_user(m)), default=-1)
        per = [self.msg_tokens(m, not self.cfg.reasoning_last_turn_only or i > last_user) for i, m in enumerate(msgs)]
        return self.tools_tokens(tools) + sum(per) + 3, per

    # ---------- costruzione del prompt fisico ----------
    def out_placeholder(self, info: dict | None, orig: str, n: int, rid: str, rid_args: str | None = None) -> str:
        """Segnaposto di un'uscita nascosta: ricevuta tipizzata (paging.typed_receipt) o, se il tipo non è
        riconosciuto o l'opzione è spenta, l'inizio vero dell'uscita. Funzione pura: stesso pezzo -> stesso testo."""
        if self.cfg.typed_receipts:
            failed = outcome((info or {}).get("name") or "", orig) == "fallito"
            kind, rec = typed_receipt(info, orig, failed, rid_args, self.rn)
            if rec:
                return OUT_RECEIPT.format(origin=origin_text(info), n=n, rid=rid, receipt=rec, rn=self.rn)
        return OUT_PLACEHOLDER.format(origin=origin_text(info), n=n, rid=rid, rn=self.rn,
                                      head=head_text(orig).replace("\u00bb", "\"").replace("\u00ab", "\""))
    def _segment_for(self, conv: str, hs: list[str]):
        segs = self.store.segments(conv) if conv else []
        for seg, cut_idx, cut_h, notes_msg, kind in segs:
            if 0 < cut_idx <= len(hs) and hs[cut_idx - 1] == cut_h:
                return seg, cut_idx, notes_msg, kind, bool(segs)
        return None, 0, None, None, bool(segs)

    def build(self, msgs: list, hs: list[str], start: int, notes_msg: str | None, pins_msg: str | None = None):
        """-> (fisico, origine) dove origine[j] = indice client del messaggio fisico j, o None (testa/interni)."""
        masks = self.store.masks_for(hs[start:] + [h + THINK_SUFFIX for h in hs[start:]]
                                     + [h + ARGS_SUFFIX for h in hs[start:]] + [h + DROP_SUFFIX for h in hs[start:]])
        inserts = self.store.inserts_for(hs[start:] + [h + AUTO_SUFFIX for h in hs[start:]])
        phys, origin = [], []
        if notes_msg is not None:
            if msgs and msgs[0].get("role") in ("system", "developer"):
                phys.append(msgs[0]); origin.append(0)
            if pins_msg:
                phys.append({"role": "user", "content": pins_msg}); origin.append(None)
            phys.append({"role": "user", "content": notes_msg}); origin.append(None)
        strip = None
        pend_notes = {}
        calls: dict = {}          # tool_call_id -> call_info (provenienza delle uscite)
        call_aid: dict = {}       # tool_call_id -> id d'archivio degli argomenti
        hidden_to = -1            # scambio usa e getta nascosto: messaggi client fino a questo indice esclusi
        for i in range(start, len(msgs)):
            m = msgs[i]
            if i < hidden_to:
                continue
            if m.get("role") == "assistant":
                calls.update(call_info(m))
                if m.get("tool_calls"):
                    a_id = aid_for(i, m)
                    call_aid.update({c.get("id"): a_id for c in m["tool_calls"]})
            dk = masks.get(hs[i] + DROP_SUFFIX)
            if dk and is_human_user(m):
                end = self.exchange_end(msgs, i)
                fx = self.exchange_receipts(msgs, i, end)
                n_hidden = sum(self.msg_tokens(x, False) for x in msgs[i + 1:end])
                m = {**m, "content": content_text(m.get("content")) +
                     DROP_NOTE.format(n=n_hidden, fx=fx or "nessuna chiamata", rid=dk[0], rn=self.rn)}
                phys.append(m); origin.append(i)
                hidden_to = end
                strip = None
                continue
            mk = masks.get(hs[i])
            if mk and m.get("role") == "tool":
                info = calls.get(m.get("tool_call_id"))
                orig = content_text(msgs[i].get("content"))
                m = {**m, "content": self.out_placeholder(info, orig, mk[1], mk[0],
                                                          call_aid.get(m.get("tool_call_id")))}
            if m.get("role") == "tool" and m.get("tool_call_id") in pend_notes:
                c0 = m.get("content")
                name, path, rid = pend_notes.pop(m["tool_call_id"])
                res = content_text(msgs[i].get("content"))
                m = {**m, "content": call_note_text(name, path, rid, res, self.rn) +
                     (c0 if isinstance(c0, str) else content_text(c0))}
            tk = masks.get(hs[i] + THINK_SUFFIX)
            if tk and m.get("role") == "assistant" and isinstance(m.get("reasoning_content"), str):
                # fix live 4/10: niente segnaposto nel ragionamento (il modello lo imitava al posto di ragionare);
                # il ragionamento vecchio sparisce e basta, resta in archivio
                m = {**m, "reasoning_content": ""}
            if m.get("role") == "assistant" and isinstance(m.get("reasoning_content"), str) \
                    and m["reasoning_content"].lstrip().startswith("[ragionamento omesso:"):
                m = {**m, "reasoning_content": ""}   # segnaposti finti scritti dal modello per imitazione
            ak = masks.get(hs[i] + ARGS_SUFFIX)
            if ak and m.get("role") == "assistant" and m.get("tool_calls"):
                pend_notes.update(call_notes(m, ak[0]))
                m = {**m, "tool_calls": masked_calls(m, ak[0], ak[1])}
            if m.get("role") == "assistant" and m.get("tool_calls") and _FAKE_OMIT.search(json.dumps(m["tool_calls"])):
                # segnaposti imitati dal modello dentro i propri comandi: via (non devono fare da esempio)
                m = {**m, "tool_calls": [{**c, "function": {**(c.get("function") or {}), "arguments":
                     _FAKE_OMIT.sub("…", (c.get("function") or {}).get("arguments") or "")}}
                     for c in m["tool_calls"]]}
            if strip and m.get("role") == "assistant":
                m = dict(m)
                c = content_text(m.get("content"))
                if strip["strip_content"] and c.startswith(strip["strip_content"]):
                    m["content"] = c[len(strip["strip_content"]):]
                r = m.get("reasoning_content")
                if strip["strip_reasoning"] and isinstance(r, str) and r.startswith(strip["strip_reasoning"]):
                    m["reasoning_content"] = r[len(strip["strip_reasoning"]):]
            strip = None
            phys.append(m); origin.append(i)
            ains = inserts.get(hs[i] + AUTO_SUFFIX)
            if ains:                   # richiamo automatico deciso a quel turno: reinserito identico
                for x in ains["msgs"]:
                    phys.append(x); origin.append(None)
            ins = inserts.get(hs[i])
            if ins:
                for x in ins["msgs"]:
                    phys.append(x); origin.append(None)
                strip = ins
        return phys, origin

    @staticmethod
    def exchange_end(msgs: list, i: int) -> int:
        """Fine (esclusa) dello scambio che inizia col messaggio utente i: fino al prossimo messaggio utente umano."""
        for k in range(i + 1, len(msgs)):
            if is_human_user(msgs[k]):
                return k
        return len(msgs)

    @staticmethod
    def exchange_receipts(msgs: list, i: int, end: int) -> str:
        """Ricevute di una riga per le chiamate di uno scambio: «bash `node serve.mjs`: riuscito; write a.js: …»."""
        calls, out = {}, []
        for m in msgs[i:end]:
            if m.get("role") == "assistant":
                calls.update(call_info(m))
            elif m.get("role") == "tool":
                out.append(receipt(calls.get(m.get("tool_call_id")), content_text(m.get("content"))))
        if len(out) > 8:
            out = out[:7] + ["altre %d chiamate" % (len(out) - 7)]
        return "; ".join(out)

    # ---------- passo principale ----------
    def prepare(self, req: dict, upstream=None, hint: str | None = None) -> Prepared:
        cfg = self.cfg
        msgs = list(req.get("messages") or [])
        client_tools = req.get("tools") or None
        hs = chain(msgs, client_tools)
        events: list[dict] = []
        conv, known = self.store.find_conv(hs)
        if hint:
            conv = "c-" + H(hint)[:16]
        existed = conv is not None and self.store.has_conv(conv)   # già nell'archivio prima di questa richiesta
        if conv is None:
            conv = "c" + uuid.uuid4().hex[:12]
            events.append(self.journal.log("new_conversation", conv=conv, messages=len(msgs)))
        elif known < len(hs) - 1 and known >= 0 and self.store.has_successor(conv, known):
            # un messaggio in posizione known+1 era già stato visto, ma diverso: modifica/rigenerazione = ramo
            events.append(self.journal.log("branch", conv=conv, known=known, messages=len(msgs)))
        self.store.add_chain(conv, hs)
        now = time.time()
        rows = [(rid_for(i, m), conv, i, m.get("role"), m.get("name") or m.get("tool_call_id"),
                 content_text(m.get("content")), self.msg_tokens(m, False), now) for i, m in enumerate(msgs)]
        rows += [(tid_for(i, m), conv, i, "assistant-reasoning", None, m["reasoning_content"],
                  self.tc.count(m["reasoning_content"]), now)
                 for i, m in enumerate(msgs) if m.get("role") == "assistant" and m.get("reasoning_content")]
        rows += [(aid_for(i, m), conv, i, "assistant-tool-args", None, args_text(m), self.tc.count(args_text(m)), now)
                 for i, m in enumerate(msgs) if m.get("role") == "assistant" and m.get("tool_calls")]
        self.store.archive_many(rows)
        self.kw = template_kwargs(req)
        events.extend(self._scan_marks(conv, msgs, hs))
        events.extend(self._scan_fileops(conv, msgs))

        names = self.tool_names(conv, client_tools, legacy=existed)
        self.rn, self.tn = names["recall"], names["tools"]
        tools = list(self.shorten_tools(client_tools) or [])
        catalog: dict = {}
        if cfg.tools_paging:
            tools, catalog = self.pager.split(tools)
        if cfg.inject_recall and not any((t.get("function") or {}).get("name") == self.rn for t in tools):
            from .recall2 import recall_tool
            tools.append(recall_tool(cfg, recall_tool_def(self.rn)))
        if catalog:
            tools.append(self.pager.tools_tool(catalog, self.tn))

        seg, start, notes_msg, _, had_segments = self._segment_for(conv, hs)
        if upstream is not None and hasattr(upstream, "ctx"):
            upstream.ctx = (conv, seg or 0)     # autosave.Tracker: a chi appartiene ciò che Strata legge ora
        if had_segments and seg is None:
            events.append(self.journal.log("segment_miss", conv=conv,
                                           note="storia modificata prima del taglio: segmenti non applicabili"))
        pins_msg = self.store.segment_pins(conv, seg) if seg else None
        phys, origin = self.build(msgs, hs, start, notes_msg, pins_msg)
        est, per = self.estimate(phys, tools)
        virtual, _ = self.estimate(msgs, client_tools)

        # ---- livello 1: masking a pacchetti ----
        if cfg.mask_enabled and est > cfg.mask_trigger:
            ev = self._mask_batch(conv, msgs, hs, phys, origin, per, est, seg or 0)
            if ev:
                phys, origin = self.build(msgs, hs, start, notes_msg, pins_msg)
                est2, per = self.estimate(phys, tools)
                # Δ MISURATO sulla stessa richiesta con/senza il pacchetto (GPT-ANSWER3 passo 1)
                ev["tokens_saved_measured"] = est - est2
                ev["est_after_measured"] = est2
                self.journal.log("mask_measured", conv=conv, seg=seg or 0, est_before=est, est_after=est2,
                                 saved=est - est2, saved_planned=ev.get("tokens_saved"))
                est = est2
                events.append(ev)
                if cfg.mask_anchor and upstream is not None and cfg.slot_save:
                    events.extend(self._anchor(conv, seg or 0, req, phys, origin, per, tools, ev, upstream))

        # ---- livello 2: segmenti ----
        resp = int(req.get("max_tokens") or req.get("max_completion_tokens") or cfg.default_response)
        if resp <= 0:
            resp = cfg.default_response
        resp = max(resp, cfg.response_floor)
        if cfg.segments_enabled and est + resp + cfg.reserve + 8 > cfg.window and upstream is not None:
            ev = self._switch(conv, req, msgs, hs, phys, origin, per, tools, seg or 0, upstream, est, resp)
            events.extend(ev)
            seg, start, notes_msg, _, _ = self._segment_for(conv, hs)
            pins_msg = self.store.segment_pins(conv, seg) if seg else None
            phys, origin = self.build(msgs, hs, start, notes_msg, pins_msg)
            est, per = self.estimate(phys, tools)

        # ---- richiamo automatico (spento di serie): pezzi nascosti pertinenti in coda, deciso una volta ----
        if cfg.auto_recall and msgs and len(hs) == len(msgs):
            ev = self._auto_recall(conv, msgs, hs, start, phys, est, resp)
            if ev is not None:
                events.append(ev)
                if ev.get("injected"):
                    phys, origin = self.build(msgs, hs, start, notes_msg, pins_msg)
                    est, per = self.estimate(phys, tools)

        masks = self.store.masks_for(hs + [h + THINK_SUFFIX for h in hs] + [h + ARGS_SUFFIX for h in hs]
                                     + [h + DROP_SUFFIX for h in hs])
        inv = self._invalidation(conv, phys, tools, per, est, events)
        for ev in events:
            if ev.get("event") == "mask":
                # regola di convenienza (ricerca §2): S = token da rileggere, Δ = token tolti; conviene se il
                # risparmio dura più di S/Δ turni. Solo misura.
                d = ev.get("tokens_saved_measured") or ev.get("tokens_saved") or 0
                s_meas = inv.get("tokens") if inv.get("cause") == "blocco" else None
                rec = {"conv": conv, "seg": seg or 0, "S_planned": ev.get("reread_est"), "S_measured": s_meas,
                       "delta": d, "ratio_planned": round(ev.get("reread_est", 0) / d, 3) if d else None,
                       "ratio_measured": round(s_meas / d, 3) if d and s_meas is not None else None,
                       "first_index": ev.get("first_index"), "count": ev.get("count")}
                ev["s_over_delta"] = rec["ratio_measured"] if rec["ratio_measured"] is not None \
                    else rec["ratio_planned"]
                self.journal.log("mask_cost", **rec)
        return Prepared(conv=conv, messages=phys, tools=tools, hs=hs, seg=seg or 0, est_tokens=est,
                        virtual_tokens=virtual, events=events, masked=len(masks),
                        masked_tokens=sum(n for _, n in masks.values()),
                        masked_saved=self.store.masks_saved(list(masks)), catalog=catalog,
                        invalidated=inv, recall_name=self.rn, tools_name=self.tn,
                        recall_aliases=self.call_aliases("recall", client_tools),
                        tools_aliases=self.call_aliases("tools", client_tools))

    # ---------- nomi degli strumenti del proxy (0.2.0) ----------
    def tool_names(self, conv: str, client_tools, legacy: bool = False) -> dict:
        """Nomi di recall/tools per la conversazione. La scelta di base si fa alla prima richiesta e si ricorda (kv),
        così il prompt resta identico fra richieste e fra riavvii: conversazione già nell'archivio senza nomi
        registrati = nata prima della 0.2.0 -> tiene strata_recall/strata_tools (prefisso, cache e salvataggi restano
        validi); conversazione nuova -> recall_tool_name/tools_tool_name. Poi, funzione pura degli strumenti del
        client: se il client dichiara già strata_recall/strata_tools si usano quelli (comportamento 0.1); se dichiara
        uno strumento con il nome scelto, si passa a un nome alternativo invece di sovrascrivere il suo."""
        taken = {(t.get("function") or {}).get("name") for t in client_tools or [] if isinstance(t, dict)}
        key = NAMES_KEY + conv
        raw = self.store.kv_get(key)
        base = None
        if raw:
            try:
                base = json.loads(raw)
            except ValueError:
                base = None
        if not isinstance(base, dict) or not base.get("recall") or not base.get("tools"):
            if legacy:
                base = {"recall": LEGACY_RECALL_NAME, "tools": LEGACY_TOOLS_NAME}
            else:
                base = {"recall": self.cfg.recall_tool_name or RECALL_NAME,
                        "tools": self.cfg.tools_tool_name or TOOLS_NAME}
            self.store.kv_set(key, json.dumps(base, sort_keys=True))
        out = dict(base)
        for k, legacy_name, alts in (("recall", LEGACY_RECALL_NAME, RECALL_ALTERNATES),
                                     ("tools", LEGACY_TOOLS_NAME, TOOLS_ALTERNATES)):
            if legacy_name in taken:
                out[k] = legacy_name
            elif out[k] in taken:
                out[k] = next(n for n in alts + ("%s_%s" % (k, H(conv)[:6]),) if n not in taken)
                if raw is None:
                    self.journal.log("tool_name_collision", conv=conv, kind=k, client=base[k], used=out[k])
        return out

    def call_aliases(self, kind: str, client_tools) -> frozenset:
        """Nomi di chiamata risolti dal proxy come recall (o tools): quello della conversazione più i nomi storici,
        esclusi quelli che il client dichiara come propri (quelle chiamate sono sue). Il nome storico strata_* resta
        sempre del proxy (come nella 0.1)."""
        taken = {(t.get("function") or {}).get("name") for t in client_tools or [] if isinstance(t, dict)}
        if kind == "recall":
            cur, legacy, cands = self.rn, LEGACY_RECALL_NAME, (RECALL_NAME, self.cfg.recall_tool_name)
        else:
            cur, legacy, cands = self.tn, LEGACY_TOOLS_NAME, (TOOLS_NAME, self.cfg.tools_tool_name)
        return frozenset({cur, legacy} | {n for n in cands if n and n not in taken})

    # ---------- misura: token di cache invalidati (ricerca §21) ----------
    def _invalidation(self, conv, phys, tools, per, est, events) -> dict:
        """Confronto col prompt fisico precedente della stessa conversazione: token del prompt precedente che stanno
        dopo la prima differenza (quindi persi dalla cache a prefisso e da rileggere). Causa: tipo del primo
        messaggio diverso / eventi di questa richiesta."""
        cur = [canon(m) + "\x00" + (m.get("reasoning_content") or "") for m in phys]
        th = H(json.dumps(tools or [], sort_keys=True, ensure_ascii=False), json.dumps(self.kw, sort_keys=True))
        prev = self._last_phys.get(conv)
        self._last_phys[conv] = (cur, list(per), est, th, phys)
        if len(self._last_phys) > 64:
            self._last_phys.pop(next(iter(self._last_phys)))
        if prev is None:
            return {"tokens": 0, "cause": "prima", "first_diff": None}
        pcur, pper, pest, pth, pphys = prev
        if pth != th:
            cause = "strumenti" if self.cfg.tools_paging else "testa"
            return {"tokens": pest, "cause": cause, "first_diff": 0}
        k = 0
        for a, b in zip(pcur, cur):
            if a != b:
                break
            k += 1
        if k >= len(pcur):
            return {"tokens": 0, "cause": None, "first_diff": None}
        lost = sum(pper[k:])
        names = {e.get("event") for e in events}

        def kind(m):
            c = content_text((m or {}).get("content"))
            if c.startswith(AUTO_HEAD):
                return "richiamo_automatico"
            if m and m.get("role") == "tool" and is_tools_result(c):
                return "strumenti"
            return None
        cause = kind(pphys[k] if k < len(pphys) else None) or kind(phys[k] if k < len(phys) else None)
        if cause is None:
            cause = "segmento" if "switch" in names else "blocco" if "mask" in names else \
                "client" if names & {"branch", "new_conversation"} else "altro"
        return {"tokens": lost, "cause": cause, "first_diff": k}

    # ---------- richiamo automatico (ricerca §6-8) ----------
    def _hidden_rids(self, msgs, hs, start) -> set:
        """id d'archivio dei pezzi della storia corrente che NON sono nel prompt fisico: uscite/ragionamenti/argomenti
        nascosti (tabella masks per questa catena) e messaggi prima del taglio di segmento."""
        masks = self.store.masks_for(hs + [h + THINK_SUFFIX for h in hs] + [h + ARGS_SUFFIX for h in hs])
        out = {rid for rid, _ in masks.values()}
        for i in range(1, min(start, len(msgs))):
            m = msgs[i]
            out.add(rid_for(i, m))
            if m.get("role") == "assistant":
                if m.get("reasoning_content"):
                    out.add(tid_for(i, m))
                if m.get("tool_calls"):
                    out.add(aid_for(i, m))
        return out

    def _auto_query(self, msgs, trigger_idx) -> tuple[str, list[str]]:
        """Query deterministica: richiesta corrente, file toccati di recente, errori recenti, ultimo comando."""
        user = ""
        for k in range(trigger_idx, -1, -1):
            if is_human_user(msgs[k]):
                user = content_text(msgs[k].get("content"))
                break
        files, errors, last_cmd, calls = [], [], "", {}
        lo = max(0, trigger_idx - 40)
        for m in msgs[lo:trigger_idx + 1]:
            if m.get("role") == "assistant":
                ci = call_info(m)
                calls.update(ci)
                for c in ci.values():
                    if c["path"] and c["path"] not in files:
                        files.append(c["path"])
                    if c["cmd"]:
                        last_cmd = c["cmd"]
            elif m.get("role") == "tool":
                info = calls.get(m.get("tool_call_id"))
                txt = content_text(m.get("content"))
                if outcome((info or {}).get("name") or "", txt) == "fallito":
                    errors.append(next((l for l in txt.splitlines() if _FAIL.search(l)), txt[:200])[:200])
        if is_human_user(msgs[trigger_idx]):
            errors = errors[-1:]
        terms = query_terms(user, files[-4:][::-1], errors[-2:], last_cmd)
        return user, terms

    def auto_candidates(self, conv, terms, hidden, already, max_idx) -> list:
        """Candidati del richiamo automatico: pezzi NASCOSTI con score = -bm25 (più alto = migliore) sopra
        auto_recall_min_score e almeno auto_recall_min_terms termini non comuni. -> [(rid, idx, role, name,
        content, tokens, score, matched)] in ordine di score."""
        cfg = self.cfg
        hits = self.store.search_fts_scored(conv, terms, limit=80, max_idx=max_idx) if hidden and terms else []
        pre = []
        for rid, idx, role, name, content, tokens, score in hits:
            if rid not in hidden or rid in already or not content:
                continue
            low = content.lower()
            pre.append((rid, idx, role, name, content, tokens, score, [t for t in terms if t in low]))
        # termini comuni (presenti in gran parte dei pezzi trovati: percorso del progetto, 'node', 'game', 'ogni'…)
        # non distinguono un pezzo dall'altro: non contano per la soglia dei termini
        df = {}
        for c in pre:
            for t in c[7]:
                df[t] = df.get(t, 0) + 1
        common = {t for t, n in df.items() if len(pre) >= 8 and n > cfg.auto_recall_common_frac * len(pre)}
        cands, seen_txt = [], set()
        for rid, idx, role, name, content, tokens, score, matched in pre:
            distinct = [t for t in matched if t not in common]
            if score < cfg.auto_recall_min_score or len(distinct) < cfg.auto_recall_min_terms:
                continue
            h = hashlib.sha1(content.strip().encode("utf-8", "replace")).hexdigest()
            if h in seen_txt:                       # stesso testo già scelto (comandi ripetuti identici)
                continue
            seen_txt.add(h)
            cands.append((rid, idx, role, name, content, tokens, score, distinct + [t for t in matched if t in common]))
        return cands

    def auto_hint(self, cands, terms) -> tuple[str, list]:
        """Indizio al posto dei pezzi: una riga con i termini trovati e id/messaggi dei candidati migliori (nessun
        testo dell'archivio). -> (testo, scelti)."""
        cfg = self.cfg
        what = {"tool": "uscita", "assistant-reasoning": "ragionamento", "assistant-tool-args": "argomenti",
                "user": "utente", "assistant": "risposta"}
        chosen = [{"rid": c[0], "idx": c[1], "role": c[2], "score": round(c[6], 2), "matched": c[7][:6]}
                  for c in cands[:cfg.auto_recall_hint_k]]
        if not chosen:
            return "", []
        words = []
        for c in chosen:
            for t in c["matched"]:
                if t not in words:
                    words.append(t)
        text = (AUTO_HEAD + " Nell'archivio NASCOSTO di questa conversazione ci sono pezzi su: %s (%s). Se servono "
                "per la richiesta, usa %s id=<id> (testo intero) o query=...; non ricostruirli a memoria."
                % (", ".join(words[:8]),
                   "; ".join("id=%s msg %d %s" % (c["rid"], c["idx"] + 1, what.get(c["role"], c["role"]))
                             for c in chosen),
                   self.rn))
        return text, chosen

    def _auto_recall(self, conv, msgs, hs, start, phys, est, resp) -> dict | None:
        cfg = self.cfg
        i = len(msgs) - 1
        last = msgs[i]
        trig = None
        if is_human_user(last):
            trig = "utente"
        elif cfg.auto_recall_on_error and last.get("role") == "tool":
            ci = {}
            for m in msgs[max(0, i - 8):i]:
                if m.get("role") == "assistant":
                    ci.update(call_info(m))
            info = ci.get(last.get("tool_call_id"))
            if outcome((info or {}).get("name") or "", content_text(last.get("content"))) == "fallito":
                trig = "errore"
        if trig is None:
            return None
        key = hs[i] + AUTO_SUFFIX
        if self.store.inserts_for([key]):
            return None                     # già deciso a una richiesta precedente: build() lo reinserisce
        user, terms = self._auto_query(msgs, i)
        hidden = self._hidden_rids(msgs, hs, start)
        already = set()
        for m in phys:
            c = content_text(m.get("content"))
            if c.startswith(AUTO_HEAD) or c.startswith("[recall:"):
                already.update(re.findall(r"id=([rta][0-9a-f]{12})", c))
        t0 = time.time()
        cands = self.auto_candidates(conv, terms, hidden, already, i)
        room = cfg.window - cfg.reserve - resp - est - 64
        budget = min(cfg.auto_recall_max_tokens, max(0, room))
        if cfg.auto_recall_hint:
            text, chosen = self.auto_hint(cands, terms)
            used = self.tc.count(text) if chosen else 0
            self.store.add_insert(key, conv, [{"role": "user", "content": text}] if chosen else [])
            return self.journal.log("auto_recall", conv=conv, trigger=trig, index=i, terms=terms, mode="hint",
                                    candidates=len(cands), hidden=len(hidden), injected=len(chosen), tokens=used,
                                    pieces=chosen, budget=budget, ms=round((time.time() - t0) * 1000, 1),
                                    last_user=user[:300])
        n_args = 0
        head = (AUTO_HEAD + " Pezzi della parte NASCOSTA di questa conversazione che potrebbero servire per la "
                "richiesta qui sopra (trovati dal gestore del contesto, non scritti dall'utente). Testo vero, "
                "eventualmente estratto; testo intero con %s id=<id>. Lo stato attuale dei file può "
                "essere diverso: se serve, rileggili." % self.rn)
        blocks, used, chosen = [], self.tc.count(head), []
        for rid, idx, role, name, content, tokens, score, matched in cands[:cfg.auto_recall_k * 3]:
            if len(chosen) >= cfg.auto_recall_k:
                break
            if role == "assistant-tool-args":       # argomenti di chiamata: al massimo N per iniezione
                if n_args >= cfg.auto_recall_max_args:
                    continue
                n_args += 1
            low = content.lower()
            p = min([low.find(t) for t in matched if low.find(t) >= 0], default=0)
            s = content[max(0, p - 300):p + 2400]
            what = {"tool": "uscita di strumento", "assistant-reasoning": "ragionamento",
                    "assistant-tool-args": "argomenti di chiamata", "user": "messaggio dell'utente",
                    "assistant": "risposta dell'assistente"}.get(role, role)
            b = "\n--- id=%s · messaggio %d (%s) · %d token%s\n" % (
                rid, idx + 1, what, tokens, "" if len(s) >= len(content) else " · estratto")
            b += self.fit_tokens(s, cfg.auto_recall_piece_tokens) if self.tc.count(s) > cfg.auto_recall_piece_tokens \
                else s
            t = self.tc.count(b)
            if used + t > budget:
                continue
            blocks.append(b)
            used += t
            chosen.append({"rid": rid, "idx": idx, "role": role, "score": round(score, 2), "tokens": t,
                           "matched": matched[:8]})
        msgs_ins = [{"role": "user", "content": head + "".join(blocks)}] if chosen else []
        self.store.add_insert(key, conv, msgs_ins)
        return self.journal.log("auto_recall", conv=conv, trigger=trig, index=i, terms=terms,
                                candidates=len(cands), hidden=len(hidden), injected=len(chosen),
                                tokens=used if chosen else 0, pieces=chosen, budget=budget,
                                ms=round((time.time() - t0) * 1000, 1), last_user=user[:300])

    # ---------- registro delle operazioni sui file ----------
    def _scan_fileops(self, conv: str, msgs: list) -> list:
        """Registro file (operazione, esito, id di argomenti e uscita) per recall path=… e, come segnali per il
        futuro selettore, riletture dello stesso file e modifiche fallite (eventi `reread`, `edit_failed`)."""
        calls, rows = {}, []
        for i, m in enumerate(msgs):
            if m.get("role") == "assistant":
                ci = call_info(m)
                for k in ci:
                    ci[k]["idx"], ci[k]["rid_args"] = i, aid_for(i, m)
                calls.update(ci)
            elif m.get("role") == "tool":
                info = calls.get(m.get("tool_call_id"))
                if not info or not info["path"]:
                    continue
                res = content_text(m.get("content"))
                rows.append((conv, i, m.get("tool_call_id") or "", info["name"], info["path"],
                             outcome(info["name"], res), info["rid_args"], rid_for(i, m), head_text(res, 160)))
        if not rows or not self.store.add_fileops(rows):
            return []
        # eventi solo per le operazioni nuove dell'ultima richiesta (le precedenti sono già nel giornale)
        events, last = [], {}
        seen = self._fileops_seen.get(conv, -1)
        with self.store.lock:
            allops = self.store.db.execute("SELECT idx, op, path, outcome FROM fileops WHERE conv=? ORDER BY idx",
                                           (conv,)).fetchall()
        for idx, op, path, out in allops:
            prev = last.get(path)
            if idx > seen:
                if op == "read" and prev is not None:
                    events.append(self.journal.log("reread", conv=conv, index=idx, path=path, prev_index=prev[0],
                                                   prev_op=prev[1], turns_since=idx - prev[0]))
                if op in ("edit", "write") and out == "fallito":
                    events.append(self.journal.log("edit_failed", conv=conv, index=idx, path=path, op=op))
            last[path] = (idx, op)
        self._fileops_seen[conv] = max(r[1] for r in rows)
        return events

    # ---------- 📌 / 🗑 dai messaggi dell'utente ----------
    def _scan_marks(self, conv: str, msgs: list, hs: list[str]) -> list:
        """Registra i punti fermi e gli scambi usa e getta scritti dall'utente (etichette umane per il selettore,
        testo completo nell'archivio). Idempotente: ogni richiesta rivede la storia intera."""
        events = []
        drops = self.store.drops(conv)
        for i, m in enumerate(msgs):
            if not is_human_user(m):
                continue
            t = content_text(m.get("content"))
            for p in pin_texts(t):
                if not self.store.has_pin(conv, i, p):
                    pid = self.store.add_pin(conv, i, p)
                    events.append(self.journal.log("pin", conv=conv, id=pid, index=i, source="testo", text=p,
                                                   tokens=self.tc.count(p)))
            if is_disposable(t) and i not in drops:
                self.store.add_drop(conv, i)
                drops.add(i)
                events.append(self.journal.log("drop_mark", conv=conv, index=i, source="testo", text=t[:2000]))
        return events

    def _mask_batch(self, conv, msgs, hs, phys, origin, per, est, seg=0):
        cfg = self.cfg
        # frontiera monotona: con l'ancora attiva un pacchetto non tocca mai messaggi prima della frontiera del
        # pacchetto precedente (il prefisso salvato nell'ancora resta identico)
        anc = self.store.anchor(conv, seg) if cfg.mask_anchor else None
        floor = anc[0] if anc else 0
        # coda protetta INCLUSIVA: gli ultimi keep_recent_tokens del prompt fisico più il messaggio che attraversa
        # il confine (GPT-ANSWER3: prima il messaggio a cavallo restava scoperto)
        protected, acc = set(), 0
        for j in range(len(phys) - 1, -1, -1):
            protected.add(j)
            acc += per[j]
            if acc > cfg.keep_recent_tokens:
                break
        # età in turni: numero di assistant successivi nella storia client
        after = [0] * (len(msgs) + 1)
        for i in range(len(msgs) - 1, -1, -1):
            after[i] = after[i + 1] + (1 if msgs[i].get("role") == "assistant" else 0)
        pinned = {r[1] for r in self.store.pins(conv)}          # messaggi con 📌: mai nascosti
        drops = self.store.drops(conv)
        exact = self.tc.exact
        calls: dict = {}
        call_aid: dict = {}
        for k, mm in enumerate(msgs):
            if mm.get("role") == "assistant":
                calls.update(call_info(mm))
                if mm.get("tool_calls"):
                    a_id = aid_for(k, mm)
                    call_aid.update({c.get("id"): a_id for c in mm["tool_calls"]})
        phys_of = {i: j for j, i in enumerate(origin) if i is not None}
        cand = []  # (indice client, indice fisico, chiave, id, token archiviati, risparmio, meta)
        young = None  # primo messaggio ancora non mascherabile per età/coda: un pacchetto futuro potrà toccarlo
        skip_to = -1
        for j, i in enumerate(origin):
            if i is None or i < skip_to:
                continue
            if j in protected or after[i] < cfg.min_age_turns:
                if young is None:
                    young = i
                continue
            if i < floor or i in pinned:
                continue
            m = phys[j]
            age = after[i]
            # 🗑 scambio usa e getta: tutto lo scambio (domanda esclusa) diventa una ricevuta di una riga
            if i in drops and is_human_user(m):
                end = self.exchange_end(msgs, i)
                js = [phys_of[k] for k in range(i + 1, end) if k in phys_of]
                if js and all(x not in protected for x in js) and after[end - 1] >= cfg.min_age_turns \
                        and not any(k in pinned for k in range(i, end)):
                    before = sum(per[x] for x in js)
                    note = DROP_NOTE.format(n=before, fx=self.exchange_receipts(msgs, i, end) or "nessuna chiamata",
                                            rid=rid_for(i, msgs[i]), rn=self.rn)
                    sv = before - self.tc.count(note)
                    if sv > 0:
                        cand.append((i, j, hs[i] + DROP_SUFFIX, rid_for(i, msgs[i]), before, sv,
                                     {"tipo": "scambio", "fine": end, "eta": age}))
                        skip_to = end
                        continue
            if m.get("role") == "tool":
                txt = content_text(m.get("content"))
                if txt.startswith("[uscita strumento omessa:") or OUT_PREFIX in txt or RECEIPT_OPEN in txt:
                    continue
                n = self.msg_tokens(m, False)
                if n >= cfg.min_mask_tokens:
                    info = calls.get(m.get("tool_call_id"))
                    orig = content_text(msgs[i].get("content"))
                    # build() antepone la nota della chiamata anche al segnaposto: va contata in entrambi i lati
                    note = txt[:len(txt) - len(orig)] if txt.endswith(orig) and txt != orig else ""
                    n_orig = self.msg_tokens(msgs[i], False)
                    ph = {**m, "content": note + self.out_placeholder(info, orig, n_orig, rid_for(i, msgs[i]),
                                                                      call_aid.get(m.get("tool_call_id")))}
                    sv = n - self.msg_tokens(ph, False)
                    cand.append((i, j, hs[i], rid_for(i, msgs[i]), n_orig, sv,
                                 {"tipo": "uscita", "strumento": (info or {}).get("name"),
                                  "path": (info or {}).get("path") or None, "eta": age}))
            elif cfg.mask_reasoning and m.get("role") == "assistant" and isinstance(m.get("reasoning_content"), str) \
                    and m["reasoning_content"].strip():
                n = self.tc.count(m["reasoning_content"])
                if n >= cfg.min_mask_tokens:
                    # risparmio = Δ del frammento renderizzato (niente guadagni su campi già esclusi)
                    sv = (self.msg_tokens(m, True) - self.msg_tokens({**m, "reasoning_content": ""}, True)) \
                        if exact else n
                    if sv > 0:
                        cand.append((i, j, hs[i] + THINK_SUFFIX, tid_for(i, msgs[i]), n, sv,
                                     {"tipo": "ragionamento", "eta": age}))
            if cfg.mask_tool_args and m.get("role") == "assistant" and m.get("tool_calls") \
                    and '"_omesso"' not in args_text(m):
                n = self.tc.count(args_text(m))
                if n >= cfg.min_mask_tokens:
                    rid = aid_for(i, msgs[i])
                    m2 = {**m, "tool_calls": masked_calls(m, rid, n)}
                    # costo delle note che build() aggiunge nei risultati (una per chiamata accorciata, con l'esito
                    # letto dal risultato vero: il testo della nota cambia con l'esito)
                    res = {x.get("tool_call_id"): content_text(x.get("content"))
                           for x in msgs[i + 1:i + 1 + len(m["tool_calls"]) + 2] if x.get("role") == "tool"}
                    note_cost = sum(self.tc.count(call_note_text(nm, pa, rid, res.get(cid, ""), self.rn)) + 1
                                    for cid, (nm, pa, _) in call_notes(m, rid).items())
                    sv = self.msg_tokens(m, True) - self.msg_tokens(m2, True) - note_cost
                    if sv > 0:
                        ci = list(call_info(m).values())
                        cand.append((i, j, hs[i] + ARGS_SUFFIX, rid, n, sv,
                                     {"tipo": "argomenti", "strumento": ",".join(c["name"] for c in ci),
                                      "path": next((c["path"] for c in ci if c["path"]), None), "eta": age}))
        rows, saved, now, pieces = [], 0, time.time(), []
        align = int(cfg.checkpoint_align_tokens or 0)
        offs, acc = [], max(0, est - sum(per))   # testa fissa (strumenti, template) prima del primo messaggio
        for x in per:
            offs.append(acc)
            acc += x
        skipped = 0
        all_cand = cand
        if align > 0 and cand:
            # primo cambiamento poco dopo un multiplo di `align` (checkpoint del motore): si saltano i primi candidati
            # finché uno cade entro lo spreco accettato, purché i restanti bastino per un pacchetto
            for s, c in enumerate(cand):
                if sum(x[5] for x in cand[s:]) < cfg.min_batch_tokens:
                    break
                if offs[c[1]] % align <= cfg.checkpoint_align_slack * align:
                    skipped = s
                    break
            cand = cand[skipped:]
        for i, j, key, rid, n, sv, meta in cand:  # dal più vecchio
            if est - saved <= cfg.mask_target:
                break
            rows.append((key, conv, i, rid, n, now, sv))
            saved += sv
            pieces.append({"id": rid, "indice": i, "token": n, "risparmio": sv, **meta})
        if not rows or saved < cfg.min_batch_tokens:
            return None
        self.store.add_masks(rows)
        first = rows[0][2]
        rest = all_cand[:skipped] + cand[len(rows):]
        frontier = min([c[0] for c in rest] + ([young] if young is not None else []), default=len(msgs))
        reread = sum(per[j] for j, i in enumerate(origin) if i is not None and i >= first)
        extra = {"align": align, "align_skipped": skipped, "first_offset_est": offs[cand[0][1]]} if align else {}
        return self.journal.log("mask", conv=conv, seg=seg, count=len(rows), tokens_masked=sum(r[4] for r in rows),
                                tokens_saved=saved, est_before=est, est_after=est - saved,
                                first_index=first, frontier_index=frontier, floor_index=floor, reread_est=reread,
                                ids=[r[3] for r in rows][:50], pieces=pieces, **extra)

    # ---------- segmenti ----------
    def choose_cut(self, msgs, origin, per, start):
        """Indice client c da cui parte la coda di B. Preferenza: prima di un messaggio user umano con coda
        <= tail_max; altrimenti confine di gruppo (assistant dopo tool/user: chiamate e risultati completi).
        Mai fra un assistant con tool_calls e i suoi risultati."""
        tail_from = {}
        acc = 0
        for j in range(len(origin) - 1, -1, -1):
            acc += per[j]
            if origin[j] is not None:
                tail_from[origin[j]] = acc
        n = len(msgs)

        def ok_user(c):
            return is_human_user(msgs[c])

        def ok_group(c):
            return msgs[c].get("role") == "assistant" and msgs[c - 1].get("role") in ("tool", "user")

        for kind, pred in (("user", ok_user), ("group", ok_group)):
            for c in range(start + 1, n):
                if c in tail_from and tail_from[c] <= self.cfg.tail_max and pred(c):
                    return c, kind
        for c in range(n - 1, start, -1):  # coda minima comunque valida
            if ok_user(c) or ok_group(c):
                return c, "group-min"
        return None, None

    def archive_index(self, msgs, upto: int, conv: str) -> str:
        calls = {}
        for m in msgs[:upto]:
            for c in m.get("tool_calls") or []:
                f = c.get("function") or {}
                a = f.get("arguments")
                a = a if isinstance(a, str) else json.dumps(a, ensure_ascii=False)
                calls[c.get("id")] = "%s %s" % (f.get("name"), a.replace("\n", " ")[:70])
        lines, used = [], 0
        for i in range(upto - 1, -1, -1):
            m = msgs[i]
            if m.get("role") != "tool":
                continue
            n = self.msg_tokens(m, False)
            if n < self.cfg.min_mask_tokens:
                continue
            line = "- recall:%s \u00b7 %s \u00b7 %d token" % (rid_for(i, m), calls.get(m.get("tool_call_id"), "?"), n)
            t = self.tc.count(line)
            if used + t > self.cfg.index_max_tokens:
                lines.append("- \u2026 (blocchi più vecchi: cerca con %s query)" % self.rn)
                break
            lines.append(line)
            used += t
        return "\n".join(lines)

    def _switch(self, conv, req, msgs, hs, phys, origin, per, tools, seg, upstream, est, resp):
        cfg, events = self.cfg, []
        start = next((i for i in origin if i is not None and i > 0), 0) if seg else 0
        c, kind = self.choose_cut(msgs, origin, per, start)
        if c is None:
            events.append(self.journal.log("switch_skipped", conv=conv, reason="nessun confine di taglio sicuro"))
            return events
        params = {k: v for k, v in req.items() if k not in ("messages", "tools", "stream", "stream_options",
                                                             "max_tokens", "max_completion_tokens", "n", "tool_choice")}
        events.append(self.journal.log("freeze", conv=conv, seg=seg, est_tokens=est, response=resp,
                                       window=cfg.window, cut_index=c, cut_kind=kind))
        # 1) note di passaggio in coda ad A (stesso system/tool/reasoning: il prefisso resta in cache)
        t0 = time.time()
        notes, usage, finish, timings = "", {}, None, None
        try:
            r = upstream.chat({**params, "messages": phys + [{"role": "user", "content": NOTES_INSTRUCTION.replace("{rn}", self.rn)}],
                               "tools": tools, "max_tokens": cfg.notes_max_tokens, "stream": False})
            notes = (r["choices"][0]["message"].get("content") or "").strip()
            usage = r.get("usage") or {}
            finish, timings = r["choices"][0].get("finish_reason"), r.get("timings")
        except Exception as e:  # noqa: BLE001
            events.append(self.journal.log("notes_error", conv=conv, error=str(e)[:300]))
        events.append(self.journal.log("notes", conv=conv, seg=seg + 1, ms=round((time.time() - t0) * 1000),
                                       tokens=self.tc.count(notes), usage=usage, finish=finish, timings=timings,
                                       cached=(usage.get("prompt_tokens_details") or {}).get("cached_tokens")))
        if not notes:
            notes = "(note non disponibili: usa %s per ricostruire i dettagli)" % self.rn
        # 2) sigillo (sperimentale) + SAVE di A
        if cfg.slot_save:
            if cfg.seal_experimental:
                t0 = time.time()
                try:
                    r = upstream.chat({**params, "messages": phys, "tools": tools, "max_tokens": 1, "stream": False})
                    u = r.get("usage") or {}
                    events.append(self.journal.log("seal", conv=conv, ms=round((time.time() - t0) * 1000), usage=u,
                                                   cached=(u.get("prompt_tokens_details") or {}).get("cached_tokens"),
                                                   timings=r.get("timings")))
                except Exception as e:  # noqa: BLE001
                    events.append(self.journal.log("seal_error", conv=conv, error=str(e)[:300]))
            fn = "%s-seg%d-A.bin" % (conv, seg)
            t0 = time.time()
            try:
                r = upstream.slot("save", fn)
                events.append(self.journal.log("save", conv=conv, file=fn, ms=round((time.time() - t0) * 1000),
                                               result=r))
                if cfg.data_dir:
                    # il prompt fisico di A serve al consulto (A + domanda deve ricostruire lo stesso prefisso)
                    with open(os.path.join(cfg.data_dir, fn + ".json"), "w", encoding="utf-8") as f:
                        json.dump({"conv": conv, "seg": seg, "file": fn, "messages": phys, "tools": tools,
                                   "params": params}, f, ensure_ascii=False)
            except Exception as e:  # noqa: BLE001
                events.append(self.journal.log("save_error", conv=conv, file=fn, error=str(e)[:300]))
        # 3) B = system + tool + [note + indice] + coda
        first_user = next((m for m in msgs if is_human_user(m)), None)
        parts = [SEG_MARK.format(seg=seg + 1),
                 "I messaggi 1\u2013%d di questa conversazione sono stati archiviati e non sono più nel contesto. "
                 "Il loro testo esatto è recuperabile con lo strumento %s (per id o per ricerca)." % (c, self.rn)]
        if cfg.keep_first_user and first_user is not None and msgs.index(first_user) < c:
            parts += ["", "## Prima richiesta dell'utente (letterale)", content_text(first_user.get("content"))]
        parts += ["", "## Note di passaggio", notes]
        idx = self.archive_index(msgs, c, conv)
        if idx:
            parts += ["", "## Indice dell'archivio (più recenti prima)", idx]
        parts += ["", "La conversazione prosegue qui sotto."]
        notes_msg = "\n".join(parts)
        pins_msg, pins_ev = self.pins_block(conv, c)
        self.store.add_segment(conv, seg + 1, c, hs[c - 1], notes_msg, kind)
        self.store.set_segment_pins(conv, seg + 1, pins_msg)
        if pins_ev:
            events.append(pins_ev)
        events.append(self.journal.log("switch", conv=conv, seg=seg + 1, cut_index=c, cut_kind=kind,
                                       notes_msg_tokens=self.tc.count(notes_msg),
                                       pins_tokens=self.tc.count(pins_msg) if pins_msg else 0))
        return events

    def pins_block(self, conv: str, cut: int | None = None) -> tuple[str | None, dict | None]:
        """Blocco «Punti fermi» (testuale) dopo il system prompt del nuovo segmento: cambia solo allo switch, quindi
        non provoca riletture. Solo i punti fermi di messaggi rimasti PRIMA del taglio (gli altri sono nella coda).
        Tetto pins_max_tokens: oltre, si tengono i più recenti e si avvisa."""
        rows = [r for r in self.store.pins(conv) if cut is None or r[1] < cut]
        if not rows:
            return None, None
        lines, used, dropped = [], 0, 0
        for pid, idx, text, _, _ in reversed(rows):
            line = "- (messaggio %d) %s" % (idx + 1, text)
            t = self.tc.count(line)
            if used + t > self.cfg.pins_max_tokens:
                dropped += 1
                continue
            lines.append(line)
            used += t
        lines.reverse()
        head = [PIN_HEAD, "Punti fissati dall'utente nella parte archiviata della conversazione: valgono ancora, "
                "testuali."]
        if dropped:
            head.append("(avviso: %d punti fermi più vecchi oltre il tetto di %d token: cercali con %s)"
                        % (dropped, self.cfg.pins_max_tokens, self.rn))
        ev = self.journal.log("pins_block", conv=conv, count=len(lines), dropped=dropped, tokens=used,
                              over_limit=bool(dropped))
        return "\n".join(head + lines), ev

    # ---------- recall ----------
    def fit_tokens(self, text: str, max_tokens: int) -> str:
        """Taglia un testo a max_tokens (conteggio del contatore) con indicazione esplicita."""
        if self.tc.count(text) <= max_tokens:
            return text
        lo, hi = 0, len(text)
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if self.tc.count(text[:mid]) <= max(max_tokens - 30, 0):
                lo = mid
            else:
                hi = mid - 1
        return text[:lo] + "\n[recall troncato per spazio nel contesto: chiedi un id preciso o usa offset]"

    def recall(self, conv: str, args, max_idx: int | None = None, meta: list | None = None) -> str:
        """id (testo esatto, solo della conversazione corrente), path (storia delle operazioni sul file), query
        (FTS5 bm25 + sottostringa esatta per percorsi/identificatori). Filtro di catena: max_idx esclude la domanda
        corrente e i rami successivi; l'archivio è per conversazione. Tetto: recall_max_tokens.
        meta (facoltativo): riceve un dict per pezzo restituito {rid, idx, rank, tokens} (giornale recall)."""
        if isinstance(args, str):
            try:
                args = json.loads(args or "{}")
            except ValueError:
                args = {"query": args}
        args = args if isinstance(args, dict) else {"query": str(args)}
        if self.cfg.recall_struct or self.cfg.recall_multi or self.cfg.recall_flex:
            if getattr(self, "_recall2", None) is None:
                from .recall2 import Recall2
                self._recall2 = Recall2(self)
            return self._recall2.recall(conv, args, max_idx, meta)
        return self.recall_legacy(conv, args, max_idx, meta)

    def recall_legacy(self, conv: str, args: dict, max_idx: int | None = None, meta: list | None = None,
                      head_fn=None) -> str:
        """recall di serie (id / path / query). head_fn: intestazione alternativa per id (recall2)."""
        rid = str(args.get("id") or "").strip().removeprefix("recall:").removeprefix("id=")
        try:
            off = max(0, int(args.get("offset") or 0))
        except (TypeError, ValueError):
            off = 0
        lim = self.cfg.recall_max_chars
        if rid:
            r = self.store.get(rid, conv) or self.store.get(rid)
            if r is None:
                return ("recall: id %s non trovato nell'archivio (gli id validi sono quelli scritti dal gestore del "
                        "contesto; prova con query o path)" % rid)
            text = r[5] or ""
            chunk = text[off:off + lim]
            chunk = self.fit_tokens(chunk, self.cfg.recall_max_tokens)
            if chunk.endswith("usa offset]"):
                chunk = chunk[:chunk.rfind("\n[recall troncato")]
            if meta is not None:
                meta.append({"rid": rid, "idx": r[2], "rank": 0, "tokens": r[6], "offset": off, "chars": len(chunk)})
            head = "[recall:%s \u2014 messaggio %d (%s%s), %d token, caratteri %d\u2013%d di %d]\n" % (
                rid, r[2] + 1, r[3], (" " + r[4]) if r[4] else "", r[6], off, off + len(chunk), len(text))
            if head_fn is not None:
                head = head_fn(r, off, len(chunk), len(text))
            tail = "" if off + len(chunk) >= len(text) else \
                "\n[continua: %s id=%s offset=%d]" % (self.rn, rid, off + len(chunk))
            return head + chunk + tail
        path = str(args.get("path") or "").strip()
        if path:
            ops = self.store.fileops(conv, path, max_idx=max_idx)
            if not ops:
                return "recall: nessuna operazione registrata sul file %r" % path
            out = ["[recall: storia del file %r, %d operazioni, dalla più vecchia; per lo stato attuale rileggilo]"
                   % (path, len(ops))]
            for k, (idx, op, p2, esito, ra, ro, head) in enumerate(ops):
                out.append("- messaggio %d: %s %s \u2014 %s \u00b7 argomenti: id=%s \u00b7 risultato: id=%s \u00b7 %s"
                           % (idx + 1, op, p2, esito, ra, ro, head))
                if meta is not None:
                    meta.append({"rid": ro, "idx": idx, "rank": k, "tokens": 0, "path": p2})
            return self.fit_tokens("\n".join(out), self.cfg.recall_max_tokens)
        q = str(args.get("query") or "").strip()
        if not q:
            return "recall: serve id, path oppure query"
        exact = self.store.search(conv, q, limit=6, max_idx=max_idx)
        fts = self.store.search_fts(conv, q, limit=12, max_idx=max_idx)
        hits, seen = [], set()
        for h in exact + fts:                       # prima le corrispondenze esatte (più recenti), poi bm25
            if h[0] not in seen:
                seen.add(h[0])
                hits.append((h, h in exact))
        if not hits:
            return "recall: nessun risultato per %r" % q
        terms = [t.lower() for t in re.findall(r"\w+", q) if len(t) > 1]
        out = ["[recall: %d risultati per %r (prima i testi che contengono la frase esatta, poi per pertinenza)]"
               % (len(hits), q)]
        budget = self.cfg.recall_max_tokens - self.tc.count(out[0])
        for rank, ((rid2, idx, role, name, content, tokens), is_exact) in enumerate(hits):
            low = content.lower()
            p = low.find(q.lower()) if is_exact else min([x for x in (low.find(t) for t in terms) if x >= 0],
                                                         default=0)
            s = content[max(0, p - 400):p + len(q) + 1200]
            block = "\n--- id=%s \u00b7 messaggio %d (%s%s) \u00b7 %d token%s\n%s" % (
                rid2, idx + 1, role, (" " + name) if name and role == "tool" else "", tokens,
                "" if len(s) >= len(content) else " \u00b7 estratto, testo intero con id", s)
            t = self.tc.count(block)
            if t > budget:
                if rank < 2 and budget > 300:
                    block = self.fit_tokens(block, budget)
                    t = budget
                else:
                    out.append("\n[altri %d risultati non mostrati: affina la query]" % (len(hits) - rank))
                    break
            out.append(block)
            budget -= t
            if meta is not None:
                meta.append({"rid": rid2, "idx": idx, "rank": rank, "tokens": tokens, "chars": len(s),
                             "exact": is_exact})
        return "\n".join(out)

    def remember_internal(self, conv: str, h_last: str, msgs: list, strip_content: str = "",
                          strip_reasoning: str = "") -> None:
        """Eventi interni (chiamata recall + risultato) inseriti dopo l'ultimo messaggio client, chiavati dal suo
        hash: alla richiesta successiva vengono reinseriti identici, così il prefisso fisico resta stabile."""
        self.store.add_insert(h_last, conv, msgs, strip_content, strip_reasoning)

    # ---------- ancora di masking ----------
    @staticmethod
    def anchor_cut(phys: list, origin: list, frontier: int) -> int:
        """Indice fisico F tale che phys[:F] è un prefisso che nessun pacchetto futuro cambierà e che il template
        rende identico anche dentro il prompt completo: phys[F] è un messaggio client user/assistant (mai tool,
        mai evento interno), e tutto ciò che viene prima ha indice client < frontiera."""
        f = next((j for j, i in enumerate(origin) if i is not None and i >= frontier), len(phys) - 1)
        while f > 1 and (origin[f] is None or phys[f].get("role") not in ("user", "assistant")):
            f -= 1
        return max(f, 0)

    @staticmethod
    def prefix_hash(phys: list, tools, params: dict) -> str:
        return H(json.dumps({"m": phys, "t": tools, "p": params}, sort_keys=True, ensure_ascii=False))

    def _anchor(self, conv, seg, req, phys, origin, per, tools, mask_ev, upstream) -> list:
        """Dopo un pacchetto di mask: (1) se c'è l'ancora del pacchetto precedente ed è ancora prefisso esatto del
        nuovo prompt fisico, RESTORE (Strata riparte dalla vecchia frontiera invece che dal checkpoint radice);
        (2) richiesta di 1 token col prompt fisico fino alla nuova frontiera (Strata mette un checkpoint esattamente
        lì); (3) SAVE in un file nuovo = nuova ancora. La richiesta vera che segue riparte dalla nuova frontiera."""
        cfg, events = self.cfg, []
        params = {k: v for k, v in req.items() if k not in ("messages", "tools", "stream", "stream_options",
                                                             "max_tokens", "max_completion_tokens", "n",
                                                             "tool_choice")}
        old = self.store.anchor(conv, seg)
        t0 = time.time()
        if old:
            oidx, ophash, ofile, _ = old
            oF = next((j for j, i in enumerate(origin) if i is not None and i >= oidx), None)
            if oF is not None and self.prefix_hash(phys[:oF], tools, params) == ophash:
                try:
                    r = upstream.slot("restore", ofile)
                    events.append(self.journal.log("anchor_restore", conv=conv, seg=seg, file=ofile, index=oidx,
                                                   ms=round((time.time() - t0) * 1000), result=r))
                except Exception as e:  # noqa: BLE001
                    events.append(self.journal.log("anchor_error", conv=conv, step="restore", error=str(e)[:300]))
            else:
                events.append(self.journal.log("anchor_stale", conv=conv, seg=seg, index=oidx))
        F = self.anchor_cut(phys, origin, mask_ev["frontier_index"])
        ftok = sum(per[:F]) + self.tools_tokens(tools)
        if F <= 1 or ftok < cfg.anchor_min_tokens or origin[F] is None:
            events.append(self.journal.log("anchor_skip", conv=conv, seg=seg, phys_index=F, est_tokens=ftok))
            return events
        idx = origin[F]
        t1 = time.time()
        try:
            r = upstream.chat({**params, "messages": phys[:F], "tools": tools, "max_tokens": 1, "stream": False})
            u = r.get("usage") or {}
            t2 = time.time()
            fn = "%s-seg%d-anchor%d.bin" % (conv, seg, idx)
            s = upstream.slot("save", fn)
            self.store.set_anchor(conv, seg, idx, self.prefix_hash(phys[:F], tools, params), fn,
                                  int(u.get("prompt_tokens") or 0))
            events.append(self.journal.log(
                "anchor_save", conv=conv, seg=seg, file=fn, index=idx, phys_index=F, prompt_tokens=u.get("prompt_tokens"),
                cached=(u.get("prompt_tokens_details") or {}).get("cached_tokens"),
                prompt_read=(r.get("timings") or {}).get("prompt_n"), prefill_ms=round((t2 - t1) * 1000),
                save_ms=round((time.time() - t2) * 1000), total_ms=round((time.time() - t0) * 1000), result=s))
            if old and old[2] != fn and cfg.slot_dir:
                try:
                    os.remove(os.path.join(cfg.slot_dir, old[2]))
                except OSError:
                    pass
                if cfg.kv_archive:
                    from .kvarchive import discard
                    discard(cfg, old[2], self.journal)
        except Exception as e:  # noqa: BLE001
            events.append(self.journal.log("anchor_error", conv=conv, step="save", error=str(e)[:300]))
        return events
