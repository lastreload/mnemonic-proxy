#!/bin/sh
# Author: Maurizio Verde — LastReload
# setup.sh — prepara o ambiente do mnemonic-proxy, como o setup.sh do Strata:
# git clone, ./setup.sh, e o proxy sobe. Nada de passos manuais.
#
#   ./setup.sh            cria o venv e instala o proxy (usa sudo so se faltar Python)
#   ./setup.sh --check    so verifica e reporta; exit 0 se esta tudo pronto, 1 se falta algo
#   ./setup.sh --no-kv    nao mexe no kv_archive (venv 3.14) mesmo se estiver faltando
#   ./setup.sh --no-sudo  nunca chama sudo: informa o que falta e sai
#
# O que o proxy precisa de verdade: Python >= 3.10 com venv+pip. O pacote nao tem
# dependencias (biblioteca padrao). Duas coisas sao opcionais, e este script prepara
# as duas quando consegue:
#   * tokenizers (extra "exact")  -> contagem exata de tokens
#   * Python >= 3.14 (compression.zstd) -> kv_archive, o arquivo frio dos session files
# O kv_archive e' opt-in: sem Python 3.14 o proxy funciona igual, so sem o arquivo frio.

set -u
cd "$(dirname "$0")" || exit 1

VENV_MAIN="${VENV_MAIN:-.venv}"
VENV_KV="${VENV_KV:-.venv314}"
CHECK=0; WANT_KV=1; ALLOW_SUDO=1
for a in "$@"; do
  case "$a" in
    --check)   CHECK=1 ;;
    --no-kv)   WANT_KV=0 ;;
    --no-sudo) ALLOW_SUDO=0 ;;
    -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
    *) echo "setup.sh: opcao desconhecida: $a (--help mostra as opcoes)" >&2; exit 2 ;;
  esac
done

# um python serve se e' >= 3.10 e consegue criar venv COM pip: Debian/Ubuntu trazem o
# venv sem ensurepip (pacote python3-venv a parte), e um venv sem ensurepip nao tem pip
ok_py() { "$1" -c 'import sys, venv, ensurepip; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; }
py_ver() { "$1" -c 'import sys; print(".".join(str(x) for x in sys.version_info[:3]))' 2>/dev/null; }
ge314() { "$1" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 14) else 1)' 2>/dev/null; }
venv_has_pip() { [ -x "$1/bin/python" ] && "$1/bin/python" -m pip --version >/dev/null 2>&1; }
# o import e' testato fuori dal progetto: dentro della cartella il Python vede il
# pacchetto ctxproxy/ sorgente e direbbe "gia' installato" anche con il venv vuoto
venv_has_pkg()
{
  [ -x "$1/bin/python" ] || return 1
  vp=$(cd "$1/bin" && pwd)/python
  (cd / && "$vp" -c 'import ctxproxy' >/dev/null 2>&1)
}
venv_zstd() { [ -x "$1/bin/python" ] && "$1/bin/python" -c 'import compression.zstd' >/dev/null 2>&1; }
venv_tok() { [ -x "$1/bin/python" ] && "$1/bin/python" -c 'import tokenizers' >/dev/null 2>&1; }
say() { printf '%s\n' "$*"; }

# ---- 1) um python bom -----------------------------------------------------
pick_py()
{
  for c in python3 python python3.14 python3.13 python3.12 python3.11 python3.10; do
    p=$(command -v "$c" 2>/dev/null) || continue
    if ok_py "$p"; then printf '%s\n' "$p"; return 0; fi
  done
  return 1
}

install_python()
{
  say "Python 3.10+ com venv+pip nao encontrado. Instalando (sudo vai pedir a senha) ..."
  if [ "$ALLOW_SUDO" != 1 ]; then
    say "setup.sh --no-sudo: instale voce mesmo:"
    say "  Ubuntu/Debian: sudo apt-get install -y python3 python3-venv python3-pip"
    exit 1
  fi
  if command -v apt-get >/dev/null 2>&1; then
    sudo apt-get update && sudo apt-get install -y python3 python3-venv python3-pip
  elif command -v dnf >/dev/null 2>&1; then
    sudo dnf install -y python3 python3-pip
  elif command -v pacman >/dev/null 2>&1; then
    sudo pacman -S --noconfirm python python-pip
  else
    say "Instale Python 3.10+ com venv (Ubuntu/Debian: sudo apt install python3-venv) e rode ./setup.sh de novo."
    exit 1
  fi
}

PY="$(pick_py)"
if [ -z "$PY" ]; then
  if [ "$CHECK" = 1 ]; then
    say "FALTA: Python 3.10+ com venv+pip (Ubuntu/Debian: sudo apt install python3-venv)"
    exit 1
  fi
  install_python
  PY="$(pick_py)"
  [ -n "$PY" ] || { say "setup.sh: ainda sem um Python 3.10+ com venv+pip."; exit 1; }
fi

# ---- 2) o venv do proxy ----------------------------------------------------
# um .venv de uma corrida que parou no meio tem python sem pip: comeca de novo
if [ -x "$VENV_MAIN/bin/python" ] && ! venv_has_pip "$VENV_MAIN"; then
  say "$VENV_MAIN existe sem pip (criacao parada no meio): recriando."
  rm -rf "$VENV_MAIN"
fi

if [ ! -x "$VENV_MAIN/bin/python" ] && [ "$CHECK" = 1 ]; then
  say "FALTA: $VENV_MAIN (rode ./setup.sh)"
  exit 1
fi

if [ ! -x "$VENV_MAIN/bin/python" ]; then
  UV=$(command -v uv 2>/dev/null || true)
  say "criando $VENV_MAIN com $(py_ver "$PY") ..."
  if [ -n "$UV" ]; then
    # --seed: uv faz o venv SEM pip; o seed poe o pip dentro (o proxy instala com pip)
    "$UV" venv --seed "$VENV_MAIN" --python "$PY" || { say "setup.sh: uv venv falhou."; exit 1; }
  else
    "$PY" -m venv "$VENV_MAIN" || { say "setup.sh: python -m venv falhou (Ubuntu/Debian: sudo apt install python3-venv)."; exit 1; }
  fi
fi

if ! venv_has_pkg "$VENV_MAIN"; then
  say "instalando o pacote mnemonic-proxy em $VENV_MAIN ..."
  if [ -x "$VENV_MAIN/bin/pip" ]; then
    "$VENV_MAIN/bin/pip" install -q --upgrade pip >/dev/null 2>&1
    "$VENV_MAIN/bin/pip" install -q -e ".[exact]" \
      || "$VENV_MAIN/bin/pip" install -q -e . \
      || { say "setup.sh: pip install falhou."; exit 1; }
  elif [ -n "${UV:-}" ]; then
    "$UV" pip install --python "$VENV_MAIN/bin/python" -e ".[exact]" \
      || "$UV" pip install --python "$VENV_MAIN/bin/python" -e . \
      || { say "setup.sh: uv pip install falhou."; exit 1; }
  else
    say "setup.sh: nem pip nem uv disponiveis para instalar o pacote."
    exit 1
  fi
fi

# ---- 3) kv_archive: precisa de Python >= 3.14 (compression.zstd) -----------
# se o python do .venv ja e' 3.14, o kv_archive funciona no proprio .venv: nada a fazer
KV_VENV=""
if venv_zstd "$VENV_MAIN"; then
  KV_VENV="$VENV_MAIN"
elif venv_zstd "$VENV_KV" && venv_has_pkg "$VENV_KV"; then
  KV_VENV="$VENV_KV"
fi

if [ -z "$KV_VENV" ] && [ "$WANT_KV" = 1 ] && [ "$CHECK" != 1 ]; then
  UV=$(command -v uv 2>/dev/null || true)
  if [ -n "$UV" ]; then
    say "kv_archive pede Python >= 3.14: criando $VENV_KV com uv (baixa o 3.14 se precisar) ..."
    rm -rf "$VENV_KV" 2>/dev/null
    "$UV" venv --seed "$VENV_KV" --python 3.14 >/dev/null 2>&1 \
      && "$UV" pip install --python "$VENV_KV/bin/python" -q -e ".[exact]" >/dev/null 2>&1 \
      && venv_zstd "$VENV_KV" && KV_VENV="$VENV_KV"
    if [ -z "$KV_VENV" ]; then
      "$UV" venv --seed "$VENV_KV" --python 3.14 >/dev/null 2>&1 \
        && "$UV" pip install --python "$VENV_KV/bin/python" -q -e . >/dev/null 2>&1 \
        && venv_zstd "$VENV_KV" && KV_VENV="$VENV_KV"
    fi
    [ -n "$KV_VENV" ] || say "aviso: nao consegui o Python 3.14; o proxy sobe igual, sem kv_archive."
  elif [ "$CHECK" != 1 ]; then
    say "aviso: kv_archive pede Python >= 3.14 e o uv nao esta instalado."
    say "       o proxy funciona sem ele (kv_archive e' opt-in). Para habilitar: uv python install 3.14"
  fi
fi

# ---- 4) relatorio ---------------------------------------------------------
TOK=no; venv_tok "$VENV_MAIN" && TOK=sim
ZSTD=no; [ -n "$KV_VENV" ] && ZSTD=sim
say "python do sistema usado : $(py_ver "$PY") ($PY)"
say "$VENV_MAIN              : $(py_ver "$VENV_MAIN/bin/python") | pacote $(venv_has_pkg "$VENV_MAIN" && echo sim || echo nao) | tokenizers $TOK"
say "kv_archive (zstd)       : $ZSTD$( [ -n "$KV_VENV" ] && printf ' em %s' "$KV_VENV" )"
if [ "$CHECK" = 1 ]; then
  if venv_has_pkg "$VENV_MAIN"; then
    [ -n "$KV_VENV" ] || say "check: ok (sem kv_archive: opt-in, o proxy sobe assim mesmo)"
    exit 0
  fi
  say "check: FALTA o pacote instalado em $VENV_MAIN (rode ./setup.sh)"
  exit 1
fi

say ""
say "pronto. para subir o proxy na frente do Strata:"
say "  ./run-strata-proxy.sh"
say "para conferir o ambiente sem instalar nada:"
say "  ./setup.sh --check"
