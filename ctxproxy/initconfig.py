# Author: Maurizio Verde — LastReload
"""Where the example configurations are, and the URL of the one that matches the installed version.

`pip install mnemonic-proxy` installs the `ctxproxy` package: the repository folder `examples/` is simply not
there, so a README line or a `check` hint that says `examples/config.llama-server.json` names a path the user
does not have (friction A2 of the 0.3.23 "new user" trial). Two answers live here:

  * `url()` prints the FULL raw URL of the file *for the version that is installed* — the `vX.Y.Z` tag of a
    release, `main` for a development version (0.3.23.dev0), because that tag does not exist yet;
  * the examples ship inside the wheel (`ctxproxy.examples`, mapped from the repository's `examples/`), so
    `mnemonic-proxy init-config --engine ...` writes one on disk with no network at all.

Standard library only; nothing here ever reaches the network.
"""
from __future__ import annotations

import json
import os

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
                "or write the example with `mnemonic-proxy init-config --engine strata`")
    if kind == "llama.cpp":
        return ("for llama-server: `mnemonic-proxy init-config --engine llama-server` (or download %s)"
                % url_for("llama-server", version))
    if kind == "ds4":
        return ("ds4-server keeps its own state: `mnemonic-proxy init-config --engine ds4` (or download %s)"
                % url_for("ds4", version))
    if kind == "openai":
        return ("for an OpenAI-compatible engine at 32K: `mnemonic-proxy init-config --engine openai-32k` "
                "(or download %s); at 131072 the defaults already fit" % url_for("openai-32k", version))
    return ("`mnemonic-proxy init-config --engine strata|llama-server|ds4|openai-32k` writes one, or see %s"
            % folder_url(version))


def missing_config_fix(name: str, version: str | None = None) -> str:
    """What to do about a `--config` file that does not exist (friction A1): the three real options."""
    return ("create it with `mnemonic-proxy init-config --engine strata|llama-server|ds4|openai-32k` "
            "(writes config.json; examples: %s), or drop --config: the proxy works with the defaults "
            "(saved-state features off)" % folder_url(version))


def list_lines(version: str | None = None) -> list[str]:
    """`init-config` without `--engine`: what it can write, one line each."""
    width = max(len(a) for a in ENGINES)
    return ["  %-*s  %s  (%s)" % (width, alias, why, name) for alias, (name, why) in ENGINES.items()]


def write_config(alias: str, directory: str = ".", force: bool = False, filename: str = "config.json") -> str:
    """Write the example of `alias` as `directory/config.json`. -> the path written.

    Never overwrites an existing file unless `force`: a config the user already edited is theirs.
    """
    text = read(alias)
    json.loads(text)        # never write a file the proxy would then refuse to read
    os.makedirs(directory or ".", exist_ok=True)
    target = os.path.join(directory or ".", filename)
    if os.path.exists(target) and not force:
        raise FileExistsError(target)
    with open(target, "w", encoding="utf-8") as f:
        f.write(text if text.endswith("\n") else text + "\n")
    return target
