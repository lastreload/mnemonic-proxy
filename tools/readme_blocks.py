#!/usr/bin/env python3
"""Inventory of the fenced code blocks in README.md.

Every executable block must carry a label in an HTML comment on the line just above the
opening fence, so the label stays invisible when GitHub renders the page:

    <!-- readme-test: ci -->           executed for real by tools/readme_run.sh (level 2)
    <!-- readme-test: windows -->      PowerShell / Windows-only, not run on Linux CI
    <!-- readme-test: real-engine -->  needs a real model, a real engine or a third-party program
    <!-- readme-test: illustrative --> nothing for the harness to execute: output, configuration
                                       data, destructive cleanup, or a command CI already runs as
                                       itself (the unit-test suite)

A JSON block carries an extra attribute in the same comment, so a partial or non-proxy fragment
is never fed to `Config.from_dict`:

    <!-- readme-test: illustrative; config: proxy -->    full proxy configuration
    <!-- readme-test: illustrative; config: partial -->  a few proxy keys, not a whole file
    <!-- readme-test: illustrative; config: client -->   another program's configuration

Standard library only: the README tests must run anywhere the package runs.

Usage:
    python tools/readme_blocks.py            # table: line, label, language, section
    python tools/readme_blocks.py --json     # machine-readable
    python tools/readme_blocks.py --unlabeled
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import os
import re
import sys

LABELS = ("ci", "windows", "real-engine", "illustrative")
CONFIG_KINDS = ("proxy", "partial", "client")
LABEL_RE = re.compile(r"<!--\s*readme-test:\s*([a-z-]+)\s*((?:;[^>]*?)?)-->")
EXTRA_RE = re.compile(r"([a-z-]+)\s*:\s*([A-Za-z0-9_.-]+)")
FENCE_RE = re.compile(r"^(\s*)(`{3,}|~{3,})\s*([A-Za-z0-9_+-]*)\s*$")
HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
README = os.path.join(REPO_ROOT, "README.md")


@dataclasses.dataclass
class Block:
    line: int               # 1-based line of the opening fence
    end_line: int           # 1-based line of the closing fence
    lang: str               # fence language, "" when absent
    label: str | None       # readme-test label, None when missing
    section: str            # nearest heading above the block
    section_line: int
    text: str               # block body, without the fences
    extras: dict = dataclasses.field(default_factory=dict)   # e.g. {"config": "proxy"}

    @property
    def executable(self) -> bool:
        """A block the label system cares about: shell-ish or JSON configuration."""
        return self.lang in ("sh", "bash", "shell", "console", "powershell", "ps1", "json", "")

    @property
    def config_kind(self) -> str | None:
        return self.extras.get("config")

    def as_dict(self) -> dict:
        d = dataclasses.asdict(self)
        d["executable"] = self.executable
        return d


def parse(text: str) -> list[Block]:
    lines = text.splitlines()
    blocks: list[Block] = []
    section, section_line = "", 0
    i = 0
    while i < len(lines):
        line = lines[i]
        h = HEADING_RE.match(line)
        if h:
            section, section_line = h.group(2).strip(), i + 1
            i += 1
            continue
        m = FENCE_RE.match(line)
        if not m:
            i += 1
            continue
        indent, fence, lang = m.group(1), m.group(2), m.group(3)
        # label: nearest HTML comment above, skipping blank lines
        label = None
        extras: dict = {}
        j = i - 1
        while j >= 0 and not lines[j].strip():
            j -= 1
        if j >= 0:
            lm = LABEL_RE.search(lines[j])
            if lm:
                label = lm.group(1)
                extras = dict(EXTRA_RE.findall(lm.group(2) or ""))
        body: list[str] = []
        k = i + 1
        while k < len(lines):
            c = lines[k]
            cm = FENCE_RE.match(c)
            if cm and cm.group(2)[0] == fence[0] and len(cm.group(2)) >= len(fence) and not cm.group(3):
                break
            body.append(c[len(indent):] if c.startswith(indent) else c)
            k += 1
        blocks.append(Block(line=i + 1, end_line=k + 1, lang=lang, label=label,
                            section=section, section_line=section_line, text="\n".join(body),
                            extras=extras))
        i = k + 1
    return blocks


def load(path: str = README) -> list[Block]:
    with open(path, encoding="utf-8") as f:
        return parse(f.read())


def heading_slug(title: str) -> str:
    """GitHub's anchor rule: strip markup, lowercase, drop everything but word chars,
    spaces and hyphens, then spaces -> hyphens."""
    t = re.sub(r"<[^>]+>", "", title)                       # inline HTML
    t = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", t)        # links and images
    t = re.sub(r"[`*_~]", "", t)                            # code / emphasis markers
    s = re.sub(r"[^\w\- ]", "", t.strip().lower(), flags=re.UNICODE)
    return s.replace(" ", "-")


def headings(text: str) -> list[tuple[int, int, str]]:
    """(line, level, title) of every heading outside a fenced block."""
    out = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        m = FENCE_RE.match(lines[i])
        if m:
            fence = m.group(2)
            i += 1
            while i < len(lines):
                cm = FENCE_RE.match(lines[i])
                if cm and cm.group(2)[0] == fence[0] and len(cm.group(2)) >= len(fence) and not cm.group(3):
                    break
                i += 1
            i += 1
            continue
        h = HEADING_RE.match(lines[i])
        if h:
            out.append((i + 1, len(h.group(1)), h.group(2).strip()))
        i += 1
    return out


def anchors(text: str) -> set[str]:
    """Anchors a `](#...)` link can resolve to, with GitHub's `-1`, `-2`, … for duplicates."""
    seen: dict[str, int] = {}
    out = set()
    for _, _, title in headings(text):
        s = heading_slug(title)
        n = seen.get(s, 0)
        seen[s] = n + 1
        out.add(s if n == 0 else "%s-%d" % (s, n))
    # explicit HTML anchors, if any
    out |= set(re.findall(r"<a\s+(?:[^>]*\s)?(?:name|id)=\"([^\"]+)\"", text))
    return out


def internal_links(text: str) -> list[str]:
    return re.findall(r"\]\(#([^)\s]+)\)", text)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="readme_blocks")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--unlabeled", action="store_true", help="only executable blocks without a label")
    ap.add_argument("--label", help="only blocks with this label")
    ap.add_argument("path", nargs="?", default=README)
    a = ap.parse_args(argv)
    blocks = load(a.path)
    if a.unlabeled:
        blocks = [b for b in blocks if b.executable and b.label is None]
    if a.label:
        blocks = [b for b in blocks if b.label == a.label]
    if a.json:
        print(json.dumps([b.as_dict() for b in blocks], ensure_ascii=False, indent=1))
        return 0
    counts: dict[str, int] = {}
    for b in blocks:
        key = b.label or ("MISSING" if b.executable else "-")
        counts[key] = counts.get(key, 0) + 1
        print("%5d  %-12s %-10s %s" % (b.line, key, b.lang or "(none)", b.section))
    print("\n%d blocks: %s" % (len(blocks), ", ".join("%s=%d" % kv for kv in sorted(counts.items()))))
    return 0


if __name__ == "__main__":
    sys.exit(main())
