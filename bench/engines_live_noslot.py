"""Prova reale breve: llama-server SENZA --slot-save-path (rilevamento, nessun salvataggio) e --engine openai.

    PYTHONPATH=. python bench/engines_live_noslot.py --server BIN --model GGUF --port 8131
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import tempfile
import time
import urllib.request

from ctxproxy import engines
from ctxproxy.core import Config, Journal, Store, TokenCounter
from ctxproxy.server import Proxy
from ctxproxy.upstream import Upstream


def wait_up(url, timeout=120):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            with urllib.request.urlopen(url + "/health", timeout=2) as r:
                if r.status == 200:
                    return
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.3)
    raise RuntimeError("llama-server non parte")


def run_case(url, forced, tmp):
    cfg = Config.from_dict({"engine": forced, "slot_save": True, "autosave": True, "mask_anchor": True})
    up = Upstream(url)
    journal = Journal(os.path.join(tmp, forced + "-journal.jsonl"))
    counter = TokenCounter(cfg.chars_per_token)
    eng = engines.setup(cfg, up, journal, explicit={"engine", "slot_save", "autosave", "mask_anchor"},
                        counter=counter, log=lambda m: None)
    proxy = Proxy(cfg, up, Store(os.path.join(tmp, forced + ".sqlite")), journal, counter, mode="active")
    proxy.engine = eng
    body = {"model": "x", "messages": [{"role": "user", "content": "Rispondi con una parola: ciao /no_think"}],
            "max_tokens": 8, "stream": False}
    t0 = time.time()
    st, out, _ = proxy.chat(body)
    wall = round(time.time() - t0, 3)
    ans = None
    if isinstance(out, dict):
        ans = ((out.get("choices") or [{}])[0].get("message") or {}).get("content")
    return {"forced": forced, "engine": eng.summary(), "cfg_after": {
        "slot_save": cfg.slot_save, "autosave": cfg.autosave, "mask_anchor": cfg.mask_anchor},
        "status_normalized": engines.normalize_status(eng, up), "chat_status": st, "answer": ans, "wall_s": wall,
        "events": sorted({e.get("type") or e.get("event") or "?" for e in journal.mem})}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--server", required=True)
    ap.add_argument("--model", required=True)
    ap.add_argument("--port", type=int, default=8131)
    ap.add_argument("--threads", type=int, default=8)
    ap.add_argument("--out", default="")
    a = ap.parse_args()
    env = dict(os.environ, CUDA_VISIBLE_DEVICES="")
    cmd = ["nice", "-n", "19", a.server, "-m", a.model, "-ngl", "0", "-t", str(a.threads), "-c", "4096", "-np",
           "1", "--host", "127.0.0.1", "--port", str(a.port), "--jinja"]
    p = subprocess.Popen(cmd, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    url = "http://127.0.0.1:%d" % a.port
    res = {}
    try:
        wait_up(url)
        with tempfile.TemporaryDirectory() as tmp:
            for forced in ("auto", "openai"):
                res[forced] = run_case(url, forced, tmp)
    finally:
        p.terminate()
        p.wait(timeout=30)
    s = json.dumps(res, indent=1, ensure_ascii=False, default=str)
    print(s)
    if a.out:
        with open(a.out, "w", encoding="utf-8") as f:
            f.write(s)


if __name__ == "__main__":
    main()
