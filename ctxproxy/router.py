# Author: Maurizio Verde — LastReload
"""llama-server in router mode (issue #12, 0.3.7): several models behind one server, loaded and unloaded on demand.

The router (llama-server started without `-m`) answers `GET /props` for itself (`role: router`, n_ctx 0) and wants
the model on every per-model call: `"model"` in POST bodies (chat, /tokenize, /slots/{id}?action=...), `?model=` on
GET (/slots, /props). `GET /models` lists the models with `status.value` (unloaded | loading | loaded | sleeping;
`failed` after a load that died) and, when loaded, `meta.n_ctx`. Behaviour verified on llama.cpp b11723
(docs/dev-notes/ROUTER-MODE-RESULT.md):

- the window is the one of the slot the model really got (`GET /slots?model=` -> n_ctx): the `-c` of the preset is
  capped to the model's training context and `context_length` in /models can be stale, so neither is trusted;
- the engine restores a slot file saved by another model without complaining: the proxy has to refuse it;
- `?autoload=false` works on POST too (`model is not loaded` 400 instead of loading the model).

What the proxy does (one request at a time, under the Proxy lock):

1. `select(req)` before the prompt is built: the model of the request (or `router_model` from the config when the
   client's name is not served) -> `GET /models` -> if it is not loaded, or its n_ctx for this load is not known
   yet, `GET /slots?model=...&autoload=true` (loads it, waits up to `router_load_timeout_s`) -> slot n_ctx. If the
   n_ctx cannot be read the request is refused with a clear error: never sent with the config window.
2. a different model or n_ctx -> window = n_ctx (or the config `window` if it is smaller) and the default thresholds
   scaled from the startup values, exactly as `engines.apply` does for a single-model llama-server.
3. every observation of a model that is not loaded bumps its load generation: the engine status the Tracker sees is
   `started = "<model>#<generation>"`, so a model switch or an unload/reload drops the "warm cache" state.
4. saved files are bound to the model that saved them (`<data>/router-slot-models.json`): a restore with another
   model, or of a file whose model is unknown, is refused (409) before it reaches the engine.
5. token counts (`/tokenize` with the model) and their cache are per model.
"""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.parse

OWNERS_FILE = "router-slot-models.json"
LOAD_TIMEOUT_S = 600.0


class RouterError(Exception):
    """A request the proxy must not send: status for the client + message."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status, self.message = status, message


def q(model: str) -> str:
    return urllib.parse.quote(str(model), safe="")


def _json(up, method, path, body=None, timeout=None):
    try:
        if timeout is None:
            st, _, data = up.raw(method, path, body)
        else:
            st, _, data = up.raw(method, path, body, timeout=timeout)
    except Exception as e:  # noqa: BLE001
        return 0, {"error": {"message": repr(e)[:200]}}
    try:
        return st, json.loads(data) if data else None
    except ValueError:
        return st, {"raw": data[:200].decode("utf-8", "replace")}


def _err_msg(obj) -> str:
    if isinstance(obj, dict):
        e = obj.get("error")
        if isinstance(e, dict):
            return str(e.get("message") or e)[:300]
        if e:
            return str(e)[:300]
    return str(obj)[:300]


def is_router_props(props) -> bool:
    """`GET /props` of a router: `role: router` (b11723); older builds: no model and n_ctx 0."""
    if not isinstance(props, dict):
        return False
    if props.get("role") == "router":
        return True
    dgs = props.get("default_generation_settings") or {}
    return str(props.get("model_path") or "") == "none" and not (isinstance(dgs, dict) and dgs.get("n_ctx"))


def list_models(up) -> list[dict] | None:
    """`GET /models` -> model entries with a `status` object (router), None if this is not a router."""
    st, obj = _json(up, "GET", "/models")
    if st != 200 or not isinstance(obj, dict) or not isinstance(obj.get("data"), list):
        return None
    out = [m for m in obj["data"] if isinstance(m, dict) and m.get("id") and isinstance(m.get("status"), dict)]
    return out if out else None


def _args(m: dict) -> list:
    a = (m.get("status") or {}).get("args")
    return [str(x) for x in a] if isinstance(a, list) else []


def detect_router(up, slot_id: int = 0, detected: bool = True):
    """Engine for a router (kind llama.cpp, router=True). Saved state only if every model is started with
    --slot-save-path (read from the instance arguments in /models: nothing is loaded to find out)."""
    from .engines import Engine
    models = list_models(up) or []
    e = Engine("llama.cpp", slot_save=False, slot_id=slot_id, status_kind="llama.cpp", tokenize=True,
               detected=detected, router=True)
    ids = [m["id"] for m in models]
    with_slots = [m["id"] for m in models if "--slot-save-path" in _args(m)]
    e.slot_save = bool(models) and len(with_slots) == len(models)
    e.notes.append("llama-server router mode: %d models (%s); window read from each model's n_ctx at every request"
                   % (len(ids), ", ".join(ids[:8]) + (", ..." if len(ids) > 8 else "")))
    if not models:
        e.notes.append("GET /models lists no model")
    elif not e.slot_save:
        e.notes.append("saved state off: not every model has --slot-save-path (%d of %d)"
                       % (len(with_slots), len(models)))
    par = []
    for m in models:
        a = _args(m)
        if "--parallel" in a and a[a.index("--parallel") + 1:a.index("--parallel") + 2] not in (["1"], []):
            par.append(m["id"])
    if par:
        e.notes.append("models with --parallel > 1 (%s): the proxy pins id_slot=%d" % (", ".join(par), slot_id))
    return e


class SlotOwners:
    """file name -> model that saved it. Persisted in the data folder (survives a proxy restart); in memory only
    when there is no data folder."""

    def __init__(self, path: str = ""):
        self.path = path
        self.mu = threading.Lock()
        self.map: dict[str, str] = {}
        if path:
            try:
                with open(path, encoding="utf-8") as f:
                    d = json.load(f)
                self.map = {str(k): str((v or {}).get("model") if isinstance(v, dict) else v)
                            for k, v in (d or {}).items()}
            except (OSError, ValueError, AttributeError):
                self.map = {}

    def get(self, filename: str) -> str | None:
        with self.mu:
            return self.map.get(filename)

    def record(self, filename: str, model: str):
        with self.mu:
            self.map[filename] = model
            if not self.path:
                return
            tmp = self.path + ".tmp"
            try:
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump({k: {"model": v} for k, v in self.map.items()}, f, ensure_ascii=False, indent=0)
                os.replace(tmp, self.path)
            except OSError:
                pass


class Router:
    """Per-model state of a llama-server router (see the module docstring)."""

    def __init__(self, up, cfg, engine, journal=None, counter=None, explicit=(), owners_path: str = ""):
        from .engines import SCALED
        self.up, self.cfg, self.engine, self.journal, self.counter = up, cfg, engine, journal, counter
        self.explicit = set(explicit or ())
        # startup values (after engines.apply): every model's window and thresholds are computed from these
        self.base = {f: getattr(cfg, f) for f in ("window", "response_floor") + SCALED if hasattr(cfg, f)}
        self.current: str | None = None          # model whose window is applied
        self.applied: tuple | None = None        # (model, n_ctx) applied
        self.gen: dict[str, int] = {}            # model -> load generation (bumped when seen not loaded)
        self.seen: dict[str, str] = {}           # model -> last status seen
        self.nctx: dict[str, tuple] = {}         # model -> (generation, slot n_ctx, meta.n_ctx in /models)
        self.tok_cache: dict[str, dict] = {}     # model -> TokenCounter cache
        self.tok_scale: dict[str, float] = {}
        self.owners = SlotOwners(owners_path)
        self.switches = 0
        self.loads = 0

    # ---------- engine state ----------
    def started(self, model: str | None) -> str | None:
        return "%s#%d" % (model, self.gen.get(model, 0)) if model else None

    def models(self) -> dict[str, dict]:
        ms = list_models(self.up)
        if ms is None:
            raise RouterError(502, "llama-server router: GET /models did not answer with the model list")
        return {m["id"]: m for m in ms}

    def observe(self, models: dict[str, dict]):
        """A model seen not loaded loses its in-memory state: new generation, n_ctx to read again."""
        for mid, m in models.items():
            v = str((m.get("status") or {}).get("value") or "")
            prev = self.seen.get(mid)
            if v != "loaded" and prev != v:
                self.gen[mid] = self.gen.get(mid, 0) + 1
                self.nctx.pop(mid, None)
                if prev is not None and self.journal is not None:
                    self.journal.log("router_model_state", model=mid, status=v, was=prev, generation=self.gen[mid])
            self.seen[mid] = v

    def resolve(self, name, models: dict[str, dict]) -> str | None:
        if not name:
            return None
        name = str(name)
        if name in models:
            return name
        for mid, m in models.items():
            if name in (m.get("aliases") or []):
                return mid
        return None

    def read_n_ctx(self, mid: str) -> int:
        """Loads the model if needed (autoload=true) and reads the n_ctx of the proxy's slot."""
        sid = int(getattr(self.engine, "slot_id", 0) or 0)
        timeout = float(getattr(self.cfg, "router_load_timeout_s", LOAD_TIMEOUT_S) or LOAD_TIMEOUT_S)
        t0 = time.time()
        st, obj = _json(self.up, "GET", "/slots?model=%s&autoload=true" % q(mid), timeout=timeout)
        if st != 200 or not isinstance(obj, list):
            raise RouterError(503, "llama-server router: model %r could not be loaded or its context size could not "
                                   "be read (GET /slots HTTP %s: %s); the request was not sent"
                              % (mid, st, _err_msg(obj)))
        mine = next((x for x in obj if isinstance(x, dict) and x.get("id") == sid), None)
        n = int((mine or {}).get("n_ctx") or 0)
        if n <= 0:
            raise RouterError(503, "llama-server router: model %r has no slot %d with a context size (slots: %s); the "
                                   "request was not sent" % (mid, sid, [x.get("id") for x in obj if isinstance(x, dict)]))
        ms = round((time.time() - t0) * 1000)
        if self.seen.get(mid) != "loaded":
            self.loads += 1
        self.seen[mid] = "loaded"
        if self.journal is not None:
            self.journal.log("router_n_ctx", model=mid, n_ctx=n, generation=self.gen.get(mid, 0), ms=ms)
        return n

    # ---------- per request ----------
    def select(self, req: dict) -> str:
        """Before the prompt is built: model, n_ctx, window. -> model id. Raises RouterError (nothing is sent)."""
        models = self.models()
        self.observe(models)
        name = req.get("model")
        mid = self.resolve(name, models)
        if mid is None:
            mid = self.resolve(getattr(self.cfg, "router_model", "") or "", models)
        if mid is None:
            raise RouterError(400, "llama-server router: model %r is not served by the engine (models: %s); use one of "
                                   "them as \"model\" or set \"router_model\" in the proxy config"
                              % (name, ", ".join(sorted(models)) or "none"))
        m = models[mid]
        stv = str((m.get("status") or {}).get("value") or "")
        meta_n = (m.get("meta") or {}).get("n_ctx") if isinstance(m.get("meta"), dict) else None
        known = self.nctx.get(mid)
        if known and stv == "loaded" and meta_n is not None and known[2] is not None and meta_n != known[2]:
            # reloaded with another context size between two of our requests (never seen unloaded): new generation
            self.gen[mid] = self.gen.get(mid, 0) + 1
            known = None
        if stv == "loaded" and known and known[0] == self.gen.get(mid, 0):
            n = known[1]
        else:
            n = self.read_n_ctx(mid)
            m2 = self.models().get(mid) or {}
            meta2 = (m2.get("meta") or {}).get("n_ctx") if isinstance(m2.get("meta"), dict) else None
            self.nctx[mid] = (self.gen.get(mid, 0), n, meta2)
        self.apply(mid, n)
        return mid

    def apply(self, mid: str, n: int):
        from .engines import (MIN_WINDOW, RECOMMENDED_WINDOW, SCALED, SCALED_MIN, small_window_allowed)
        if self.applied == (mid, n):
            self.up.router_model = mid
            return
        cfg, base = self.cfg, self.base
        if n < MIN_WINDOW and not small_window_allowed():
            raise RouterError(400, "llama-server router: model %r has a context of %d tokens: the minimum is %d, "
                                   "recommended %d or more (raise its ctx-size in the preset); the request was not sent"
                              % (mid, n, MIN_WINDOW, RECOMMENDED_WINDOW))
        bw = int(base.get("window") or 0)
        w = min(bw, n) if "window" in self.explicit and bw else n
        ratio = w / float(bw or n)
        scaled = {}
        for f in SCALED:
            if f in self.explicit or f not in base:
                continue
            v = base[f]
            nv = v
            if isinstance(v, int) and v > 0 and ratio != 1:
                # same formula as engines.apply
                nv = max(64, SCALED_MIN.get(f, 0) if ratio < 1 else 0, int(v * ratio))
                nv = min(nv, v) if ratio < 1 else nv
            setattr(cfg, f, nv)
            if nv != v:
                scaled[f] = nv
        cfg.window = w
        if "response_floor" in self.explicit and "response_floor" in base:
            cfg.response_floor = base["response_floor"]     # undo the lowering made for a previous model
        floor = int(getattr(cfg, "response_floor", 0) or 0)
        if floor and w and floor > w // 4:
            cfg.response_floor = w // 4
            scaled["response_floor"] = cfg.response_floor
        # token counts are per model (different tokenizers)
        tc = self.counter
        if tc is not None:
            if self.current is not None:
                self.tok_cache[self.current] = tc._cache
                self.tok_scale[self.current] = tc.scale
            tc._cache = self.tok_cache.setdefault(mid, {})
            tc.scale = self.tok_scale.get(mid, 1.0)
        was = self.current
        self.current, self.applied = mid, (mid, n)
        self.up.router_model = mid
        self.engine.model, self.engine.n_ctx = mid, n
        if was is not None and was != mid:
            self.switches += 1
        if self.journal is not None:
            self.journal.log("router_model", model=mid, was=was, n_ctx=n, window=w, generation=self.gen.get(mid, 0),
                             scaled=scaled)

    # ---------- saved files ----------
    def restore_refusal(self, filename: str) -> str | None:
        """None = this file may be restored with the current model; otherwise why not."""
        owner, cur = self.owners.get(filename), self.current
        if cur is None:
            return "no model selected yet"
        if owner is None:
            return "%s was not saved through this proxy in router mode (model unknown)" % filename
        if owner != cur:
            return "%s was saved by model %s, the current model is %s" % (filename, owner, cur)
        return None

    def summary(self) -> dict:
        return {"current": self.current, "n_ctx": self.applied[1] if self.applied else None,
                "generations": dict(self.gen), "switches": self.switches, "loads": self.loads}
