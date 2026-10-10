#!/bin/sh
# Sobe o mnemonic-proxy na frente do servidor Strata local.
#
# O Strata precisa JA estar rodando (./setup.sh na pasta do Strata).
# Depois aponte o cliente (OpenAI base URL) para http://127.0.0.1:8096/v1
#
# Tudo e sobrescritavel por variavel de ambiente, ex.:
#   STRATA_URL=http://127.0.0.1:8080 PORT=8096 ./run-strata-proxy.sh
#
# Caminhos locais (ajuste na linha do STRATA_DIR / TOKENIZER, ou exporte-os):
STRATA_DIR="${STRATA_DIR:-$HOME/Documentos/Strata}"
TOKENIZER="${TOKENIZER:-$HOME/Documentos/Strata-data/packs/swift-iq3_xxs/tokenizer}"
STRATA_URL="${STRATA_URL:-http://127.0.0.1:8080}"
HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-8096}"
DATA="${DATA:-./data}"
CONFIG="${CONFIG:-./config.json}"

cd "$(dirname "$0")" || exit 1

# Ambiente: a mesma filosofia do Strata — se faltar algo, o setup.sh prepara na hora.
# SKIP_SETUP=1 pula a checagem (para quem ja sabe que o ambiente esta pronto).
if [ "${SKIP_SETUP:-0}" != 1 ] && [ -x ./setup.sh ]; then
  if ! ./setup.sh --check >/dev/null 2>&1; then
    echo "[run-strata-proxy] ambiente incompleto: rodando ./setup.sh ..."
    ./setup.sh || exit 1
  fi
fi

# Python 3.14 (.venv314) habilita o kv_archive (compression.zstd); .venv e 3.12 e roda sem ele.
VENV="${VENV:-}"
if [ -z "$VENV" ]; then
  if [ -x .venv314/bin/mnemonic-proxy ]; then VENV=.venv314
  else VENV=.venv; fi
fi
[ -x "$VENV/bin/mnemonic-proxy" ] || { echo "mnemonic-proxy nao instalado em $VENV (./setup.sh instala)" >&2; exit 1; }

# config.json e' locale (gitignored): se manca, nasce dall'esempio. Il slot_dir viene
# letto dal config del Strata (slot_save_path nei JSON della cartella del Strata); se
# non c'e' si usa STRATA_DIR/sessions, che e' la convenzione del Strata.
if [ ! -f "$CONFIG" ] && [ -f examples/config.strata-saved-state.json ]; then
  SLOT="${SLOT_DIR:-}"
  if [ -z "$SLOT" ] && [ -x "$VENV/bin/python" ]; then
    SLOT=$("$VENV/bin/python" - "$STRATA_DIR" <<'PY' 2>/dev/null
import glob, json, os, sys
base = sys.argv[1]
for p in sorted(glob.glob(os.path.join(base, "*.json"))):
    try:
        cfg = json.load(open(p, encoding="utf-8"))
    except Exception:
        continue
    v = cfg.get("slot_save_path")
    if v:
        print(v)
        break
PY
)
  fi
  [ -n "$SLOT" ] || SLOT="$STRATA_DIR/sessions"
  cp examples/config.strata-saved-state.json "$CONFIG"
  if [ -x "$VENV/bin/python" ]; then
    "$VENV/bin/python" - "$CONFIG" "$SLOT" <<'PY'
import json, sys
p, slot = sys.argv[1], sys.argv[2]
cfg = json.load(open(p, encoding="utf-8"))
cfg["slot_dir"] = slot
json.dump(cfg, open(p, "w", encoding="utf-8"), indent=2)
print("[run-strata-proxy] config criado em %s com slot_dir=%s" % (p, slot))
PY
  else
    echo "[run-strata-proxy] config criado em $CONFIG: ponha slot_dir na pasta sessions do Strata"
  fi
fi

# O proxy nao sobe sem o motor: avisa antes, com o comando de start do Strata.
if ! command -v curl >/dev/null 2>&1 || ! curl -s -m 3 "$STRATA_URL/v1/status" >/dev/null 2>&1; then
  echo "Strata nao responde em $STRATA_URL - suba-o antes: cd $STRATA_DIR && ./setup.sh" >&2
  exit 1
fi

echo "[run-strata-proxy] venv=$VENV upstream=$STRATA_URL porta=$HOST:$PORT data=$DATA config=$CONFIG tokenizer=$TOKENIZER"
exec "$VENV/bin/mnemonic-proxy" \
  --upstream "$STRATA_URL" \
  --host "$HOST" --port "$PORT" \
  --data "$DATA" \
  --config "$CONFIG" \
  --tokenizer "$TOKENIZER"
