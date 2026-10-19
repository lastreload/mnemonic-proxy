# Author: Maurizio Verde — LastReload
"""Generic engines that tell their real context size: Ollama and LM Studio (0.3.8).

Both run in base mode (engine kind `openai`, no saved state) and both cut an over-long prompt IN SILENCE, answering
HTTP 200 (measured on AI395, docs/dev-notes/ENGINE-WINDOW-038-RESULT.md): Ollama drops the oldest messages, LM Studio
drops the middle. Their default contexts are small (Ollama 4096, LM Studio 8192). The proxy therefore reads the
context the engine really runs with and never builds a prompt for a bigger window.

Recognition (in `engines.detect`, only after Strata, ds4 and llama-server were ruled out):
  Ollama     GET /api/version -> {"version": ...}; context of a loaded model: GET /api/ps -> models[].context_length
             (the runtime context, the CONTEXT column of `ollama ps`; NOT /api/show, whose `<arch>.context_length`
             is the training context, e.g. 262144 for Qwen3-4B).
  LM Studio  GET /api/v0/models -> data[] with `state`; context of a loaded model: `loaded_context_length`
             (`max_context_length` is the model's maximum, not what it was loaded with).

A model that is not loaded has no known context (Ollama: OLLAMA_CONTEXT_LENGTH / its own default; LM Studio: the
JIT load settings). Like llama-server in router mode, the context is read before EVERY request is built
(`GenericWindow.select`, one cheap GET): if the model is not loaded the proxy first loads it with a 1-token request
and reads the context again; if it still cannot be read the request is refused (HTTP 503), never sent with a guessed
window. A context below the minimum window refuses the request (HTTP 400). A config `window` bigger than the
engine context refuses the start (`engines.window_mismatch`, also a `check` failure); if the engine context shrinks
later (model reloaded with less) the window is lowered to it, with a warning: never over.

Engine truncation (`TruncWatch`, every generic engine): the window guard counts each prompt before it is sent;
when the engine reports `usage.prompt_tokens` < TRUNC_RATIO x that count the engine has cut the prompt. Event
`engine_truncated` in the journal at every occurrence, one warning line in the log per conversation. Answers are
not changed (the engine already answered); the token counter is not calibrated on a cut prompt (it would learn a
ratio of 0.2-0.5 and underestimate everything after). Measured: cut prompts 0.12-0.48 of the estimate, whole prompts
1.00-1.10.
"""
from __future__ import annotations

import json
import time

TRUNC_RATIO = 0.8           # prompt_tokens < 0.8 x the proxy's count = cut by the engine
TRUNC_MIN_TOKENS = 1024     # below this an estimate is too rough to judge
GENERIC_SLACK = 0.15        # window_estimate_slack on generic engines without --tokenizer (measured +6.5%, ~+10%)
LOAD_TIMEOUT_S = 600.0

FIX = {"ollama": "start Ollama with a bigger context: OLLAMA_CONTEXT_LENGTH=%d ollama serve (check: ollama ps)",
       "lmstudio": "load the model with a bigger context: lms load <model> --context-length %d (check: lms ps)"}
NAME = {"ollama": "Ollama", "lmstudio": "LM Studio"}


def fix_text(flavor: str | None, n: int) -> str:
    return (FIX.get(flavor or "") or "give the engine a context of at least %d tokens") % n


class EngineCtxError(Exception):
    """A request the proxy must not send (same shape as router.RouterError: status for the client + message)."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status, self.message = status, message


def _get(up, path, timeout=10.0):
    try:
        st, _, data = up.raw("GET", path, None, timeout=timeout)
        if st != 200:
            return None
        return json.loads(data)
    except Exception:  # noqa: BLE001
        return None


def detect_flavor(up) -> str | None:
    """ollama | lmstudio | None (unknown generic engine: unchanged behaviour)."""
    v = _get(up, "/api/version", 5.0)
    if isinstance(v, dict) and isinstance(v.get("version"), str):
        return "ollama"
    m = _get(up, "/api/v0/models", 5.0)
    if isinstance(m, dict) and isinstance(m.get("data"), list) and any(
            isinstance(x, dict) and "state" in x for x in m["data"]):
        return "lmstudio"
    return None


def _int(v) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def loaded_contexts(up, flavor: str) -> dict[str, int] | None:
    """{model: runtime context} of the loaded models (0 = loaded, context not reported); None = no answer."""
    if flavor == "ollama":
        ps = _get(up, "/api/ps")
        if not isinstance(ps, dict):
            return None
        out = {}
        for m in ps.get("models") or []:
            if isinstance(m, dict):
                n = _int(m.get("context_length") or m.get("num_ctx"))
                for k in {m.get("name"), m.get("model")} - {None, ""}:
                    out[str(k)] = n
        return out
    if flavor == "lmstudio":
        ms = _get(up, "/api/v0/models")
        if not isinstance(ms, dict):
            return None
        return {str(m.get("id")): _int(m.get("loaded_context_length"))
                for m in ms.get("data") or [] if isinstance(m, dict) and m.get("state") == "loaded" and m.get("id")}
    return None


def _norm(name: str) -> str:
    return name[:-7] if name.endswith(":latest") else name


def pick(ctxs: dict[str, int], name) -> tuple[str | None, int]:
    """Context for the requested model. No match: one loaded model -> that one (the engine serves it); several ->
    the smallest context (safe). -> (model, n); n 0 = unknown."""
    if not ctxs:
        return None, 0
    if name:
        for k, n in ctxs.items():
            if k == name or _norm(k) == _norm(str(name)):
                return k, n
    vals = {n for n in ctxs.values()}
    if len(set(_norm(k) for k in ctxs)) == 1 or len(vals) == 1:
        k = next(iter(ctxs))
        return k, ctxs[k]
    known = [(n, k) for k, n in ctxs.items() if n]
    if not known:
        return None, 0
    n, k = min(known)
    return k, n


def read_context(up, flavor: str, model=None) -> tuple[str | None, int]:
    ctxs = loaded_contexts(up, flavor)
    return pick(ctxs or {}, model) if ctxs else (None, 0)


class GenericWindow:
    """Window of an Ollama / LM Studio engine, settled before each request (see the module docstring)."""

    def __init__(self, up, cfg, engine, journal=None, explicit=(), log=print):
        from .engines import SCALED
        self.up, self.cfg, self.engine, self.journal, self.log = up, cfg, engine, journal, log
        self.explicit = set(explicit or ())
        # startup values (after engines.apply): every later window is computed from these
        self.base = {f: getattr(cfg, f) for f in ("window", "response_floor") + SCALED if hasattr(cfg, f)}
        self.base_ctx = int(engine.n_ctx or 0)     # context the startup values were computed for (0 = unknown)
        self.applied: int | None = int(engine.n_ctx) if engine.n_ctx else None
        self.loading: str | None = None
        self.loads = 0
        self.lowered_warned = False

    def _load(self, model) -> None:
        """The model is not loaded: a 1-token request loads it (Ollama and LM Studio both load on demand)."""
        body = {"model": model or "local-model", "max_tokens": 1, "temperature": 0, "stream": False,
                "messages": [{"role": "user", "content": "hi"}]}
        t0 = time.time()
        timeout = float(getattr(self.cfg, "router_load_timeout_s", LOAD_TIMEOUT_S) or LOAD_TIMEOUT_S)
        try:
            st, _, data = self.up.raw("POST", "/v1/chat/completions", json.dumps(body).encode("utf-8"),
                                      timeout=timeout)
        except Exception as e:  # noqa: BLE001
            st, data = 0, repr(e)[:200].encode()
        self.loads += 1
        if self.journal is not None:
            self.journal.log("engine_load", flavor=self.engine.flavor, model=model, status=st,
                             ms=round((time.time() - t0) * 1000))
        if st != 200:
            raise EngineCtxError(503, "%s: model %r is not loaded and could not be loaded (HTTP %s: %s); the request "
                                      "was not sent. %s" % (NAME.get(self.engine.flavor), model, st,
                                                            data[:200].decode("utf-8", "replace"),
                                                            fix_text(self.engine.flavor, 32768)))

    def select(self, req: dict):
        """Before the prompt is built: real context of the requested model -> window. Raises EngineCtxError."""
        fl, model = self.engine.flavor, req.get("model")
        mid, n = read_context(self.up, fl, model)
        if not n:
            self.loading = model
            try:
                self._load(model)
            finally:
                self.loading = None
            mid, n = read_context(self.up, fl, model)
        if not n:
            raise EngineCtxError(503, "%s: the context size of model %r cannot be read (%s); the request was not "
                                      "sent" % (NAME.get(fl), model, "GET /api/ps" if fl == "ollama" else
                                                "GET /api/v0/models"))
        self.apply(mid, n)
        return mid

    def apply(self, mid, n: int):
        from .engines import MIN_WINDOW, RECOMMENDED_WINDOW, SCALED, SCALED_MIN, small_window_allowed
        fl = self.engine.flavor
        if n < MIN_WINDOW and not small_window_allowed():
            raise EngineCtxError(400, "%s: model %r runs with a context of %d tokens: the minimum is %d, recommended "
                                      "%d or more (%s); the request was not sent"
                                 % (NAME.get(fl), mid, n, MIN_WINDOW, RECOMMENDED_WINDOW,
                                    fix_text(fl, RECOMMENDED_WINDOW)))
        if self.applied == n and self.engine.model == mid:
            return
        cfg, base = self.cfg, self.base
        bw = int(base.get("window") or 0)
        # config window = upper bound (lowered to the engine context, never above it); otherwise the engine context
        w = min(bw, n) if "window" in self.explicit and bw else n
        ratio = w / float(bw or n)
        scaled = {}
        for f in SCALED:
            if f in self.explicit or f not in base:
                continue
            v = base[f]
            nv = v
            if isinstance(v, int) and v > 0 and ratio != 1:
                nv = max(64, SCALED_MIN.get(f, 0) if ratio < 1 else 0, int(v * ratio))     # as engines.apply
                nv = min(nv, v) if ratio < 1 else nv
            setattr(cfg, f, nv)
            if nv != v:
                scaled[f] = nv
        lowered = "window" in self.explicit and bw > n
        cfg.window = w
        if "response_floor" in self.explicit and "response_floor" in base:
            cfg.response_floor = base["response_floor"]
        floor = int(getattr(cfg, "response_floor", 0) or 0)
        if floor and w and floor > w // 4:
            cfg.response_floor = w // 4
            scaled["response_floor"] = cfg.response_floor
        was = self.applied
        self.applied = n
        self.engine.n_ctx, self.engine.model = n, mid
        if self.journal is not None:
            self.journal.log("engine_window", flavor=fl, model=mid, n_ctx=n, window=w, was=was, scaled=scaled,
                             lowered=lowered or None)
        if lowered and self.log and not self.lowered_warned:
            self.lowered_warned = True
            self.log("[mnemonic-proxy] warning: %s runs %s with a context of %d tokens, smaller than the config "
                     "window %d: window lowered to %d (%s)" % (NAME.get(fl), mid, n, bw, n, fix_text(fl, bw)))

    def summary(self) -> dict:
        return {"flavor": self.engine.flavor, "n_ctx": self.applied, "loads": self.loads}


class TruncWatch:
    """Detects prompts cut by a generic engine (see the module docstring). Signal only."""

    def __init__(self, cfg, engine, journal=None, log=print):
        self.cfg, self.engine, self.journal, self.log = cfg, engine, journal, log
        self.warned: set = set()
        self.count = 0

    @staticmethod
    def cut(est, real) -> bool:
        try:
            est, real = int(est or 0), int(real or 0)
        except (TypeError, ValueError):
            return False
        return est >= TRUNC_MIN_TOKENS and real > 0 and real < TRUNC_RATIO * est

    def observe(self, est, usage, where: str = "", ctx=None) -> bool:
        real = (usage or {}).get("prompt_tokens") if isinstance(usage, dict) else None
        if not self.cut(est, real):
            return False
        self.count += 1
        conv, seg = (ctx or (None, None))[:2] if isinstance(ctx, (tuple, list)) else (None, None)
        fl = getattr(self.engine, "flavor", None)
        if self.journal is not None:
            self.journal.log("engine_truncated", conv=conv, seg=seg, where=where or "call", est_tokens=int(est),
                             prompt_tokens=int(real), ratio=round(int(real) / float(est), 3),
                             window=int(getattr(self.cfg, "window", 0) or 0), n_ctx=self.engine.n_ctx, flavor=fl)
        key = conv or "-"
        if key not in self.warned and self.log:
            self.warned.add(key)
            self.log("[mnemonic-proxy] warning: the engine cut the prompt in silence: the proxy counted %d tokens, "
                     "the engine read %d (conversation %s). Its context is smaller than the proxy window %d or the "
                     "estimate is too low: %s, or run the proxy with --tokenizer"
                     % (int(est), int(real), conv or "-", int(getattr(self.cfg, "window", 0) or 0),
                        fix_text(fl, int(getattr(self.cfg, "window", 0) or 32768))))
        return True
