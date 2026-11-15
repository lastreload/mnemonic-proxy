# Author: Maurizio Verde — LastReload
"""Issue #90: separazione fra CATALOGO RESO nel prompt e ARRAY DI GRAMMATICA delle chiamate.

Un motore come llama.cpp (misurato alla build 11723, revisione 31288c9c6) costruisce grammatica e
parser delle chiamate SOLO dall'array `tools` della richiesta, ma rende nel prompt
`chat_template_kwargs["tools"]` con precedenza su `tools` (`common/chat.cpp:894-902`). Si possono
quindi separare le due cose:

  chat_template_kwargs["tools"]  catalogo VISIBILE in forma canonica  -> testo del prompt (cache)
  tools                          catalogo + schemi già CARICATI       -> grammatica e parser

Il prompt reso resta identico byte per byte a quello di oggi (prova nativa: 150 assertion,
workspaces/mp-issue90-cache-native), mentre la chiamata nativa a uno strumento caricato diventa
ammessa. Nessuna attivazione automatica: vedi `Config.tools_paging_native_split` e `qualify()`.

Solo stdlib, nessuno stato.
"""
from __future__ import annotations

import hashlib
import json
import logging

# Campo privato del corpo interno al proxy: porta l'array di grammatica fino a Upstream, che lo
# toglie sempre dal corpo HTTP. Non esce mai dal processo.
GRAMMAR_KEY = "_mp_grammar_tools"

# Marcatori dell'handler dedicato delle chiamate in llama.cpp (`common/chat.cpp:1246-1251`): la scelta
# dell'handler si fa UNA volta dal solo testo del template. Misurati sulla sorgente reale di produzione.
MARKERS = ("<tool_call>", "<function=", "<parameter=")
# marcatore di esclusione: un template che compone il nome della funzione dentro il tag non usa
# l'handler dedicato di Qwen3-Coder
EXCLUSION = "'<tool_call><function=' ~ tool_call.name ~ '>'"


def qualify(engine_kind: str, props, expected_build: str = "", expected_template: str = ""):
    """Il motore dietro `props` ammette la separazione? -> (ok, ragione, dettagli).

    Condizioni NECESSARIE, verificabili a motore accesso. Non sono sufficienti: grammatica e parser
    non sono osservabili da endpoint (`/apply-template` restituisce SOLO il prompt,
    `server-context.cpp:5703-5714`), quindi quella parte resta chiusa fuori banda dall'harness
    nativo, una volta per build del motore.
    """
    detail = {"engine": engine_kind or "", "build_info": "", "markers": [], "expected_build": expected_build or "",
              "expected_template_sha256": (expected_template or "").strip().lower()}
    if (engine_kind or "") != "llama.cpp":
        return False, "engine %r is not llama.cpp" % (engine_kind or ""), detail
    if not isinstance(props, dict):
        return False, "/props unavailable", detail
    build = str(props.get("build_info") or "")
    tpl = str(props.get("chat_template") or "")
    detail["build_info"] = build
    detail["markers"] = [m for m in MARKERS if m in tpl]
    detail["template_sha256"] = hashlib.sha256(tpl.encode("utf-8")).hexdigest() if tpl else ""
    if not build:
        return False, "/props without build_info", detail
    if expected_build and expected_build not in build:
        return False, "build_info %r does not contain the expected build %r" % (build, expected_build), detail
    if len(detail["markers"]) != len(MARKERS):
        return False, "chat_template without the dedicated-handler markers %s" % (
            [m for m in MARKERS if m not in tpl],), detail
    if EXCLUSION in tpl:
        return False, "chat_template carries the exclusion marker (no dedicated handler)", detail
    want = detail["expected_template_sha256"]
    if want and not str(detail.get("template_sha256") or "").startswith(want):
        return False, "chat_template sha256 %r does not match the expected template %r" % (
            detail.get("template_sha256") or "", want), detail
    return True, "", detail


def canonical_tools(tools):
    """Forma canonica di `common_chat_tools_to_json_oaicompat` (misurata, non dedotta).

    Ogni voce è SOLO {"type":"function","function":{"name","description","parameters"}}, in
    quest'ordine; `description` assente -> "", `parameters` assente -> {}; nessun campo extra
    (un `strict` in più rompe l'identità del prompt al byte 6 202). `parameters` è copiato
    VERBATIM, ordine di inserimento compreso: nessuna mutazione di proprietà o descrizioni.
    """
    if tools is None:
        return None
    out = []
    for t in tools:
        f = (t or {}).get("function") if isinstance(t, dict) else None
        f = f if isinstance(f, dict) else (t if isinstance(t, dict) else {})
        out.append({"type": "function", "function": {"name": f.get("name") or "",
                                                     "description": f.get("description") or "",
                                                     "parameters": f.get("parameters")
                                                     if isinstance(f.get("parameters"), dict) else {}}})
    return out


def wire_body(body: dict) -> dict:
    """Corpo da mandare al motore: `tools` = array di grammatica, `chat_template_kwargs["tools"]` =
    catalogo canonico reso nel prompt. Il campo privato `GRAMMAR_KEY` è SEMPRE rimosso.

    Senza il campo privato il corpo esce identico (nessun comportamento nuovo quando il modo è
    spento). Gli altri `chat_template_kwargs` del cliente sono fusi, mai sostituiti; se il cliente
    ha già una chiave `tools` la sua resta (la decisione di non separare sta a monte, qui è difesa
    in profondità) e nessuno schema viene promosso.
    """
    if GRAMMAR_KEY not in body:
        return body
    out = {k: v for k, v in body.items() if k != GRAMMAR_KEY}
    grammar = body.get(GRAMMAR_KEY)
    ctk = out.get("chat_template_kwargs")
    ctk = ctk if isinstance(ctk, dict) else {}
    if "tools" in ctk:
        return out
    out["chat_template_kwargs"] = {**ctk, "tools": canonical_tools(out.get("tools"))}
    if grammar is not None:
        out["tools"] = grammar
    return out


class Gate:
    """Qualifica del motore per la separazione, UNA volta per motore (tipo + URL), con diagnostica.

    Non indovina niente: se il motore non risponde, se non è llama.cpp, se la build non è quella
    attesa o se il template non usa l'handler dedicato, la separazione resta spenta e la ragione
    finisce nel giornale e in un avviso (una volta sola per motore). Nessun ritorno silenzioso alla
    promozione costosa degli schemi: quella è un'altra opzione, e la decide l'operatore.
    """

    def __init__(self, cfg, journal=None):
        self.cfg, self.journal = cfg, journal
        self._cache: dict = {}        # chiave motore -> (ok, ragione, dettagli)
        self._said: set = set()       # chiavi già dette (giornale + avviso)

    def _props(self, upstream):
        try:
            st, _, data = upstream.raw("GET", "/props")
            if st != 200:
                return None
            return json.loads(data)
        except Exception:  # noqa: BLE001
            return None

    def active(self, upstream=None) -> bool:
        if not (getattr(self.cfg, "tools_paging", False)
                and getattr(self.cfg, "tools_paging_native_split", False)):
            return False
        kind = str(getattr(getattr(upstream, "engine", None), "kind", "") or "") if upstream is not None else ""
        key = "%s|%s" % (kind, getattr(upstream, "base", "") or "") if upstream is not None else "|"
        if key in self._cache:
            return self._cache[key][0]
        if upstream is None:
            ok, reason, detail = False, "no upstream to qualify the engine", {"engine": ""}
        else:
            props = self._props(upstream) if kind == "llama.cpp" else None
            ok, reason, detail = qualify(kind, props, self.cfg.tools_paging_native_split_build,
                                         getattr(self.cfg, "tools_paging_native_split_template_sha256", ""))
        if ok or "unavailable" not in reason:
            self._cache[key] = (ok, reason, detail)      # un motore acceso e noto si giudica una volta
        if key not in self._said:
            self._said.add(key)
            if self.journal is not None:
                self.journal.log("native_split_gate", ok=ok, reason=reason, **detail)
            if not ok:
                logging.warning("tools_paging_native_split is on but the engine does not qualify (%s): the proxy "
                                "keeps the standard behaviour (loaded tools described in the prompt, native call "
                                "not admitted). No fallback to tools_paging_native_schemas.", reason)
        return ok
