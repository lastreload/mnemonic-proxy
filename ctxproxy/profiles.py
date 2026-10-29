# Author: Maurizio Verde — LastReload
"""Per-model configuration profiles (issue #27).

A profile is selected from the request's concrete ``model`` id. Exact names win
over shell-style patterns. Every concrete model gets its own Proxy, token
counter/template, engine adapter, SQLite store and saved-state tracker. The
only shared objects are the journal, the slot-file ownership index and one
re-entrant lock (an engine with one active model must stay serialized).

Conversations are deliberately ISOLATED per concrete model. Switching models
starts (or resumes) that model's own accounting and archive; switching back
finds the earlier history. A pattern therefore never shares mutable runtime
state between the concrete ids it matches.
"""
from __future__ import annotations

import copy
import dataclasses
import fnmatch
import hashlib
import json
import os
import threading
import time

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .server import Proxy

from .core import Config, Journal, Store, TokenCounter
from .router import SlotOwners
from .upstream import Upstream

PROFILE_ONLY = {"tokenizer", "window_override"}
OWNERS_FILE = "profile-slot-models.json"


class ProfileError(Exception):
    def __init__(self, status: int, message: str, code: str = "model_profile_error"):
        super().__init__(message)
        self.status, self.message, self.code = status, message, code


def _error(error: ProfileError) -> tuple[int, dict, dict]:
    return error.status, {"error": {"message": error.message, "type": "invalid_request_error",
                                    "code": error.code}}, {}


def _pattern(name: str) -> bool:
    return any(c in name for c in "*?[")


def _path(value: str) -> str:
    return os.path.abspath(os.path.expanduser(value)) if value else value


@dataclasses.dataclass
class ProfileSpec:
    name: str
    values: dict
    base_raw: dict
    default_tokenizer: str | None
    counter: TokenCounter

    @property
    def pattern(self) -> bool:
        return _pattern(self.name)

    @property
    def tokenizer(self) -> str | None:
        return self.values.get("tokenizer", self.default_tokenizer) or None

    def matches(self, model: str) -> bool:
        return fnmatch.fnmatchcase(model, self.name)

    def raw_config(self) -> tuple[dict, set]:
        """Base config + profile overrides, and the fields considered explicit.

        A profile ``window`` is a fallback: an engine-reported context wins and
        scales unset thresholds, just as it does without profiles. Set
        ``window_override: true`` for the legacy explicit-window behaviour
        (an upper bound/override validated against the engine context).
        """
        raw = {k: v for k, v in self.base_raw.items() if k not in ("profiles", "default_profile")}
        raw.update({k: v for k, v in self.values.items() if k not in PROFILE_ONLY})
        for key in ("slot_dir", "kv_archive_dir"):
            if raw.get(key):
                raw[key] = _path(raw[key])
        explicit = set(raw)
        if "window" in self.values and not self.values.get("window_override", False):
            explicit.discard("window")
        # Defaults describe the built-in window, not an arbitrary fallback.
        # Establish proportional defaults first; engines.apply then scales from
        # this configured window to the selected model's actual context.
        from .engines import BASE_WINDOW, SCALED, SCALED_MIN
        defaults = Config()
        window = int(raw.get("window", defaults.window))
        ratio = window / float(BASE_WINDOW)
        if ratio != 1:
            for field in SCALED:
                value = getattr(defaults, field, None)
                if field not in raw and isinstance(value, int) and value > 0:
                    scaled = max(64, SCALED_MIN.get(field, 0) if ratio < 1 else 0, int(value * ratio))
                    raw[field] = min(scaled, value) if ratio < 1 else scaled
        return raw, explicit

    def new_counter(self) -> TokenCounter:
        # Tokenizer and PackTemplate are immutable after startup. Caches and
        # calibration are per concrete model and must not be shared.
        tc = copy.copy(self.counter)
        tc._cache = {}
        tc.scale = 1.0
        return tc

    def summary(self) -> dict:
        raw, explicit = self.raw_config()
        cfg = Config.from_dict(raw)
        return {"profile": self.name, "pattern": self.pattern, "tokenizer": self.tokenizer or "estimated",
                "template": "pack" if self.counter.prompt_template else "builtin-qwen",
                "slot_dir": cfg.slot_dir, "window": cfg.window,
                "window_source": "override" if "window" in explicit else "engine when available, config fallback"}


def load_profiles(raw_cfg: dict, cfg: Config, default_tokenizer: str | None = None) -> tuple[list[ProfileSpec], str]:
    """Validate profile declarations and load every tokenizer/template eagerly.

    Eager loading makes a bad path or a missing [exact] dependency a startup /
    check error, never a silent estimated-count fallback on one model.
    """
    profiles = raw_cfg.get("profiles") or {}
    if not isinstance(profiles, dict) or not profiles:
        raise ValueError("profiles must be a non-empty JSON object keyed by model name or pattern")
    fields = {f.name for f in dataclasses.fields(Config)}
    allowed = fields | PROFILE_ONLY
    specs = []
    for name, values in profiles.items():
        if not isinstance(name, str) or not name.strip():
            raise ValueError("profile names must be non-empty model names or patterns")
        if not isinstance(values, dict):
            raise ValueError("profile %r must be a JSON object" % name)
        unknown = sorted(set(values) - allowed)
        if unknown:
            raise ValueError("profile %r has unknown fields: %s" % (name, ", ".join(unknown)))
        if "profiles" in values or "default_profile" in values:
            raise ValueError("profile %r cannot nest profiles/default_profile" % name)
        if "window_override" in values and not isinstance(values["window_override"], bool):
            raise ValueError("profile %r window_override must be true or false" % name)
        tokenizer = values.get("tokenizer", default_tokenizer)
        if tokenizer is not None and not isinstance(tokenizer, str):
            raise ValueError("profile %r tokenizer must be a path string" % name)
        tokenizer = _path(tokenizer) if tokenizer else None
        values = dict(values)
        if tokenizer:
            values["tokenizer"] = tokenizer
        counter = TokenCounter(values.get("chars_per_token", cfg.chars_per_token), tokenizer)
        if tokenizer is not None and not counter.exact:
            raise ValueError("profile %r: cannot load tokenizer %s. Install 'mnemonic-proxy[exact]' and check the "
                             "path/format; refusing to start with an estimated window guard. %s"
                             % (name, tokenizer, counter.kind))
        specs.append(ProfileSpec(name, values, dict(raw_cfg), tokenizer, counter))
    default = raw_cfg.get("default_profile") or ""
    if default:
        matches = [s for s in specs if s.name == default and not s.pattern]
        if not matches:
            raise ValueError("default_profile %r must name an exact (non-pattern) profile" % default)
    return specs, default


class ProfileAutoSaver:
    """One worker; only the currently active concrete model may save the one active engine slot."""

    def __init__(self, dispatcher: "ProfileProxy"):
        self.dispatcher = dispatcher
        self.stop_ev = threading.Event()
        self.th = None

    def start(self):
        self.th = threading.Thread(target=self._loop, daemon=True, name="model-profile-autosave")
        self.th.start()
        return self

    def stop(self):
        self.stop_ev.set()

    def _loop(self):
        while not self.stop_ev.wait(1.0):
            d = self.dispatcher
            with d.lock:
                current = d.contexts.get(d.active_model)
                if current is None or current.autosaver is None:
                    continue
                if time.time() - ((current.up.state or {}).get("t_end", 0)) < current.cfg.autosave_poll_s:
                    continue
                try:
                    current.autosaver.tick()
                except Exception as e:  # noqa: BLE001
                    d.journal.log("autosave_error", step="profile_tick", model=d.active_model, error=repr(e)[:300])


class ProfileProxy:
    """Model-selecting facade used by the normal HTTP/API handler.

    The legacy Proxy is constructed separately when no profiles are configured;
    this class is never on that path.
    """

    def __init__(self, legacy, args, raw_cfg: dict, specs: list[ProfileSpec], default_profile: str = ""):
        self.base_proxy = legacy
        self.args = args
        self.raw_cfg = raw_cfg
        self.specs = specs
        self.default_profile = default_profile
        self.lock = threading.RLock()
        self.up = legacy.up
        self.store = legacy.store
        self.journal = legacy.journal
        self.engine = legacy.engine
        self.mode = legacy.mode
        self.contexts: dict[str, Proxy] = {}
        self.context_specs: dict[str, ProfileSpec] = {}
        self.archive_readers: dict[str, Proxy] = {}
        self.active_model: str | None = None
        self.owners = SlotOwners(os.path.join(os.path.abspath(args.data), OWNERS_FILE))
        self.autosaver = None
        self.archiver = None
        if any(cfg.get("autosave") for cfg in (s.raw_config()[0] for s in specs)):
            self.autosaver = ProfileAutoSaver(self).start()

    @property
    def cfg(self):
        return self.base_proxy.cfg

    @property
    def tc(self):
        return self.base_proxy.tc

    def profile_summary(self) -> list[dict]:
        return [s.summary() for s in self.specs]

    def resolve(self, requested) -> tuple[str, ProfileSpec, bool]:
        name = requested if isinstance(requested, str) else ""
        exact = next((s for s in self.specs if not s.pattern and s.name == name), None)
        if exact is not None:
            return name, exact, False
        pattern = next((s for s in self.specs if name and s.pattern and s.matches(name)), None)
        if pattern is not None:
            return name, pattern, False
        if self.default_profile:
            spec = next(s for s in self.specs if s.name == self.default_profile)
            return spec.name, spec, True
        names = ", ".join(s.name for s in self.specs)
        shown = repr(requested) if requested is not None else "<missing>"
        raise ProfileError(400, "unknown model %s; configured profiles: %s. Use one of those model ids/patterns or "
                                "set default_profile to an exact profile" % (shown, names), "unknown_model")

    def _activate_strata(self, model: str):
        """PolyStrata has one active model. A one-token raw probe selects it before its n_ctx is read.

        No managed prompt or saved state is touched by this probe. Subsequent requests for the same model do not
        probe. Engines with per-model routing/context selection use their existing selectors instead.
        """
        if self.active_model == model:
            from .engines import _get_json
            status = _get_json(self.up, "/v1/status") or {}
            actual = status.get("model") or status.get("active_model")
            if status.get("loaded", True) and (actual is None or actual == model):
                return
        body = {"model": model, "max_tokens": 1, "temperature": 0, "stream": False,
                "messages": [{"role": "user", "content": "hi"}]}
        st, _, data = self.up.raw("POST", "/v1/chat/completions", json.dumps(body).encode("utf-8"))
        if st >= 400:
            raise ProfileError(502 if st >= 500 else st,
                               "could not activate model %r before reading its context (HTTP %d): %s"
                               % (model, st, data[:300].decode("utf-8", "replace")), "model_activation_failed")
        self.active_model = model

    @staticmethod
    def _copy_cfg(dst: Config, src: Config):
        for field in dataclasses.fields(Config):
            setattr(dst, field.name, getattr(src, field.name))

    def _make_context(self, model: str, spec: ProfileSpec):
        from . import engines
        from .server import Proxy

        raw, explicit = spec.raw_config()
        cfg = Config.from_dict(raw)
        if (cfg.slot_save or cfg.live_dump) and not cfg.data_dir:
            cfg.data_dir = os.path.abspath(self.args.data)
        counter = spec.new_counter()
        up = Upstream(self.up.base, self.up.api_key, timeout=self.up.timeout)
        engine = engines.setup(cfg, up, self.journal, explicit=explicit, counter=counter,
                               log=lambda m: print(m, file=__import__("sys").stderr, flush=True))
        problems = engines.window_problems(cfg, engine, explicit) + engines.window_mismatch(cfg, engine, explicit)
        if problems:
            raise ProfileError(400, "model %r profile has an invalid context window: %s"
                               % (model, "; ".join(e for e, _ in problems)), "invalid_profile_window")
        digest = hashlib.sha256(model.encode("utf-8", "surrogatepass")).hexdigest()[:20]
        directory = os.path.join(os.path.abspath(self.args.data), "models", digest)
        os.makedirs(directory, exist_ok=True)
        try:
            with open(os.path.join(directory, "model.json"), "w", encoding="utf-8") as f:
                json.dump({"model": model, "profile": spec.name}, f, ensure_ascii=False, indent=1)
        except OSError:
            pass
        child = Proxy(cfg, up, Store(os.path.join(directory, "archive.sqlite")), self.journal, counter,
                      mode=self.mode)
        # Keep Proxy's regular Lock: autosave/archiving use locked(), which RLock
        # only gained in Python 3.14. Dispatcher serializes outside the child lock.
        child.engine = engine
        child.mgr.conversation_namespace = model
        if engine.router_state is not None:
            # All concrete runtimes share one ownership map; independent maps would overwrite each other's file.
            engine.router_state.owners = self.owners
        up.profile_model = model
        up.slot_owners = self.owners
        if cfg.autosave:
            child.enable_autosave(start=False)
        if cfg.kv_archive:
            from .kvarchive import enable as enable_kv_archive, zstd
            if zstd is None:
                raise ProfileError(400, "model %r enables kv_archive but Python >= 3.14 is required" % model)
            enable_kv_archive(child)
        self.contexts[model] = child
        self.context_specs[model] = spec
        self.journal.log("model_profile_runtime", model=model, profile=spec.name, store=directory,
                         tokenizer=counter.kind, template="pack" if counter.prompt_template else "builtin-qwen",
                         window=cfg.window)
        return child

    def _refresh_window(self, model: str, child):
        """Read the active model's context before planning, without cumulative threshold rescaling."""
        from . import engines
        engine = child.engine
        if engine.router_state is not None or getattr(engine, "flavor", None):
            return  # Router/GenericWindow select and apply the requested model in Proxy.chat.
        if engine.kind == "openai":
            return  # No engine context is available: setup already applied the configured fallback and slack.
        if engine.kind == "strata":
            self._activate_strata(model)
        spec = self.context_specs[model]
        raw, explicit = spec.raw_config()
        cfg = Config.from_dict(raw)
        if (cfg.slot_save or cfg.live_dump) and not cfg.data_dir:
            cfg.data_dir = os.path.abspath(self.args.data)
        detected = engines.detect(child.up, cfg.engine, int(cfg.slot_id or 0))
        if detected.kind == "ds4":
            from .engines import _get_json
            models = _get_json(child.up, "/v1/models") or {}
            mine = next((m for m in models.get("data") or []
                         if model in (m.get("id"), m.get("name"))), None)
            if mine:
                detected.model = model
                detected.n_ctx = int(mine.get("context_length") or 0) or detected.n_ctx
        child.up.set_engine(detected, cfg.slot_dir)
        engines.apply(cfg, detected, self.journal, explicit, log=None)
        self._copy_cfg(child.cfg, cfg)
        child.engine = detected
        problems = engines.window_problems(cfg, detected, explicit) + engines.window_mismatch(cfg, detected, explicit)
        if problems:
            raise ProfileError(400, "model %r has an invalid active context window: %s"
                               % (model, "; ".join(e for e, _ in problems)), "invalid_profile_window")

    def chat(self, req: dict, hint: str | None = None, emit=None):
        try:
            model, spec, used_default = self.resolve(req.get("model"))
        except ProfileError as e:
            return _error(e)
        # The concrete model is forced on the client request and on every
        # internal call by Upstream._model. Unknown ids never reach this path.
        routed = {**req, "model": model}
        with self.lock:
            try:
                child = self.contexts.get(model)
                if child is None:
                    if getattr(self.engine, "kind", None) == "strata":
                        self._activate_strata(model)  # detect the target's n_ctx, not the previously active model's
                    child = self._make_context(model, spec)
                self._refresh_window(model, child)
                self.active_model = model
                if used_default:
                    self.journal.log("model_profile_default", requested=req.get("model"), model=model,
                                     profile=spec.name)
                self.journal.log("model_profile", model=model, profile=spec.name, default=used_default,
                                 window=child.cfg.window)
                return child.chat(routed, hint, emit)
            except ProfileError as e:
                self.journal.log("model_profile_refused", model=model, status=e.status, error=e.message[:300])
                return _error(e)

    def count_tokens(self, req: dict):
        """Anthropic count_tokens: apply the same fail-closed model validation and tokenizer/template selection."""
        try:
            model, spec, _ = self.resolve(req.get("model"))
            with self.lock:
                child = self.contexts.get(model)
                if child is None:
                    if getattr(self.engine, "kind", None) == "strata":
                        self._activate_strata(model)
                    child = self._make_context(model, spec)
                self._refresh_window(model, child)
                n, _ = child.mgr.estimate(req.get("messages") or [], req.get("tools"))
                return 200, {"input_tokens": int(n)}
        except ProfileError as e:
            status, body, _ = _error(e)
            return status, body

    def _child_for(self, conv: str | None = None, rid: str | None = None):
        # Dashboard/archive reads must survive a restart without loading a model
        # or probing the engine. Lightweight facades reuse only Proxy's archive
        # and mark methods; they never handle chat or saved-state operations.
        from pathlib import Path
        with self.lock:
            root = Path(os.path.abspath(self.args.data)) / "models"
            for metadata in sorted(root.glob("*/model.json")):
                try:
                    model = json.loads(metadata.read_text(encoding="utf-8"))["model"]
                    if model in self.contexts or model in self.archive_readers:
                        continue
                    concrete, spec, defaulted = self.resolve(model)
                    if defaulted or concrete != model:
                        continue
                    digest = hashlib.sha256(model.encode("utf-8", "surrogatepass")).hexdigest()[:20]
                    if metadata.parent.name != digest or not (metadata.parent / "archive.sqlite").is_file():
                        continue
                    reader = copy.copy(self.base_proxy)
                    reader.store = Store(str(metadata.parent / "archive.sqlite"))
                    reader.cfg = Config.from_dict(spec.raw_config()[0])
                    reader.tc = spec.new_counter()
                    self.archive_readers[model] = reader
                except (OSError, ValueError, KeyError, TypeError, ProfileError):
                    continue
            children = list(self.contexts.values()) + [r for m, r in self.archive_readers.items()
                                                        if m not in self.contexts]
            if conv:
                return next((c for c in children if c.store.has_conv(conv)), None)
            if rid:
                return next((c for c in children if c.store.get(rid)), None)
            return None

    def store_for(self, conv: str | None = None, rid: str | None = None):
        child = self._child_for(conv, rid)
        return child.store if child is not None else self.store

    def marks(self, conv: str):
        child = self._child_for(conv=conv)
        return child.marks(conv) if child is not None else self.base_proxy.marks(conv)

    def mark_action(self, data: dict):
        child = self._child_for(conv=data.get("conv"))
        return (child or self.base_proxy).mark_action(data)

    def workers(self):
        out = [self.autosaver]
        for child in self.contexts.values():
            out.extend((child.autosaver, getattr(child, "archiver", None)))
        return [w for w in out if w is not None]

    def close(self):
        for worker in self.workers():
            worker.stop()
        for worker in self.workers():
            if worker.th is not None:
                worker.th.join()
        for child in list(self.contexts.values()) + list(self.archive_readers.values()):
            child.store.db.close()
        self.store.db.close()
