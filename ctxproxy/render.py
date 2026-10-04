"""Conteggio token ESATTO del prompt che Strata leggerà.

Strata (serve/frontend.py + chat_template.jinja del pack) fa: openai_to_messages(req) -> template Jinja -> tokenizer
(parse_special=True). Qui lo stesso calcolo in Python puro (niente jinja2 nel venv di produzione), PEZZO PER PEZZO:
render_pieces() restituisce i frammenti di testo del prompt con l'indice del messaggio che li ha prodotti, così
  - il totale = somma dei token dei frammenti (verificato == prompt_tokens di Strata: tests/test_postprova.py::TestExact);
  - il costo FISICO di un messaggio = token del suo frammento (con le regole del template: ragionamento tenuto o no,
    argomenti in XML <parameter=...> e non JSON, intestazioni di gruppo dei risultati tool);
  - il risparmio di una trasformazione = Δ fra i frammenti prima/dopo sulla stessa richiesta.

Il tokenizer è un tokenizer.json HF costruito dal pack (tools/make_hf_tokenizer.py): il tokenizer.json del pack è
un descrittore, non un file HF (per questo la produzione contava caratteri/3.5).
"""
from __future__ import annotations

import json

IM_START, IM_END = "<|im_start|>", "<|im_end|>"
TOOLS_HEAD = "# Tools\n\nYou have access to the following functions:\n\n<tools>"
TOOLS_TAIL = (
    "\n</tools>\n\nIf you choose to call a function ONLY reply in the following format with NO suffix:\n\n<tool_call>\n"
    "<function=example_function_name>\n<parameter=example_parameter_1>\nvalue_1\n</parameter>\n"
    "<parameter=example_parameter_2>\nThis is the value for the second parameter\nthat can span\nmultiple lines\n"
    "</parameter>\n</function>\n</tool_call>\n\n<IMPORTANT>\nReminder:\n- Function calls MUST follow the specified "
    "format: an inner <function=...></function> block must be nested within <tool_call></tool_call> XML tags\n"
    "- Required parameters MUST be specified\n- You may provide optional reasoning for your function call in natural "
    "language BEFORE the function call, but NOT after\n- If there is no function call available, answer the question "
    "like normal with your current knowledge and do not tell the user about function calls\n</IMPORTANT>")
EFFORT_TEXT = {
    "xhigh": "Reasoning effort is set to xhigh. Please think carefully through the task, validate key assumptions, "
             "consider plausible alternatives, and prioritize correctness, consistency, and clarity in the final "
             "answer.",
    "medium": "",
    "low": "Reasoning effort is set to low. Keep your thinking brief and focused, moving directly to the conclusion "
           "without unnecessary elaboration.",
}
EFFORT = {"none": None, "off": None, "minimal": None, "disabled": None, "false": None,
          "low": "low", "medium": "medium", "high": "xhigh", "xhigh": "xhigh", "max": "xhigh", "maximum": "xhigh"}
VISION = "<|vision_start|><|image_pad|><|vision_end|>"
IMAGE_TYPES = ("image_url", "input_image", "image")


def _tojson(x) -> str:
    return json.dumps(x, ensure_ascii=False)


def _effort_kwargs(value) -> dict:
    if value is None or value == "":
        return {}
    if value is False:
        return {"enable_thinking": False}
    level = EFFORT.get(str(value).strip().lower(), "xhigh")
    return {"enable_thinking": False} if level is None else {"reasoning_effort": level}


def template_kwargs(req: dict) -> dict:
    """Come frontend.openai_to_messages: reasoning_effort / reasoning.effort / chat_template_kwargs."""
    reasoning = req.get("reasoning") if isinstance(req.get("reasoning"), dict) else {}
    kw = dict(_effort_kwargs(req.get("reasoning_effort") or reasoning.get("effort")))
    for k, v in (req.get("chat_template_kwargs") or {}).items():
        if k == "enable_thinking" and not v:
            kw = {"enable_thinking": False}
        elif k == "reasoning_effort" and "enable_thinking" not in kw:
            kw.update(_effort_kwargs(v))
    return kw


def _has_image(c) -> bool:
    return isinstance(c, list) and any(isinstance(p, dict) and p.get("type") in IMAGE_TYPES for p in c)


def _text_of(c) -> str:
    if c is None:
        return ""
    if isinstance(c, str):
        return c
    return "".join(p.get("text", "") for p in c if isinstance(p, dict) and p.get("type") in ("text", "input_text", None))


def _content(c, n_img: list) -> str:
    """render_content del template sul contenuto convertito da Strata (_parts_of)."""
    if not _has_image(c):
        return _text_of(c)
    out = []
    for p in c:
        if not isinstance(p, dict):
            continue
        if p.get("type") in IMAGE_TYPES:
            out.append(VISION)
            n_img[0] += 1
        elif p.get("type") in ("text", "input_text", None) and "text" in p:
            out.append(p.get("text", ""))
    return "".join(out)


def _args(a):
    if isinstance(a, str):
        return json.loads(a) if a.strip() else {}
    return a or {}


def message_piece(m: dict, role: str | None = None, prev_role: str | None = None, next_role: str | None = None,
                  keep_thinking: bool = True, n_img: list | None = None) -> str:
    """Il frammento di prompt di UN messaggio (non system iniziale). Dipende dai vicini solo per i risultati tool
    (apertura <|im_start|>user se il precedente non è tool, chiusura se il successivo non è tool)."""
    role = role or m.get("role")
    if role == "developer":
        role = "system"
    c = _content(m.get("content"), n_img if n_img is not None else [0]).strip()
    if role in ("user", "system"):
        return IM_START + "user\n" + c + IM_END + "\n"
    if role == "assistant":
        rc = m.get("reasoning_content") if m.get("reasoning_content") else ""
        rc = (rc if isinstance(rc, str) else "").strip()
        s = (IM_START + "assistant\n<think>\n" + rc + "\n</think>\n\n" + c) if keep_thinking \
            else (IM_START + "assistant\n" + c)
        for j, tc in enumerate(m.get("tool_calls") or []):
            f = tc.get("function", tc)
            name = f.get("name")
            if j == 0:
                s += ("\n\n" if c.strip() else "") + "<tool_call>\n<function=" + name + ">\n"
            else:
                s += "\n<tool_call>\n<function=" + name + ">\n"
            for an, av in _args(f.get("arguments")).items():
                s += "<parameter=" + an + ">\n" + (av if isinstance(av, str) else _tojson(av)) + "\n</parameter>\n"
            s += "</function>\n</tool_call>"
        return s + IM_END + "\n"
    if role == "tool":
        s = (IM_START + "user") if (prev_role is not None and prev_role != "tool") else ""
        s += "\n<tool_response>\n" + c + "\n</tool_response>"
        if next_role is None or next_role != "tool":
            s += IM_END + "\n"
        return s
    raise ValueError("Unexpected message role.")


def render_pieces(messages: list, tools: list | None, kwargs: dict | None = None,
                  add_generation_prompt: bool = True) -> list[tuple]:
    """-> [(indice messaggio | None, testo)] la cui concatenazione è il prompt che Strata tokenizza.
    None = testa (system+tool) o coda (prompt di generazione)."""
    kw = kwargs or {}
    msgs = []
    for i, m in enumerate(messages):
        role = m.get("role")
        if role == "developer":
            role = "system"
        if role == "system" and i > 0:
            role = "user"         # _late_system_to_user
        msgs.append((i, role, m))
    tdefs = [t.get("function", t) if isinstance(t, dict) and t.get("type") == "function" else t
             for t in tools or []] or None
    n_img = [0]
    # system iniziali fusi
    sys_text, num_sys = "", 0
    for k, (i, role, m) in enumerate(msgs):
        if num_sys == k and role == "system":
            s = _text_of(m.get("content")).strip()
            if s:
                sys_text += ("\n" if sys_text else "") + s
            num_sys += 1
    instr = ""
    if kw.get("enable_thinking") is None or kw.get("enable_thinking") is True:
        instr = EFFORT_TEXT[kw.get("reasoning_effort") or "xhigh"]
    head = ""
    if tdefs:
        head = IM_START + "system\n" + (instr + "\n\n" if instr else "") + TOOLS_HEAD
        head += "".join("\n" + _tojson(t) for t in tdefs) + TOOLS_TAIL
        if sys_text:
            head += "\n\n" + sys_text
        head += IM_END + "\n"
    elif sys_text:
        head = IM_START + "system\n" + (instr + "\n\n" if instr else "") + sys_text + IM_END + "\n"
    elif instr:
        head = IM_START + "system\n" + instr + IM_END + "\n"
    pieces = [(None, head)] if head else []
    # ultimo messaggio utente "vero"
    last_q = len(msgs) - 1
    for k in range(len(msgs) - 1, -1, -1):
        i, role, m = msgs[k]
        if role == "user":
            c = _content(m.get("content"), [0]).strip()
            if not (c.startswith("<tool_response>") and c.endswith("</tool_response>")):
                last_q = k
                break
    preserve = kw.get("preserve_thinking")
    for k, (i, role, m) in enumerate(msgs):
        if k < num_sys:
            continue
        if role == "system":
            raise ValueError("System message must be at the beginning.")
        prev = msgs[k - 1][1] if k > 0 else None
        nxt = msgs[k + 1][1] if k + 1 < len(msgs) else None
        keep = preserve is None or preserve is True or k > last_q
        pieces.append((i, message_piece(m, role, prev, nxt, keep, n_img)))
    if add_generation_prompt:
        tail = IM_START + "assistant\n"
        tail += "<think>\n\n</think>\n\n" if kw.get("enable_thinking") is False else "<think>\n"
        pieces.append((None, tail))
    return pieces


def render(messages, tools, kwargs=None, add_generation_prompt=True) -> str:
    return "".join(t for _, t in render_pieces(messages, tools, kwargs, add_generation_prompt))


def leading_systems(messages: list) -> int:
    n = 0
    for m in messages:
        if m.get("role") in ("system", "developer"):
            n += 1
        else:
            break
    return n


def head_piece(messages: list, tools, kwargs=None) -> str:
    """Testa del prompt (system iniziali fusi + definizioni degli strumenti + istruzione di ragionamento)."""
    k = leading_systems(messages)
    p = render_pieces(messages[:k], tools, kwargs, add_generation_prompt=False)
    return p[0][1] if p and p[0][0] is None else ""


def tail_piece(kwargs=None) -> str:
    kw = kwargs or {}
    return IM_START + "assistant\n" + ("<think>\n\n</think>\n\n" if kw.get("enable_thinking") is False else "<think>\n")


def last_query_index(messages: list) -> int:
    """Indice dell'ultimo messaggio user 'vero' (non un <tool_response> scritto a mano), come il template."""
    for k in range(len(messages) - 1, -1, -1):
        m = messages[k]
        if m.get("role") == "user":
            c = _content(m.get("content"), [0]).strip()
            if not (c.startswith("<tool_response>") and c.endswith("</tool_response>")):
                return k
    return len(messages) - 1


class ExactCounter:
    """Token esatti del prompt fisico: tokenizer HF del pack + render_pieces. Cache per frammento."""

    def __init__(self, tokenizer_path: str, image_tokens: int = 1024):
        from tokenizers import Tokenizer  # type: ignore
        self.tk = Tokenizer.from_file(tokenizer_path)
        self.image_tokens = image_tokens
        self._cache: dict[str, int] = {}

    def text(self, s: str) -> int:
        if not s:
            return 0
        n = self._cache.get(s)
        if n is None:
            n = len(self.tk.encode(s, add_special_tokens=False).ids)
            n += s.count(VISION) * (self.image_tokens - 1)    # ogni immagine diventa image_tokens pad
            if len(self._cache) > 100000:
                self._cache.clear()
            self._cache[s] = n
        return n

    def pieces(self, messages, tools, kwargs=None) -> list[tuple]:
        """-> [(indice, token)] per frammento."""
        return [(i, self.text(t)) for i, t in render_pieces(messages, tools, kwargs)]

    def per_message(self, messages, tools, kwargs=None) -> tuple[int, list[int], int]:
        """-> (totale, token per messaggio, token di testa+coda)."""
        per = [0] * len(messages)
        fixed = 0
        for i, n in self.pieces(messages, tools, kwargs):
            if i is None:
                fixed += n
            else:
                per[i] += n
        return fixed + sum(per), per, fixed
