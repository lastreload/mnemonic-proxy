# Author: Maurizio Verde — LastReload
"""Process-local circuit breaker for engine saved-state sessions.

One ``SavedState`` belongs to one concrete ``Upstream``.  It keeps independent
states for explicit concrete model ids used by that upstream.  Profile runtimes
(PR #45) can therefore keep their separate upstreams and set ``profile_model``
without sharing or renaming this policy.
"""
from __future__ import annotations

import json
import sys
import threading
import time

FEATURES = ("mask_anchor", "slot_save", "autosave", "autorestore", "seal_experimental", "kv_archive")
_FATAL_MARKERS = ("the engine answered a session", "the server ended the engine")
# 501 texts documented as permanent (matched case-insensitively): llama-server and PolyStrata without
# --slot-save-path, PolyStrata with parallel requests, PolyStrata on strata-poly (VecSzn/PolyStrata#114).
_CONFIG_501 = ("slot save/restore is disabled", "--slot-save-path", "not available with parallel requests",
               "not supported by this model's engine")
COOLDOWN_S = 900.0   # an unrecognised HTTP 501 pauses saved state for 15 minutes, then one call may probe


class SavedStateUnavailable(RuntimeError):
    """A saved-state operation was blocked locally after an unsupported reply."""

    def __init__(self, model: str, action: str, reason: str, kind: str = "fatal", retry_after_s=None):
        self.model, self.action, self.reason, self.kind = model, action, reason, kind
        if kind == "unknown" and retry_after_s is None:
            until = "while a retry is in progress (after an unrecognised HTTP 501)"
        elif kind == "unknown":
            until = "for another %d s (after an unrecognised HTTP 501), then one call may retry" % retry_after_s
        else:
            until = "until proxy restart or POST /v1/strata/saved-state/reset from this machine"
        super().__init__("saved state is suspended for model %r after %s; blocked %s %s"
                         % (model or "<default>", reason, action, until))


def classify(status, body) -> tuple[str, str] | None:
    """-> (kind, reason) for a saved-state refusal, else None.

    kind: "fatal" (documented HTTP 500: the server ended the engine), "config" (documented permanent
    HTTP 501), "unknown" (any other HTTP 501).  404/400/409/503/507, generic 500 and transport errors
    are not refusals.
    """
    if status == 501:
        reason = _message(body) or "HTTP 501: slot save/restore is not supported"
        low = reason.lower()
        return ("config" if any(m.lower() in low for m in _CONFIG_501) else "unknown"), reason
    if status != 500:
        return None
    text = body.decode("utf-8", "replace") if isinstance(body, bytes) else str(body or "")
    if any(marker in text for marker in _FATAL_MARKERS):
        return "fatal", _message(body) or text[:500]
    return None


def unsupported_reason(status, body) -> str | None:
    """#47 API kept for its callers and tests: the reason text of any refusal."""
    c = classify(status, body)
    return c[1] if c else None


def _message(body: bytes) -> str:
    try:
        obj = json.loads(body)
        message = obj["error"]["message"]
        return message if isinstance(message, str) else ""
    except (ValueError, TypeError, KeyError, AttributeError):
        return body.decode("utf-8", "replace")[:500] if isinstance(body, bytes) else ""


def _stderr(message: str):
    try:
        print(message, file=sys.stderr, flush=True)
    except OSError:
        pass


class SavedState:
    """Thread-safe, per-upstream and per-concrete-model saved-state policy."""

    def __init__(self, upstream):
        self.upstream = upstream
        self.journal = None
        self.log = _stderr
        self._current = ""
        self._suspended: dict[str, dict] = {}
        self._lock = threading.RLock()
        self.clock = time.monotonic
        self._generation = 0

    def bind(self, journal=None, log=None):
        """Attach runtime observability without changing an already-trip state."""
        self.journal = journal
        if log is not None:
            self.log = log

    def select(self, model=None):
        """Select an exact request model id; never infer an alias or family."""
        if not isinstance(model, str):
            model = ""
        with self._lock:
            self._current = model

    def key(self, model=None) -> str:
        if isinstance(model, str) and model:
            return model
        # These explicit selectors are used by PR #45 and llama.cpp router mode.
        for name in ("profile_model", "router_model"):
            selected = getattr(self.upstream, name, None)
            if isinstance(selected, str) and selected:
                return selected
        with self._lock:
            return self._current

    def _expired(self, rec) -> bool:
        return rec["kind"] == "unknown" and not rec["probing"] and self.clock() >= rec["until"]

    def suspension(self, model=None) -> dict | None:
        with self._lock:
            rec = self._suspended.get(self.key(model))
            return None if rec is None or self._expired(rec) else rec

    def available(self, model=None) -> bool:
        return self.suspension(model) is None

    def require(self, action: str, model=None, key=None):
        """Raise if suspended. -> a probe token (key, generation) when this call is the single retry after a pause,
        else None.  Pass key= to use a key fixed by the caller (Upstream.slot fixes it once per call).

        Concurrency contract: calls made through Proxy.chat are serialised by Proxy.lock.  Direct callers of
        Upstream.slot on several threads must serialise model selection and slot calls themselves."""
        key = self.key(model) if key is None else key
        with self._lock:
            rec = self._suspended.get(key)
            if rec is None:
                return None
            if self._expired(rec):
                rec["probing"] = True
                return key, rec["gen"]
            if rec["probing"]:
                left = None
            else:
                left = max(0, int(rec["until"] - self.clock())) if rec["until"] is not None else None
            kind, reason = rec["kind"], rec["reason"]
        raise SavedStateUnavailable(key, action, reason, kind, left)

    def end_probe(self, token, ok: bool):
        """Close the probe opened by require(): resume after a success, re-arm the pause otherwise.  A token whose
        record was reset (or replaced by a newer episode) is ignored."""
        key, gen = token
        with self._lock:
            rec = self._suspended.get(key)
            if rec is None or rec["gen"] != gen or not rec["probing"]:
                return
            rec["probing"] = False
            if ok:
                del self._suspended[key]
            elif rec["kind"] == "unknown":
                rec["until"] = self.clock() + COOLDOWN_S
            kind = rec["kind"]
            retry = int(COOLDOWN_S) if kind == "unknown" else None
        if ok:
            self._event("saved_state_resumed", "[mnemonic-proxy] saved state resumed for model %r: the retry after "
                        "the pause succeeded" % (key or "<default>"), model=key or None)
        else:
            self._event("saved_state_probe_failed", None, model=key or None, kind=kind, retry_after_s=retry)

    def view(self) -> list:
        """Suspended models for GET /v1/strata/saved-state; retry_after_s only for paused unknown 501s."""
        now = self.clock()
        with self._lock:
            return [{"model": r["model"], "kind": r["kind"], "status": r["status"], "action": r["action"],
                     "reason": r["reason"], "probing": r["probing"],
                     "retry_after_s": max(0, int(r["until"] - now)) if r["until"] is not None else None}
                    for r in self._suspended.values()]

    def observe(self, error, action: str, filename: str = "", model=None, key=None) -> bool:
        """Trip once for a refusal; return True only for a new trip. A fatal or config reply upgrades a paused
        record to permanent."""
        status = getattr(error, "status", None)
        c = classify(status, getattr(error, "body", b""))
        if c is None:
            return False
        kind, reason = c
        key = self.key(model) if key is None else key
        with self._lock:
            rec = self._suspended.get(key)
            if rec is not None:
                if kind != "unknown" and rec["kind"] == "unknown":
                    rec.update(kind=kind, status=status, reason=reason[:500], until=None)
                return False
            self._generation += 1
            rec = {"model": key or None, "action": action, "filename": filename, "status": status,
                   "kind": kind, "reason": reason[:500], "probing": False, "gen": self._generation,
                   "until": self.clock() + COOLDOWN_S if kind == "unknown" else None}
            self._suspended[key] = rec
        if kind == "unknown":
            ends = "for %d s (an unrecognised HTTP 501), then one call retries" % COOLDOWN_S
        else:
            ends = "until the proxy restarts or POST /v1/strata/saved-state/reset is sent from this machine"
        warning = ("[mnemonic-proxy] warning: saved state suspended for model %r after HTTP %s during %s: %s. "
                   "Anchors and their one-token calls, segment seal/save, SAVE/RESTORE, autosave/autorestore and "
                   "kv_archive are disabled for this model %s. To keep them off, set mask_anchor, slot_save, "
                   "autosave, autorestore, seal_experimental and kv_archive to false in the configuration "
                   "(or in this model's profile) and restart."
                   % (key or "<default>", status, action, reason[:300], ends))
        self._event("saved_state_suspended", warning,
                    **{k: v for k, v in rec.items() if k not in ("probing", "until", "gen")},
                    upstream=self.upstream.base, features=list(FEATURES), recovery=ends)
        return True

    def _event(self, name: str, message, **fields):
        if message and self.log is not None:
            self.log(message)
        if self.journal is not None:
            self.journal.log(name, **fields)


def select_model(upstream, model=None):
    state = getattr(upstream, "saved_state", None)
    if state is not None:
        state.select(model)


def state_available(upstream, model=None) -> bool:
    """Tolerate small fake upstreams used by offline tools and older tests."""
    state = getattr(upstream, "saved_state", None)
    return state is None or state.available(model)
