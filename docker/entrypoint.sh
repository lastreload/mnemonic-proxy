#!/bin/bash
# One container, two processes: the proxy, then the dashboard. Exit as soon as either one dies.
set -euo pipefail

mkdir -p "$DATA"

# Keys are written to files in DATA (0600) instead of passed as arguments, so they never show up in the process
# list. The client key is a separate secret from the engine key: handing clients the engine key would let them reach
# the engine directly, bypassing the proxy.
engine_key=""
if [ -n "${API_KEY:-}" ]; then
  engine_key="$DATA/.api_key"
  printf '%s' "$API_KEY" > "$engine_key"
  chmod 600 "$engine_key"
fi

client_key="${CLIENT_KEY:-$API_KEY}"
client_file=""
if [ -n "$client_key" ]; then
  client_file="$DATA/.client_key"
  printf '%s' "$client_key" > "$client_file"
  chmod 600 "$client_file"
fi

proxy=(python -m ctxproxy.server --upstream "$UPSTREAM" --host "$PROXY_HOST" --port "$PROXY_PORT" --data "$DATA")
if [ -n "$API_KEY" ]; then proxy+=(--api-key-file "$engine_key"); fi
if [ -n "$client_file" ]; then proxy+=(--client-key-file "$client_file"); fi
if [ -n "${PROXY_ARGS:-}" ]; then proxy+=($PROXY_ARGS); fi   # word-splitting intended: extra proxy flags (config, tokenizer, engine)

dash=(python dashboard/ctxdash.py --strata "$UPSTREAM" --data "$DATA"
      --proxy "http://127.0.0.1:$PROXY_PORT" --host "$DASH_HOST" --port "$DASH_PORT")
if [ -n "$engine_key" ]; then dash+=(--api-key-file "$engine_key"); fi
if [ -n "$client_file" ]; then dash+=(--proxy-api-key-file "$client_file"); fi

"${proxy[@]}" & proxy_pid=$!
"${dash[@]}" & dash_pid=$!
code=0
wait -n || code=$?   # first process to exit ends the container
kill "$proxy_pid" "$dash_pid" 2>/dev/null || true
exit "$code"
