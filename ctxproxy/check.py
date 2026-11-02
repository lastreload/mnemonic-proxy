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
from . import initconfig
from .core import Config
from .enginectx import NAME, loaded_contexts
from .savedstate import state_available
from .upstream import Upstream

OK, WARN, FAIL, INFO = "ok", "warn", "fail", "info"
ICON = {OK: "\u2705", WARN: "\u26a0\ufe0f ", FAIL: "\u274c", INFO: "\u2139\ufe0f "}
PLAIN = {OK: "[ OK ]", WARN: "[WARN]", FAIL: "[FAIL]", INFO: "[INFO]"}
# autorestore defaults to true but is inert without one of the save-producing
# features; keep it out of the configured/verified save decision.
SAVED_STATE = engines.SAVE_PRODUCING


class Report:
    def __init__(self):
        self.items: list[dict] = []
        self.auto: dict | None = None      # 0.3.23: automatic configuration (machine, decisions, report lines)

    def add(self, status, name, evidence, fix=""):
        item = {"status": status, "check": name, "evidence": evidence, "fix": fix}
        self.items.append(item)
        return item          # the caller may amend it later (the config hint waits for the detected engine)

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
        with open(p, "w", encoding="utf-8") as f:
            f.write("x")
        os.remove(p)
        return True, ""
    except OSError as e:
        return False, str(e)


def config_error_evidence(name, exc) -> str:
    """The `config` line's evidence when the file could not be read (friction A1)."""
    if isinstance(exc, FileNotFoundError):
        return "%s does not exist (the proxy does not create it)" % name
    return "%s cannot be read: %s" % (name, exc)


def config_error_fix(name, exc) -> str:
    """...and its fix: what the user can really do, never \"fix the JSON file\" about a file that is absent."""
    if isinstance(exc, FileNotFoundError):
        return initconfig.missing_config_fix(name)
    return "fix the JSON file, or drop --config: the proxy works with the defaults (saved-state features off)"


def run_checks(a, raw_cfg: dict, cfg: Config, live: bool = False, config_error=None) -> Report:
    rep = Report()
    explicit = set(raw_cfg)
    # --- config file
    # 0.3.23 (friction A1): a --config that cannot be read is one line with the three real options, and the
    # report goes on: the engine is what the user came here to check. `config_hint` is amended once the engine
    # is known (friction A9: no llama-server advice in front of Strata).
    if config_error is not None:
        rep.add(FAIL, "config", "%s" % config_error_evidence(a.config, config_error),
                config_error_fix(a.config, config_error))
        config_hint = None
    elif a.config:
        names = {f.name for f in dataclasses.fields(Config)}
        unknown = sorted(k for k in raw_cfg if k not in names)
        if unknown:
            rep.add(WARN, "config", "%s: unknown fields ignored: %s" % (a.config, ", ".join(unknown)),
                    "check the spelling against the Configuration table in the README")
        else:
            rep.add(OK, "config", "%s: %d fields, all known" % (a.config, len(raw_cfg)))
        config_hint = None
    else:
        config_hint = rep.add(INFO, "config", "no --config: defaults (saved-state features off)",
                              initconfig.engine_hint(None))
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
    if config_hint is not None:
        # friction A9: now that the engine is known, the configuration advice is about THIS engine
        config_hint["fix"] = initconfig.engine_hint(eng.kind)
    rep.add(OK if eng.detected or cfg.engine != "auto" else WARN, "engine type",
            "%s (%s)%s" % (eng.kind, how, ("; notes: " + "; ".join(eng.notes)) if eng.notes else ""),
            "" if eng.kind != "openai" or cfg.engine != "auto" else
            "llama-server, Strata and ds4-server are recognised (Ollama and LM Studio: base mode with the window read "
            "from the engine); anything else runs in base mode (no saved state)")
    # --- model loaded
    if st_h == 503:
        rep.add(FAIL, "model loaded", "/health answers 503: the engine is still loading the model",
                "wait until llama-server prints 'server is listening', then run check again")
    else:
        ids = [m.get("id") for m in (models.get("data") or []) if isinstance(m, dict) and m.get("id")] \
            if isinstance(models, dict) and isinstance(models.get("data"), list) else []
        if eng.kind == "strata":
            s = engines._get_json(up, "/v1/status") or {}
            loaded = bool(s.get("loaded"))
            rep.add(OK if loaded else FAIL, "model loaded", "Strata /v1/status loaded=%s" % s.get("loaded"),
                    "" if loaded else "load a model in Strata")
        elif getattr(eng, "router", False):
            rep.add(OK if ids else WARN, "model loaded", "llama-server router mode: models loaded on demand (%s)"
                    % (", ".join(map(str, ids[:8])) or "none listed"), "" if ids else "add models to the preset")
        elif eng.flavor:
            ctxs = loaded_contexts(up, eng.flavor)
            endpoint = "/api/ps" if eng.flavor == "ollama" else "/api/v0/models"
            evidence = ("runtime information unavailable or malformed; loaded state unknown" if ctxs is None else
                        "no model loaded" if not ctxs else "loaded: " + ", ".join(ctxs))
            rep.add(OK if ctxs else WARN, "model loaded", "%s %s: %s" % (NAME[eng.flavor], endpoint, evidence),
                    "" if ctxs else "check the engine's runtime model status and run check again")
            if not eng.model and ids:
                eng.model = ids[0]        # --live target only; catalog is not loaded-state evidence
        elif eng.kind == "openai":
            rep.add(WARN, "model availability", "/v1/models catalog: %s; loaded state unverified"
                    % (", ".join(map(str, ids[:8])) or "none listed"),
                    "verify model availability and per-model runtime context in the upstream gateway; "
                    "a catalog does not prove a model is loaded")
            if ids:
                eng.model = ids[0]        # --live target only, not proof of loading
        elif ids or eng.model:
            rep.add(OK, "model loaded", "model: %s" % (eng.model or ", ".join(map(str, ids[:3]))))
        else:
            rep.add(WARN, "model loaded", "the engine did not list a model (/v1/models %s)" % st_m,
                    "check that the engine has a model loaded")
    # --- window vs n_ctx
    flavor = getattr(eng, "flavor", None)
    if eng.n_ctx:
        if "window" in explicit and flavor:
            if cfg.window <= eng.n_ctx:          # bigger: engines.window_mismatch below (failure)
                rep.add(OK if cfg.window == eng.n_ctx else WARN, "context window",
                        "config window=%d, %s context=%d (%s)" % (cfg.window, NAME[flavor], eng.n_ctx, eng.model),
                        "" if cfg.window == eng.n_ctx else "window smaller than the engine context wastes context")
        elif "window" in explicit:
            if cfg.window > eng.n_ctx:
                rep.add(FAIL, "context window", "config window=%d > engine n_ctx=%d: prompts would overflow"
                        % (cfg.window, eng.n_ctx), "remove \"window\" from the config (taken from n_ctx) or start "
                        "the engine with -c %d" % cfg.window)
            else:
                rep.add(OK if cfg.window == eng.n_ctx else WARN, "context window",
                        "config window=%d, engine n_ctx=%d" % (cfg.window, eng.n_ctx),
                        "" if cfg.window == eng.n_ctx else "window smaller than n_ctx wastes context")
        elif eng.kind in ("llama.cpp", "ds4", "strata") or flavor:   # same discovery as engines.apply
            probe = Config.from_dict(raw_cfg)
            engines.apply(probe, eng, None, explicit, log=None)
            rep.add(OK, "context window", "window from engine n_ctx=%d%s; thresholds scaled: mask at %d -> %d tokens, "
                    "protected tail %d" % (eng.n_ctx, " (%s %s, read again before every request)"
                                           % (NAME[flavor], eng.model) if flavor else "",
                                           probe.mask_trigger, probe.mask_target, probe.keep_recent_tokens))
            bad = [k for k in ("notes_max_tokens", "response_floor", "tail_max", "reserve")
                   if k in explicit and getattr(probe, k) * 4 > eng.n_ctx]
            if bad:
                rep.add(WARN, "context window", "fields written for a bigger window: %s (n_ctx %d)"
                        % (", ".join("%s=%d" % (k, getattr(probe, k)) for k in bad), eng.n_ctx),
                        "remove them from the config: unset thresholds scale with n_ctx")
        else:
            rep.add(INFO, "context window", "engine n_ctx=%d, proxy window=%d" % (eng.n_ctx, cfg.window))
    elif getattr(eng, "router", False):
        from .router import list_models
        ms = list_models(up) or []
        desc = ", ".join("%s %s%s" % (m["id"], (m.get("status") or {}).get("value"),
                                      " n_ctx=%s" % (m.get("meta") or {}).get("n_ctx") if (m.get("meta") or {}).get(
                                          "n_ctx") else "") for m in ms[:8])
        rep.add(INFO, "context window", "llama-server router mode: the window is read from the n_ctx of the requested "
                "model at every request, before the prompt is built (%s)%s" % (
                    desc or "no model listed", "; config window=%d is used as an upper bound" % cfg.window
                    if "window" in explicit else ""),
                "give each model a ctx-size of at least %d in the preset" % engines.MIN_WINDOW)
    elif flavor:
        rep.add(WARN, "context window", "%s: runtime context unknown (model unloaded, metadata unavailable or "
                "context not reported); the proxy reads it before the first request (and loads the model if needed); "
                "proxy window=%d is not a verified runtime context and is an upper bound only if written in the config"
                % (NAME[flavor], cfg.window),
                "check the loaded model's runtime context and run check again")
    elif eng.kind == "openai":
        rep.add(WARN, "context window",
                "generic engine: runtime context unverified; proxy window=%d (%s, not a measured engine window)"
                % (cfg.window, "configured" if "window" in explicit else "default"),
                "verify the runtime context of every routed model and set \"window\" no higher than the smallest; "
                "the proxy cannot discover per-model contexts from this gateway")
    # Report the actual startup adaptations, including explicit small-window budgets.
    probe = Config.from_dict(raw_cfg)
    for warning in engines.apply(probe, eng, None, explicit, log=None):
        if "too large for window" in warning:
            rep.add(WARN, "context budgets", warning,
                    "remove oversized overrides to use scaled defaults, or use the lowered values shown")
    # --- minimum window (0.3.0): the server refuses to start below it, so this is a failure, not a warning
    for evidence, fix in engines.window_problems(cfg, eng, explicit):
        rep.add(FAIL, "context window", evidence + " (the proxy refuses to start)", fix)
    # --- 0.3.8 Ollama / LM Studio: config window bigger than the engine context (they cut in silence)
    for evidence, fix in engines.window_mismatch(cfg, eng, explicit):
        rep.add(FAIL, "context window", evidence + " (the proxy refuses to start)", fix)
    # --- 0.3.8 generic engines: without a tokenizer the window guard only estimates
    if eng.kind == "openai" and not getattr(a, "tokenizer", None):
        sl = engines.generic_slack(cfg, eng, explicit)
        rep.add(WARN, "token count", "no tokenizer: the window guard is estimated (chars/%.1f%s)"
                % (cfg.chars_per_token, ", slack %.2f" % sl if sl is not None else
                   ", slack %.2f from the config" % cfg.window_estimate_slack),
                "use --tokenizer <tokenizer.json of the model> for an exact count")
    # --- 0.3.23 automatic configuration: the engine/machine-dependent options the proxy chooses by itself.
    # The decisions are applied to `cfg` so that every check below reports the configuration the server would
    # really run with, and are printed with their reason (`--explain-config` adds what was NOT changed).
    from . import autoconfig
    auto_probe = Config.from_dict(raw_cfg)
    engines.apply(auto_probe, eng, None, explicit, log=None)
    machine = autoconfig.Machine.read(eng)
    decisions = autoconfig.decide(auto_probe, eng, explicit, machine, exact=bool(getattr(a, "tokenizer", None)),
                                  existing=autoconfig.existing_install(getattr(a, "data", "") or ""))
    for d in decisions:
        if d.changed:
            setattr(cfg, d.field, d.value)
    rep.auto = {"machine": machine.summary(), "decisions": [d.summary() for d in decisions],
                "lines": autoconfig.explain(auto_probe, eng, machine, decisions, full=True)}
    picked = [d for d in decisions if d.changed]
    kept_existing = [d for d in decisions if d.kept_existing]
    if not cfg.auto_config:
        rep.add(INFO, "auto config", "off (\"auto_config\": false): every default is used as written",
                "remove \"auto_config\" from the config to let the proxy choose the engine/machine-dependent "
                "options")
    elif picked:
        rep.add(INFO, "auto config", "chosen for this engine and this machine: %s (%s)"
                % (", ".join("%s=%s" % (d.field, autoconfig.fmt(d.value)) for d in picked), machine.line()),
                "`mnemonic-proxy check --explain-config ...` prints every reason; a field written in the config "
                "always wins")
    elif kept_existing:
        # 0.3.23 (friction A8): one line here, the four identical reasons in the extended block only
        rep.add(INFO, "auto config", "%d choice%s not applied: this data folder already has conversations, so "
                "an upgrade keeps how they ran" % (len(kept_existing), "" if len(kept_existing) == 1 else "s"),
                "`mnemonic-proxy check --explain-config ...` prints each choice, its value and its reason; "
                "write \"auto_config\": true in the config to apply them")
    else:
        why = list(dict.fromkeys(d.why for d in decisions))
        rep.add(INFO, "auto config", "nothing to choose: %s (%s)"
                % ("; ".join(why) or "the config already decides every engine/machine-dependent option",
                   machine.line()),
                "`mnemonic-proxy check --explain-config ...` prints every reason")
    # --- saved state: supported / configured / verified
    configured = [k for k in SAVED_STATE if getattr(cfg, k, False)]
    if eng.slot_save and configured:
        rep.add(OK, "saved state", "supported by the engine, configured in the proxy (%s)" % ", ".join(configured),
                "" if live else "run `mnemonic-proxy check --live ...` to verify a real save/restore")
    elif eng.slot_save:
        rep.add(WARN, "saved state", "supported by the engine, not enabled in the proxy config: the proxy works "
                "without it (%s)" % engines.BASE_MODE, engines.saved_state_later(eng.kind))
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
        rep.add(INFO, "saved state", "not supported by the engine and not configured: the proxy works without it "
                "(%s)" % engines.BASE_MODE, engines.saved_state_later(eng.kind))
    if eng.slot_save and configured:
        if not cfg.slot_dir:
            rep.add(WARN, "slot_dir", "not set: old anchors and autosaves are never cleaned up, kv_archive off",
                    "set \"slot_dir\" to the engine's --slot-save-path folder")
        else:
            # 0.3.22: the proxy creates the session folder itself (nobody should have to mkdir it by hand),
            # but only when the engine is on this host: otherwise the local path is not --slot-save-path
            # and an empty folder would hide the mismatch.
            outcome, detail = engines.ensure_slot_dir(cfg.slot_dir, getattr(a, "upstream", "") or "")
            if outcome == "remote":
                rep.add(FAIL, "slot_dir", "%s does not exist and the engine is not on this host (%s): the proxy "
                        "did not create it" % (cfg.slot_dir, a.upstream),
                        "slot_dir must be the engine's --slot-save-path as seen from this host (a shared mount); "
                        + engines.mkdir_hint(cfg.slot_dir))
            elif outcome == "failed":
                rep.add(FAIL, "slot_dir", "%s does not exist and the proxy could not create it: %s"
                        % (cfg.slot_dir, detail),
                        engines.mkdir_hint(cfg.slot_dir) + ", or point slot_dir at a writable folder "
                        "(the engine's --slot-save-path)")
            else:
                ok, err = _writable(cfg.slot_dir)
                made = " (created by the proxy)" if outcome == "created" else ""
                rep.add(OK if ok else FAIL, "slot_dir", "%s exists%s%s" % (
                    cfg.slot_dir, made, ", writable" if ok else ", NOT writable: " + err),
                    "it must be the engine's --slot-save-path folder" if live else
                    "it must be the engine's --slot-save-path folder; check --live verifies that")
    if live:
        _live(rep, up, eng, cfg)
        if eng.kind == "openai" and not any(
                i["check"].startswith("live") and i["status"] == FAIL for i in rep.items):
            _live_full_window(rep, up, eng, cfg, explicit)
    _reminder(rep)
    return rep


FILL_LINE = "Record %05d: the value %05d was checked by the auditor and it is correct.\n"


def _live_full_window(rep, up, eng, cfg, explicit):
    """0.3.8, generic engines: a prompt of ~0.9 x window must reach the engine whole (Ollama and LM Studio answer 200
    after cutting it in silence). The size is calibrated on the engine itself: two short prompts give the tokens per
    line of the filler text and the chat-template overhead, so no tokenizer is needed."""
    w = int(cfg.window if "window" in explicit or not eng.n_ctx else eng.n_ctx)
    model = (eng.model or "local-model") if eng.kind == "openai" else "local-model"

    def ask(n):
        text = "".join(FILL_LINE % (i, (i * 7919) % 100000) for i in range(n))
        r = up.chat({"model": model, "max_tokens": 1, "temperature": 0, "stream": False,
                     "messages": [{"role": "user", "content": text + "Reply with the single word: ok"}]})
        return int((r.get("usage") or {}).get("prompt_tokens") or 0)

    t0 = time.time()
    try:
        a, b = ask(20), ask(80)
        if not a or b <= a:
            rep.add(WARN, "live: full window", "the engine does not report usage.prompt_tokens: cut prompts cannot "
                    "be detected", "")
            return
        per = (b - a) / 60.0
        over = max(0.0, a - 20 * per)
        n = int((0.9 * w - over) / per)
        want = int(over + n * per)
        got = ask(n)
    except Exception as e:  # noqa: BLE001
        msg = str(e)[:300]
        rep.add(FAIL, "live: full window", "engine context smaller than window: a prompt of ~%d tokens (0.9 x window "
                "%d) was refused: %s" % (int(0.9 * w), w, msg), _fill_fix(eng, w))
        return
    if got < 0.8 * want:
        rep.add(FAIL, "live: full window", "engine context smaller than window: sent ~%d tokens (0.9 x window %d), "
                "the engine read %d (it cut the prompt in silence), %.0f s" % (want, w, got, time.time() - t0),
                _fill_fix(eng, w))
    else:
        rep.add(OK, "live: full window", "sent ~%d tokens (0.9 x window %d), the engine read %d: whole, %.0f s"
                % (want, w, got, time.time() - t0))


def _fill_fix(eng, w):
    from .enginectx import fix_text
    return "remove \"window\" from the config or set it to the engine's real context; " + fix_text(
        getattr(eng, "flavor", None), w)


def _live(rep, up, eng, cfg):
    if getattr(eng, "router", False):
        rep.add(WARN, "live", "llama-server router mode: --live (real save/restore) is not implemented for the router "
                "yet", "check the saved state with a real request: the journal shows save/restore per model")
        return
    up.timeout = 600
    up.set_engine(eng, cfg.slot_dir)
    verify_slots = bool(eng.slot_save and any(getattr(cfg, key, False) for key in SAVED_STATE))
    all_off = not any(getattr(cfg, key, False) for key in engines.NEEDS_SLOTS)
    if eng.slot_save and not verify_slots:
        reason = ("all six saved-state options are false" if all_off else
                  "no save-producing saved-state option is enabled (autorestore alone is inert)")
        rep.add(INFO, "live: save/restore", "skipped: %s; no SAVE or RESTORE request was sent" % reason,
                "enable at least one save-producing saved-state option to verify session files")
    st = engines.normalize_status(eng, up) or {}
    if (st.get("activity") or {}).get("in_flight"):
        rep.add(FAIL, "live", "the engine slot is busy right now", "run check --live when no agent is working")
        return
    tag = "mnemonic-check-%d" % os.getpid()
    backup = None
    if verify_slots:
        if eng.kind == "strata":
            # New Strata exposes /slots; older versions may not.  Only "is_processing" is trusted here:
            # Strata 0.2.x reports n_prompt_tokens 0 right after a restore although the slot holds the
            # session (a save then succeeds), so an empty slot is recognised only by the engine refusing the save.
            slots = engines._get_json(up, "/slots")
            mine = next((s for s in slots if isinstance(s, dict) and s.get("id") == eng.slot_id), {}) \
                if isinstance(slots, list) else {}
            if mine.get("is_processing"):
                rep.add(FAIL, "live", "the engine slot is busy right now", "run check --live when no agent is working")
                return
        empty = False
        try:
            up.slot("save", tag + "-backup.bin")
            backup = tag + "-backup.bin"
        except Exception as e:  # noqa: BLE001
            if "no complete session" in str(e).lower():
                empty = True
            else:
                rep.add(FAIL, "live: backup", "could not save the current slot first: %s" % str(e)[:200],
                        "not touching the engine; fix saved state first")
                return
        if empty:
            rep.add(INFO, "live: backup", "the slot is empty (no complete session): skipping backup and final restore")
    try:
        lm = (eng.model or "local-model") if eng.kind == "openai" else "local-model"   # Ollama: real name
        t0 = time.time()
        r = up.chat({"model": lm, "max_tokens": 16, "temperature": 0,
                     **({"chat_template_kwargs": {"enable_thinking": False}} if eng.kind == "strata" else {}),
                     "messages": [{"role": "user", "content": "Reply with the single word: ready"}]})
        txt = ((r.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
        rep.add(OK, "live: generation", "%.1f s, reply %r" % (time.time() - t0, txt.strip()[:60]))
        t0 = time.time()
        tool = {"type": "function", "function": {
            "name": "get_time", "description": "Return the current time in a city",
            "parameters": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]}}}
        r = up.chat({"model": lm, "max_tokens": 128, "temperature": 0, "tools": [tool],
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
        if verify_slots:
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
        if backup and not state_available(up):
            rep.add(INFO, "live", "the previous slot content was not restored: saved state was suspended after "
                    "an unsupported session reply; no further RESTORE was attempted")
        elif backup:
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


def render(rep: Report, out=None, explain: bool = False):
    out = sys.stdout if out is None else out       # resolved at call time: `check` is testable under redirect
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
    if explain and rep.auto:
        print("\nautomatic configuration (a field written in the config always wins):", file=out)
        for line in rep.auto["lines"]:
            print("  " + line, file=out)
    word = {0: "READY", 2: "READY with warnings", 1: "NOT READY"}[rep.exit_code]
    print("\n%s (exit %d)" % (word, rep.exit_code), file=out)


def build_check_parser():
    """The `check` parser: the server's options, plus the two of its own."""
    from .server import build_parser
    ap = build_parser(prog="mnemonic-proxy check",
                      description="Check the setup the proxy would run with (same options as the server). "
                                  "Exit 0 = ready, 2 = ready with warnings, 1 = not ready.")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--live", action="store_true",
                    help="also run a short generation, a tool call and a save/restore on the engine "
                         "(refused while the slot is busy; the slot content is put back)")
    ap.add_argument("--explain-config", action="store_true",
                    help="print the automatic configuration in full: what was read about the engine and the "
                         "machine, every option chosen (and every one left alone) with its reason and how to "
                         "force it otherwise. The proxy is not started.")
    return ap


def main(argv=None):
    from .server import load_config
    ap = build_check_parser()
    a = ap.parse_args(argv)
    config_error = None
    try:
        raw_cfg, cfg = load_config(a)
    except (OSError, ValueError) as e:
        # 0.3.23 (friction A1): the report does not stop at a config file that is missing or broken. The
        # remaining checks run against the defaults — exactly the configuration the user is told to fall back
        # to — so one `check` still says everything there is to say about this machine and this engine.
        config_error = e
        raw_cfg, cfg = {}, Config.from_dict({})
    rep = run_checks(a, raw_cfg, cfg, live=a.live, config_error=config_error)
    if a.json:
        out = {"ready": rep.exit_code != 1, "exit_code": rep.exit_code, "checks": rep.items}
        if rep.auto is not None:
            out["auto_config"] = rep.auto
        print(json.dumps(out, ensure_ascii=False, indent=1))
    else:
        render(rep, explain=bool(getattr(a, "explain_config", False)))
    sys.exit(rep.exit_code)


if __name__ == "__main__":
    main()
