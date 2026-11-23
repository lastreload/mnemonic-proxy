#!/bin/bash
# One container, two processes: the proxy, then the dashboard. Exit as soon as either one dies.
set -euo pipefail

mkdir -p "$DATA"

# Keys are written to files in DATA (0600) instead of passed as arguments, so they never show up in the process
# list. The client key is a separate secret from the engine key: handing clients the engine key would let them reach
# the engine directly, bypassing the proxy. *_FILE envs point at an already-existing key file (a quadlet secret mount),
# so nothing is written.
engine_key="${API_KEY_FILE:-}"
if [ -z "$engine_key" ] && [ -n "${API_KEY:-}" ]; then
  engine_key="$DATA/.api_key"
  printf '%s' "$API_KEY" > "$engine_key"
  chmod 600 "$engine_key"
fi

client_file="${CLIENT_KEY_FILE:-}"
if [ -z "$client_file" ] && [ -n "${CLIENT_KEY:-$API_KEY}" ]; then
  client_file="$DATA/.client_key"
  printf '%s' "${CLIENT_KEY:-$API_KEY}" > "$client_file"
  chmod 600 "$client_file"
fi
if [ -z "$client_file" ] && [ -n "$engine_key" ]; then client_file="$engine_key"; fi

proxy=(python -m ctxproxy.server --upstream "$UPSTREAM" --host "$PROXY_HOST" --port "$PROXY_PORT" --data "$DATA")
if [ -n "$engine_key" ]; then proxy+=(--api-key-file "$engine_key"); fi
if [ -n "$client_file" ]; then proxy+=(--client-key-file "$client_file"); fi
if [ -n "${PROXY_ARGS:-}" ]; then proxy+=($PROXY_ARGS); fi   # word-splitting intended: extra proxy flags (config, tokenizer, engine)

dash=(python dashboard/ctxdash.py --strata "$UPSTREAM" --data "$DATA"
      --proxy "http://127.0.0.1:$PROXY_PORT" --host "$DASH_HOST" --port "$DASH_PORT")
if [ -n "$engine_key" ]; then dash+=(--api-key-file "$engine_key"); fi
if [ -n "$client_file" ]; then dash+=(--proxy-api-key-file "$client_file"); fi

cleanup() {
  trap - EXIT TERM INT
  # Query our job table rather than saved $! values: a signal can arrive
  # between starting a child and assigning its PID. Only our own jobs exist here.
  local pid
  for pid in $(jobs -p); do kill "$pid" 2>/dev/null || true; done
  wait 2>/dev/null || true
}
trap cleanup EXIT
trap 'exit 143' TERM
trap 'exit 130' INT

"${proxy[@]}" & proxy_pid=$!
"${dash[@]}" & dash_pid=$!
code=0
wait -n || code=$?   # first process to exit ends the container
exit "$code"
