#!/usr/bin/env python3
"""The command lines of the README, and the real argument parsers to check them against.

`commands(text)` turns a shell (or PowerShell) block into a list of argv lists: line
continuations joined, comments dropped, `&&`/`;`/`|` honoured, quoting respected.

`check_argv(argv)` validates one argv against the *real* parser of the program it calls
(built from the code, never a copy of it) and returns None when it is fine or a string
describing the problem.

Standard library only.
"""
from __future__ import annotations

import argparse
import io
import os
import re
import shlex
import sys

OPERATORS = (";", "&&", "||", "|", "&")
# wrappers that may precede the real command in a README line
WRAPPERS = {"sudo", "uvx", "time", "exec", "env", "nohup"}

# argv[0] (basename, suffix removed) -> how to build the parser
PROG_SERVER = ("mnemonic-proxy", "ctxproxy")
PROG_MCP = ("mnemonic-mcp",)


def _logical_lines(text: str) -> list[str]:
    out: list[str] = []
    cur = ""
    for raw in text.splitlines():
        line = raw.rstrip()
        if line.endswith("\\"):
            cur += line[:-1] + " "
            continue
        if line.endswith("`"):          # PowerShell continuation
            cur += line[:-1] + " "
            continue
        out.append(cur + line)
        cur = ""
    if cur.strip():
        out.append(cur)
    return [line for line in out if line.strip()]


def _tokenize(line: str) -> list[str]:
    # <id>, <request 1>, <PORT>: README placeholders, not shell redirections
    line = re.sub(r"<([A-Za-z][A-Za-z0-9_. -]*)>", lambda m: "PLACEHOLDER_" + re.sub(r"\W", "_", m.group(1)), line)
    lex = shlex.shlex(line, posix=True, punctuation_chars=True)
    lex.whitespace_split = True
    try:
        return list(lex)
    except ValueError:
        return line.split()


def commands(text: str) -> list[list[str]]:
    """Every command in a shell/PowerShell block, as argv lists.

    A `--` separator (`claude mcp add name -- mnemonic-mcp --db …`) also yields its tail as
    a command of its own, so the wrapped invocation gets checked too.
    """
    out: list[list[str]] = []
    for line in _logical_lines(text):
        cur: list[str] = []
        for tok in _tokenize(line):
            if tok in OPERATORS:
                if cur:
                    out.append(cur)
                cur = []
                continue
            cur.append(tok)
        if cur:
            out.append(cur)
    expanded: list[list[str]] = []
    for argv in out:
        expanded.append(argv)
        if "--" in argv[1:]:
            tail = argv[argv.index("--", 1) + 1:]
            if tail:
                expanded.append(tail)
    return [c for c in (_strip_wrappers(c) for c in expanded) if c]


def _strip_wrappers(argv: list[str]) -> list[str]:
    i = 0
    while i < len(argv):
        tok = argv[i]
        if "=" in tok and not tok.startswith("-") and "/" not in tok.split("=")[0]:
            i += 1                       # VAR=value prefix
            continue
        if tok == "export":
            return []                    # a variable assignment, not a command
        base = os.path.basename(tok)
        if base in WRAPPERS:
            i += 1
            continue
        break
    return argv[i:]


def program(argv: list[str]) -> str | None:
    """Which of our entry points this argv calls, or None."""
    if not argv:
        return None
    base = os.path.basename(argv[0])
    for suffix in (".exe", ".cmd", ".bat"):
        if base.lower().endswith(suffix):
            base = base[: -len(suffix)]
    if base in PROG_SERVER:
        return "server"
    if base in PROG_MCP:
        return "mcp"
    # python -m ctxproxy..., python -m tests.fake_engine
    if base.startswith("python") or base in ("py", "python3"):
        rest = argv[1:]
        while rest and rest[0].startswith("-") and rest[0] != "-m":
            rest = rest[1:]
        if rest and rest[0] == "-m" and len(rest) > 1:
            mod = rest[1]
            if mod in ("ctxproxy.server", "ctxproxy"):
                return "server"
            if mod == "ctxproxy.mcp_server":
                return "mcp"
    return None


class _ParserError(Exception):
    pass


def _no_exit(ap: argparse.ArgumentParser) -> argparse.ArgumentParser:
    def error(message):            # noqa: ANN001
        raise _ParserError(message)
    ap.error = error               # type: ignore[method-assign]
    return ap


# The real parsers, built by the code that the programs themselves use. Nothing here is a copy.
def server_parser() -> argparse.ArgumentParser:
    from ctxproxy.server import build_parser
    return _no_exit(build_parser())


def check_parser() -> argparse.ArgumentParser:
    from ctxproxy.check import build_check_parser
    return _no_exit(build_check_parser())


def demo_parser() -> argparse.ArgumentParser:
    from ctxproxy.demo import build_demo_parser
    return _no_exit(build_demo_parser())


def forget_parser() -> argparse.ArgumentParser:
    from ctxproxy.forget import build_forget_parser
    return _no_exit(build_forget_parser())


def mcp_parser() -> argparse.ArgumentParser:
    from ctxproxy.mcp_server import build_mcp_parser
    return _no_exit(build_mcp_parser())


SUBCOMMAND_PARSERS = {"demo": demo_parser, "check": check_parser, "forget": forget_parser}


def options(ap: argparse.ArgumentParser) -> list[str]:
    """Every long option of a parser, `--help` excluded (argparse's own)."""
    out = []
    for action in ap._actions:                     # noqa: SLF001 - argparse has no public API for this
        for flag in action.option_strings:
            if flag.startswith("--") and flag != "--help":
                out.append(flag)
    return sorted(set(out))


def known_subcommands() -> list[str]:
    """The subcommands `ctxproxy/server.py:main` really dispatches on."""
    import inspect
    import re
    from ctxproxy import server
    src = inspect.getsource(server.main)
    return sorted(set(re.findall(r"argv\[0\]\s*==\s*[\"']([a-z_-]+)[\"']", src)))


def check_argv(argv: list[str]) -> str | None:
    """None when the command line is valid, otherwise what is wrong with it."""
    prog = program(argv)
    if prog is None:
        return None
    rest = argv[1:]
    if prog == "server" and rest and rest[0] == "-m":
        rest = rest[2:]
    if prog == "mcp":
        ap, args = mcp_parser(), rest
    elif rest and not rest[0].startswith("-"):
        sub = rest[0]
        if sub not in SUBCOMMAND_PARSERS:
            return "unknown subcommand %r (known: %s)" % (sub, ", ".join(known_subcommands()))
        ap, args = SUBCOMMAND_PARSERS[sub](), rest[1:]
    else:
        ap, args = server_parser(), rest
    # argparse writes to stdout/stderr for --help/--version and then exits: keep it quiet
    out, err = io.StringIO(), io.StringIO()
    old = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = out, err
    try:
        ap.parse_args(args)
    except _ParserError as e:
        return str(e)
    except SystemExit:
        pass                       # --help / --version: a real, documented behaviour
    finally:
        sys.stdout, sys.stderr = old
    return None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="readme_cli", description="check the README's command lines")
    ap.add_argument("path", nargs="?")
    a = ap.parse_args(argv)
    here = os.path.dirname(os.path.abspath(__file__))
    sys.path.insert(0, os.path.dirname(here))
    sys.path.insert(0, here)
    import readme_blocks
    bad = 0
    for b in (readme_blocks.load(a.path) if a.path else readme_blocks.load()):
        if b.lang in ("json", "yaml", "toml", "bibtex"):
            continue
        for cmd in commands(b.text):
            if program(cmd) is None:
                continue
            problem = check_argv(cmd)
            status = "OK  " if problem is None else "BAD "
            if problem:
                bad += 1
            print("%s line %5d  %s%s" % (status, b.line, " ".join(cmd[:8]),
                                         "   <-- " + problem if problem else ""))
    print("\n%d invalid command line(s)" % bad)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
