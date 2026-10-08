# Author: Maurizio Verde — LastReload
"""Language of the texts the proxy writes into the model's prompt (0.3.1).

New conversations use `Config.prompt_language` (default "en"). The choice is stored per conversation on first use
(kv `prompt_language:<conv>`), so the physical prompt stays identical across requests and restarts. A conversation
that already has data in the archive but no stored language was born before 0.3.1 and keeps Italian: its stable
prefix (archive, session files, KV cache) stays valid byte for byte.

Recognition of the proxy's own markers (placeholders, receipts, guard, notes request...) accepts BOTH languages in
every conversation: an Italian marker inside an English conversation (or the reverse) is still recognized.

The Italian texts are kept verbatim from 0.3.0; do not edit them (old prefixes depend on them).
"""
from __future__ import annotations

LANGS = ("en", "it")
LANG_KEY = "prompt_language:"      # kv: language chosen for a conversation ("en" | "it")


def norm(lang) -> str:
    """'it' -> 'it'; anything else -> 'en'."""
    return "it" if str(lang or "").strip().lower() == "it" else "en"


def pick(lang: str, it: str, en: str) -> str:
    return it if lang == "it" else en


# Image note (api_anthropic / api_responses). The API layer writes the Italian one (0.3.0 text) because it runs
# BEFORE the conversation is known and the note is part of the hashed history (chain): changing it would make an
# existing conversation unrecognizable. Manager.build() swaps it for the English one in conversations with lang "en".
IMAGE_NOTE_IT = "[immagine omessa: il proxy non inoltra immagini al motore]"
IMAGE_NOTE_EN = "[image omitted: the proxy does not forward images to the engine]"
# Same reasoning for the description of the `input` parameter of Responses custom tools (it is in the hashed tool
# list): the API layer keeps the 0.3.0 text, Manager.prepare() swaps it in the physical tools of "en" conversations.
CUSTOM_INPUT_DESC_IT = "testo libero passato allo strumento"
CUSTOM_INPUT_DESC_EN = "free text passed to the tool"


# outcome() values are internal and stored in the archive (fileops.outcome): they stay Italian; only what the
# model reads is translated.
OUTCOME_EN = {"riuscito": "succeeded", "fallito": "failed", "non verificato": "not verified"}


def outcome_text(o: str, lang: str) -> str:
    return o if lang == "it" else OUTCOME_EN.get(o, o)


# role names used in recall headers
ROLE_IT = {"tool": "uscita di strumento", "assistant-reasoning": "ragionamento", "assistant-tool-args":
           "argomenti di chiamata", "user": "utente", "assistant": "risposta", "system": "sistema"}
ROLE_EN = {"tool": "tool output", "assistant-reasoning": "reasoning", "assistant-tool-args": "call arguments",
           "user": "user", "assistant": "answer", "system": "system"}


def role_text(role: str, lang: str) -> str:
    return (ROLE_IT if lang == "it" else ROLE_EN).get(role, role)
