# Author: Maurizio Verde — LastReload
"""Portability helpers (Linux / macOS / native Windows), standard library only.

- `lock_fd` / `unlock_fd`: exclusive, blocking lock on an open file descriptor. POSIX: `fcntl.flock` (unchanged
  behaviour); Windows: `msvcrt.locking` on the first byte, retried until it is granted.
- `console_safe`: a Windows console (cp1252, cp850...) cannot encode characters like ⟪ 📌 🗑, and the bytes it can
  encode are read back with whatever code page the terminal happens to use, so `·`, `—`, `«»` come out as `?`.
  Non-UTF-8 stdout/stderr are therefore wrapped so that what reaches them is plain ASCII, with readable stand-ins
  (`⟪` -> `<<`, `—` -> `--`, `✅` -> `[ OK ]`-style icons are already ASCII in `check`); UTF-8 streams are
  untouched and keep every character. Only the console view changes: the text sent to the engine, written into
  the archive, the journal and `last_request.json` never passes through here.
- `sqlite_ro_uri`: `file:` URI for a read-only sqlite connection that also works with `C:\\...` paths.
- `bad_name`: characters that cannot appear in a single file name on this OS.
"""
from __future__ import annotations

import os
import sys
import time

WINDOWS = os.name == "nt"

if WINDOWS:  # pragma: no cover - exercised on the windows-latest CI runner
    import msvcrt

    def lock_fd(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        while True:
            try:
                msvcrt.locking(fd, msvcrt.LK_LOCK, 1)   # LK_LOCK itself retries for ~10 s, then OSError
                return
            except OSError:
                time.sleep(0.05)

    def unlock_fd(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
else:
    import fcntl

    def lock_fd(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_EX)

    def unlock_fd(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_UN)


# ASCII stand-ins for every non-ASCII character the proxy can print. Used only on a non-UTF-8 console: there the
# code page the bytes are written with and the one the terminal reads them back with need not agree, so anything
# outside ASCII is a guess. `·` and `—` separate the fields of a placeholder, `«»` quote a verbatim line, `⟪⟫`
# open and close a receipt: the stand-ins keep them visible without a single byte above 0x7F.
ASCII_FALLBACK = {
    "\u27ea": "<<", "\u27eb": ">>",                     # receipt / placeholder brackets
    "\u00ab": "<<", "\u00bb": ">>",                     # quoted verbatim line
    "\u00b7": "-", "\u2014": "--", "\u2013": "-",       # field separators, dashes
    "\u2026": "...", "\u22ee": "...",                   # ellipsis, leaf-select gap
    "\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"',
    "\u2192": "->", "\u2190": "<-", "\u25c0": "<", "\u2502": "|", "\u23ce": "\\n",
    "\u00d7": "x", "\u00b1": "+/-", "\u2265": ">=", "\u2264": "<=", "\u2248": "~",
    "\u2705": "[ OK ]", "\u274c": "[FAIL]", "\u26a0": "[WARN]", "\u2139": "[INFO]",
    "\u2713": "ok", "\u2717": "x", "\u2716": "x",
    "\U0001f4cc": "[pin]", "\U0001f5d1": "[del]",
    "\ufe0f": "", "\u200b": "", "\u00a0": " ",          # variation selector, zero width, nbsp
}
_TABLE = {ord(k): v for k, v in ASCII_FALLBACK.items()}


def ascii_only(text: str) -> str:
    """`text` with every non-ASCII character replaced: the table above first, then the accent-stripped form
    (`défaut` -> `defaut`), then `?`. The result is pure ASCII, so no code page can corrupt it."""
    if text.isascii():
        return text
    import unicodedata
    out = []
    for ch in text.translate(_TABLE):
        if ch.isascii():
            out.append(ch)
            continue
        folded = "".join(c for c in unicodedata.normalize("NFKD", ch) if ord(c) < 128)
        out.append(folded or "?")
    return "".join(out)


class AsciiConsole:
    """Write-through view of a text stream that lets only ASCII reach it. Everything else is delegated, so
    `.encoding`, `.buffer`, `.fileno()` and `.isatty()` are those of the real stream."""

    def __init__(self, stream):
        self._stream = stream

    def write(self, text):
        self._stream.write(ascii_only(text))
        return len(text)

    def writelines(self, lines):
        for line in lines:
            self.write(line)

    def __getattr__(self, name):
        return getattr(self._stream, name)


def utf8_stream(s) -> bool:
    """True if `s` already carries every character unchanged (UTF-8, or no declared encoding at all)."""
    enc = (getattr(s, "encoding", None) or "").lower().replace("-", "").replace("_", "")
    return enc in ("utf8", "utf8sig")


def console_safe(*streams):
    """Make the console able to SHOW the proxy's output, whatever its code page. UTF-8 streams are returned as
    they are (byte for byte the output of every earlier version); a non-UTF-8 stream is wrapped in `AsciiConsole`
    and also moved to `errors="replace"`, so no character can raise UnicodeEncodeError.

    Called with no arguments it installs the result on `sys.stdout` and `sys.stderr` (the command entry points);
    called with streams it returns them, wrapped or not, in the same order."""
    wrapped = []
    for s in streams or (sys.stdout, sys.stderr):
        if s is None or utf8_stream(s):
            wrapped.append(s)
            continue
        if hasattr(s, "reconfigure"):
            try:
                s.reconfigure(errors="replace")
            except (ValueError, OSError):
                pass
        wrapped.append(AsciiConsole(s))
    if not streams:
        sys.stdout, sys.stderr = wrapped
        return None
    return wrapped[0] if len(wrapped) == 1 else tuple(wrapped)


def sqlite_ro_uri(path: str) -> str:
    """Read-only sqlite URI. POSIX: `file:/abs/path?mode=ro` (as before). Windows: `file:///C:/abs/path?mode=ro`."""
    p = os.path.abspath(path)
    if WINDOWS:
        p = "/" + p.replace("\\", "/")
    if WINDOWS:
        return "file://%s?mode=ro" % p.replace("%", "%25").replace("?", "%3f").replace("#", "%23")
    return "file:%s?mode=ro" % p.replace("%", "%25").replace("?", "%3f").replace("#", "%23")


def replace(src: str, dst: str, tries: int = 50) -> None:
    """os.replace. On Windows a destination briefly open in another process (a reader of last_request.json, an
    antivirus scan) makes it fail with PermissionError: retry for up to ~2.5 s. POSIX: a single os.replace."""
    if not WINDOWS:
        os.replace(src, dst)
        return
    for i in range(tries):  # pragma: no cover - Windows only
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if i == tries - 1:
                raise
            time.sleep(0.05)


O_BINARY = getattr(os, "O_BINARY", 0)   # Windows: os.open is text mode (LF -> CRLF on write) without it


def bad_name(name: str) -> bool:
    """True if `name` is not a plain file name here (path separators; on Windows also `:` and reserved chars)."""
    if "/" in name or name.startswith("."):
        return True
    if WINDOWS:
        return any(c in name for c in '\\:*?"<>|') or name.rstrip(" .") != name
    return False
