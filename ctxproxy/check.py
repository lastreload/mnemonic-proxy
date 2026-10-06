# Author: Maurizio Verde — LastReload
"""`mnemonic-proxy check`: is this setup ready? Reads the configuration the server would run with (same options:
--upstream, --config, --data, --port, --engine ...) and checks it against the engine that is actually running.

Every line: status, evidence, fix. Saved state is reported on three levels:
  supported   the engine can save/restore its state (llama-server started with --slot-save-path, Strata
              session-files);
  configured  the proxy config turns the saved-state features on (slot_save, mask_anchor, autosave ...);
  verified    `--live` really saved and restored a state file, and found it in `slot_dir`.

`--live` also runs one short generation and one tool-call request on the engine. It refuses to run while the engine
slot is busy; the slot content is saved first and put back at the end, so an idle conversation is not lost.

Exit codes: 0 = ready, 2 = ready with warnings, 1 = not ready (at least one failure).
"""
from __future__ import annotations

import dataclasses
import json
import os
import socket
import sys
import time
import urllib.error
import urllib.request

from . import engines
from .core import Config
from .upstream import Upstream

OK, WARN, FAIL, INFO = "ok", "warn", "fail", "info"
ICON = {OK: "\u2705", WARN: "\u26a0\ufe0f ", FAIL: "\u274c", INFO: "\u2139\ufe0f "}
PLAIN = {OK: "[ OK ]", WARN: "[WARN]", FAIL: "[FAIL]", INFO: "[INFO]"}
SAVED_STATE = ("slot_save", "mask_anchor", "autosave", "seal_experimental", "kv_archive")


class Report:
    def __init__(self):
        self.items: list[dict] = []

    def add(self, status, name, evidence, fix=""):
        self.items.append({"status": status, "check": name, "evidence": evidence, "fix": fix})

    @property
    def exit_code(self) -> int:
        st = {i["status"] for i in self.items}
        return 1 if FAIL in st else 2 if WARN in st else 0


def _get(url, timeout=5.0):
    """-> (status, parsed JSON or text) ; status 0 = not reachable."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            data, st = r.read(), r.status
    except urllib.error.HTTPError as e:
        data, st = e.read(), e.code
    except Exception as e:  # noqa: BLE001
        return 0, repr(e)
    try:
        return st, json.loads(data)
    except ValueError:
        return st, data.decode("utf-8", "replace")[:300]


def _port_state(host, port):
    """-> "free" | "proxy" (a mnemonic-proxy answers there) | "busy"."""
    s = socket.socket(socket.AF_INET6 if ":" in host else socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind((host, port))
        return "free", None
    except OSError:
        pass
    finally:
        s.close()
    st, body = _get("http://%s:%d/v1/engine" % (host if ":" not in host else "[%s]" % host, port), 3)
    if st == 200 and isinstance(body, dict) and "kind" in body:
        return "proxy", body
    return "busy", None


def _writable(d):
    try:
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, ".mnemonic-check-%d" % os.getpid())
        with open(p, "w") as f:
            f.write("x")
        os.remove(p)
        return True, ""
    except OSError as e:
        return False, str(e)


def run_checks(a, raw_cfg: dict, cfg: Config, live: bool = False) -> Report:
    rep = Report()
    explicit = set(raw_cfg)
    # --- config file
    if a.config:
        names = {f.name for f in dataclasses.fields(Config)}
        unknown = sorted(k for k in raw_cfg if k not in names)
        if unknown:
            rep.add(WARN, "config", "%s: unknown fields ignored: %s" % (a.config, ", ".join(unknown)),
                    "check the spelling against the Configuration table in the README")
        else:
            rep.add(OK, "config", "%s: %d fields, all known" % (a.config, len(raw_cfg)))
    else:
        rep.add(INFO, "config", "no --config: defaults (saved-state features off)",
                "for llama-server use examples/config.llama-server.json")
    # --- python
    if cfg.kv_archive and sys.version_info < (3, 14):
        rep.add(FAIL, "python", "kv_archive needs Python >= 3.14, this is %d.%d" % sys.version_info[:2],
                'set "kv_archive": false or use Python 3.14')
    else:
        rep.add(OK, "python", "Python %d.%d" % sys.version_info[:2])
    # --- data folder
    ok, err = _writable(a.data)
    rep.add(OK if ok else FAIL, "data folder", "%s %s" % (os.path.abspath(a.data), "is writable" if ok else
                                                           "is not writable: " + err),
            "" if ok else "choose another --data folder or fix its permissions")
    # --- port
    state, body = _port_state(a.host, a.port)
    if state == "free":
        rep.add(OK, "port", "%s:%d is free" % (a.host, a.port))
    elif state == "proxy":
        rep.add(INFO, "port", "%s:%d: a mnemonic-proxy is already running there (engine %s)"
                % (a.host, a.port, (body or {}).get("kind")), "fine if it is this one; otherwise use another --port")
    else:
        rep.add(FAIL, "port", "%s:%d is taken by another program" % (a.host, a.port),
                "pick a free --port (and the same port in the client's base URL)")
    # --- upstream
    up = Upstream(a.upstream, a.api_key, timeout=10)
    st_h, h = _get(up.base + "/health", 5)
    st_m, models = _get(up.base + "/v1/models", 5)
    if st_h == 0 and st_m == 0:
        rep.add(FAIL, "engine reachable", "%s does not answer (%s)" % (a.upstream, h),
                "start the engine first (llama-server ... --port N) and pass the same URL to --upstream")
        _reminder(rep)
        return rep
    rep.add(OK, "engine reachable", "%s answers (/health %s, /v1/models %s)" % (a.upstream, st_h or "-", st_m or "-"))
    try:
        eng = engines.detect(up, cfg.engine, int(cfg.slot_id or 0))
    except ValueError as e:
        rep.add(FAIL, "engine type", str(e), 'use "engine": "auto", "strata", "llama.cpp", "ds4" or "openai"')
        return rep
    how = "detected" if eng.detected else "forced by config/--engine"
    rep.add(OK if eng.detected or cfg.engine != "auto" else WARN, "engine type",
            "%s (%s)%s" % (eng.kind, how, ("; notes: " + "; ".join(eng.notes)) if eng.notes else ""),
            "" if eng.kind != "openai" or cfg.engine != "auto" else
            "llama-server, Strata and ds4-server are recognised; anything else runs in base mode (no saved state)")
    # --- model loaded
    if st_h == 503:
        rep.add(FAIL, "model loaded", "/health answers 503: the engine is still loading the model",
                "wait until llama-server prints 'server is listening', then run check again")
    else:
        ids = [m.get("id") for m in (models.get("data") or [])] if isinstance(models, dict) else []
        if eng.kind == "strata":
            s = engines._get_json(up, "/v1/status") or {}
            loaded = bool(s.get("loaded"))
            rep.add(OK if loaded else FAIL, "model loaded", "Strata /v1/status loaded=%s" % s.get("loaded"),
                    "" if loaded else "load a model in Strata")
        elif ids or eng.model:
            rep.add(OK, "model loaded", "model: %s" % (eng.model or ", ".join(map(str, ids[:3]))))
        else:
            rep.add(WARN, "model loaded", "the engine did not list a model (/v1/models %s)" % st_m,
                    "check that the engine has a model loaded")
    # --- window vs n_ctx
    if eng.n_ctx:
        if "window" in explicit:
            if cfg.window > eng.n_ctx:
                rep.add(FAIL, "context window", "config window=%d > engine n_ctx=%d: prompts would overflow"
                        % (cfg.window, eng.n_ctx), "remove \"window\" from the config (taken from n_ctx) or start "
                        "the engine with -c %d" % cfg.window)
            else:
                rep.add(OK if cfg.window == eng.n_ctx else WARN, "context window",
                        "config window=%d, engine n_ctx=%d" % (cfg.window, eng.n_ctx),
                        "" if cfg.window == eng.n_ctx else "window smaller than n_ctx wastes context")
        elif eng.kind in ("llama.cpp", "ds4"):   # come engines.apply: finestra da n_ctx / context_length
            probe = Config.from_dict(raw_cfg)
            engines.apply(probe, eng, None, explicit, log=None)
            rep.add(OK, "context window", "window from engine n_ctx=%d; thresholds scaled: mask at %d -> %d tokens, "
                    "protected tail %d" % (eng.n_ctx, probe.mask_trigger, probe.mask_target,
                                           probe.keep_recent_tokens))
            bad = [k for k in ("notes_max_tokens", "response_floor", "tail_max", "reserve")
                   if k in explicit and getattr(probe, k) * 4 > eng.n_ctx]
            if bad:
                rep.add(WARN, "context window", "fields written for a bigger window: %s (n_ctx %d)"
                        % (", ".join("%s=%d" % (k, getattr(probe, k)) for k in bad), eng.n_ctx),
                        "remove them from the config: unset thresholds scale with n_ctx")
            if eng.n_ctx < 8192:
                rep.add(WARN, "context window", "n_ctx=%d is very small for an agent (its system prompt and tool "
                        "definitions take 2-5K tokens)" % eng.n_ctx, "start the engine with -c 8192 or more "
                        "(ds4-server: --ctx)")
        else:
            rep.add(INFO, "context window", "engine n_ctx=%d, proxy window=%d" % (eng.n_ctx, cfg.window))
    elif eng.kind == "openai":
        rep.add(WARN if "window" not in explicit else OK, "context window",
                "generic engine: n_ctx unknown, proxy window=%d" % cfg.window,
                "" if "window" in explicit else "set \"window\" in the config to the engine's real context size")
    # --- saved state: supported / configured / verified
    configured = [k for k in SAVED_STATE if getattr(cfg, k, False)]
    if eng.slot_save and configured:
        rep.add(OK, "saved state", "supported by the engine, configured in the proxy (%s)" % ", ".join(configured),
                "" if live else "run `mnemonic-proxy check --live ...` to verify a real save/restore")
    elif eng.slot_save:
        rep.add(WARN, "saved state", "supported by the engine, disabled in the proxy config",
                "use examples/config.llama-server.json (slot_save, mask_anchor, autosave) with slot_dir set")
    elif configured:
        hint = {"llama.cpp": "start llama-server with --slot-save-path DIR (same folder as slot_dir)",
                "strata": "official Strata has no session files: build the session-files branch (README, Engines), "
                          "or use examples/config.official-strata.json",
                "openai": "this engine cannot save state: drop the saved-state fields (the proxy turns them off)",
                "ds4": "ds4-server saves its own state with --kv-disk-dir: drop the saved-state fields "
                       "(use examples/config.ds4.json)"}
        rep.add(WARN, "saved state", "configured in the proxy (%s) but not supported by the engine: the proxy will "
                "turn them off" % ", ".join(configured), hint.get(eng.kind, ""))
    elif eng.kind == "ds4":
        rep.add(INFO, "saved state", "ds4-server keeps its own state (start it with --kv-disk-dir DIR); the proxy "
                "does not save or restore it")
    else:
        rep.add(INFO, "saved state", "not supported by the engine and not configured: base mode (masking, archive, "
                "recall, segments)")
    if eng.slot_save and configured:
        if not cfg.slot_dir:
            rep.add(WARN, "slot_dir", "not set: old anchors and autosaves are never cleaned up, kv_archive off",
                    "set \"slot_dir\" to the engine's --slot-save-path folder")
        elif not os.path.isdir(cfg.slot_dir):
            rep.add(FAIL, "slot_dir", "%s does not exist" % cfg.slot_dir,
                    "create it and start the engine with --slot-save-path pointing to it (same folder)")
        else:
            ok, err = _writable(cfg.slot_dir)
            rep.add(OK if ok else FAIL, "slot_dir", "%s exists%s" % (cfg.slot_dir, ", writable" if ok else
                                                                     ", NOT writable: " + err),
                    "" if live else "check --live verifies it is the engine's --slot-save-path")
    if live:
        _live(rep, up, eng, cfg)
    _reminder(rep)
    return rep


def _live(rep, up, eng, cfg):
    up.timeout = 600
    up.set_engine(eng, cfg.slot_dir)
    st = engines.normalize_status(eng, up) or {}
    if (st.get("activity") or {}).get("in_flight"):
        rep.add(FAIL, "live", "the engine slot is busy right now", "run check --live when no agent is working")
        return
    tag = "mnemonic-check-%d" % os.getpid()
    backup = None
    if eng.slot_save:
        try:
            up.slot("save", tag + "-backup.bin")
            backup = tag + "-backup.bin"
        except Exception as e:  # noqa: BLE001
            rep.add(FAIL, "live: backup", "could not save the current slot first: %s" % str(e)[:200],
                    "not touching the engine; fix saved state first")
            return
    try:
        t0 = time.time()
        r = up.chat({"model": "local-model", "max_tokens": 16, "temperature": 0,
                     "messages": [{"role": "user", "content": "Reply with the single word: ready"}]})
        txt = ((r.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
        rep.add(OK, "live: generation", "%.1f s, reply %r" % (time.time() - t0, txt.strip()[:60]))
        t0 = time.time()
        tool = {"type": "function", "function": {
            "name": "get_time", "description": "Return the current time in a city",
            "parameters": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]}}}
        r = up.chat({"model": "local-model", "max_tokens": 128, "temperature": 0, "tools": [tool],
                     "messages": [{"role": "user", "content": "What time is it in Rome? Use the tool."}]})
        calls = ((r.get("choices") or [{}])[0].get("message") or {}).get("tool_calls") or []
        good = False
        if calls:
            try:
                good = bool(json.loads(calls[0]["function"]["arguments"]).get("city"))
            except (ValueError, KeyError, TypeError, AttributeError):
                good = False
        rep.add(OK if good else WARN, "live: tool call",
                ("%.1f s, %s(%s)" % (time.time() - t0, calls[0]["function"]["name"],
                                      calls[0]["function"]["arguments"])) if calls else
                "the model answered without calling the tool", "" if good else
                "llama-server: start with --jinja (chat template with tools); try a model with tool support")
        if eng.slot_save:
            fn = tag + ".bin"
            t0 = time.time()
            s = up.slot("save", fn)
            r2 = up.slot("restore", fn)
            n_s, n_r = s.get("n_saved"), r2.get("n_restored")
            rep.add(OK if n_s and n_s == n_r else FAIL, "live: save/restore",
                    "saved %s tokens, restored %s, %.2f s" % (n_s, n_r, time.time() - t0),
                    "" if n_s and n_s == n_r else "the engine could not restore its own state file")
            p = os.path.join(cfg.slot_dir, fn) if cfg.slot_dir else ""
            if p and os.path.exists(p):
                rep.add(OK, "live: slot_dir", "the state file appeared in %s: slot_dir matches --slot-save-path"
                        % cfg.slot_dir)
                os.remove(p)
            elif cfg.slot_dir:
                rep.add(FAIL, "live: slot_dir", "the engine saved %s but it is not in %s" % (fn, cfg.slot_dir),
                        "slot_dir must be the same folder as llama-server --slot-save-path (and on the same host)")
    except Exception as e:  # noqa: BLE001
        rep.add(FAIL, "live", "request failed: %s" % str(e)[:300], "see the engine log")
    finally:
        if backup:
            try:
                up.slot("restore", backup)
                if cfg.slot_dir and os.path.exists(os.path.join(cfg.slot_dir, backup)):
                    os.remove(os.path.join(cfg.slot_dir, backup))
                rep.add(INFO, "live", "the slot content from before the check was put back")
            except Exception as e:  # noqa: BLE001
                rep.add(WARN, "live", "could not put back the previous slot content: %s" % str(e)[:200],
                        "the next request re-reads its prompt once")


def _reminder(rep):
    rep.add(INFO, "client", "turn off the client's own auto-compaction (pi: \"compaction\": {\"enabled\": false}; "
            "Claude Code: DISABLE_AUTO_COMPACT=1), otherwise two context managers fight over the same history")


def render(rep: Report, out=sys.stdout):
    enc = (getattr(out, "encoding", None) or "ascii").lower()
    try:
        "\u2705\u26a0\ufe0f\u274c\u2139\ufe0f".encode(enc)
        icons = ICON
    except (UnicodeEncodeError, LookupError):
        icons = PLAIN
    for i in rep.items:
        print("%s %-18s %s" % (icons[i["status"]], i["check"], i["evidence"]), file=out)
        if i["fix"]:
            print("   %-18s -> %s" % ("", i["fix"]), file=out)
    word = {0: "READY", 2: "READY with warnings", 1: "NOT READY"}[rep.exit_code]
    print("\n%s (exit %d)" % (word, rep.exit_code), file=out)


def main(argv=None):
    from .server import build_parser, load_config
    ap = build_parser(prog="mnemonic-proxy check",
                      description="Check the setup the proxy would run with (same options as the server). "
                                  "Exit 0 = ready, 2 = ready with warnings, 1 = not ready.")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--live", action="store_true",
                    help="also run a short generation, a tool call and a save/restore on the engine "
                         "(refused while the slot is busy; the slot content is put back)")
    a = ap.parse_args(argv)
    try:
        raw_cfg, cfg = load_config(a)
    except (OSError, ValueError) as e:
        rep = Report()
        rep.add(FAIL, "config", "%s: %s" % (a.config, e), "fix the JSON file")
    else:
        rep = run_checks(a, raw_cfg, cfg, live=a.live)
    if a.json:
        print(json.dumps({"ready": rep.exit_code != 1, "exit_code": rep.exit_code, "checks": rep.items},
                         ensure_ascii=False, indent=1))
    else:
        render(rep)
    sys.exit(rep.exit_code)


if __name__ == "__main__":
    main()
