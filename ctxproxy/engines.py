# Author: Maurizio Verde — LastReload
"""Motori dietro al proxy: che cosa sa fare ciascuno (docs/dev-notes/ENGINES-RESULT.md).

Il proxy è nato davanti a Strata; qui si descrive il motore e lo si riconosce all'avvio:

  kind          strata | llama.cpp | openai (generico OpenAI-compatibile, anche online)
  slot_save     salvataggio/ripristino della sessione su file (POST /slots/{id}?action=save|restore)
  slot_id       slot usato (llama-server con --parallel N: il proxy fissa `id_slot` nelle richieste)
  status        stato del motore normalizzato nella forma di /v1/status di Strata:
                {"loaded", "started", "activity": {"requests", "in_flight"}}
                - Strata: /v1/status così com'è;
                - llama-server: /slots (id_task dello slot = contatore richieste, is_processing = in corso);
  tokenize      conteggio token esatto lato motore (POST /tokenize di llama-server)
  n_ctx         finestra di contesto (da /props o /slots; Strata: dalla configurazione)

ds4-server (https://github.com/antirez/ds4): niente /v1/status, /props, /slots, /tokenize; si riconosce da
`GET /v1/models`, i cui modelli hanno `owned_by: "ds4.c"` e `context_length` (= --ctx). Salva da solo lo stato su
disco (`--kv-disk-dir`, checkpoint per prefisso di testo) e ricorda il testo esatto delle tool call per id
(«exact DSML tool replay»): il proxy quindi non salva né archivia stato (slot_save spento) e non accorcia gli
argomenti delle chiamate (`mask_tool_args`), perché ds4 rimetterebbe nel prompt il testo originale preso per id.

Rilevamento (`detect`): /v1/status con `activity` -> Strata; /v1/models con `owned_by` "ds4.c" -> ds4;
/props con `default_generation_settings` ->
llama.cpp (salvataggi attivi se il server è partito con --slot-save-path: lo si prova con un'azione non valida,
che risponde 400 "Invalid action" se i salvataggi ci sono e 501 se mancano, senza toccare lo slot); altrimenti
OpenAI generico. `engine` in configurazione forza il tipo (auto = rileva).

`apply` spegne con un avviso le funzioni che richiedono i salvataggi quando il motore non li ha
(slot_save, mask_anchor, autosave/autorestore, seal_experimental, kv_archive) e, con llama.cpp, prende la finestra
da n_ctx se non è stata scritta in configurazione (scalando le soglie di serie, pensate per 128K).
"""
from __future__ import annotations

import dataclasses
import json
import os

KINDS = ("strata", "llama.cpp", "ds4", "openai")
ALIASES = {"llama": "llama.cpp", "llamacpp": "llama.cpp", "llama-server": "llama.cpp", "llama_cpp": "llama.cpp",
           "ds4-server": "ds4", "ds4.c": "ds4", "dwarfstar": "ds4",
           "generic": "openai", "generic-openai": "openai", "openai-compatible": "openai", "online": "openai"}
# funzioni che vivono di file di sessione del motore
NEEDS_SLOTS = ("slot_save", "mask_anchor", "autosave", "autorestore", "seal_experimental", "kv_archive")
# soglie di serie pensate per la finestra di Strata (131072): con una finestra più piccola si scalano (se non
# scritte in configurazione)
SCALED = ("mask_trigger", "mask_target", "keep_recent_tokens", "min_batch_tokens", "tail_max", "reserve",
          "default_response", "notes_max_tokens", "anchor_min_tokens", "autosave_min_tokens", "autorestore_min_gain",
          "pins_max_tokens", "recall_max_tokens", "recall_struct_max_tokens", "auto_recall_max_tokens",
          "response_floor")
BASE_WINDOW = 131072


@dataclasses.dataclass
class Engine:
    kind: str = "strata"
    slot_save: bool = True
    slot_id: int = 0
    status_kind: str | None = "strata"   # strata | llama.cpp | None
    tokenize: bool = False
    n_ctx: int | None = None
    model: str | None = None
    detected: bool = False                # True = rilevato dagli endpoint, False = forzato/di serie
    notes: list = dataclasses.field(default_factory=list)

    # il contatore richieste di llama-server (id_task) cresce di più di 1 per richiesta: va riletto dopo
    @property
    def counter_after_call(self) -> bool:
        return self.status_kind == "llama.cpp"

    def summary(self) -> dict:
        return {"kind": self.kind, "slot_save": self.slot_save, "slot_id": self.slot_id, "status": self.status_kind,
                "tokenize": self.tokenize, "n_ctx": self.n_ctx, "model": self.model, "detected": self.detected,
                "notes": self.notes}


def norm_kind(k: str | None) -> str:
    k = (k or "auto").strip().lower()
    k = ALIASES.get(k, k)
    if k not in KINDS + ("auto",):
        raise ValueError("engine sconosciuto %r (strata | llama.cpp | openai | auto)" % k)
    return k


def _get_json(up, path):
    try:
        st, _, data = up.raw("GET", path)
        if st != 200:
            return None
        return json.loads(data)
    except Exception:  # noqa: BLE001
        return None


def _slots_enabled(up, slot_id: int) -> bool | None:
    """llama-server: prova un'azione non valida. 400 'Invalid action' = salvataggi attivi; 501 = server senza
    --slot-save-path. Lo slot non viene toccato. None = non si sa."""
    try:
        st, _, data = up.raw("POST", "/slots/%d?action=ctxproxy-probe" % slot_id, b"{}")
    except Exception:  # noqa: BLE001
        return None
    if st == 501:
        return False
    if st == 400 and b"action" in data.lower():
        return True
    if st in (404, 405):
        return False
    return None


def detect(up, forced: str | None = "auto", slot_id: int = 0) -> Engine:
    """Riconosce il motore dietro `up` (Upstream). Con `forced` diverso da auto prova solo quel tipo, ma legge
    comunque n_ctx/salvataggi se gli endpoint rispondono."""
    kind = norm_kind(forced)
    s = _get_json(up, "/v1/status") if kind in ("auto", "strata") else None
    if kind == "strata" or (kind == "auto" and isinstance(s, dict) and ("activity" in s or s.get("service") ==
                                                                          "strata")):
        e = Engine("strata", slot_save=True, slot_id=slot_id, status_kind="strata", detected=kind == "auto")
        if s is None:
            e.notes.append("/v1/status non risponde (motore spento o non raggiungibile)")
        return e
    models = _get_json(up, "/v1/models") if kind in ("auto", "ds4") else None
    ds4_models = [m for m in (models or {}).get("data") or [] if isinstance(m, dict)
                  and m.get("owned_by") == "ds4.c"] if isinstance(models, dict) else []
    if kind == "ds4" or (kind == "auto" and ds4_models):
        e = Engine("ds4", slot_save=False, slot_id=slot_id, status_kind=None, detected=kind == "auto")
        if ds4_models:
            m0 = ds4_models[0]
            e.model = m0.get("name") or m0.get("id")
            e.n_ctx = int(m0.get("context_length") or 0) or None
        else:
            e.notes.append("/v1/models non risponde come ds4-server (motore spento o non raggiungibile)")
        return e
    props = _get_json(up, "/props") if kind in ("auto", "llama.cpp") else None
    if kind == "llama.cpp" or (kind == "auto" and isinstance(props, dict) and "default_generation_settings" in props):
        e = Engine("llama.cpp", slot_save=False, slot_id=slot_id, status_kind="llama.cpp", tokenize=True,
                   detected=kind == "auto")
        props = props or {}
        e.model = os.path.basename(str(props.get("model_path") or "")) or None
        slots = _get_json(up, "/slots")
        if isinstance(slots, list):
            mine = next((x for x in slots if x.get("id") == slot_id), None)
            if mine is None:
                e.notes.append("slot %d assente (slot: %s)" % (slot_id, [x.get("id") for x in slots]))
            else:
                e.n_ctx = int(mine.get("n_ctx") or 0) or None
            if len(slots) > 1:
                e.notes.append("%d slot (--parallel): il proxy fissa id_slot=%d nelle richieste" % (len(slots),
                                                                                                   slot_id))
        else:
            e.status_kind = None
            e.notes.append("/slots non disponibile (server con --no-slots): niente stato del motore")
        if e.n_ctx is None:
            dgs = props.get("default_generation_settings") or {}
            e.n_ctx = int(dgs.get("n_ctx") or 0) or None
        en = _slots_enabled(up, slot_id)
        e.slot_save = bool(en) and e.status_kind is not None
        if en is False:
            e.notes.append("salvataggi spenti: avviare llama-server con --slot-save-path DIR")
        return e
    return Engine("openai", slot_save=False, slot_id=slot_id, status_kind=None, detected=kind == "auto")


def normalize_status(engine: Engine | None, up) -> dict | None:
    """Stato del motore nella forma di /v1/status di Strata (la usano Tracker e Archiver)."""
    if engine is None or engine.status_kind == "strata":
        return _get_json(up, "/v1/status")
    if engine.status_kind == "llama.cpp":
        slots = _get_json(up, "/slots")
        if not isinstance(slots, list):
            return None
        mine = next((x for x in slots if x.get("id") == engine.slot_id), None)
        if mine is None:
            return None
        return {"service": "llama.cpp", "loaded": True, "started": None,
                "activity": {"requests": mine.get("id_task"), "in_flight": int(bool(mine.get("is_processing"))),
                             "n_prompt_tokens": mine.get("n_prompt_tokens")}}
    return None


def apply(cfg, engine: Engine, journal=None, explicit: set | None = None, log=print) -> list[str]:
    """Adatta la configurazione al motore: spegne le funzioni senza supporto, finestra da n_ctx. -> avvisi."""
    explicit = set(explicit or ())
    warn = []
    if not engine.slot_save:
        off = [f for f in NEEDS_SLOTS if getattr(cfg, f, False) and (f != "autorestore" or cfg.autosave)]
        for f in NEEDS_SLOTS:
            if getattr(cfg, f, False):
                setattr(cfg, f, False)
        if off:
            warn.append("engine %s has no saved-state support: disabled %s" % (engine.kind, ", ".join(off)))
    if engine.kind == "ds4" and getattr(cfg, "mask_tool_args", False) and getattr(cfg, "ds4_exact_tool_replay", True):
        # ds4 rimette nel prompt il testo campionato della chiamata, preso per id: gli argomenti accorciati dal
        # proxy non arriverebbero al modello (nessun risparmio) ma il proxy li conterebbe come risparmiati
        cfg.mask_tool_args = False
        warn.append("engine ds4 replays sampled tool calls by id: disabled mask_tool_args (set "
                    "ds4_exact_tool_replay=false only if ds4-server runs with --disable-exact-dsml-tool-replay)")
    if engine.n_ctx and "window" not in explicit and engine.kind in ("llama.cpp", "ds4") \
            and engine.n_ctx != cfg.window:
        ratio = engine.n_ctx / float(cfg.window or BASE_WINDOW)
        old = cfg.window
        cfg.window = engine.n_ctx
        scaled = {}
        for f in SCALED:
            if f in explicit or not hasattr(cfg, f):
                continue
            v = getattr(cfg, f)
            if isinstance(v, int) and v > 0:
                nv = max(64, int(v * ratio))
                if nv != v:
                    setattr(cfg, f, nv)
                    scaled[f] = nv
        warn.append("window from engine n_ctx: %d (was %d); thresholds scaled x%.3f" % (cfg.window, old, ratio))
        if journal is not None:
            journal.log("engine_window", window=cfg.window, was=old, scaled=scaled)
    # una risposta minima che da sola riempie (quasi) la finestra fa scattare un cambio di segmento a ogni richiesta
    floor = int(getattr(cfg, "response_floor", 0) or 0)
    if floor and cfg.window and floor > cfg.window // 4:
        cfg.response_floor = cfg.window // 4
        warn.append("response_floor %d too large for window %d: lowered to %d" % (floor, cfg.window,
                                                                                    cfg.response_floor))
    for w in warn:
        if log:
            log("[mnemonic-proxy] warning: " + w)
        if journal is not None:
            journal.log("engine_warning", message=w)
    if journal is not None:
        journal.log("engine", **engine.summary())
    return warn


def setup(cfg, up, journal=None, explicit=None, counter=None, log=print) -> Engine:
    """All'avvio del server: rileva (o forza) il motore, lo collega all'Upstream, adatta la configurazione e, se
    chiesto e possibile, usa /tokenize del motore per contare i token."""
    eng = detect(up, getattr(cfg, "engine", "auto"), int(getattr(cfg, "slot_id", 0) or 0))
    if hasattr(up, "set_engine"):
        up.set_engine(eng, getattr(cfg, "slot_dir", "") or "")
    apply(cfg, eng, journal, explicit, log)
    if counter is not None and getattr(cfg, "engine_tokenize", False) and eng.tokenize and not counter.exact:
        counter.tk = RemoteTokenizer(up)
        counter.kind = "esatto:/tokenize"
    return eng


class RemoteTokenizer:
    """Tokenizer via POST /tokenize di llama-server, con l'interfaccia minima di `tokenizers` usata da
    TokenCounter (encode(text, add_special_tokens=False).ids). TokenCounter mette in cache i conteggi."""

    class _Enc:
        def __init__(self, ids):
            self.ids = ids

    def __init__(self, up):
        self.up = up

    def encode(self, text, add_special_tokens=False):
        st, _, data = self.up.raw("POST", "/tokenize", json.dumps({"content": text, "add_special": bool(
            add_special_tokens)}, ensure_ascii=False).encode("utf-8"))
        if st != 200:
            raise RuntimeError("tokenize HTTP %d" % st)
        return self._Enc(json.loads(data).get("tokens") or [])


def slot_error_status(engine: Engine | None, action: str, status: int, body: bytes, slot_dir: str = "",
                      filename: str = "") -> int:
    """llama-server risponde 400 anche per un file mancante: lo si riporta a 404 (come Strata) se il file non c'è
    nella slot_dir, così autorestore lo segna come cancellato."""
    if engine is None or engine.kind != "llama.cpp" or action != "restore" or status != 400:
        return status
    if slot_dir and filename and not os.path.exists(os.path.join(slot_dir, filename)):
        return 404
    return status
