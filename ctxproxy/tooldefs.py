"""Definizioni degli strumenti accorciate in modo DETERMINISTICO (card t_e41e63fd, TOOLDEFS-RESULT.md).

Le definizioni dei 49 strumenti di pi pesano ~21,5K token fissi in ogni richiesta. Qui si accorciano SOLO i testi
descrittivi lunghi (description dello strumento e dei parametri, anche annidati); nomi, tipi, enum, required,
default e struttura dello schema restano identici, e così l'ordine delle chiavi.

Stabilità: il risultato è funzione pura del JSON dello strumento e dei limiti (nessun dato della conversazione),
quindi a ogni richiesta Strata vede lo stesso testo e il prefisso del prompt resta riutilizzabile.

Regola di taglio di un testo più lungo di `max_chars` (caratteri):
  1. si tiene il primo paragrafo (fino a una riga vuota);
  2. se è ancora troppo lungo, si tengono le frasi intere iniziali che stanno nel limite;
  3. se nemmeno la prima frase ci sta, si taglia all'ultimo spazio prima del limite;
  4. si aggiunge MARK («…») perché il modello sappia che il testo è stato accorciato.
"""
from __future__ import annotations

import copy
import json
import re

MARK = " …"
_SENT = re.compile(r"(?<=[.!?])\s+")
_SCHEMA_KIDS = ("items", "additionalProperties", "not", "contains", "if", "then", "else")
_SCHEMA_LISTS = ("anyOf", "oneOf", "allOf", "prefixItems")


def shorten_text(s: str, max_chars: int) -> str:
    if not max_chars or not isinstance(s, str) or len(s) <= max_chars:
        return s
    t = s.split("\n\n", 1)[0].strip()
    if len(t) <= max_chars and len(t) < len(s):
        return t + MARK
    out = ""
    for sent in _SENT.split(t):
        cand = (out + " " + sent) if out else sent
        if len(cand) > max_chars:
            break
        out = cand
    if not out:
        cut = t[:max_chars]
        sp = cut.rfind(" ")
        out = cut[:sp] if sp > max_chars // 2 else cut
    return out.rstrip() + MARK


def _schema(node, max_chars: int) -> None:
    """Accorcia in place le description di uno schema JSON (properties, items, anyOf…), non tocca altro."""
    if isinstance(node, list):
        for x in node:
            _schema(x, max_chars)
        return
    if not isinstance(node, dict):
        return
    if isinstance(node.get("description"), str):
        node["description"] = shorten_text(node["description"], max_chars)
    props = node.get("properties")
    if isinstance(props, dict):
        for v in props.values():
            _schema(v, max_chars)
    pp = node.get("patternProperties")
    if isinstance(pp, dict):
        for v in pp.values():
            _schema(v, max_chars)
    for k in _SCHEMA_KIDS:
        if isinstance(node.get(k), dict):
            _schema(node[k], max_chars)
    for k in _SCHEMA_LISTS:
        if isinstance(node.get(k), list):
            _schema(node[k], max_chars)


def shorten_tool(tool: dict, desc_max: int, param_max: int) -> dict:
    t = copy.deepcopy(tool)
    f = t.get("function") if isinstance(t.get("function"), dict) else t
    if desc_max and isinstance(f.get("description"), str):
        f["description"] = shorten_text(f["description"], desc_max)
    if param_max and isinstance(f.get("parameters"), dict):
        _schema(f["parameters"], param_max)
    return t


class ToolShortener:
    """Cache per definizione: stesso JSON in ingresso -> stesso oggetto in uscita (stabile fra richieste)."""

    def __init__(self, desc_max: int = 0, param_max: int = 0, keep=()):
        self.desc_max, self.param_max, self.keep = int(desc_max or 0), int(param_max or 0), set(keep or ())
        self._cache: dict[str, dict] = {}

    @property
    def active(self) -> bool:
        return bool(self.desc_max or self.param_max)

    def __call__(self, tools: list | None) -> list | None:
        if not self.active or not tools:
            return tools
        out = []
        for t in tools:
            f = t.get("function") if isinstance(t, dict) and isinstance(t.get("function"), dict) else t
            if not isinstance(f, dict) or f.get("name") in self.keep:
                out.append(t)
                continue
            k = json.dumps(t, sort_keys=True, ensure_ascii=False)
            r = self._cache.get(k)
            if r is None:
                if len(self._cache) > 2000:
                    self._cache.clear()
                r = self._cache[k] = shorten_tool(t, self.desc_max, self.param_max)
            out.append(r)
        return out
