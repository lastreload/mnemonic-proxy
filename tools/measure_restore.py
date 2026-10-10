#!/usr/bin/env python3
# Author: Maurizio Verde — LastReload (protocolo de medição)
"""Mede o RESTAURO automático do estado do motor, sem reiniciar o motor.

O proxy restaura um estado salvo quando o motor perde a conversa. Os gatilhos são
( ctxproxy/autosave.py, Tracker ):

  1. o motor reiniciou            -> "started" mudou no /v1/status
  2. outra conversa usou o motor  -> "requests" mudou no /v1/status
  3. volta a um segmento selado

O gatilho 2 não é destrutivo: funciona no motor vivo. Este script o explora:

  conversa A  cresce até --tokens, fica ociosa -> autosave salva <conv>-seg0-auto-<impronta>.bin
  conversa B  usa o motor                       -> o estado da A sai da memória
  conversa A  próximo request                   -> Tracker vê requests diferente -> restore

Mede os dois lados e imprime a comparação:

  re-read : primeiro request da A (motor sem nada) -> prompt_read = prompt_tokens
  restore : request da A depois da B               -> evento autorestore + prompt_read novo

Sobe um proxy próprio (--port, default 8196) com dados próprios (--data, default ./data-restore-test)
na frente do MESMO motor. Não toca no proxy que já está rodando e não reinicia o motor.

Uso:
    .venv314/bin/python tools/measure_restore.py            # roda tudo
    .venv314/bin/python tools/measure_restore.py --dry-run  # só o plano
    .venv314/bin/python tools/measure_restore.py --keep-data

Requer o motor OpenAI-compatível com saved state já ligado (Strata >= 0.1.40 com
slot_save_path no config, ou llama-server com --slot-save-path).
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.request

HERE = os.path.abspath(os.path.dirname(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))

# Config do teste. mask_trigger alto: o que se mede aqui e o custo de reler o prompt,
# entao a conversa de teste nao deve ser mascarada antes da hora.
TEST_CONFIG = {
    "window": 131072,
    "mask_trigger": 60000,
    "mask_target": 45000,
    "mask_reasoning": False,
    "mask_tool_args": False,
    "mask_anchor": True,
    "slot_save": True,
    "seal_experimental": True,
    "notes_max_tokens": 12288,
    "response_floor": 16384,
    "autosave": True,
    "autosave_idle_s": 20.0,
    "autosave_keep": 4,
    "autosave_max_gb": 12.0,
    "autosave_min_free_gb": 8.0,
    "autorestore": True,
    "autorestore_min_gain": 4096,
    "inject_recall": False,
    "tools_paging": False,
    "live_dump": False,
}


def free_port(base: int) -> int:
    for p in range(base, base + 40):
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", p))
                return p
            except OSError:
                continue
    raise SystemExit("nenhuma porta livre a partir de %d" % base)


def get_json(url: str, timeout: float = 10.0):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode())


def chat(base: str, model: str, messages: list, max_tokens: int, timeout: float = 900.0):
    """Um request de chat completions. -> (ms totais, usage, texto da resposta)."""
    payload = {"model": model, "messages": messages, "max_tokens": max_tokens,
               "stream": False, "temperature": 0.2}
    req = urllib.request.Request(base.rstrip("/") + "/v1/chat/completions",
                                 data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = json.loads(r.read().decode())
    ms = (time.time() - t0) * 1000.0
    usage = body.get("usage") or {}
    txt = ((body.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
    return ms, usage, txt


def filler(turn: int, target_tokens: int) -> str:
    """Texto longo e deterministico: cresce o prompt sem virar instrucao para o modelo."""
    unit = ("linha %03d: registro de teste de contexto, campo a=1 b=2 c=3, "
            "valor estavel, nada a fazer, nada a responder. " % (turn % 100))
    return (unit * max(1, int(target_tokens / 22)))[: target_tokens * 4]


def journal_events(path: str) -> list:
    out = []
    if not os.path.exists(path):
        return out
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="mede o restore automatico do saved state")
    ap.add_argument("--upstream", default="http://127.0.0.1:8080", help="motor (default: Strata em 8080)")
    ap.add_argument("--port", type=int, default=8196, help="porta do proxy de teste (default 8196)")
    ap.add_argument("--data", default=os.path.join(ROOT, "data-restore-test"), help="pasta de dados do teste")
    ap.add_argument("--slot-dir", default="", help="pasta dos session files (default: lida do config.json)")
    ap.add_argument("--tokens", type=int, default=24000, help="tokens de prompt alvo da conversa A")
    ap.add_argument("--turn-tokens", type=int, default=4000, help="tokens por turno da conversa A")
    ap.add_argument("--probe-tokens", type=int, default=6000, help="tokens da conversa B (a que ocupa o motor)")
    ap.add_argument("--idle", type=float, default=20.0, help="autosave_idle_s do teste (default 20)")
    ap.add_argument("--venv", default="", help="venv a usar (default: .venv314 se existir, senao .venv)")
    ap.add_argument("--keep-data", action="store_true", help="nao apagar a pasta de dados do teste")
    ap.add_argument("--dry-run", action="store_true", help="so imprime o plano")
    a = ap.parse_args()

    TEST_CONFIG["autosave_idle_s"] = a.idle
    slot_dir = a.slot_dir
    if not slot_dir:
        cfgp = os.path.join(ROOT, "config.json")
        if os.path.exists(cfgp):
            try:
                slot_dir = json.load(open(cfgp)).get("slot_dir", "")
            except Exception:
                slot_dir = ""
    TEST_CONFIG["slot_dir"] = slot_dir

    venv = a.venv
    if not venv:
        venv = ".venv314" if os.path.exists(os.path.join(ROOT, ".venv314/bin/mnemonic-proxy")) else ".venv"
    proxy_bin = os.path.join(ROOT, venv, "bin/mnemonic-proxy")
    tok = os.environ.get("TOKENIZER", "")
    port = free_port(a.port)
    base = "http://127.0.0.1:%d" % port

    print("=== plano ===")
    print("motor de teste   :", a.upstream, "(o MESMO que ja esta rodando: nada e reiniciado)")
    print("proxy de teste   :", proxy_bin, "->", base, "data:", a.data)
    print("session files    :", slot_dir or "(nenhum: saved state indisponivel)")
    print("conversa A       : cresce ate ~%d tokens (%d por turno), ociosa %ss -> autosave"
          % (a.tokens, a.turn_tokens, a.idle))
    print("conversa B       : ~%d tokens, ocupa o motor -> o estado da A sai da memoria" % a.probe_tokens)
    print("medicao          : autorestore da A (ms, tokens) vs re-read da A (prompt_read, prefill)")
    if a.dry_run:
        return 0
    if not slot_dir:
        print("\nFALTA: --slot-dir (a pasta dos session files, a mesma do --slot-save-path do motor).")
        return 2

    try:
        st = get_json(a.upstream.rstrip("/") + "/v1/status")
    except Exception as e:
        print("motor nao responde em %s (%s)" % (a.upstream, e))
        return 2
    model = st.get("model") or "local-model"
    print("\nmotor: model=%s started=%s requests=%s"
          % (model, st.get("started"), ((st.get("activity") or {}).get("requests")) or 0))

    os.makedirs(a.data, exist_ok=True)
    cfgpath = os.path.join(a.data, "config.test.json")
    with open(cfgpath, "w", encoding="utf-8") as f:
        json.dump(TEST_CONFIG, f, indent=2)

    cmd = [proxy_bin, "--upstream", a.upstream, "--host", "127.0.0.1", "--port", str(port),
           "--data", a.data, "--config", cfgpath]
    if tok:
        cmd += ["--tokenizer", tok]
    log = open(os.path.join(a.data, "proxy.log"), "w")
    proc = subprocess.Popen(cmd, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT)
    ready = False
    for _ in range(90):
        time.sleep(1.0)
        if proc.poll() is not None:
            break
        try:
            if get_json(base + "/health").get("status") == "ok":
                ready = True
                break
        except Exception:
            pass
    if not ready:
        print("\nproxy de teste nao ficou pronto; saida em", os.path.join(a.data, "proxy.log"))
        proc.terminate()
        return 2
    print("proxy de teste pronto em", base)

    results = {}
    try:
        # 2) conversa A: cresce o prompt; o primeiro request e o re-read puro (motor sem nada)
        msgs_a = [{"role": "system", "content": "Responda em uma linha. Nao use ferramentas."}]
        first_ms = first_prompt = first_read = 0
        turn = 0
        grown = 0
        while grown < a.tokens:
            turn += 1
            msgs_a.append({"role": "user", "content": filler(turn, a.turn_tokens)})
            ms, usage, txt = chat(base, model, msgs_a, 48)
            grown = usage.get("prompt_tokens") or grown
            if turn == 1:
                first_ms = ms
                first_prompt = usage.get("prompt_tokens") or 0
                first_read = first_prompt
            print("  A turno %2d: prompt=%s tokens, %.0f ms" % (turn, usage.get("prompt_tokens"), ms))
            msgs_a.append({"role": "assistant", "content": (txt or "ok")[:200]})

        # 3) ocioso -> autosave
        print("\nocioso: esperando autosave (autosave_idle_s=%ss) ..." % a.idle)
        deadline = time.time() + a.idle + 60
        saved = None
        while time.time() < deadline:
            time.sleep(3.0)
            for e in journal_events(os.path.join(a.data, "journal.jsonl")):
                if e.get("event") == "autosave":
                    saved = e
            if saved:
                break
        if saved:
            print("  autosave: file=%s tokens=%s n_saved=%s bytes=%s em %s ms (idle %ss)"
                  % (saved.get("file"), saved.get("tokens"), saved.get("n_saved"),
                     saved.get("bytes"), saved.get("ms"), saved.get("idle_s")))
        else:
            print("  nenhum autosave registrado: veja", os.path.join(a.data, "proxy.log"))
        results["autosave"] = saved

        # 4) conversa B ocupa o motor
        msgs_b = [{"role": "system", "content": "Responda em uma linha. Nao use ferramentas."}]
        grown_b = 0
        turn_b = 0
        while grown_b < a.probe_tokens:
            turn_b += 1
            msgs_b.append({"role": "user", "content": filler(500 + turn_b, max(1500, a.probe_tokens // 2))})
            ms, usage, txt = chat(base, model, msgs_b, 32)
            grown_b = usage.get("prompt_tokens") or grown_b
            print("  B turno %2d: prompt=%s tokens, %.0f ms" % (turn_b, usage.get("prompt_tokens"), ms))
            msgs_b.append({"role": "assistant", "content": (txt or "ok")[:120]})

        # 5) volta a A: o Tracker ve requests diferente -> restore
        msgs_a.append({"role": "user", "content": filler(900, 400)})
        ms_r, usage_r, _ = chat(base, model, msgs_a, 32)
        evs = journal_events(os.path.join(a.data, "journal.jsonl"))
        reqs = [e for e in evs if e.get("event") == "request"]
        req = reqs[-1] if reqs else {}
        restores = [e for e in evs if e.get("event") == "autorestore"]
        results["restore_request"] = req
        results["autorestore"] = restores[-1] if restores else None

        print("\n=== resultado ===")
        print("A antes do teste (motor sem nada): prompt=%s lidos=%s, %.0f ms"
              % (first_prompt, first_read, first_ms))
        if results["autorestore"]:
            rs = results["autorestore"]
            print("restore da A apos a B: file=%s tokens=%s em %s ms"
                  % (rs.get("file"), rs.get("tokens"), rs.get("ms")))
            print("request da A apos o restore: prompt=%s reused=%s lidos=%s, %.0f ms"
                  % (req.get("prompt_tokens"), req.get("reused"), req.get("prompt_read"),
                     req.get("prompt_ms") or 0))
            if first_read and req.get("prompt_tokens"):
                per_tok = first_ms / max(first_read, 1)
                est = req.get("prompt_tokens") * per_tok
                gain = est / max(rs.get("ms") or 1, 1)
                print("estimativa de reler tudo: %.1f s  vs  restore %.1f s  (ganho ~%.0fx)"
                      % (est / 1000.0, (rs.get("ms") or 0) / 1000.0, gain))
        else:
            print("nenhum autorestore registrado. request da A: prompt=%s reused=%s lidos=%s"
                  % (req.get("prompt_tokens"), req.get("reused"), req.get("prompt_read")))
            print("Se reused e alto, o motor guardou o prefixo (checkpoint do motor) e o restore")
            print("nao foi acionado: aumente --probe-tokens para a B ocupar o motor de fato.")

        with open(os.path.join(a.data, "measure_result.json"), "w", encoding="utf-8") as f:
            json.dump(results, f, indent=2, ensure_ascii=False)
        print("\nresultados em", os.path.join(a.data, "measure_result.json"))
    finally:
        proc.send_signal(signal.SIGINT)
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
        log.close()
        if not a.keep_data:
            print("(use --keep-data para conservar %s)" % a.data)
    return 0


if __name__ == "__main__":
    sys.exit(main())
