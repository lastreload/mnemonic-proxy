"""Misura reale: proxy davanti a llama-server con --slot-save-path (ENGINES-RESULT.md).

    python -m bench.engines_llama --server BIN --model GGUF --port 8131 --slots DIR --out result.json

Il proxy gira nello stesso processo (niente porta in più); llama-server è lanciato e riavviato da qui, solo CPU
(CUDA_VISIBLE_DEVICES="" e -ngl 0), nice 19. Passi:
  1. conversazione di qualche migliaio di token (uscite di strumenti = file veri del progetto) passata dal proxy:
     prima lettura completa;
  2. autosalvataggio (AutoSaver.tick) -> ms, byte, n_saved;
  3. riavvio di llama-server (stato perso) + turno successivo: ripristino automatico dal file + lettura della sola
     coda;
  4. riavvio + stesso turno SENZA ripristino: rilettura completa;
  5. altra conversazione nello slot e ritorno alla prima (other_state);
  6. kv_archive sul file di llama-server: archivia, verifica, ricostruisce byte per byte.
"""
from __future__ import annotations

import argparse
import glob
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import time
import urllib.request

from ctxproxy import engines
from ctxproxy.core import Config, Journal, Store, TokenCounter
from ctxproxy.server import Proxy
from ctxproxy.upstream import Upstream

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def wait_up(url, timeout=120):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            with urllib.request.urlopen(url + "/health", timeout=2) as r:
                if r.status == 200:
                    return time.time() - t0
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.3)
    raise RuntimeError("llama-server non parte")


class Server:
    def __init__(self, a):
        self.a, self.p = a, None

    def start(self):
        env = dict(os.environ, CUDA_VISIBLE_DEVICES="")
        cmd = ["nice", "-n", "19", self.a.server, "-m", self.a.model, "-ngl", "0", "-t", str(self.a.threads),
               "-c", str(self.a.ctx), "-np", "1", "--slot-save-path", self.a.slots, "--host", "127.0.0.1",
               "--port", str(self.a.port), "--jinja"]
        self.log = open(os.path.join(os.path.dirname(self.a.slots.rstrip("/")), "server.log"), "ab")
        t0 = time.time()
        self.p = subprocess.Popen(cmd, env=env, stdout=self.log, stderr=subprocess.STDOUT)
        wait_up("http://127.0.0.1:%d" % self.a.port)
        return round(time.time() - t0, 2)

    def stop(self):
        if self.p:
            self.p.terminate()
            try:
                self.p.wait(20)
            except subprocess.TimeoutExpired:
                self.p.kill()
            self.p = None

    def restart(self):
        self.stop()
        return self.start()


def build_history(n_files: int, first: str):
    """system + utente + giri di strumento che leggono file veri del progetto."""
    files = sorted(glob.glob(os.path.join(ROOT, "ctxproxy", "*.py")))
    msgs = [{"role": "system", "content": "Sei un assistente di programmazione. Rispondi in italiano, breve."},
            {"role": "user", "content": first}]
    tools = [{"type": "function", "function": {"name": "read", "description": "legge un file",
                                               "parameters": {"type": "object", "properties": {
                                                   "path": {"type": "string"}}, "required": ["path"]}}}]
    k = 0
    for f in files:
        if k >= n_files:
            break
        text = open(f, encoding="utf-8").read()[:5200]
        cid = "call_%d" % k
        rel = os.path.relpath(f, ROOT)
        msgs.append({"role": "assistant", "content": "", "tool_calls": [
            {"id": cid, "type": "function", "function": {"name": "read", "arguments": json.dumps({"path": rel})}}]})
        msgs.append({"role": "tool", "tool_call_id": cid, "content": text})
        k += 1
    msgs.append({"role": "assistant", "content": "Ho letto i file principali del proxy."})
    msgs.append({"role": "user", "content": "In una frase: a cosa serve il proxy?"})
    return msgs, tools


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--server", required=True)
    ap.add_argument("--model", required=True)
    ap.add_argument("--port", type=int, default=8131)
    ap.add_argument("--slots", required=True)
    ap.add_argument("--ctx", type=int, default=16384)
    ap.add_argument("--threads", type=int, default=8)
    ap.add_argument("--files", type=int, default=6)
    ap.add_argument("--out", default="engines_llama.json")
    a = ap.parse_args()
    os.makedirs(a.slots, exist_ok=True)
    for f in glob.glob(os.path.join(a.slots, "*")):
        os.remove(f)
    work = tempfile.mkdtemp(prefix="eng-bench-", dir=os.path.dirname(a.slots.rstrip("/")))
    srv = Server(a)
    res = {"model": os.path.basename(a.model), "ctx": a.ctx, "threads": a.threads}
    try:
        res["server_start_s"] = srv.start()
        url = "http://127.0.0.1:%d" % a.port
        raw = {"autosave": True, "autosave_idle_s": 1, "autosave_min_free_gb": 0, "slot_dir": a.slots,
               "slot_save": True}
        cfg = Config.from_dict(raw)
        up = Upstream(url)
        j = Journal(os.path.join(work, "journal.jsonl"))
        eng = engines.setup(cfg, up, j, explicit=set(raw), log=print)
        res["engine"] = eng.summary()
        res["window"] = cfg.window
        proxy = Proxy(cfg, up, Store(os.path.join(work, "archive.sqlite")), j, TokenCounter(3.5))
        saver = proxy.enable_autosave(start=False)
        params = {"model": "m", "max_tokens": 24, "temperature": 0,
                  "chat_template_kwargs": {"enable_thinking": False}}

        def send(msgs, tools):
            t0 = time.time()
            st, r, _ = proxy.chat({**params, "messages": msgs, "tools": tools})
            assert st == 200, r
            tm, u = r.get("timings") or {}, r.get("usage") or {}
            return {"wall_s": round(time.time() - t0, 3), "prompt_tokens": u.get("prompt_tokens"),
                    "prompt_read": tm.get("prompt_n"), "cache_n": tm.get("cache_n"),
                    "prompt_ms": round(tm.get("prompt_ms") or 0), "answer": r["choices"][0]["message"].get("content")}

        def last(ev):
            return next((e for e in reversed(j.mem) if e["event"] == ev), None)

        h, tools = build_history(a.files, "Analizza il progetto virtual-context-proxy.")
        res["1_first_read"] = send(h, tools)
        ev = saver.tick(now=time.time() + 5)
        assert ev, [e for e in j.mem if e["event"].startswith("autosave")]
        res["2_autosave"] = {k: ev[k] for k in ("file", "tokens", "n_saved", "bytes", "ms")}
        fpath = os.path.join(a.slots, ev["file"])
        res["2_autosave"]["file_bytes"] = os.path.getsize(fpath)

        h2 = h + [{"role": "assistant", "content": "Serve a dare a un motore locale un contesto virtuale."},
                  {"role": "user", "content": "E il nascondimento delle uscite a cosa serve? Una frase."}]
        res["3_restart_s"] = srv.restart()
        r3 = send(h2, tools)
        ar = last("autorestore")
        r3["autorestore"] = {k: ar.get(k) for k in ("reason", "tokens", "ms", "restore_ms", "n_restored")} if ar \
            else None
        res["3_resume_with_file"] = r3

        res["4_restart_s"] = srv.restart()
        cfg.autorestore = False
        r4 = send(h2, tools)
        r4["autorestore"] = None
        res["4_resume_full_reread"] = r4
        cfg.autorestore = True

        # 5) altra conversazione nello slot, poi ritorno alla prima
        saver.tick(now=time.time() + 5)            # salva lo stato attuale (h2 riletta)
        hb, _ = build_history(2, "Altro progetto: un parser JSON.")
        res["5_other_conversation"] = send(hb, tools)
        h3 = h2 + [{"role": "assistant", "content": "Libera spazio nel contesto."},
                   {"role": "user", "content": "Grazie. Ultima domanda: cosa sono i segmenti?"}]
        n_ar = len([e for e in j.mem if e["event"] == "autorestore"])
        r5 = send(h3, tools)
        ar = last("autorestore") if len([e for e in j.mem if e["event"] == "autorestore"]) > n_ar else None
        r5["autorestore"] = {k: ar.get(k) for k in ("reason", "tokens", "ms", "restore_ms", "n_restored")} if ar \
            else None
        res["5_back_to_first"] = r5

        # 6) kv_archive sul file di llama-server
        from ctxproxy.kvarchive import KvArchive
        arc_root = os.path.join(work, "kv-archive")
        arc = KvArchive(arc_root, level=3, threads=4)
        files = sorted(glob.glob(os.path.join(a.slots, "*.bin")), key=os.path.getsize)
        f = files[-1]
        sha = hashlib.sha256(open(f, "rb").read()).hexdigest()
        cp = os.path.join(work, os.path.basename(f))
        shutil.copy(f, cp)
        r = arc.archive(cp, delete_original=False)
        dest = os.path.join(work, "rebuilt.bin")
        rr = arc.restore(os.path.basename(cp), dest)
        res["6_kv_archive"] = {"file": os.path.basename(f), "bytes": os.path.getsize(f),
                               "stored_bytes": r.get("new_stored_bytes"), "blocks": r.get("blocks"),
                               "archive_ms": r.get("archive_ms"), "restore_ms": rr.get("ms"),
                               "verify": arc.verify(os.path.basename(cp)),
                               "identical": hashlib.sha256(open(dest, "rb").read()).hexdigest() == sha,
                               "ratio": round(os.path.getsize(f) / max(1, r.get("new_stored_bytes") or 1), 2)}
        res["slot_files"] = {os.path.basename(x): os.path.getsize(x) for x in glob.glob(os.path.join(a.slots, "*"))}
        res["journal_events"] = sorted({e["event"] for e in j.mem})
    finally:
        srv.stop()
    with open(a.out, "w") as f:
        json.dump(res, f, ensure_ascii=False, indent=1)
    print(json.dumps(res, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
