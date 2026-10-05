"""Ingresso Anthropic Messages (Claude Code) -> formato interno Chat Completions -> cuore invariato -> uscita
riconvertita (anche in streaming).

    POST /v1/messages               (stream o no)
    POST /v1/messages/count_tokens

Il cuore (Proxy.chat) non sa da che client arriva la richiesta: qui si converte solo il formato.
Scelte:
- id degli strumenti: in ingresso restano come sono (la conversione è una funzione pura del contenuto, così la
  catena di hash è stabile da un turno all'altro); in uscita un id interno «call_x» diventa «toolu_call_x».
- `thinking` <-> `reasoning_content`. La firma (`signature`) non è verificabile in locale: in uscita se ne genera
  una fittizia (hash del testo), in ingresso è ignorata.
- `cache_control` ignorato. Immagini nei messaggi utente: errore 400 chiaro. Immagini dentro un `tool_result`:
  sostituite da una nota testuale (rifiutarle bloccherebbe per sempre la sessione, che le rimanda a ogni turno).
- `tool_result` con `is_error`: il testo è preceduto da «Error: » (così anche le ricevute del cuore lo vedono
  fallito), salvo che lo dica già.
- Blocchi di sistema «x-anthropic-billing-header: …» di Claude Code (cambiano a ogni richiesta) scartati: altrimenti
  ogni richiesta sarebbe una conversazione nuova e la cache a prefisso del motore non servirebbe.
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
import uuid

IMAGE_NOTE = "[immagine omessa: il proxy non inoltra immagini al motore]"
BILLING = re.compile(r"^\s*x-anthropic-billing-header\s*:", re.I)
_ID_BAD = re.compile(r"[^A-Za-z0-9_-]")


class BadRequest(ValueError):
    pass


# ---------------------------------------------------------------- id
def out_tool_id(cid: str | None) -> str:
    """id interno -> id Anthropic (toolu_…, solo [A-Za-z0-9_-])."""
    if not cid:
        return "toolu_" + uuid.uuid4().hex[:24]
    cid = _ID_BAD.sub("_", cid)
    return cid if cid.startswith("toolu_") else "toolu_" + cid


def signature_for(thinking: str) -> str:
    return "strata-" + hashlib.sha256((thinking or "").encode("utf-8")).hexdigest()[:40]


# ---------------------------------------------------------------- richiesta
def _text_of(blocks, where: str, allow_images: bool) -> str:
    if blocks is None:
        return ""
    if isinstance(blocks, str):
        return blocks
    out = []
    for b in blocks:
        if isinstance(b, str):
            out.append(b)
            continue
        t = b.get("type")
        if t == "text":
            out.append(b.get("text") or "")
        elif t in ("image", "document"):
            if not allow_images:
                raise BadRequest("%s: blocchi '%s' non supportati da questo proxy (il motore riceve solo testo); "
                                 "togli l'immagine/il documento dal messaggio" % (where, t))
            out.append(IMAGE_NOTE)
        elif t in ("search_result",):
            out.append(_text_of(b.get("content"), where, allow_images))
        else:
            raise BadRequest("%s: tipo di blocco non supportato: %r" % (where, t))
    return "".join(out)


def _system_text(system) -> str | None:
    if system is None:
        return None
    if isinstance(system, str):
        return system
    parts = [b.get("text") or "" for b in system if isinstance(b, dict) and b.get("type") == "text"
             and not BILLING.match(b.get("text") or "")]
    return "\n\n".join(p for p in parts if p) if parts else None


def to_chat_messages(system, messages: list) -> list[dict]:
    out: list[dict] = []
    s = _system_text(system)
    if s:
        out.append({"role": "system", "content": s})
    for k, m in enumerate(messages or []):
        role, content = m.get("role"), m.get("content")
        where = "messages[%d]" % k
        if role == "user":
            if isinstance(content, str):
                out.append({"role": "user", "content": content})
                continue
            texts: list[str] = []
            for b in content or []:
                t = b.get("type") if isinstance(b, dict) else "text"
                if t == "tool_result":
                    if texts:   # testo prima dei risultati: messaggio utente a sé (raro)
                        out.append({"role": "user", "content": "".join(texts)})
                        texts = []
                    r = _text_of(b.get("content"), where + ".tool_result", True)
                    if b.get("is_error") and not r.lstrip().lower().startswith("error"):
                        r = "Error: " + r
                    out.append({"role": "tool", "tool_call_id": b.get("tool_use_id"), "content": r})
                else:
                    texts.append(_text_of([b], where, False))
            if texts:
                out.append({"role": "user", "content": "".join(texts)})
        elif role == "assistant":
            if isinstance(content, str):
                out.append({"role": "assistant", "content": content})
                continue
            text, think, calls = [], [], []
            for b in content or []:
                t = b.get("type")
                if t == "text":
                    text.append(b.get("text") or "")
                elif t == "thinking":
                    think.append(b.get("thinking") or "")
                elif t == "redacted_thinking":
                    continue
                elif t in ("tool_use", "server_tool_use"):
                    calls.append({"id": b.get("id"), "type": "function",
                                  "function": {"name": b.get("name"),
                                               "arguments": json.dumps(b.get("input") or {}, ensure_ascii=False)}})
                else:
                    raise BadRequest("%s: tipo di blocco assistant non supportato: %r" % (where, t))
            am: dict = {"role": "assistant", "content": "".join(text)}
            if think:
                am["reasoning_content"] = "".join(think)
            if calls:
                am["tool_calls"] = calls
            out.append(am)
        elif role in ("system", "developer"):
            # Claude Code recente manda anche messaggi system dentro messages: in testa si uniscono al system,
            # dopo diventano un messaggio utente marcato (i template chat vogliono il system solo in testa)
            text = _text_of([b for b in content if not (isinstance(b, dict) and BILLING.match(b.get("text") or ""))]
                            if isinstance(content, list) else content, where, False)
            if not text:
                continue
            if all(x["role"] == "system" for x in out):
                if out:
                    out[-1] = {"role": "system", "content": out[-1]["content"] + "\n\n" + text}
                else:
                    out.append({"role": "system", "content": text})
            else:
                out.append({"role": "user", "content": "[system]\n" + text})
        else:
            raise BadRequest("%s: ruolo non supportato: %r" % (where, role))
    return out


def to_chat_tools(tools) -> list | None:
    out = []
    for t in tools or []:
        if t.get("type") not in (None, "custom"):
            continue        # strumenti lato server Anthropic (web_search, bash_2025…): il motore non li ha
        out.append({"type": "function", "function": {"name": t["name"], "description": t.get("description") or "",
                                                      "parameters": t.get("input_schema") or {"type": "object"}}})
    return out or None


def to_chat_request(a: dict) -> dict:
    if not isinstance(a.get("messages"), list):
        raise BadRequest("messages: campo obbligatorio")
    if a.get("max_tokens") is None:
        raise BadRequest("max_tokens: campo obbligatorio")
    req = {"model": a.get("model"), "messages": to_chat_messages(a.get("system"), a["messages"]),
           "max_tokens": int(a["max_tokens"])}
    tools = to_chat_tools(a.get("tools"))
    if tools:
        req["tools"] = tools
    tc = a.get("tool_choice")
    if tools and isinstance(tc, dict):
        ty = tc.get("type")
        if ty == "any":
            req["tool_choice"] = "required"
        elif ty == "tool":
            req["tool_choice"] = {"type": "function", "function": {"name": tc.get("name")}}
        elif ty in ("none", "auto"):
            req["tool_choice"] = ty
        if tc.get("disable_parallel_tool_use"):
            req["parallel_tool_calls"] = False
    for k in ("temperature", "top_p", "top_k"):
        if a.get(k) is not None:
            req[k] = a[k]
    if a.get("stop_sequences"):
        req["stop"] = list(a["stop_sequences"])
    if a.get("stream"):
        req["stream"] = True
    return req


# ---------------------------------------------------------------- risposta
STOP = {"tool_calls": "tool_use", "length": "max_tokens", "stop": "end_turn", "function_call": "tool_use",
        "content_filter": "refusal"}


def _usage(u: dict | None) -> dict:
    u = u or {}
    pt = int(u.get("prompt_tokens") or 0)
    cached = int((u.get("prompt_tokens_details") or {}).get("cached_tokens") or 0)
    return {"input_tokens": max(pt - cached, 0), "cache_read_input_tokens": cached,
            "cache_creation_input_tokens": 0, "output_tokens": int(u.get("completion_tokens") or 0)}


def _input(args: str):
    if not args or not args.strip():
        return {}
    try:
        v = json.loads(args)
        return v if isinstance(v, dict) else {"value": v}
    except ValueError:
        return {"_raw_arguments": args}


def from_chat_response(resp: dict, model: str | None) -> dict:
    ch = (resp.get("choices") or [{}])[0]
    msg = ch.get("message") or {}
    blocks = []
    if msg.get("reasoning_content"):
        blocks.append({"type": "thinking", "thinking": msg["reasoning_content"],
                       "signature": signature_for(msg["reasoning_content"])})
    if msg.get("content"):
        blocks.append({"type": "text", "text": msg["content"]})
    for c in msg.get("tool_calls") or []:
        f = c.get("function") or {}
        blocks.append({"type": "tool_use", "id": out_tool_id(c.get("id")), "name": f.get("name"),
                       "input": _input(f.get("arguments") or "")})
    stop = STOP.get(ch.get("finish_reason") or "stop", "end_turn")
    if msg.get("tool_calls"):
        stop = "tool_use"
    out = {"id": "msg_" + uuid.uuid4().hex[:24], "type": "message", "role": "assistant",
           "model": model or resp.get("model"), "content": blocks, "stop_reason": stop, "stop_sequence": None,
           "usage": _usage(resp.get("usage"))}
    if resp.get("strata_context"):
        out["strata_context"] = resp["strata_context"]
    return out


ERR_TYPE = {400: "invalid_request_error", 401: "authentication_error", 403: "permission_error",
            404: "not_found_error", 413: "request_too_large", 429: "rate_limit_error", 529: "overloaded_error"}


def error_body(status: int, message: str) -> dict:
    return {"type": "error", "error": {"type": ERR_TYPE.get(status, "api_error"), "message": message}}


def _err_message(obj) -> str:
    if isinstance(obj, dict):
        e = obj.get("error")
        if isinstance(e, dict):
            return str(e.get("message") or e)
        if e:
            return str(e)
    return json.dumps(obj, ensure_ascii=False)[:2000]


class StreamWriter:
    """Chunk OpenAI (dal cuore) -> eventi SSE Anthropic. `feed(None)` = keep-alive (evento ping)."""

    def __init__(self, write, model):
        self.write, self.model = write, model
        self.idx = -1
        self.kind = None        # "thinking" | "text" | "tool"
        self.think = []
        self.tools: dict[int, int] = {}     # indice tool_call OpenAI -> indice blocco
        self.any_tool = False
        self.started = False
        self.done = False

    def event(self, name, data):
        self.write(("event: %s\ndata: %s\n\n" % (name, json.dumps({"type": name, **data}, ensure_ascii=False)))
                   .encode("utf-8"))

    def start(self):
        if self.started:
            return
        self.started = True
        self.event("message_start", {"message": {
            "id": "msg_" + uuid.uuid4().hex[:24], "type": "message", "role": "assistant", "model": self.model,
            "content": [], "stop_reason": None, "stop_sequence": None,
            "usage": {"input_tokens": 0, "output_tokens": 0}}})

    def _close(self):
        if self.kind is None:
            return
        if self.kind == "thinking":
            self.event("content_block_delta", {"index": self.idx, "delta": {
                "type": "signature_delta", "signature": signature_for("".join(self.think))}})
        self.event("content_block_stop", {"index": self.idx})
        self.kind = None

    def _open(self, kind, block):
        self._close()
        self.idx += 1
        self.kind = kind
        if kind == "thinking":
            self.think = []
        self.event("content_block_start", {"index": self.idx, "content_block": block})

    def feed(self, ch):
        self.start()
        if ch is None:
            self.event("ping", {})
            return
        if ch.get("error"):
            self.error(500, _err_message(ch))
            return
        choice = (ch.get("choices") or [None])[0]
        if not choice:
            return
        d = choice.get("delta") or {}
        if d.get("reasoning_content"):
            if self.kind != "thinking":
                self._open("thinking", {"type": "thinking", "thinking": "", "signature": ""})
            self.think.append(d["reasoning_content"])
            self.event("content_block_delta", {"index": self.idx, "delta": {
                "type": "thinking_delta", "thinking": d["reasoning_content"]}})
        if d.get("content"):
            if self.kind != "text":
                self._open("text", {"type": "text", "text": ""})
            self.event("content_block_delta", {"index": self.idx, "delta": {"type": "text_delta",
                                                                             "text": d["content"]}})
        for tc in d.get("tool_calls") or []:
            i = tc.get("index", 0)
            f = tc.get("function") or {}
            if i not in self.tools:
                self._open("tool", {"type": "tool_use", "id": out_tool_id(tc.get("id")), "name": f.get("name"),
                                    "input": {}})
                self.tools[i] = self.idx
                self.any_tool = True
            if f.get("arguments"):
                if self.tools[i] != self.idx:      # argomenti intercalati (raro): non rappresentabile, si scarta
                    continue
                self.event("content_block_delta", {"index": self.idx, "delta": {
                    "type": "input_json_delta", "partial_json": f["arguments"]}})
        fin = choice.get("finish_reason")
        if fin:
            self._close()
            stop = "tool_use" if self.any_tool else STOP.get(fin, "end_turn")
            u = _usage(ch.get("usage"))
            delta = {"delta": {"stop_reason": stop, "stop_sequence": None}, "usage": u}
            if ch.get("strata_context"):
                delta["strata_context"] = ch["strata_context"]
            self.event("message_delta", delta)
            self.event("message_stop", {})
            self.done = True

    def error(self, status, message):
        if self.done:
            return
        self.event("error", {"error": error_body(status, message)["error"]})
        self.done = True


# ---------------------------------------------------------------- HTTP
def handle(h, proxy, path: str, raw: bytes):
    """Chiamato da server.make_handler per /v1/messages e /v1/messages/count_tokens."""
    try:
        a = json.loads(raw or b"{}")
        req = to_chat_request(a)
    except (ValueError, TypeError, KeyError, AttributeError) as e:
        return h._send(400, error_body(400, str(e)))
    if path.endswith("/count_tokens"):
        n, _ = proxy.mgr.estimate(req["messages"], req.get("tools"))
        return h._send(200, {"input_tokens": int(n)})
    hint = h.headers.get("X-Strata-Conversation")
    model = a.get("model")
    if not req.get("stream"):
        try:
            st, resp, hdr = proxy.chat(req, hint)
        except Exception as e:  # noqa: BLE001
            proxy.journal.log("proxy_error", api="anthropic", error=repr(e)[:500])
            return h._send(500, error_body(500, "proxy: %r" % e))
        if st != 200:
            return h._send(st, error_body(st, _err_message(resp)))
        return h._send(200, from_chat_response(resp, model), hdr)
    return _stream(h, proxy, req, hint, model)


def _stream(h, proxy, req, hint, model):
    from .server import ClientGone
    h.send_response(200)
    h.send_header("Content-Type", "text/event-stream")
    h.send_header("Cache-Control", "no-cache")
    h.send_header("Connection", "close")
    h.end_headers()
    h.close_connection = True
    wlock = threading.Lock()

    def write(data: bytes):
        try:
            with wlock:
                h.wfile.write(data)
                h.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            raise ClientGone()

    w = StreamWriter(write, model)
    try:
        w.start()
        st, resp, _ = proxy.chat(req, hint, emit=w.feed)
        if st != 200:
            w.error(st, _err_message(resp))
        elif not w.done:       # il cuore chiude sempre con un chunk finale; per sicurezza
            w.feed({"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}], "usage": resp.get("usage")})
    except ClientGone:
        pass
    except Exception as e:  # noqa: BLE001
        proxy.journal.log("proxy_error", api="anthropic", error=repr(e)[:500])
        try:
            w.error(500, "proxy: %r" % e)
        except ClientGone:
            pass
