#!/bin/sh
# Sobe o mnemonic-proxy na frente do servidor Strata local.
#
# O Strata precisa JA estar rodando (./setup.sh na pasta do Strata).
# Depois aponte o cliente (OpenAI base URL) para http://127.0.0.1:8096/v1
#
# Tudo e sobrescritavel por variavel de ambiente, ex.:
#   STRATA_URL=http://127.0.0.1:8080 PORT=8096 ./run-strata-proxy.sh
#
# kv_archive e' opt-in: o proxy sobe sem Python 3.14 (sem arquivo frio). O venv e' escolhido
# por quem tem compression.zstd; o config CRIATO aqui nasce com kv_archive coerente com o
# venv. Um config seu nunca e' alterado: se ele pedir kv_archive sem suporte, isto para com
# a explicacao.
#
# Caminhos do motor: STRATA_DIR e' a pasta do Strata (default ~/Strata). TOKENIZER e SLOT_DIR sono
# opzionali: se mancano si leggono dal file del motore in uso; senza tokenizer il proxy conta
# caratteri / 3.5, senza slot le sessioni vanno in $STRATA_DIR/sessions.
STRATA_DIR="${STRATA_DIR:-$HOME/Strata}"
TOKENIZER="${TOKENIZER:-}"
STRATA_URL="${STRATA_URL:-http://127.0.0.1:8080}"
HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-8096}"
DATA="${DATA:-./data}"
CONFIG="${CONFIG:-./config.json}"
PROBE_TIMEOUT="${PROBE_TIMEOUT:-3}"
# un numero: senza curl il fallback python fa float(), e un valore non numerico darebbe
# un codice HTTP vuoto letto come "la porta ha risposto"
case "$PROBE_TIMEOUT" in ''|*[!0-9]*) PROBE_TIMEOUT=3 ;; esac

cd "$(dirname "$0")" || exit 1

# ---- helpers -------------------------------------------------------------
venv_has() { [ -x "$1/bin/mnemonic-proxy" ]; }
# Il kv_archive chiede compression.zstd (Python >= 3.14): si guarda il supporto, non il
# nome della cartella — .venv314 puo essere un 3.12 e .venv puo essere un 3.14.
venv_zstd() { [ -x "$1/bin/python" ] && "$1/bin/python" -c 'import compression.zstd' >/dev/null 2>&1; }
# Il config chiede kv_archive? 1 si, 0 no, vuoto se il JSON non e' leggibile
cfg_wants_kv()
{
  [ -x "$VENV/bin/python" ] || return 1
  "$VENV/bin/python" - "$CONFIG" <<'PY' 2>/dev/null
import json, sys
try:
    cfg = json.load(open(sys.argv[1], encoding="utf-8"))
except Exception:
    sys.exit(1)
print("1" if cfg.get("kv_archive") else "0")
PY
}
# Codice HTTP di /v1/status: "conn" per connessione o timeout falliti, "nocurl" se non
# c'e' modo di probes. Un 404/503 NON e' il motore: e' un servizio che risponde e dice no.
probe_status()
{
  if command -v curl >/dev/null 2>&1; then
    code=$(curl -s -o /dev/null -w '%{http_code}' -m "$PROBE_TIMEOUT" "$STRATA_URL/v1/status" 2>/dev/null)
    case "$code" in ""|000) printf 'conn\n' ;; *) printf '%s\n' "$code" ;; esac
    return 0
  fi
  if [ -x "$VENV/bin/python" ]; then
    "$VENV/bin/python" - "$STRATA_URL" "$PROBE_TIMEOUT" <<'PY' 2>/dev/null
import sys, socket, urllib.error, urllib.request
url, to = sys.argv[1] + "/v1/status", float(sys.argv[2])
try:
    with urllib.request.urlopen(url, timeout=to) as r:
        print(r.status)
except urllib.error.HTTPError as e:
    print(e.code)
except (socket.timeout, OSError):
    print("conn")
PY
    return 0
  fi
  printf 'nocurl\n'
}

# Ambiente: a mesma filosofia do Strata — se faltar algo, o setup.sh prepara na hora.
# SKIP_SETUP=1 pula a checagem (para quem ja sabe que o ambiente esta pronto).
if [ "${SKIP_SETUP:-0}" != 1 ] && [ -x ./setup.sh ]; then
  if ! ./setup.sh --check >/dev/null 2>&1; then
    echo "[run-strata-proxy] ambiente incompleto: rodando ./setup.sh ..."
    ./setup.sh || exit 1
  fi
fi

# Il venv si sceglie da chi ha compression.zstd (kv_archive), non dal nome: senza 3.14 il
# proxy sobe igual, senza arquivo frio.
if [ -z "$VENV" ]; then
  if venv_has .venv314 && venv_zstd .venv314; then VENV=.venv314
  elif venv_has .venv && venv_zstd .venv; then VENV=.venv
  elif venv_has .venv314; then VENV=.venv314
  else VENV=.venv; fi
fi
venv_has "$VENV" || { echo "mnemonic-proxy nao instalado em $VENV (./setup.sh instala)" >&2; exit 1; }
KV_CAP=0; venv_zstd "$VENV" && KV_CAP=1

# tokenizer e slot_dir: se non dati, si deducono dal config del motore (le chiavi "tokenizer" e
# "slot_save_path" nei JSON della cartella del Strata). Con piu' di un config nella cartella -
# installazione con piu' di un pack - il file si prende solo se il motore in uso e' identificato
# (il "model" di /v1/status, o la porta dell'URL): altrimenti niente --tokenizer e niente slot
# dedotto. Un file preso a caso darebbe numeri e sessioni di un pack che non e' quello in uso.
# TOKENIZER e SLOT_DIR dati da chi chiamano hanno precedenza e saltano la scoperta.
SLOT="${SLOT_DIR:-}"
if [ -x "$VENV/bin/python" ] && { [ -z "$TOKENIZER" ] || [ -z "$SLOT" ]; }; then
  DISC=$("$VENV/bin/python" - "$STRATA_DIR" "$STRATA_URL" "$PROBE_TIMEOUT" <<'PY' 2>/dev/null
import glob, json, os, sys
base, url, to = sys.argv[1], sys.argv[2], float(sys.argv[3])
cands = []
for p in sorted(glob.glob(os.path.join(base, "*.json"))):
    try:
        cfg = json.load(open(p, encoding="utf-8"))
    except Exception:
        continue
    if isinstance(cfg, dict):
        cands.append(cfg)
if not cands:
    print("NONE")
    sys.exit(0)
if len(cands) > 1:
    model, port = None, None
    try:
        from urllib.parse import urlparse
        u = urlparse(url)
        port = u.port or (443 if u.scheme == "https" else 80)
    except Exception:
        pass
    try:
        import urllib.request
        with urllib.request.urlopen(url.rstrip("/") + "/v1/status", timeout=to) as r:
            model = json.loads(r.read().decode("utf-8", "replace")).get("model")
    except Exception:
        pass
    hits = [c for c in cands
            if (model and c.get("model_name") == model) or (port is not None and c.get("port") == port)]
    if len({json.dumps(c, sort_keys=True) for c in hits}) != 1:
        print("AMBIGUOUS %d" % len(cands))
        sys.exit(0)
    cands = hits
tok = cands[0].get("tokenizer")
slot = cands[0].get("slot_save_path")
print("OK")
print(tok if isinstance(tok, str) and os.path.isdir(tok) else "")
print(slot if isinstance(slot, str) and slot else "")
PY
)
  case "$DISC" in
    OK*)
      [ -n "$TOKENIZER" ] || TOKENIZER=$(printf '%s\n' "$DISC" | sed -n '2p')
      [ -n "$SLOT" ] || SLOT=$(printf '%s\n' "$DISC" | sed -n '3p')
      ;;
    AMBIGUOUS*)
      echo "[run-strata-proxy] ${DISC#AMBIGUOUS } configs di motor in $STRATA_DIR, motore in uso non identificato (/v1/status non riporta il model e la porta dell'URL non corrisponde)." >&2
      echo "Nessun file scelto: --tokenizer non passato e slot_dir non dedotto - il proxy conta caratteri / chars_per_token e le sessioni vanno in $STRATA_DIR/sessions." >&2
      ;;
  esac
fi

# config.json e' locale (gitignored): se manca, nasce dall'esempio. Il slot_dir viene dal file
# del motore identificato sopra (slot_save_path); se non c'e' si usa STRATA_DIR/sessions, che
# e' la convenzione del Strata.
if [ ! -f "$CONFIG" ] && [ -f examples/config.example.json ]; then
  [ -n "$SLOT" ] || SLOT="$STRATA_DIR/sessions"
  cp examples/config.example.json "$CONFIG"
  if [ -x "$VENV/bin/python" ]; then
    "$VENV/bin/python" - "$CONFIG" "$SLOT" "$KV_CAP" <<'PY'
import json, sys
p, slot, kv = sys.argv[1], sys.argv[2], sys.argv[3] == "1"
cfg = json.load(open(p, encoding="utf-8"))
cfg["slot_dir"] = slot
cfg["kv_archive"] = kv
json.dump(cfg, open(p, "w", encoding="utf-8"), indent=2)
print("[run-strata-proxy] config criado em %s: slot_dir=%s, kv_archive=%s" % (p, slot, "true" if kv else "false"))
PY
  else
    echo "[run-strata-proxy] config criado em $CONFIG: ponha slot_dir na pasta sessions do Strata"
  fi
fi

# ---- kv_archive: il gate, prima che il proxy si fermi all'avvio ---------
WANT=$(cfg_wants_kv)
if [ -z "$WANT" ]; then
  echo "nao consegui ler $CONFIG (JSON invalido?)" >&2
  exit 1
fi
if [ "$WANT" = 1 ] && [ "$KV_CAP" != 1 ] && [ "${SKIP_SETUP:-0}" != 1 ] && [ -x ./setup.sh ]; then
  echo "[run-strata-proxy] o config pede kv_archive e $VENV nao tem compression.zstd: tentando ./setup.sh ..."
  ./setup.sh --no-sudo >/dev/null 2>&1 || true
  if venv_has .venv314 && venv_zstd .venv314; then VENV=.venv314; KV_CAP=1; fi
fi
if [ "$WANT" = 1 ] && [ "$KV_CAP" != 1 ]; then
  echo "o config $CONFIG pede \"kv_archive\": true, mas o Python de $VENV nao tem compression.zstd (pede Python >= 3.14)." >&2
  echo "  o proxy pararia na inicializacao. Opcoes:" >&2
  echo "    ./setup.sh               constroi .venv314 com uv (uv python install 3.14)" >&2
  echo "    \"kv_archive\": false      no config: o proxy sobe igual, sem arquivo frio" >&2
  echo "    VENV=/caminho/venv314 ./run-strata-proxy.sh" >&2
  echo "  $CONFIG nao foi tocado." >&2
  exit 1
fi

# ---- o motor: avisa antes, com o comando de start do Strata --------------
# Solo 2xx e o motor. Connessione/timeout -> "conn"; 4xx/5xx -> la porta risponde, ma
# non e il motore.
CODE=$(probe_status)
case "$CODE" in
  2*) : ;;
  conn)
    echo "Strata nao responde em $STRATA_URL/v1/status (conexao ou timeout) - suba-o antes: cd $STRATA_DIR && ./setup.sh" >&2
    exit 1 ;;
  nocurl)
    echo "sem curl e sem python no venv: nao consegui verificar $STRATA_URL/v1/status (instale curl, ou rode ./setup.sh)" >&2
    exit 1 ;;
  *)
    echo "Strata respondeu HTTP $CODE em $STRATA_URL/v1/status: a porta responde, mas nao e o motor (servico de outra coisa, ou motor fora do ar)." >&2
    exit 1 ;;
esac

echo "[run-strata-proxy] venv=$VENV kv_archive=$([ "$WANT" = 1 ] && [ "$KV_CAP" = 1 ] && echo on || echo off) upstream=$STRATA_URL porta=$HOST:$PORT data=$DATA config=$CONFIG tokenizer=$([ -n "$TOKENIZER" ] && echo "$TOKENIZER" || echo 'chars/3.5')"
set --
[ -n "$TOKENIZER" ] && set -- --tokenizer "$TOKENIZER"
exec "$VENV/bin/mnemonic-proxy" \
  --upstream "$STRATA_URL" \
  --host "$HOST" --port "$PORT" \
  --data "$DATA" \
  --config "$CONFIG" "$@"
