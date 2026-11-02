# Author: Maurizio Verde — LastReload
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
import re

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
# Nessuno di questi motori legge filmati o audio: un marcatore visivo per un video varrebbe un costo non contato.
VIDEO_TYPES = ("video_url", "video", "input_video", "input_audio", "audio")
# Chiavi che portano i binari di un'immagine: non sono testo del prompt (il template scrive un marcatore).
IMAGE_KEYS = ("image_url", "input_image", "image", "source", "data", "b64_json")


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
    ctk = req.get("chat_template_kwargs") or {}
    if isinstance(ctk.get("preserve_thinking"), bool):
        kw["preserve_thinking"] = ctk["preserve_thinking"]
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


class UnsupportedPicture(ValueError):
    """Un'immagine che il proxy non sa descrivere al motore come il motore la leggerà. Il rifiuto esplicito è
    l'unica risposta onesta: una sorgente vuota, un'immagine persa per strada o una non contata sono un prompt cui
    nessuno può garantire la finestra, e uno stato KV che verrebbe ripristinato sotto l'immagine sbagliata."""


def image_source(p: dict) -> str:
    """La sorgente di una parte immagine come una stringa sola, letta come la legge _image_source di Strata:
    image_url di OpenAI (dizionario o stringa), input_image di Responses, image di Anthropic con fonte base64
    o url/path. E' la forma che il pack riceve, non una nostra: discostarsene significa contare un prompt che
    il motore non vedrà."""
    if p.get("type") == "image":
        src = p.get("source")
        if isinstance(src, dict):
            if src.get("type") == "base64":
                return "data:%s;base64,%s" % (src.get("media_type", "image/png"), src.get("data", ""))
            return str(src.get("url") or src.get("path") or "")
        return str(src or "")
    url = p.get("image_url")
    if isinstance(url, dict):
        url = url.get("url")
    return str(url or "")


def picture_source(p: dict, where: str = "picture") -> str:
    """`image_source` con il rifiuto che la accompagna: per una parte il motore non legge (un file_id, un url
    vuoto) Strata ottiene una sorgente vuota, quindi un proxy che le contasse dei token prometterebbe un'immagine
    che non c'è. Prima si rifiuta qui, più onesto che inviare un'immagine vuota o bruciata."""
    src = image_source(p)
    if not src.strip():
        raise UnsupportedPicture("%s: picture part with no readable source (a file_id, or an empty url): the engine "
                                 "would be handed an empty image" % where)
    return src


def external_picture(src) -> bool:
    """True per una sorgente di cui il proxy non può nominare i byte: un url http(s) o un percorso, che al turno
    dopo possono puntare a un contenuto diverso. Un data: URI porta il contenuto con sé, quindi il suo hash
    è un'identità, non solo un nome."""
    return not str(src or "").strip().startswith("data:")


def volatile_pictures(messages) -> bool:
    """True se una delle immagini che il motore leggerà arriva da fuori il prompt: un url, un percorso.

    L'identità dello stato del motore non può essere quella stringa: sotto lo stesso nome il contenuto può essere
    un altro al turno successivo, e un restore riporterebbe i KV con l'embedding dell'immagine vecchia. Chi porta
    immagini così non registra e non ripristina alcuno stato (le uniche immagini di cui si può provare il contenuto
    sono i data: URI); il costo in token le conta comunque, perché il motore le espande uguale."""
    for m in messages or []:
        c = m.get("content") if isinstance(m, dict) else None
        if not isinstance(c, list):
            continue
        for p in c:
            if isinstance(p, dict) and p.get("type") in IMAGE_TYPES and external_picture(image_source(p)):
                return True
    return False


def image_hash(p: dict) -> str:
    from .core import H          # import tardivo: core importa render, il ciclo si chiude solo a runtime
    return H(str(image_source(p)))[:16]


def image_hashes(c) -> list:
    """Hash (16 hex, identici al segnaposto <immagine:..> di core.content_text) delle immagini, nell'ordine."""
    if not isinstance(c, list):
        return []
    return [image_hash(p) for p in c if isinstance(p, dict) and p.get("type") in IMAGE_TYPES]


def image_markers(c) -> str:
    """I segnaposto delle immagini di un content, in ordine: ciò che resta nel prompt quando l'immagine viene tolta."""
    return "".join("<immagine:%s>" % h for h in image_hashes(c))


def count_images(messages) -> int:
    """Quante immagini porta una lista di messaggi (usata per il journal, non per il conteggio token)."""
    return sum(len(image_hashes(m.get("content"))) for m in messages or [] if isinstance(m, dict))


def parts_of(c, where: str = "content"):
    """Il content normalizzato negli item che il chat_template del pack riceve DA Strata: {'type':'text','text':..}
    e {'type':'image','source':'<sorgente>'} - la forma stessa di _parts_of di openai_to_messages, non un'approssimazione:
    il template riconosce l'immagine da item.type=='image', quindi cambiare chiave significa contare un prompt che
    il motore non vedrà. I binari non vengono mai resi: il template scrive un solo marcatore visivo per immagine,
    e il suo costo in token lo aggiunge il guard con cfg.image_tokens. I tipi sconosciuti passano invariati, così il
    template li rifiuta a voce alta come ha sempre fatto; la loro verifica sta in picture_contract."""
    if c is None:
        return ""
    if isinstance(c, str):
        return c
    out = []
    for j, p in enumerate(c):
        if isinstance(p, str):
            out.append({"type": "text", "text": p})
        elif not isinstance(p, dict):
            continue
        elif p.get("type") in IMAGE_TYPES:
            out.append({"type": "image", "source": picture_source(p, "%s[%d]" % (where, j))})
        elif p.get("type") in ("text", "input_text", "output_text", None):
            out.append({"type": "text", "text": p.get("text") or ""})
        else:
            out.append(dict(p))
    return out


# una parte che porta un'immagine senza il type che l'ingressore legge: verrebbe scartata dal prompt. Solo chiavi
# che di immagine parlano davvero: un source di testo è un documento Anthropic, un file_id da solo è un file, e
# nessuno dei due è un'immagine persa - il motore li scarta già oggi, senza che nessuno prometta un marker.
def _hidden_picture(p: dict) -> bool:
    if any(p.get(k) not in (None, "", {}, []) for k in ("image_url", "image")):
        return True
    src = p.get("source")
    return isinstance(src, dict) and (src.get("type") == "base64"
                                      or str(src.get("media_type", "")).startswith("image/"))


def picture_contract(messages) -> int:
    """Quante immagini il motore riceverà davvero, o un rifiuto esplicito.

    Tre casi che lasciar passare significa perdere un'immagine in silenzio - il prompt contato non sarebbe quello
    che l'ingressore legge:

      - un'immagine in un messaggio system di testa: Strata gli passa solo le parti testuali e il template del pack
        rifiuta le sue immagini con raise_exception, quindi il binario non arriva mai al motore;
      - una parte immagine senza sorgente leggibile (un file_id, un url vuoto): il motore riceverebbe una sorgente
        vuota, e il guard avrebbe già contato i token di un'immagine che non c'è;
      - una parte che porta un'immagine sotto una chiave che l'ingressore non legge (un type:text con image_url):
        verrebbe scartata dal prompt senza che nessuno lo dica.

    Il conteggio segue la regola di _has_image di Strata - ciò che il motore legge - e il guard lo confronta con i
    marcatori che il template ha scritto: le due cifre devono coincidere, altrimenti il budget del prompt
    è una bugia."""
    n = 0
    head = leading_systems(messages or [])
    for i, m in enumerate(messages or []):
        if not isinstance(m, dict):
            continue
        c = m.get("content")
        if not isinstance(c, list):
            continue
        for j, p in enumerate(c):
            if not isinstance(p, dict):
                continue
            t = p.get("type")
            if t in VIDEO_TYPES:
                raise UnsupportedPicture("messages[%d]: video content is not supported by the engine: this model "
                                         "reads text and images" % i)
            if t in IMAGE_TYPES:
                if i < head:
                    raise UnsupportedPicture(
                        "messages[%d]: a system message cannot carry a picture - the engine reads its text parts "
                        "only and the pack template refuses its images, so the picture would never reach the "
                        "model" % i)
                picture_source(p, "messages[%d].content[%d]" % (i, j))
                n += 1
            elif _hidden_picture(p):
                raise UnsupportedPicture(
                    "messages[%d].content[%d]: a picture under the type %r, which the engine does not read as an "
                    "image - it would be dropped from the prompt" % (i, j, t))
    return n


def stamp_images(text: str, hashes) -> str:
    """Firma delle immagini nei punti esatti in cui il template ha scritto il marcatore.

    Serve all'identità dello stato del motore (autosave): i marcatori VISION sono identici fra immagini diverse,
    quindi due prompt con lo stesso testo e immagini diverse darebbero lo stesso phash, e un restore riporterebbe
    un KV che contiene l'embedding di un'altra immagine. Il testo firmato non viene mai inviato al motore."""
    parts = text.split(VISION)
    if len(parts) == 1:
        return text
    out = [parts[0]]
    for i, tail in enumerate(parts[1:]):
        out.append(VISION + "<img:%s>" % (hashes[i] if i < len(hashes) else i))
        out.append(tail)
    return "".join(out)


def literal_marker_count(messages, tools=None) -> int:
    """I marcatori visivi scritti *come testo*: una citazione di `<|vision_start|><|image_pad|><|vision_end|>`
    dentro un messaggio non è un'immagine.

    Il template stampa la citazione verbatim, quindi il prompt renderizzato porta una scrittura reale del marcatore
    che nessuna immagine ha prodotto: contarla come tale significherebbe prezzare pochi caratteri come un'immagine
    intera e, volendo i marcatori uguagliare le immagini, rifiutare una domanda di solo testo. Strata scuda le
    scritture letterali (#28), quindi neanche il motore ci legge un'immagine. Si cammina solo ciò che il template
    stampa (contenuto, ragionamento, argomenti dei tool call decodificati: il renderer legge le sequenze di escape
    JSON), saltando il sottoalbero di un'immagine, dove il marcatore sarebbe nei byte della fonte e non nel prompt.
    Una volta sola per gli argomenti: contarli grezzi e decodificati toglierebbe più marcatori di quanti ce ne sono."""

    def count(value, picture: bool = False) -> int:
        if isinstance(value, str):
            return 0 if picture else value.count(VISION)
        if isinstance(value, dict):
            return sum(count(k) + count(v, picture or k in IMAGE_KEYS) for k, v in value.items())
        if isinstance(value, list):
            return sum(count(v, picture) for v in value)
        return 0

    n = count(tools)
    for m in messages or []:
        if not isinstance(m, dict):
            continue
        n += count(m.get("content")) + count(m.get("reasoning_content"))
        for call in m.get("tool_calls") or []:
            args = (call.get("function") or call).get("arguments")
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except ValueError:
                    pass
            n += count(args)
    return n


# un payload d'immagine nominato dentro il testo: un data: URI, o un base64 così lungo che nessun testo lo è.
# Il selettore largo (64 caratteri dopo la virgola) è ciò che un data: URI porta davvero; il base64 nudo ne chiede
# 512, perché una parola lunga non deve diventare un'immagine.
_IMAGE_B64 = re.compile(r"data:[\w.+-]+/[\w.+-]{0,64}(?:;[\w=.+-]+)*;base64,[A-Za-z0-9+/=]{64,}"
                        r"|[A-Za-z0-9+/]{512,}={0,2}")


def _fold_blobs(value: str) -> str:
    """Chiude in un segnaposto i payload d'immagine scritti dentro una stringa (data: URI, base64 nudo)."""
    if len(value) < 64 or not _IMAGE_B64.search(value):
        return value
    from .core import H          # import tardivo: core importa render
    return _IMAGE_B64.sub(lambda m: "<immagine:%s,%d>" % (H(m.group())[:16], len(m.group())), value)


def _fold_value(v):
    """`_fold_blobs` ovunque il testo di un messaggio è testo: le chiavi che nominano un'immagine le ha già
    trattate redact_images, e i loro byte non sono prompt."""
    if isinstance(v, str):
        return _fold_blobs(v)
    if isinstance(v, dict):
        return {k: (v[k] if k in IMAGE_KEYS else _fold_value(v[k])) for k in v}
    if isinstance(v, list):
        return [_fold_value(x) for x in v]
    return v


LITERAL_MARKER = "<literal-vision-marker>"   # segnaposto di una citazione del marcatore, mai un'immagine


def quote_markers(messages):
    """Copia dei messaggi in cui una *citazione* del marcatore visivo diventa un segnaposto.

    Chi incolla `<|vision_start|><|image_pad|><|vision_end|>` in un messaggio (una domanda sul template, un log)
    mette nel prompt una scrittura reale del marcatore che nessuna immagine ha prodotto. Per chi conta le immagini
    - la firma dello stato del motore - quel marcatore non deve essere un'immagine: altrimenti assorbe una firma e
    le immagini reali restano disallineate, cioè una di esse viene nominata dall'hash di un'altra, che è esattamente
    ciò la firma impedisce. Si copiano solo i messaggi che contengono la scrittura, non tutto il prompt."""
    if not isinstance(messages, list) or literal_marker_count(messages) == 0:
        return messages          # nessun prompt senza citazione viene ricostruito: i byte restano quelli di prima

    def fold(value):
        if isinstance(value, str):
            return value.replace(VISION, LITERAL_MARKER) if VISION in value else value
        if isinstance(value, dict):
            return {k: (value[k] if k in IMAGE_KEYS else fold(value[k])) for k in value}
        if isinstance(value, list):
            return [fold(v) for v in value]
        return value

    out = []
    for m in messages or []:
        if not isinstance(m, dict) or not literal_marker_count([m]):
            out.append(m)
            continue
        out.append({k: (v if k in IMAGE_KEYS else fold(v)) for k, v in m.items()})
    return out


def redact_text(value):
    """Il linguaggio di `redact_images` per ciò che lista di messaggi non è: la risposta del motore in live_dump
    (che può citare un data: URI), un argomento di tool call preso da solo. Piega i payload d'immagine nominati nel
    testo, lasciando stare le chiavi che un'immagine la nominano già: sono quelle che redact_images tratta prima."""
    return _fold_value(value)


def redact_images(messages):
    """Copia di `messages` senza i binari: le fonti immagine diventano il loro segnaposto con hash, e così i
    payload nominati dentro il testo (un data: URI in un risultato di tool, base64 in un argomento di tool call).

    Serve ai dump su disco - il sidecar .bin.json dei salvataggi KV e live_dump - che non devono moltiplicare
    megabyte di base64 a ogni segmento, e che sono la stessa lingua del journal: un'immagine vi è nominata da un
    hash, mai dai suoi byte. Il prompt inviato al motore non passa di qui, quindi i binari dell'immagine restano
    intatti dove servono."""
    if not isinstance(messages, list):
        return messages
    out, touched = [], False
    for m in messages:
        if isinstance(m, dict) and _has_image(m.get("content")):
            parts = []
            for p in m["content"]:
                if isinstance(p, dict) and p.get("type") in IMAGE_TYPES:
                    parts.append({"type": p.get("type"), "image_url": {"url": "<immagine:%s>" % image_hash(p)},
                                  "redacted": True})
                else:
                    parts.append(_fold_value(p))
            m, touched = {**m, "content": parts}, True
        folded = _fold_value(m)
        if folded != m:
            m, touched = folded, True
        out.append(m)
    return out if touched else messages


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
