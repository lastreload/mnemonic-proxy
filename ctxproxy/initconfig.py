# Author: Maurizio Verde — LastReload
"""`init-config`: the minimal starting configuration of each engine, and the one-click launcher.

`pip install mnemonic-proxy` installs the `ctxproxy` package: the repository folder `examples/` is simply not
there, so a README line or a `check` hint that says `examples/config.llama-server.json` names a path the user
does not have (friction A2 of the 0.3.23 "new user" trial). Three answers live here:

  * `url()` prints the FULL raw URL of the file *for the version that is installed* — the `vX.Y.Z` tag of a
    release, `main` for a development version (0.3.23.dev0), because that tag does not exist yet;
  * the examples ship inside the wheel (`ctxproxy.examples`, mapped from the repository's `examples/`), so
    `mnemonic-proxy init-config --engine ... --full` writes one on disk with no network at all;
  * `init-config` *without* `--full` writes a MINIMAL file (0.3.24, below), and `--launcher` writes the
    one-click starter for the platform it runs on.

A minimal file, and why (0.3.24, reported by the first Windows user)
--------------------------------------------------------------------
Until 0.3.23 `init-config --engine strata` wrote `examples/config.example.json`: sixteen explicit fields,
among them `window` 131072, a `/path/to/strata/sessions` placeholder, `slot_save`/`mask_anchor`/`autosave`
true, `notes_max_tokens` 12288, `response_floor` 16384, `live_dump` true (the whole prompt to disk) and
`seal_experimental` true. A field written in the config always wins over the automatic configuration
(`autoconfig.py`), so that file *switched the automatic configuration off*. On an engine with a 115K window,
or on a machine without the RAM for a session file, the command `check` itself suggests handed the user a
wrong setup.

So the minimal file holds only what nothing else can work out:

  * `slot_dir` — the engine's `--slot-save-path`; only the user knows it, and it is the one thing
    `autoconfig.saved_state_gate` names as missing before it turns saved state on by itself. Written only
    when `--slot-dir DIR` is passed: a *placeholder* there would be worse than nothing.
  * `window` — for a plain OpenAI-compatible engine, which reports no context at all. Strata, llama-server,
    ds4-server, Ollama and LM Studio are read from the engine, so for those it must NOT be written.
  * the explicit `false` of base mode (`official-strata`): the engine can save state, so only the user can
    say "do not".

Everything else is left out: `slot_save`, `mask_anchor`, `autosave`, `engine_tokenize`, `response_floor` and
`notes_max_tokens` are chosen by `autoconfig.decide()` from the engine and the machine; the window-sized
thresholds scale from the window (`engines.apply`); `live_dump` writes every prompt to disk and has no place
in a starting file; `seal_experimental` and `kv_archive` stay opt-in on purpose.

**`"auto_config": true` is never written**, although the automatic configuration is exactly what this file
leaves in charge. It is already the default (`Config.auto_config`), so on a new data folder it adds nothing;
on a data folder that already holds conversations it would *remove* a protection — `autoconfig.decide()` only
applies its choices there when `auto_config` is explicit, precisely so that an upgrade cannot silently change
how a running setup behaves (and, on PolyStrata older than v0.1.45-poly.7, could switch saved state on). The
full example stays one `--full` away for anyone who wants every field spelled out.

Standard library only; nothing here ever reaches the network.
"""
from __future__ import annotations

import json
import os
import platform
import stat

REPO = "lastreload/mnemonic-proxy"
RAW = "https://raw.githubusercontent.com/%s/%s/examples/%s"
TREE = "https://github.com/%s/tree/%s/examples"

# engine alias -> (example file, what it is for). The four aliases of the README's Install section come
# first; the others name the remaining shipped examples, so every file can be written by `init-config`.
ENGINES: dict[str, tuple[str, str]] = {
    "strata": ("config.example.json",
               "Strata >= 0.1.40 with saved state: set slot_dir to Strata's --slot-save-path"),
    "llama-server": ("config.llama-server.json",
                     "llama-server started with --slot-save-path ./slots"),
    "ds4": ("config.ds4.json",
            "ds4-server, which keeps its own state with --kv-disk-dir"),
    "openai-32k": ("config.openai-32k.json",
                   "any OpenAI-compatible engine with a 32768-token window (Ollama, LM Studio, vLLM, APIs)"),
    "official-strata": ("config.official-strata.json",
                        "any Strata, base mode: no saved state, nothing to add to the engine"),
    "llama-server-32k": ("config.llama-server-32k.json",
                         "llama-server at -c 32768 (the T1 trial on CPU)"),
    "llama-server-router": ("config.llama-server-router.json",
                            "llama-server router mode, several models, one window each"),
}

# --- the minimal starting configuration ---------------------------------------------------------------
#
# alias -> (fields, accepts --slot-dir, one line about what was deliberately left out). Read the module
# docstring for the reasoning; the test suite re-checks every entry against autoconfig.AUTO_FIELDS.
PLACEHOLDER_SLOT_DIR = "/path/to/strata/sessions"

AUTO_LINE = ("the saved-state options, the exact token count and the window-sized budgets are chosen by the "
             "proxy from the engine and the machine (`check --explain-config` prints every reason)")

MINIMAL: dict[str, tuple[dict, bool, str]] = {
    # Strata reports its window on /v1/status and saved state is decided from the engine, the RAM and the
    # disk: with --slot-dir this file is one line, without it none.
    "strata": ({}, True,
               "nothing is written: Strata reports its window and " + AUTO_LINE),
    "llama-server": ({}, True,
                     "nothing is written: llama-server reports n_ctx and " + AUTO_LINE),
    "ds4": ({}, False,
            "nothing is written: ds4-server reports its --ctx, keeps its own state, and " + AUTO_LINE),
    # a plain OpenAI-compatible engine reports no context at all: `window` is the one thing it cannot
    # discover, and every threshold then scales from it (engines.apply)
    "openai-32k": ({"window": 32768}, False,
                   "only the window, which a generic engine does not report; every threshold scales from "
                   "it and " + AUTO_LINE),
    # the engine *can* save state, so base mode is a choice only the user can make
    "official-strata": ({"slot_save": False, "autosave": False}, False,
                        "only the explicit \"no\" to saved state (base mode); the window and " + AUTO_LINE),
    "llama-server-32k": ({}, True,
                         "nothing is written: the window comes from llama-server's n_ctx, the thresholds "
                         "scale to it, and " + AUTO_LINE),
    "llama-server-router": ({}, True,
                            "nothing is written: in router mode every model's window and thresholds come "
                            "from the engine, and " + AUTO_LINE),
}

LAUNCHER_STEM = "start-mnemonic-proxy"


def ref(version: str | None = None) -> str:
    """-> the git ref whose examples match this build: `vX.Y.Z` for a release, `main` for anything else.

    A development version (`0.3.23.dev0`, `0.4.0rc1`) has no tag yet, so a `vX.Y.Z` URL would be a 404.
    """
    if version is None:
        from . import __version__
        version = __version__
    parts = version.split(".")
    if len(parts) >= 3 and all(p.isdigit() for p in parts[:3]) and len(parts) == 3:
        return "v" + version
    return "main"


def url(name: str, version: str | None = None) -> str:
    """-> the full raw URL of one example file, for the version that is installed."""
    return RAW % (REPO, ref(version), name)


def folder_url(version: str | None = None) -> str:
    """-> the URL of the whole examples folder, for the version that is installed."""
    return TREE % (REPO, ref(version))


def url_for(alias: str, version: str | None = None) -> str:
    """-> the full raw URL of the example of one engine alias."""
    return url(file_for(alias), version)


def file_for(alias: str) -> str:
    try:
        return ENGINES[alias][0]
    except KeyError:
        raise KeyError("unknown engine %r: known engines are %s" % (alias, ", ".join(ENGINES))) from None


def path(name: str) -> str | None:
    """-> the local path of an example file, inside the package or in the repository; None when absent."""
    here = os.path.dirname(os.path.abspath(__file__))
    for candidate in (os.path.join(here, "examples", name),            # installed wheel
                      os.path.join(os.path.dirname(here), "examples", name)):   # source checkout
        if os.path.isfile(candidate):
            return candidate
    return None


def read(alias: str) -> str:
    """-> the text of the example configuration of one engine alias (from the package, no network)."""
    name = file_for(alias)
    p = path(name)
    if p is None:
        raise FileNotFoundError("%s is not in this installation: download it from %s" % (name, url(name)))
    with open(p, encoding="utf-8") as f:
        return f.read()


def engine_hint(kind: str | None, version: str | None = None) -> str:
    """One line of advice about the configuration for the engine that was actually detected (friction A9).

    `kind` is `engines.detect`'s kind (`strata`, `llama.cpp`, `ds4`, `openai`), or None when the engine has
    not been reached yet: then the advice has to stay generic.
    """
    if kind == "strata":
        return ("Strata needs no config: for saved state add only \"slot_dir\" (Strata's --slot-save-path), "
                "or write it with `mnemonic-proxy init-config --engine strata --slot-dir DIR`")
    if kind == "llama.cpp":
        return ("for llama-server: `mnemonic-proxy init-config --engine llama-server --slot-dir ./slots` "
                "(the folder of its --slot-save-path; `--full` writes every field, see %s)"
                % url_for("llama-server", version))
    if kind == "ds4":
        return ("ds4-server keeps its own state and reports its window: it needs no config "
                "(`mnemonic-proxy init-config --engine ds4 --full` writes every field, see %s)"
                % url_for("ds4", version))
    if kind == "openai":
        return ("for an OpenAI-compatible engine at 32K write only the window: `mnemonic-proxy init-config "
                "--engine openai-32k` (every threshold scales from it; `--full` spells them out, see %s); "
                "at 131072 the defaults already fit" % url_for("openai-32k", version))
    return ("`mnemonic-proxy init-config --engine strata|llama-server|ds4|openai-32k` writes a minimal one "
            "(`--full` for every field), or see %s" % folder_url(version))


def missing_config_fix(name: str, version: str | None = None) -> str:
    """What to do about a `--config` file that does not exist (friction A1): the three real options."""
    return ("create it with `mnemonic-proxy init-config --engine strata|llama-server|ds4|openai-32k` "
            "(writes a minimal config.json, `--full` for every field; examples: %s), or drop --config: the "
            "proxy works with the defaults (saved-state features off)" % folder_url(version))


def list_lines(version: str | None = None) -> list[str]:
    """`init-config` without `--engine`: what it can write, one line each."""
    width = max(len(a) for a in ENGINES)
    out = []
    for alias, (name, why) in ENGINES.items():
        fields, slots, _note = MINIMAL[alias]
        shows = ", ".join(sorted(fields)) if fields else "nothing to write"
        if slots:
            shows += " (+ slot_dir with --slot-dir DIR)"
        out.append("  %-*s  %s\n  %-*s  minimal: %s; --full: %s"
                   % (width, alias, why, width, "", shows, name))
    return out


# --- the minimal starting configuration -----------------------------------------------------------------

def accepts_slot_dir(alias: str) -> bool:
    """True when `--slot-dir DIR` means something for this engine (the engine's own --slot-save-path)."""
    try:
        return MINIMAL[alias][1]
    except KeyError:
        raise KeyError("unknown engine %r: known engines are %s" % (alias, ", ".join(ENGINES))) from None


def slot_dir_engines() -> list[str]:
    return [a for a in ENGINES if MINIMAL[a][1]]


def minimal(alias: str, slot_dir: str | None = None) -> dict:
    """-> the minimal starting configuration of `alias`: only what nothing else can work out.

    `slot_dir` is added only for the engines whose saved state lives in a folder the user chose; passing it
    for any other engine is an error, because the field would do nothing there.
    """
    try:
        fields, slots, _note = MINIMAL[alias]
    except KeyError:
        raise KeyError("unknown engine %r: known engines are %s" % (alias, ", ".join(ENGINES))) from None
    out = dict(fields)
    if slot_dir:
        if not slots:
            raise ValueError("--slot-dir does not apply to %s: it is for %s, whose saved state lives in the "
                             "engine's own --slot-save-path folder" % (alias, ", ".join(slot_dir_engines())))
        out["slot_dir"] = slot_dir
    return out


def minimal_text(alias: str, slot_dir: str | None = None) -> str:
    """-> the minimal configuration of `alias` as the JSON text that gets written."""
    return json.dumps(minimal(alias, slot_dir), indent=2, ensure_ascii=False) + "\n"


def minimal_note(alias: str) -> str:
    """-> one line saying what the minimal file leaves out, and who decides it instead."""
    return MINIMAL[alias][2]


def full_text(alias: str, slot_dir: str | None = None) -> str:
    """-> the complete documented example of `alias`, with the slot_dir placeholder filled in if given."""
    text = read(alias)
    if slot_dir:
        if not accepts_slot_dir(alias):
            raise ValueError("--slot-dir does not apply to %s: it is for %s"
                             % (alias, ", ".join(slot_dir_engines())))
        raw = json.loads(text)
        raw["slot_dir"] = slot_dir
        text = json.dumps(raw, indent=2, ensure_ascii=False) + "\n"
    return text


def has_slot_dir_placeholder(alias: str) -> bool:
    """True when the complete example of `alias` carries a `/path/to/...` slot_dir nobody can use as is."""
    try:
        raw = json.loads(read(alias))
    except (OSError, ValueError, KeyError):
        return False
    return str(raw.get("slot_dir", "")).startswith("/path/to")


def write_config(alias: str, directory: str = ".", force: bool = False, filename: str = "config.json",
                 full: bool = False, slot_dir: str | None = None) -> str:
    """Write the starting configuration of `alias` as `directory/config.json`. -> the path written.

    Minimal by default (0.3.24: only what the automatic configuration cannot work out); `full=True` writes
    the complete documented example from the package instead. Never overwrites an existing file unless
    `force`: a config the user already edited is theirs.
    """
    text = full_text(alias, slot_dir) if full else minimal_text(alias, slot_dir)
    json.loads(text)        # never write a file the proxy would then refuse to read
    return _write(os.path.join(directory or ".", filename), text, force)


def _write(target: str, text: str, force: bool, executable: bool = False) -> str:
    os.makedirs(os.path.dirname(os.path.abspath(target)), exist_ok=True)
    if os.path.exists(target) and not force:
        raise FileExistsError(target)
    with open(target, "w", encoding="utf-8", newline="") as f:
        f.write(text if text.endswith(("\n", "\r\n")) else text + "\n")
    if executable and os.name != "nt":
        mode = os.stat(target).st_mode
        os.chmod(target, mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return target


# --- the one-click launcher ------------------------------------------------------------------------------
#
# Asked for by the first Windows user, who had written his own `.bat` by hand: one file to double-click that
# starts the proxy in front of an engine that is already running (Strata, llama-server, anything). The engine
# is NOT started here — a Strata command line is the user's own, and guessing it is how the llama-server
# example (`examples/start-windows.bat`) ends up with four paths to edit. Style is that file's: variables at
# the top, `%~dp0`-relative paths, one `check` before the proxy, `pause` at the end so a console opened by a
# double-click does not vanish with the error in it.

def launcher_name(system: str | None = None) -> str:
    """-> `start-mnemonic-proxy.bat` on Windows, `start-mnemonic-proxy.sh` anywhere else."""
    sysname = system or platform.system()
    return LAUNCHER_STEM + (".bat" if sysname == "Windows" else ".sh")


BAT = """\
@echo off
rem Author: Maurizio Verde - LastReload
rem One click: mnemonic-proxy in front of an engine that is ALREADY RUNNING (Strata, llama-server, ...).
rem Start your engine first, then double-click this file. Written by `mnemonic-proxy init-config --launcher`.
rem Every value below can also come from the environment; nothing here is anybody's personal path.

if not defined UPSTREAM set UPSTREAM={upstream}
if not defined PORT set PORT={port}
if not defined DATA set DATA={data}
if not defined CONFIG set CONFIG={config}
if not defined VENV set VENV={venv}
rem exact token count (recommended): the model's tokenizer folder or tokenizer.json.
rem   pip install "mnemonic-proxy[exact]"
{tokenizer_line}

cd /d "%~dp0"
set PROXY=%VENV%\\Scripts\\mnemonic-proxy.exe
if not exist "%PROXY%" (
  echo mnemonic-proxy is not installed in %VENV%
  echo   python -m venv "%VENV%"
  echo   "%VENV%\\Scripts\\pip.exe" install mnemonic-proxy
  pause
  exit /b 1
)
set TOK=
if defined TOKENIZER set TOK=--tokenizer "%TOKENIZER%"

rem the engine has to answer before the proxy starts: this file does not start it
curl -sf "%UPSTREAM%/v1/models" >nul 2>&1 || curl -sf "%UPSTREAM%/health" >nul 2>&1
if errorlevel 1 (
  echo The engine at %UPSTREAM% does not answer.
  echo Start it first ^(Strata, llama-server, ...^), then run this file again.
  pause
  exit /b 1
)

rem one check before the proxy: 0 = READY, 2 = READY with warnings, 1 = NOT READY.
rem A port %PORT% taken by another program is reported there, with the fix.
"%PROXY%" check --upstream "%UPSTREAM%" --port %PORT% --data "%DATA%" --config "%CONFIG%" %TOK%
if %errorlevel% equ 1 (
  echo.
  echo mnemonic-proxy check: NOT READY, see the lines above.
  echo If port %PORT% is taken, pick a free one: set PORT=8097 and run this file again
  echo ^(the same port goes in the agent's base URL^).
  pause
  exit /b 1
)

rem point the agent at http://127.0.0.1:%PORT%/v1 instead of the engine, and turn its own compaction off
title mnemonic-proxy %PORT%
"%PROXY%" --upstream "%UPSTREAM%" --port %PORT% --data "%DATA%" --config "%CONFIG%" %TOK%
pause
"""

SH = """\
#!/bin/sh
# Author: Maurizio Verde - LastReload
# One click: mnemonic-proxy in front of an engine that is ALREADY RUNNING (Strata, llama-server, ...).
# Start your engine first, then run this file. Written by `mnemonic-proxy init-config --launcher`.
# Every value below can also come from the environment; nothing here is anybody's personal path.
set -eu

HERE=$(cd "$(dirname "$0")" && pwd)
UPSTREAM="${{UPSTREAM:-{upstream}}}"
PORT="${{PORT:-{port}}}"
DATA="${{DATA:-{data}}}"
CONFIG="${{CONFIG:-{config}}}"
VENV="${{VENV:-{venv}}}"
# exact token count (recommended): the model's tokenizer folder or tokenizer.json.
#   pip install 'mnemonic-proxy[exact]'
{tokenizer_line}

cd "$HERE"
PROXY="$VENV/bin/mnemonic-proxy"
if [ ! -x "$PROXY" ]; then
  PROXY=$(command -v mnemonic-proxy || true)
fi
if [ -z "$PROXY" ] || [ ! -x "$PROXY" ]; then
  echo "mnemonic-proxy is not installed in $VENV:" >&2
  echo "  python3 -m venv \\"$VENV\\" && \\"$VENV/bin/pip\\" install mnemonic-proxy" >&2
  exit 1
fi

set -- --upstream "$UPSTREAM" --port "$PORT" --data "$DATA" --config "$CONFIG"
if [ -n "${{TOKENIZER:-}}" ]; then
  set -- "$@" --tokenizer "$TOKENIZER"
fi

# the engine has to answer before the proxy starts: this file does not start it
if ! curl -sf "$UPSTREAM/v1/models" >/dev/null 2>&1 \\
   && ! curl -sf "$UPSTREAM/health" >/dev/null 2>&1; then
  echo "The engine at $UPSTREAM does not answer." >&2
  echo "Start it first (Strata, llama-server, ...), then run this file again." >&2
  exit 1
fi

# one check before the proxy: 0 = READY, 2 = READY with warnings, 1 = NOT READY.
# A port $PORT taken by another program is reported there, with the fix.
rc=0
"$PROXY" check "$@" || rc=$?
if [ "$rc" = 1 ]; then
  echo >&2
  echo "mnemonic-proxy check: NOT READY, see the lines above." >&2
  echo "If port $PORT is taken, pick a free one: PORT=8097 $0" >&2
  echo "(the same port goes in the agent's base URL)." >&2
  exit 1
fi

# point the agent at http://127.0.0.1:$PORT/v1 instead of the engine, and turn its own compaction off
exec "$PROXY" "$@"
"""


def launcher_text(system: str | None = None, upstream: str = "http://127.0.0.1:8095", port: int = 8096,
                  data: str = "", config: str = "", tokenizer: str = "") -> str:
    """-> the text of the one-click launcher for `system`, with the paths that were actually asked for."""
    sysname = system or platform.system()
    windows = sysname == "Windows"
    if windows:
        line = ('if not defined TOKENIZER set TOKENIZER=%s' % tokenizer) if tokenizer else \
               'rem if not defined TOKENIZER set TOKENIZER=C:\\models\\mymodel\\tokenizer'
        text = BAT.format(upstream=upstream, port=int(port), venv="%USERPROFILE%\\mnemonic-venv",
                          data=data or "%~dp0data", config=config or "%~dp0config.json",
                          tokenizer_line=line)
        return text.replace("\r\n", "\n").replace("\n", "\r\n")
    line = ('TOKENIZER="${TOKENIZER:-%s}"' % tokenizer) if tokenizer else \
           '# TOKENIZER="${TOKENIZER:-/path/to/model/tokenizer}"'
    return SH.format(upstream=upstream, port=int(port), venv="$HOME/mnemonic-venv",
                     data=data or "$HERE/data", config=config or "$HERE/config.json",
                     tokenizer_line=line)


def write_launcher(directory: str = ".", force: bool = False, system: str | None = None, **values) -> str:
    """Write the one-click launcher in `directory`. -> the path written."""
    target = os.path.join(directory or ".", launcher_name(system))
    return _write(target, launcher_text(system, **values), force, executable=True)

