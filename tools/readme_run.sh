#!/usr/bin/env bash
# Run the README tests the way a new user would meet the package: from the built wheel,
# in a fresh virtual environment, with no source tree on the import path.
#
#   tools/readme_run.sh              # build the wheel, install it, run levels 1 and 2
#   tools/readme_run.sh --source     # same tests against the working tree (fast, no build)
#
# Level 1 (static) and level 3 (exhaustiveness) run from the repository, because they read
# the README and the code. Level 2 runs from a staging folder that contains only the README,
# tools/ and the two README test files, so `import ctxproxy` can only resolve to the wheel.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$here"

mode="wheel"
[ "${1:-}" = "--source" ] && mode="source"

echo "== level 1 + level 3 (repository, standard library only)"
python3 -m unittest -v tests.test_readme_static tests.test_readme_coverage

if [ "$mode" = "source" ]; then
  echo "== level 2 (working tree)"
  README_RUN=1 python3 -m unittest -v tests.test_readme_run
  exit 0
fi

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

echo "== clean virtual environment"
python3 -m venv "$work/venv"
"$work/venv/bin/python" -m pip install --quiet --upgrade pip build

echo "== building the wheel in $work"
"$work/venv/bin/python" -m build --wheel --outdir "$work/dist" >"$work/build.log" 2>&1 || { cat "$work/build.log"; exit 1; }
wheel="$(ls "$work"/dist/*.whl)"
echo "   $wheel"
"$work/venv/bin/pip" install --quiet "$wheel"

stage="$work/stage"
mkdir -p "$stage/tests" "$stage/tools"
cp README.md "$stage/"
cp tools/readme_blocks.py tools/readme_cli.py "$stage/tools/"
cp tests/__init__.py tests/test_readme_run.py "$stage/tests/"

echo "== level 2 (wheel: $("$work/venv/bin/mnemonic-proxy" --version))"
cd "$stage"
README_RUN=1 \
  MNEMONIC_PROXY_CMD="$work/venv/bin/mnemonic-proxy" \
  MNEMONIC_MCP_CMD="$work/venv/bin/mnemonic-mcp" \
  "$work/venv/bin/python" -m unittest -v tests.test_readme_run
