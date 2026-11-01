#!/bin/sh
# "New user" run: install the package the way the README tells a newcomer to, then walk the
# first screen and T0 with no repository in sight. Meant for a clean container, so that
# `import ctxproxy` can only resolve to what was really installed; it needs a POSIX shell,
# python3 and network access to install the package.
#
#   tools/readme_newuser.sh <uvx|pip> <pypi|wheel> [WHEEL_DIR]
#
# Example (rootless podman, no GPU, no model, 6 GB, container removed on exit):
#   podman run --memory 6g --cpus 8 -v "$PWD/tools:/work:ro,z" -v "$PWD/dist:/wheel:ro,z" \
#       -w /tmp docker.io/library/python:3.14-slim sh /work/readme_newuser.sh pip wheel
#
# Exercises, in this order:
#   T0  demo                           -> PASS
#   check with no engine               -> NOT READY + "start the engine first"
#   check against ctxproxy.fake_engine -> READY
#   the proxy in front of the fake engine + one Chat Completions request -> HTTP 200
#   mnemonic-mcp                       -> installed and runnable
#
# Every command is the one README.md gives a new user. Nothing is written outside /tmp/trial.
set -eu

METHOD="$1"               # uvx | pip
SOURCE="$2"               # pypi | wheel
WHEELDIR="${3:-/wheel}"   # where to look for the .whl when SOURCE=wheel

say() { printf '\n### %s\n' "$*"; }
say "python $(python3 -VV | head -1)  method=$METHOD source=$SOURCE"

mkdir -p /tmp/trial && cd /tmp/trial

if [ "$SOURCE" = wheel ]; then
  TARGET="$(ls "$WHEELDIR"/*.whl | head -1)"
else
  TARGET="mnemonic-proxy"
fi

if [ "$METHOD" = uvx ]; then
  say "install uv (the README points at https://docs.astral.sh/uv/)"
  pip install --quiet --root-user-action=ignore uv
  PROXY="uvx --from $TARGET mnemonic-proxy"
  MCP="uvx --from $TARGET mnemonic-mcp"
  # uvx needs --from when the package comes from a wheel; from PyPI the README's own form is
  # plain `uvx mnemonic-proxy`, which is run verbatim below.
else
  say "venv + pip install (README: T0)"
  python3 -m venv .venv
  .venv/bin/pip install --quiet --root-user-action=ignore "$TARGET"
  PROXY=".venv/bin/mnemonic-proxy"
  MCP=".venv/bin/mnemonic-mcp"
fi

say "version"
$PROXY --version

if [ "$METHOD" = uvx ] && [ "$SOURCE" = pypi ]; then
  say "the README's very first command, verbatim: uvx mnemonic-proxy demo"
  uvx mnemonic-proxy demo | tee demo-verbatim.txt
  grep -q PASS demo-verbatim.txt
fi

say "T0: demo -> PASS"
$PROXY demo | tee demo.txt
grep -q PASS demo.txt
grep -i "next" demo.txt || true

say "check with no engine -> NOT READY, 'start the engine first'"
set +e
$PROXY check --upstream http://127.0.0.1:18099 --port 18096 --data ./data > check-noengine.txt 2>&1
rc=$?
set -e
cat check-noengine.txt
[ "$rc" = 1 ] || { echo "FAIL: expected exit 1, got $rc"; exit 1; }
grep -q "NOT READY" check-noengine.txt
grep -q "start the engine first" check-noengine.txt

say "start the fake engine (ctxproxy.fake_engine, llama flavor), then check -> READY"
if [ "$METHOD" = uvx ]; then
  uv run --no-project --quiet --with "$TARGET" python -m ctxproxy.fake_engine \
      --flavor llama --n-ctx 131072 --port 18095 &
else
  .venv/bin/python -m ctxproxy.fake_engine --flavor llama --n-ctx 131072 --port 18095 &
fi
ENGINE=$!
i=0
while [ $i -lt 120 ]; do
  python3 - <<'PY' && break
import sys, urllib.request
try:
    urllib.request.urlopen("http://127.0.0.1:18095/health", timeout=2)
except Exception:
    sys.exit(1)
PY
  i=$((i + 1)); sleep 0.5
done
[ $i -lt 120 ] || { echo "FAIL: the fake engine never answered"; exit 1; }

set +e
$PROXY check --upstream http://127.0.0.1:18095 --port 18096 --data ./data > check-ready.txt 2>&1
rc=$?
set -e
cat check-ready.txt
grep -q "READY" check-ready.txt
! grep -q "NOT READY" check-ready.txt
case "$rc" in 0|2) ;; *) echo "FAIL: expected exit 0 or 2, got $rc"; exit 1;; esac

say "the proxy in front of the fake engine + one Chat Completions request"
$PROXY --upstream http://127.0.0.1:18095 --port 18096 --data ./data > proxy.log 2>&1 &
PROXYPID=$!
python3 - <<'PY'
import json, sys, time, urllib.error, urllib.request
base = "http://127.0.0.1:18096"
for _ in range(200):
    try:
        urllib.request.urlopen(base + "/health", timeout=2)
        break
    except Exception:
        time.sleep(0.25)
else:
    print("FAIL: the proxy never answered /health"); sys.exit(1)
body = json.dumps({"model": "local-model", "max_tokens": 64,
                   "messages": [{"role": "user", "content": "say hello"}]}).encode()
req = urllib.request.Request(base + "/v1/chat/completions", data=body,
                             headers={"Content-Type": "application/json"})
with urllib.request.urlopen(req, timeout=60) as r:
    print("HTTP", r.status)
    assert r.status == 200
    answer = json.loads(r.read())
print("answer:", json.dumps(answer["choices"][0]["message"])[:200])
with urllib.request.urlopen(base + "/v1/engine", timeout=10) as r:
    print("engine:", r.read().decode()[:200])
PY

say "mnemonic-mcp --help (the archive search server is installed too)"
$MCP --help > /dev/null && echo "mnemonic-mcp: ok"

kill $PROXYPID $ENGINE 2>/dev/null || true
wait $PROXYPID 2>/dev/null || true
say "ALL GOOD  python=$(python3 -c 'import sys;print("%d.%d"%sys.version_info[:2])') method=$METHOD source=$SOURCE"
