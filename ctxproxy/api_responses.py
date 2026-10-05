"""Ingresso OpenAI Responses (Codex) -> formato interno Chat Completions -> cuore invariato -> uscita riconvertita
(anche in streaming).

    POST /v1/responses   (stream o no)

Codex 0.160 accetta solo `wire_api = "responses"` (`"chat"` è rifiutato: «`wire_api = "chat"` is no longer
supported»), quindi per usarlo davanti a un motore Chat Completions serve questa conversione.
Scelte:
- `instructions` -> messaggio system; messaggi `developer`/`system` -> system (il primo) o user con prefisso.
- `function_call` consecutivi (e il testo/ragionamento che li precede) -> un solo messaggio assistant con più
  tool_calls; `function_call_output` -> messaggio tool. `call_id` resta identico in entrata e in uscita.
- `reasoning` (content `reasoning_text` o, in mancanza, `summary_text`) -> reasoning_content dell'assistant
  che segue. `encrypted_content` ignorato. In uscita il ragionamento è un item `reasoning` con `content`
  reasoning_text (e lo stesso testo come summary, perché Codex mostra i summary).
- strumenti `custom` (testo libero, es. apply_patch di Codex) -> funzione con un parametro `input`; la chiamata
  torna al client come `custom_tool_call`. Strumenti lato server (web_search, local_shell…) scartati.
- Immagini in un messaggio utente: errore 400 chiaro; dentro un output di strumento: nota testuale.
- Niente stato lato server: `previous_response_id` non è supportato (Codex rimanda tutta la storia, `store=false`).
"""
from __future__ import annotations

import json
import threading
import time
import uuid

from .api_anthropic import IMAGE_NOTE, BadRequest, _err_message

CUSTOM_PARAMS = {"type": "object", "properties": {"input": {"type": "string",
                                                            "description": "testo libero passato allo strumento"}},
                 "required": ["input"]}


def _parts_text(content, where: str, allow_images: bool) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    out = []
    for p in content:
        if isinstance(p, str):
            out.append(p)
            continue
        t = p.get("type")
        if t in ("input_text", "output_text", "text", "summary_text", "reasoning_text", "refusal"):
            out.append(p.get("text") or p.get("refusal") or "")
        elif t in ("input_image", "input_file", "image_url"):
            if not allow_images:
                raise BadRequest("%s: contenuti '%s' non supportati da questo proxy (il motore riceve solo testo)"
                                 % (where, t))
            out.append(IMAGE_NOTE)
        else:
            raise BadRequest("%s: tipo di contenuto non supportato: %r" % (where, t))
    return "".join(out)


def to_chat_messages(instructions, items, custom_names=()) -> list[dict]:
    out: list[dict] = []
    if instructions:
        out.append({"role": "system", "content": instructions})
    if isinstance(items, str):
        items = [{"type": "message", "role": "user", "content": items}]
    items_l: list[dict] = list(items or [])
    pend: dict | None = None      # assistant in costruzione
    think: list[str] = []         # ragionamento in attesa del suo assistant

    def flush():
        nonlocal pend, think
        if pend is None and think:
            pend = {"role": "assistant", "content": ""}
        if pend is not None:
            if think:
                pend["reasoning_content"] = "".join(think)
            out.append(pend)
        pend, think = None, []

    def assistant():
        nonlocal pend
        if pend is None:
            pend = {"role": "assistant", "content": ""}
        return pend

    for k, it in enumerate(items_l):
        where = "input[%d]" % k
        t = it.get("type") or ("message" if it.get("role") else None)
        if t == "message":
            role = it.get("role")
            if role == "assistant":
                a = assistant()
                if a.get("tool_calls"):   # testo dopo chiamate: nuovo turno assistant
                    flush()
                    a = assistant()
                a["content"] += _parts_text(it.get("content"), where, False)
                continue
            flush()
            text = _parts_text(it.get("content"), where, False)
            if role in ("system", "developer"):
                if all(m["role"] == "system" for m in out):   # ancora in testa: si unisce al system
                    if out:
                        out[-1] = {"role": "system", "content": out[-1]["content"] + "\n\n" + text}
                    else:
                        out.append({"role": "system", "content": text})
                else:
                    out.append({"role": "user", "content": "[%s]\n%s" % (role, text)})
            elif role == "user":
                out.append({"role": "user", "content": text})
            else:
                raise BadRequest("%s: ruolo non supportato: %r" % (where, role))
        elif t == "reasoning":
            if pend is not None and (pend.get("tool_calls") or pend.get("content")):
                flush()
            txt = _parts_text(it.get("content"), where, False) if it.get("content") else \
                "\n".join((s.get("text") or "") for s in it.get("summary") or [])
            if txt:
                think.append(txt)
        elif t in ("function_call", "custom_tool_call"):
            a = assistant()
            args = it.get("arguments") if t == "function_call" else json.dumps({"input": it.get("input") or ""},
                                                                                ensure_ascii=False)
            a.setdefault("tool_calls", []).append({"id": it.get("call_id"), "type": "function",
                                                   "function": {"name": it.get("name"), "arguments": args or ""}})
        elif t in ("function_call_output", "custom_tool_call_output"):
            flush()
            o = it.get("output")
            txt = o if isinstance(o, str) else (_parts_text(o, where, True) if isinstance(o, list)
                                                else json.dumps(o, ensure_ascii=False))
            out.append({"role": "tool", "tool_call_id": it.get("call_id"), "content": txt})
        elif t in ("item_reference",):
            raise BadRequest("%s: item_reference non supportato (il proxy non conserva stato: rimanda la storia)"
                             % where)
        else:
            # web_search_call, local_shell_call, compaction… : non rappresentabili per il motore, ignorati
            continue
    flush()
    return out


def to_chat_tools(tools) -> tuple[list | None, set]:
    out, custom = [], set()
    for t in tools or []:
        ty = t.get("type")
        if ty == "function":
            out.append({"type": "function", "function": {"name": t["name"], "description": t.get("description") or "",
                                                          "parameters": t.get("parameters") or {"type": "object"}}})
        elif ty == "custom":
            custom.add(t["name"])
            desc = t.get("description") or ""
            fmt = t.get("format") or {}
            if fmt.get("type") == "grammar" and fmt.get("definition"):
                desc += "\n\nIl parametro `input` deve rispettare questa grammatica (%s):\n%s" % (
                    fmt.get("syntax") or "", fmt["definition"])
            out.append({"type": "function", "function": {"name": t["name"], "description": desc,
                                                          "parameters": CUSTOM_PARAMS}})
    return (out or None), custom


def to_chat_request(r: dict) -> tuple[dict, set]:
    if r.get("previous_response_id"):
        raise BadRequest("previous_response_id non supportato: il proxy non conserva stato, rimanda tutta la storia")
    if r.get("input") is None:
        raise BadRequest("input: campo obbligatorio")
    tools, custom = to_chat_tools(r.get("tools"))
    req = {"model": r.get("model"), "messages": to_chat_messages(r.get("instructions"), r["input"], custom)}
    if tools:
        req["tools"] = tools
        tc = r.get("tool_choice")
        if tc in ("auto", "none", "required"):
            req["tool_choice"] = tc
        elif isinstance(tc, dict) and tc.get("name"):
            req["tool_choice"] = {"type": "function", "function": {"name": tc["name"]}}
        if r.get("parallel_tool_calls") is False:
            req["parallel_tool_calls"] = False
    if r.get("max_output_tokens"):
        req["max_tokens"] = int(r["max_output_tokens"])
    for k in ("temperature", "top_p"):
        if r.get(k) is not None:
            req[k] = r[k]
    if isinstance(r.get("reasoning"), dict) and r["reasoning"].get("effort"):
        req["reasoning_effort"] = r["reasoning"]["effort"]
    if r.get("stream"):
        req["stream"] = True
    return req, custom


# ---------------------------------------------------------------- risposta
def _usage(u: dict | None) -> dict:
    u = u or {}
    pt, ct = int(u.get("prompt_tokens") or 0), int(u.get("completion_tokens") or 0)
    return {"input_tokens": pt,
            "input_tokens_details": {"cached_tokens": int((u.get("prompt_tokens_details") or {})
                                                          .get("cached_tokens") or 0)},
            "output_tokens": ct,
            "output_tokens_details": {"reasoning_tokens": int((u.get("completion_tokens_details") or {})
                                                              .get("reasoning_tokens") or 0)},
            "total_tokens": pt + ct}


def _rid(p):
    return p + "_" + uuid.uuid4().hex[:24]


def reasoning_item(text, iid=None, status="completed"):
    return {"type": "reasoning", "id": iid or _rid("rs"), "status": status,
            "summary": [{"type": "summary_text", "text": text}] if text else [],
            "content": [{"type": "reasoning_text", "text": text}] if text else []}


def message_item(text, iid=None, status="completed"):
    return {"type": "message", "id": iid or _rid("msg"), "status": status, "role": "assistant",
            "content": [{"type": "output_text", "text": text, "annotations": []}] if text is not None else []}


def call_item(cid, name, args, custom, iid=None, status="completed"):
    if name in custom:
        try:
            inp = (json.loads(args) or {}).get("input", "") if args else ""
        except (ValueError, AttributeError):
            inp = args
        return {"type": "custom_tool_call", "id": iid or _rid("ctc"), "status": status, "call_id": cid,
                "name": name, "input": inp if isinstance(inp, str) else json.dumps(inp, ensure_ascii=False)}
    return {"type": "function_call", "id": iid or _rid("fc"), "status": status, "call_id": cid, "name": name,
            "arguments": args}


def response_obj(rid, model, output, usage, status="completed", created=None, extra=None):
    o = {"id": rid, "object": "response", "created_at": created or int(time.time()), "status": status,
         "model": model, "output": output, "usage": usage, "error": None, "incomplete_details": None,
         "parallel_tool_calls": True, "tool_choice": "auto", "tools": []}
    if status == "incomplete":
        o["incomplete_details"] = {"reason": "max_output_tokens"}
    o.update(extra or {})
    return o


def from_chat_response(resp: dict, model, custom) -> dict:
    ch = (resp.get("choices") or [{}])[0]
    msg = ch.get("message") or {}
    out = []
    if msg.get("reasoning_content"):
        out.append(reasoning_item(msg["reasoning_content"]))
    if msg.get("content"):
        out.append(message_item(msg["content"]))
    for c in msg.get("tool_calls") or []:
        f = c.get("function") or {}
        out.append(call_item(c.get("id") or _rid("call"), f.get("name"), f.get("arguments") or "", custom))
    extra = {"strata_context": resp["strata_context"]} if resp.get("strata_context") else None
    return response_obj(_rid("resp"), model or resp.get("model"), out, _usage(resp.get("usage")),
                        "incomplete" if ch.get("finish_reason") == "length" else "completed", extra=extra)


def error_body(status: int, message: str) -> dict:
    return {"error": {"message": message, "type": "invalid_request_error" if status == 400 else "server_error",
                      "param": None, "code": None}}


class StreamWriter:
    """Chunk OpenAI Chat (dal cuore) -> eventi SSE Responses. feed(None) = keep-alive (commento SSE)."""

    def __init__(self, write, model, custom):
        self.write, self.model, self.custom = write, model, custom
        self.rid = _rid("resp")
        self.created = int(time.time())
        self.seq = 0
        self.output: list[dict] = []
        self.cur = None            # {"kind", "oi", "item", "buf"}
        self.calls: dict[int, dict] = {}
        self.started = False
        self.done = False

    def event(self, name, data):
        d = {"type": name, "sequence_number": self.seq, **data}
        self.seq += 1
        self.write(("event: %s\ndata: %s\n\n" % (name, json.dumps(d, ensure_ascii=False))).encode("utf-8"))

    def start(self):
        if self.started:
            return
        self.started = True
        r = response_obj(self.rid, self.model, [], None, "in_progress", self.created)
        self.event("response.created", {"response": r})
        self.event("response.in_progress", {"response": r})

    # -- apertura/chiusura degli item
    def _close(self):
        c = self.cur
        if c is None:
            return
        self.cur = None
        oi, it, text = c["oi"], c["item"], "".join(c["buf"])
        if c["kind"] == "reasoning":
            self.event("response.reasoning_text.done", {"item_id": it["id"], "output_index": oi,
                                                        "content_index": 0, "text": text})
            done = reasoning_item(text, it["id"])
        elif c["kind"] == "message":
            part = {"type": "output_text", "text": text, "annotations": []}
            self.event("response.output_text.done", {"item_id": it["id"], "output_index": oi, "content_index": 0,
                                                     "text": text})
            self.event("response.content_part.done", {"item_id": it["id"], "output_index": oi,
                                                      "content_index": 0, "part": part})
            done = message_item(text, it["id"])
        else:
            if it["type"] == "function_call":
                self.event("response.function_call_arguments.done", {"item_id": it["id"], "output_index": oi,
                                                                     "arguments": text})
            done = call_item(it["call_id"], it["name"], text, self.custom, it["id"])
        self.output[oi] = done
        self.event("response.output_item.done", {"output_index": oi, "item": done})

    def _open(self, kind, item):
        self._close()
        oi = len(self.output)
        self.output.append(item)
        self.cur = {"kind": kind, "oi": oi, "item": item, "buf": []}
        self.event("response.output_item.added", {"output_index": oi, "item": item})
        if kind == "message":
            self.event("response.content_part.added", {"item_id": item["id"], "output_index": oi,
                                                       "content_index": 0,
                                                       "part": {"type": "output_text", "text": "", "annotations": []}})
        return self.cur

    def feed(self, ch):
        self.start()
        if ch is None:
            self.write(b": keep-alive\n\n")
            return
        if ch.get("error"):
            self.error(_err_message(ch))
            return
        choice = (ch.get("choices") or [None])[0]
        if not choice:
            return
        d = choice.get("delta") or {}
        if d.get("reasoning_content"):
            c = self.cur if self.cur and self.cur["kind"] == "reasoning" else \
                self._open("reasoning", {"type": "reasoning", "id": _rid("rs"), "summary": [], "content": []})
            c["buf"].append(d["reasoning_content"])
            self.event("response.reasoning_text.delta", {"item_id": c["item"]["id"], "output_index": c["oi"],
                                                         "content_index": 0, "delta": d["reasoning_content"]})
        if d.get("content"):
            c = self.cur if self.cur and self.cur["kind"] == "message" else \
                self._open("message", {"type": "message", "id": _rid("msg"), "status": "in_progress",
                                       "role": "assistant", "content": []})
            c["buf"].append(d["content"])
            self.event("response.output_text.delta", {"item_id": c["item"]["id"], "output_index": c["oi"],
                                                      "content_index": 0, "delta": d["content"]})
        for tc in d.get("tool_calls") or []:
            i = tc.get("index", 0)
            f = tc.get("function") or {}
            c = self.calls.get(i)
            if c is None:
                name = f.get("name") or ""
                cid = tc.get("id") or _rid("call")
                if name in self.custom:
                    item = {"type": "custom_tool_call", "id": _rid("ctc"), "status": "in_progress", "call_id": cid,
                            "name": name, "input": ""}
                else:
                    item = {"type": "function_call", "id": _rid("fc"), "status": "in_progress", "call_id": cid,
                            "name": name, "arguments": ""}
                c = self.calls[i] = self._open("call", item)
            if f.get("arguments"):
                if c is not self.cur:
                    continue          # argomenti intercalati (non accade col cuore): scartati
                c["buf"].append(f["arguments"])
                if c["item"]["type"] == "function_call":
                    self.event("response.function_call_arguments.delta", {
                        "item_id": c["item"]["id"], "output_index": c["oi"], "delta": f["arguments"]})
        if choice.get("finish_reason"):
            self._close()
            st = "incomplete" if choice["finish_reason"] == "length" else "completed"
            extra = {"strata_context": ch["strata_context"]} if ch.get("strata_context") else None
            r = response_obj(self.rid, self.model, self.output, _usage(ch.get("usage")), st, self.created, extra)
            self.event("response.completed" if st == "completed" else "response.incomplete", {"response": r})
            self.done = True

    def error(self, message, code="server_error"):
        if self.done:
            return
        self._close()
        r = response_obj(self.rid, self.model, self.output, None, "failed", self.created)
        r["error"] = {"code": code, "message": message}
        self.event("response.failed", {"response": r})
        self.done = True


# ---------------------------------------------------------------- HTTP
def handle(h, proxy, path: str, raw: bytes):
    try:
        r = json.loads(raw or b"{}")
        req, custom = to_chat_request(r)
    except (ValueError, TypeError, KeyError, AttributeError) as e:
        return h._send(400, error_body(400, str(e)))
    hint = h.headers.get("X-Strata-Conversation") or r.get("prompt_cache_key")
    model = r.get("model")
    if not req.get("stream"):
        try:
            st, resp, hdr = proxy.chat(req, hint)
        except Exception as e:  # noqa: BLE001
            proxy.journal.log("proxy_error", api="responses", error=repr(e)[:500])
            return h._send(500, error_body(500, "proxy: %r" % e))
        if st != 200:
            return h._send(st, error_body(st, _err_message(resp)))
        return h._send(200, from_chat_response(resp, model, custom), hdr)
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

    w = StreamWriter(write, model, custom)
    try:
        w.start()
        st, resp, _ = proxy.chat(req, hint, emit=w.feed)
        if st != 200:
            w.error(_err_message(resp))
        elif not w.done:
            w.feed({"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}], "usage": resp.get("usage")})
    except ClientGone:
        pass
    except Exception as e:  # noqa: BLE001
        proxy.journal.log("proxy_error", api="responses", error=repr(e)[:500])
        try:
            w.error("proxy: %r" % e)
        except ClientGone:
            pass
