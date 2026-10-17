"""Rimedi allo stop prematuro della risposta finale dopo un recall. Solo stdlib, tutto spento di serie.

Con il thinking spento Flash Next (IQ3_XXS) mette a volte <|im_end|> a metà di un valore appena letto da un recall
("...is WX" per WX-5130-TEAL, "71." per 71.3): misurato in docs/dev-notes/LEAFSEL-ANSWERFIX-RESULT.md. Due rimedi, entrambi opt-in:

T  answer_thinking_after_recall: quando la richiesta che va al motore termina con il risultato di un recall (del
   client o di un giro interno del proxy) il proxy chiede il thinking acceso per quella richiesta
   (chat_template_kwargs.enable_thinking=true) con un tetto reasoning_budget_tokens (answer_thinking_budget; 0 = senza
   tetto) e allarga max_tokens dello stesso tetto (+64) solo nella richiesta al motore.
K  truncation_guard: se la risposta finale è solo testo, corta (<= truncation_guard_max_tokens) e la sua ultima
   parola è un prefisso PROPRIO di un token dell'ultimo recall consegnato, il proxy rigioca UNA volta la stessa
   richiesta con il thinking acceso (truncation_guard_budget) e restituisce quella risposta se è testo non vuoto.
   Solo non-stream (in streaming il testo è già al client). Ogni intervento va nel journal (truncation_guard).

Il ragionamento prodotto da T/K non torna al client (answer_thinking_strip, acceso): la storia del client resta
quella che avrebbe senza thinking, quindi il prefisso del turno dopo non cambia. Resta nel journal (lunghezza).
"""
from __future__ import annotations

import re
import time

from .upstream import UpstreamError

# token di un testo recall: identificatori con - . / : @ + interni (WX-5130-TEAL, 71.3, ip-10-4-77-212, tests/x.py)
TOK_RE = re.compile(r"[^\W_][\w\-./:@+]*\w|\w")
# ultima parola della risposta (può finire con '.', '-', '/', ':' come "71." o "WX-")
WORD_RE = re.compile(r"\w[\w\-./:@+]*$")
TRAIL = " \t\r\n`*\"'\u201d\u2019"


def _text(c) -> str:
    if c is None:
        return ""
    if isinstance(c, str):
        return c
    out = []
    for p in c:
        if isinstance(p, str):
            out.append(p)
        elif isinstance(p, dict) and p.get("type") in ("text", "input_text", "output_text"):
            out.append(p.get("text") or "")
    return "\n".join(out)


def recall_call_ids(msgs, aliases) -> set:
    return {c.get("id") for m in msgs if m.get("role") == "assistant"
            for c in m.get("tool_calls") or [] if (c.get("function") or {}).get("name") in aliases}


def ends_with_recall(msgs, aliases) -> bool:
    """La richiesta termina con il risultato di un recall (uno o più messaggi tool, l'ultimo di un recall)."""
    if not msgs or msgs[-1].get("role") != "tool":
        return False
    return msgs[-1].get("tool_call_id") in recall_call_ids(msgs, aliases)


def last_recall_text(msgs, aliases) -> str:
    ids = recall_call_ids(msgs, aliases)
    for m in reversed(msgs):
        if m.get("role") == "tool" and m.get("tool_call_id") in ids:
            return _text(m.get("content"))
    return ""


def thinking_fields(body: dict, budget: int, default_response: int) -> dict:
    """Corpo per il motore con il thinking acceso e il tetto di ragionamento (solo la richiesta al motore)."""
    ctk = body.get("chat_template_kwargs") if isinstance(body.get("chat_template_kwargs"), dict) else {}
    out = {**body, "chat_template_kwargs": {**ctk, "enable_thinking": True}}
    mt = int(body.get("max_tokens") or body.get("max_completion_tokens") or default_response)
    if budget and budget > 0:
        out["reasoning_budget_tokens"] = int(budget)
        out["max_tokens"] = mt + int(budget) + 64
    else:
        out.pop("reasoning_budget_tokens", None)
        out["max_tokens"] = mt
    out.pop("max_completion_tokens", None)
    return out


def prefix_hit(reply: str, recall_text: str):
    """(parola, token) se l'ultima parola della risposta è un prefisso proprio di un token del recall, altrimenti
    None. Il token scelto è il più corto fra quelli che la continuano."""
    t = (reply or "").rstrip(TRAIL)
    m = WORD_RE.search(t)
    if not m or not recall_text:
        return None
    w = m.group(0)
    best = None
    for tok in set(TOK_RE.findall(recall_text)):
        if len(tok) > len(w) and tok.startswith(w) and (best is None or len(tok) < len(best)):
            best = tok
    return (w, best) if best else None


ANSWER_MIN = 64        # token di risposta garantiti dopo il ragionamento (sotto: thinking non acceso)


def fit_thinking(proxy, p, body: dict, phys: list, budget: int, tools="_p"):
    """Corpo con il thinking e il tetto che entra nella finestra, oppure None se non c'è spazio.
    Con la guardia di finestra (0.3.3, window.py; o il vecchio upstream.guard) max_tokens viene ridotto allo spazio
    rimasto: il tetto di ragionamento si riduce di conseguenza (resta sempre spazio per ANSWER_MIN token di
    risposta); senza tetto (budget 0) il thinking si accende solo se il guard non riduce la risposta."""
    cfg = proxy.cfg
    tools = p.tools if tools == "_p" else tools
    send = thinking_fields(body, budget, int(cfg.default_response))
    wg = getattr(proxy, "wguard", None)
    g = getattr(proxy.up, "guard", None)
    want = int(send["max_tokens"])
    if wg is not None and getattr(cfg, "window_guard", True):
        try:
            got = int(wg.enforce({**send, "messages": phys, "tools": tools}, dry=True).get("max_tokens") or want)
        except UpstreamError:
            return None
    elif g is not None:
        got = int(g({**send, "messages": phys, "tools": tools}, proxy.up).get("max_tokens") or want)
    else:
        return send
    if got >= want:
        return send
    if not budget:
        return None
    mt = int(body.get("max_tokens") or body.get("max_completion_tokens") or cfg.default_response)
    eff = min(int(budget), got - 64 - min(mt, ANSWER_MIN))
    if eff < 32:
        return None
    send["reasoning_budget_tokens"] = eff
    send["max_tokens"] = got
    return send


def thinking_body(proxy, p, body: dict, phys: list, rnd: int, tools="_p", no_think: bool = False) -> dict:
    """T: il corpo da mandare al motore per questo giro (body invariato se T è spento o non si applica).
    no_think (0.3.6): ultimo tentativo finale con il ragionamento spento: T non lo riaccende."""
    cfg = proxy.cfg
    if no_think or not getattr(cfg, "answer_thinking_after_recall", False) \
            or not ends_with_recall(phys, p.recall_aliases):
        return body
    budget = int(getattr(cfg, "answer_thinking_budget", 0) or 0)
    send = fit_thinking(proxy, p, body, phys, budget, tools)
    proxy.journal.log("answer_thinking", conv=p.conv, round=rnd, budget=budget, used=send is not None,
                      effective=(send or {}).get("reasoning_budget_tokens"), max_tokens=(send or {}).get("max_tokens"))
    return send if send is not None else body


def chat(proxy, p, body: dict, phys: list, rnd: int, tools="_p", no_think: bool = False) -> dict:
    """Giro non-stream verso il motore, con T se si applica (la risposta porta allora il segno _af_thinking).
    tools: quelli del giro (0.3.3: la chiamata finale per spazio non offre gli strumenti del proxy)."""
    tools = p.tools if tools == "_p" else tools
    send = thinking_body(proxy, p, body, phys, rnd, tools, no_think)
    resp = proxy.up.chat({**send, "messages": phys, "tools": tools})
    if send is not body and isinstance(resp, dict):
        resp["_af_thinking"] = True
        msg = (resp.get("choices") or [{}])[0].get("message") or {}
        if msg.get("tool_calls") and getattr(proxy.cfg, "answer_thinking_strip", True):
            # altro recall dopo il thinking: il ragionamento non entra nei messaggi interni ricordati dal proxy
            r = msg.pop("reasoning_content", None)
            if r:
                proxy.journal.log("answer_thinking_stripped", conv=p.conv, round=rnd, chars=len(_text(r)))
    return resp


def stream_body(proxy, p, body: dict, phys: list, rnd: int, tools="_p", no_think: bool = False) -> dict:
    """Giro in streaming: stesso corpo di T (il ragionamento, se c'è, arriva al client come reasoning_content come
    per ogni modello con il thinking acceso; il proxy non lo toglie dallo stream)."""
    return thinking_body(proxy, p, body, phys, rnd, tools, no_think)


def guard(proxy, p, body: dict, phys: list, resp: dict, emit=None, tools="_p") -> dict:
    """K: controllo post-risposta. Restituisce la risposta da dare al client (la rigiocata se usata).
    tools: those of the round that produced `resp` (0.3.5: after a final round without the proxy's tools the retry
    does not offer them again - it used to re-open the search with p.tools)."""
    cfg = proxy.cfg
    tools = p.tools if tools == "_p" else tools
    if not getattr(cfg, "truncation_guard", False) or resp is None or emit is not None or resp.get("_af_thinking"):
        return resp
    msg = (resp.get("choices") or [{}])[0].get("message") or {}
    text = _text(msg.get("content"))
    if msg.get("tool_calls") or not text.strip() or not ends_with_recall(phys, p.recall_aliases):
        return resp
    rtext = last_recall_text(phys, p.recall_aliases)
    if not rtext:
        return resp
    n = (resp.get("usage") or {}).get("completion_tokens")
    if not isinstance(n, int) or n <= 0:
        n = proxy.tc.count(text)
    if n > int(getattr(cfg, "truncation_guard_max_tokens", 16)):
        return resp
    hit = prefix_hit(text, rtext)
    if not hit:
        return resp
    budget = int(getattr(cfg, "truncation_guard_budget", 512) or 0)
    send = fit_thinking(proxy, p, body, phys, budget, tools)
    if send is None:
        proxy.journal.log("truncation_guard", conv=p.conv, word=hit[0], token=hit[1], before=text[:200],
                          used=False, error="nessuno spazio per il ragionamento")
        return resp
    t0 = time.time()
    try:
        r2 = proxy.up.chat({**send, "messages": phys, "tools": tools})
    except Exception as e:  # noqa: BLE001  (UpstreamError o rete: si tiene la risposta originale)
        proxy.journal.log("truncation_guard", conv=p.conv, word=hit[0], token=hit[1], before=text[:200],
                          used=False, error=repr(e)[:200], ms=round((time.time() - t0) * 1000))
        return resp
    m2 = (r2.get("choices") or [{}])[0].get("message") or {}
    t2 = _text(m2.get("content"))
    ok = bool(t2.strip()) and not m2.get("tool_calls")
    proxy.journal.log("truncation_guard", conv=p.conv, word=hit[0], token=hit[1], before=text[:200],
                      after=t2[:300], used=ok, tokens_before=n,
                      completion_tokens=(r2.get("usage") or {}).get("completion_tokens"),
                      reasoning_chars=len(_text(m2.get("reasoning_content"))),
                      ms=round((time.time() - t0) * 1000))
    if not ok:
        return resp
    r2["_af_thinking"] = True
    return r2


def finish(proxy, p, resp: dict) -> None:
    """Toglie i campi interni; con answer_thinking_strip il ragionamento di T/K non va al client."""
    if resp is None or not resp.pop("_af_thinking", False):
        return
    if not getattr(proxy.cfg, "answer_thinking_strip", True):
        return
    msg = (resp.get("choices") or [{}])[0].get("message") or {}
    r = msg.pop("reasoning_content", None)
    if r:
        proxy.journal.log("answer_thinking_stripped", conv=p.conv, chars=len(_text(r)))
