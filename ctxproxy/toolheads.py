# Author: Maurizio Verde — LastReload
"""Evento di giornale `tool_heads`: nomi e peso delle definizioni degli strumenti in testa al prompt (card t_49579af0).

Perché. La testa del prompt — impalcatura del template + definizioni degli strumenti + prompt di sistema — è
l'occupante più grande delle sessioni interattive (19 166 token mediani, 24,8 % del prompt nella misura della card
t_d1134087), ma non era registrata da nessuna parte: l'evento `request` porta solo contatori complessivi e
l'archivio contiene i messaggi, non gli strumenti. Senza questa riga le leve che agiscono sulla testa
(`tooldefs_desc_max`, `tooldefs_param_max`, `tooldefs_keep`, `tools_paging`) non sono misurabili nemmeno a
posteriori, e un giornale senza eventi del pager non distingue «pager mai entrato in funzione» da «pager attivo e
modello che non ha mai toccato il catalogo» (il pager non scrive eventi propri: vedi `ToolPager.split`).

Privacy — la regola di questo modulo. Nell'evento entrano SOLO nomi e numeri. Nessun testo di una definizione, di
una `description` o del prompt di sistema. La garanzia è strutturale, non una promessa: `payload()` non riceve
nemmeno gli strumenti, solo i loro NOMI e liste di interi, quindi non ha modo di copiare testo nel record.
L'impronta dell'elenco (`fingerprint`) è l'unico valore che dipende dal testo, e non è invertibile.
`leaks()` e `unknown_strings()` sono i due predicati con cui i test dimostrano la regola.

Il conteggio dei token è quello che il proxy usa già (`core.TokenCounter`); il record dichiara in `counting` se è
`exact` (tokenizer del pack + template vero) o `estimated` (ripiego caratteri/`chars_per_token`).
"""
from __future__ import annotations

import hashlib
import json

EVENT = "tool_heads"

# Le uniche stringhe ammesse nel record oltre ai nomi degli strumenti e all'impronta (predicato `unknown_strings`).
LABELS = ("exact", "estimated")


def fingerprint(tools: list | None) -> str:
    """Impronta dell'elenco strumenti: 16 cifre esadecimali, deterministica, non invertibile.

    Stesso elenco (stesso ordine, stesse definizioni) -> stessa impronta: è ciò che distingue «nuova richiesta
    della stessa conversazione» da «l'elenco strumenti è cambiato».
    """
    h = hashlib.sha256()
    for t in tools or []:
        h.update(json.dumps(t, ensure_ascii=False, sort_keys=True).encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()[:16]


def payload(fp: str, names: list, def_tokens: list, desc_tokens: list, head_tokens: int, system_tokens: int,
            exact: bool, paging: bool = False, base=(), catalog=()) -> dict:
    """Record dell'evento, costruito campo per campo da nomi e numeri.

    `names` / `def_tokens` / `desc_tokens` sono paralleli e nell'ordine in cui le definizioni stanno nel prompt:
    per ogni strumento il nome, i token della definizione completa e i token della sola `description`.
    `head_tokens` è il totale della testa contata (sistema + strumenti + impalcatura del template);
    `defs_tokens` è la somma delle definizioni, `system_tokens` il prompt di sistema fuso e `frame_tokens` il
    residuo, cioè l'impalcatura del template (è un residuo e non una misura diretta: con il tokenizer esatto il
    confine fra i pezzi non cade sempre fra due token).
    Con il pager attivo si aggiungono `paging`, `base` (rimasti in testa) e `catalog` (caricabili su richiesta).
    """
    if not (len(names) == len(def_tokens) == len(desc_tokens)):
        raise ValueError("names/def_tokens/desc_tokens: liste di lunghezza diversa")
    defs = [{"name": str(n), "tokens": int(a), "desc": int(b)}
            for n, a, b in zip(names, def_tokens, desc_tokens)]
    rec = {"fp": str(fp),
           "tools": len(defs),
           "counting": "exact" if exact else "estimated",
           "head_tokens": int(head_tokens),
           "defs_tokens": sum(d["tokens"] for d in defs),
           "system_tokens": int(system_tokens),
           "frame_tokens": int(head_tokens) - sum(d["tokens"] for d in defs) - int(system_tokens),
           "defs": defs}
    if paging:
        rec["paging"] = True
        rec["base"] = [str(n) for n in base or ()]
        rec["catalog"] = sorted(str(n) for n in catalog or ())
    return rec


# ---------------------------------------------------------------- predicati per i test
def _strings(x, out: list) -> list:
    if isinstance(x, str):
        out.append(x)
    elif isinstance(x, dict):
        for k, v in x.items():
            _strings(k, out)
            _strings(v, out)
    elif isinstance(x, (list, tuple)):
        for v in x:
            _strings(v, out)
    return out


def unknown_strings(rec: dict, names) -> list[str]:
    """Le stringhe del record che non sono un nome di campo, un nome di strumento, un'etichetta nota o l'impronta.

    Vuoto = nel record non c'è nient'altro che nomi e numeri.
    """
    fields = {"fp", "tools", "counting", "head_tokens", "defs_tokens", "system_tokens", "frame_tokens", "defs",
              "name", "tokens", "desc", "paging", "base", "catalog", "ts", "event", "conv", "seg"}
    ok = fields | set(LABELS) | {str(n) for n in names or ()} | {EVENT}
    ok |= {str(rec.get(k) or "") for k in ("fp", "conv")}      # impronta e id di conversazione: derivati, non testo
    return [s for s in _strings(rec, []) if s not in ok]


def leaks(rec: dict, secrets) -> list[str]:
    """I frammenti di `secrets` che compaiono nel record serializzato. Vuoto = nessun testo è passato nel giornale."""
    blob = json.dumps(rec, ensure_ascii=False)
    return [s for s in secrets or () if s and s in blob]
