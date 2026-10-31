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

FEATURES = ("mask_anchor", "slot_save", "autosave", "autorestore", "seal_experimental", "kv_archive")
_FATAL_MARKERS = ("the engine answered a session", "the server ended the engine")
# 501 texts documented as permanent (matched case-insensitively): llama-server and PolyStrata without
# --slot-save-path, PolyStrata with parallel requests, PolyStrata on strata-poly (VecSzn/PolyStrata#114).
_CONFIG_501 = ("slot save/restore is disabled", "--slot-save-path", "not available with parallel requests",
               "not supported by this model's engine")
COOLDOWN_S = 900.0   # an unrecognised HTTP 501 pauses saved state for 15 minutes, then one call may probe


class SavedStateUnavailable(RuntimeError):
    """A saved-state operation was blocked locally after an unsupported reply."""

    def __init__(self, model: str, action: str, reason: str):
        self.model, self.action, self.reason = model, action, reason
        super().__init__("saved state is suspended for model %r after %s; blocked %s until proxy restart"
                         % (model or "<default>", reason, action))


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

    def suspension(self, model=None) -> dict | None:
        return self._suspended.get(self.key(model))

    def available(self, model=None) -> bool:
        return self.suspension(model) is None

    def require(self, action: str, model=None):
        suspended = self.suspension(model)
        if suspended is not None:
            raise SavedStateUnavailable(self.key(model), action, suspended["reason"])

    def observe(self, error, action: str, filename: str = "", model=None) -> bool:
        """Trip once for a permanent refusal; return True only for a new trip."""
        status = getattr(error, "status", None)
        body = getattr(error, "body", b"")
        reason = unsupported_reason(status, body)
        if reason is None:
            return False
        key = self.key(model)
        with self._lock:
            if key in self._suspended:
                return False
            record = {"model": key or None, "action": action, "filename": filename,
                      "status": status, "reason": reason[:500]}
            self._suspended[key] = record
        warning = ("[mnemonic-proxy] warning: saved state suspended for model %r after HTTP %s during %s: %s. "
                   "Anchors and their one-token calls, segment seal/save, SAVE/RESTORE, autosave/autorestore and "
                   "kv_archive are disabled for this model until the proxy restarts. Restart the proxy to retry, "
                   "or set mask_anchor, slot_save, autosave, autorestore, seal_experimental and kv_archive to false "
                   "in the configuration and restart."
                   % (key or "<default>", status, action, reason[:300]))
        if self.log is not None:
            self.log(warning)
        if self.journal is not None:
            self.journal.log("saved_state_suspended", **record, upstream=self.upstream.base,
                             features=list(FEATURES),
                             recovery="restart the proxy, or disable all six saved-state options and restart")
        return True


def select_model(upstream, model=None):
    state = getattr(upstream, "saved_state", None)
    if state is not None:
        state.select(model)


def state_available(upstream, model=None) -> bool:
    """Tolerate small fake upstreams used by offline tools and older tests."""
    state = getattr(upstream, "saved_state", None)
    return state is None or state.available(model)
