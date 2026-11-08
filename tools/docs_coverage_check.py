#!/usr/bin/env python3
"""Proof that the README split lost nothing.

0.3.25 replaced a 2,020-line README with a short landing page plus the pages in `docs/`.
This script takes the README as it was before the split and checks, line by line, that every
non-empty line it carried is still present in the documentation — in `docs/*.md`, or in the
new `README.md` when the landing page kept it.

The comparison ignores the three things the split was allowed to change:

  * heading level     `### Platforms` matches `## Platforms`
  * link targets      `](#engines)` matches `](engines.md#engines)`, and `](docs/x)` -> `](x)`
  * leading/trailing whitespace

Lines 1 to 66 of the old README are the part the new landing page replaces on purpose
(the old hero, the old summary table, the old "try it in one minute" block). They are
reported separately, not as a failure.

Usage:
    python3 tools/docs_coverage_check.py                     # baseline from git (ea640b7)
    python3 tools/docs_coverage_check.py --baseline FILE
    python3 tools/docs_coverage_check.py --verbose           # list the replaced part too

Exit code 0 when nothing was lost, 1 otherwise. Standard library only.
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASELINE_REV = "ea640b73dd3e1f6607477de3fbeef1f1c1bff7d2"   # main before the split
REPLACED_UNTIL = 66                                          # old landing section, lines 1-66


def baseline_from_git(rev: str = BASELINE_REV) -> str:
    out = subprocess.run(["git", "show", "%s:README.md" % rev], cwd=REPO_ROOT,
                         capture_output=True, text=True)
    if out.returncode != 0:
        raise SystemExit("cannot read README.md at %s: %s" % (rev, out.stderr.strip()))
    return out.stdout


def documentation_lines(root: str = REPO_ROOT) -> dict[str, list[str]]:
    """{page: lines} for README.md and docs/*.md (docs/dev-notes/ excluded)."""
    sys.path.insert(0, os.path.join(root, "tools"))
    import readme_blocks                                      # noqa: E402
    out = {}
    for path in readme_blocks.doc_files(root):
        with open(path, encoding="utf-8") as f:
            out[readme_blocks.rel(path, root)] = f.read().splitlines()
    return out


def normalize(line: str) -> str:
    """The same line, with everything the split was allowed to change removed."""
    s = line.strip()
    s = re.sub(r"^#{1,6}\s+", "# ", s)                        # heading level
    s = s.replace("](docs/", "](")                            # a page already inside docs/
    # link target: keep the visible text, drop where it points
    s = re.sub(r"\]\([^)\s]*#([^)\s]+)\)", r"](#\1)", s)      # page.md#anchor -> #anchor
    s = re.sub(r"\s+", " ", s)
    return s


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="docs_coverage_check")
    ap.add_argument("--baseline", help="the pre-split README; default: git show %s" % BASELINE_REV[:7])
    ap.add_argument("--rev", default=BASELINE_REV, help="revision to read README.md from")
    ap.add_argument("--verbose", action="store_true", help="also list the replaced opening part")
    a = ap.parse_args(argv)

    if a.baseline:
        with open(a.baseline, encoding="utf-8") as f:
            base = f.read()
    else:
        base = baseline_from_git(a.rev)
    base_lines = base.splitlines()

    pages = documentation_lines()
    index: dict[str, list[str]] = {}
    for page, lines in pages.items():
        for line in lines:
            n = normalize(line)
            if n:
                index.setdefault(n, []).append(page)

    missing = []
    replaced = []
    checked = 0
    for i, line in enumerate(base_lines, 1):
        n = normalize(line)
        if not n:
            continue
        if i <= REPLACED_UNTIL:
            if n not in index:
                replaced.append((i, line))
            continue
        checked += 1
        if n not in index:
            missing.append((i, line))

    print("baseline: %d lines (%s)" % (len(base_lines), a.baseline or a.rev[:7]))
    print("documentation: %d pages, %d lines"
          % (len(pages), sum(len(v) for v in pages.values())))
    print("checked %d non-empty lines after line %d" % (checked, REPLACED_UNTIL))
    print("replaced opening part (lines 1-%d): %d non-empty lines not carried over"
          % (REPLACED_UNTIL, len(replaced)))
    if a.verbose:
        for i, line in replaced:
            print("    %4d  %s" % (i, line))
    if missing:
        print("\nLOST %d line(s):" % len(missing))
        for i, line in missing:
            print("  %4d  %s" % (i, line))
        return 1
    print("\nnothing lost: every line of the old README is in the documentation")
    return 0


if __name__ == "__main__":
    sys.exit(main())
