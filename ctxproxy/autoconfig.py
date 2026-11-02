# Author: Maurizio Verde — LastReload
"""Automatic configuration: "why is the default config not the recommended config?" (r/LocalLLM).

The answer used to be "because the recommended values depend on your engine and your machine, and the proxy
did not look". This module looks, and then chooses -- out loud. Three rules:

  1. only options that depend on the ENGINE or on the MACHINE are decided here; everything that changes what
     the proxy writes into the prompt keeps its default, so an old conversation is replayed byte for byte
     (same principle as the receipts and `prompt_language`);
  2. an option written in the config file (or on the command line) always wins and is never touched;
  3. no silent choice: every decision carries the reason it was made and the way to force it otherwise.
     `mnemonic-proxy check --explain-config` prints the whole picture without starting the proxy, and the
     startup banner prints the decisions that were actually applied.

What is read, all harmless reads:

  engine    kind, version and context window from the endpoints the proxy already calls (Strata `/v1/status`
            carries `engine` = its version, `context.native` and `concurrency`; llama-server `/props` carries
            `build_info`); saved-state support as `engines.detect` establishes it; local or remote from the
            `--upstream` URL.
  machine   total and available RAM (Linux `/proc/meminfo`, macOS `sysctl`/`vm_stat`, Windows
            `GlobalMemoryStatusEx` through `ctypes`); free disk of `slot_dir` (`shutil.disk_usage`).
  VRAM      only if it comes for free: the GPU figures Strata already reports in `/v1/status`, otherwise
            `nvidia-smi` when it happens to be on PATH. Never required, never installed, no rule depends on
            it: it is printed as evidence next to the decisions.

The one sizing number: a saved session file costs about `state_kb_per_token` KB per token. The default, 19.5
KB/token, is the 20000 bytes/token `autosave.py` already uses for its own disk check, so the two agree; the
measured spread is wide (docs/dev-notes/ROUTER-MODE-RESULT.md: Qwen3.5 35B-A3B 37-73 KB/token, gemma E2B
7.6-9 KB/token; docs/dev-notes/ENGINES-RESULT.md: llama-server with Qwen3-0.6B ~115 KB/token), which is
exactly why it is a configuration field and why the rule below is a floor and not a promise. At 19.5 KB/token
a 131072-token window needs roughly 2.4 GiB of engine state: the saved-state options are only switched on when
that much RAM is actually free, which is what the 32 GB report on r/LocalLLM needed.
"""
from __future__ import annotations

import dataclasses
import os
import shutil
import sqlite3
from pathlib import Path
import subprocess
import sys

GIB = float(2 ** 30)
MIB = float(2 ** 20)
# where the default KB/token comes from, printed with the decision so nobody has to trust it blindly
STATE_KB_DEFAULT = 19.5          # kept in step with Config.state_kb_per_token
STATE_KB_SOURCE = "the 20000 bytes/token of autosave.py; spread in docs/dev-notes/ROUTER-MODE-RESULT.md"
# saved-state options the automatic configuration may switch ON (all of them only touch engine state files,
# never the prompt). `seal_experimental` and `kv_archive` stay opt-in on purpose: see README.
AUTO_SAVED_STATE = ("slot_save", "mask_anchor", "autosave")
# every field decide() may set: restored as they were on an existing data folder
AUTO_FIELDS = AUTO_SAVED_STATE + ("engine_tokenize", "response_floor", "notes_max_tokens")
# how long to wait for an external helper (nvidia-smi, sysctl, vm_stat) before giving up on it
PROBE_TIMEOUT_S = 3.0


# --- the machine ------------------------------------------------------------------------------------

def _run(cmd: list[str]) -> str:
    """A short external read, or "" when the tool is missing, slow or unhappy. Never raises."""
    if not shutil.which(cmd[0]):
        return ""
    try:
        out = subprocess.run(cmd, capture_output=True, timeout=PROBE_TIMEOUT_S, check=False)
    except (OSError, subprocess.SubprocessError):
        return ""
    if out.returncode != 0:
        return ""
    return out.stdout.decode("utf-8", "replace")


def _ram_linux() -> tuple[int | None, int | None, str]:
    try:
        with open("/proc/meminfo", encoding="ascii", errors="replace") as f:
            kv = dict(line.split(":", 1) for line in f if ":" in line)
    except OSError:
        return None, None, ""

    def mb(name):
        parts = (kv.get(name) or "").split()
        return int(int(parts[0]) // 1024) if parts and parts[0].isdigit() else None

    total = mb("MemTotal")
    free = mb("MemAvailable")
    if free is None:
        free = mb("MemFree")
    return total, free, "/proc/meminfo" if total else ""


def _ram_macos() -> tuple[int | None, int | None, str]:
    total = _run(["sysctl", "-n", "hw.memsize"]).strip()
    total_mb = int(int(total) // MIB) if total.isdigit() else None
    free_mb = None
    text = _run(["vm_stat"])
    if text:
        page = 4096
        pages = {}
        for line in text.splitlines():
            if "page size of" in line:
                digits = "".join(c for c in line.split("page size of")[-1] if c.isdigit())
                page = int(digits) if digits else page
            name, _, rest = line.partition(":")
            digits = "".join(c for c in rest if c.isdigit())
            if digits:
                pages[name.strip()] = int(digits)
        got = [pages.get(k) for k in ("Pages free", "Pages inactive", "Pages speculative")]
        if got[0] is not None:
            free_mb = int(sum(v for v in got if v is not None) * page // MIB)
    return total_mb, free_mb, "sysctl hw.memsize + vm_stat" if total_mb else ""


def _ram_windows() -> tuple[int | None, int | None, str]:
    try:
        import ctypes

        class MemoryStatusEx(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                        ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

        st = MemoryStatusEx()
        st.dwLength = ctypes.sizeof(MemoryStatusEx)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):   # type: ignore[attr-defined]
            return None, None, ""
        return int(st.ullTotalPhys // MIB), int(st.ullAvailPhys // MIB), "GlobalMemoryStatusEx"
    except Exception:  # noqa: BLE001  - a missing API must never stop the proxy
        return None, None, ""


def read_ram() -> tuple[int | None, int | None, str]:
    """-> (total MiB, available MiB, where it was read). (None, None, "") when the platform does not say."""
    if os.name == "nt":
        return _ram_windows()
    if sys.platform == "darwin":
        return _ram_macos()
    return _ram_linux()


def _vram_from_engine(engine) -> tuple[int | None, int | None, str]:
    """Strata already reports the GPU in /v1/status (machine.gpu, vram): no extra tool, no extra request."""
    m = getattr(engine, "machine", None) or {}
    gpu = m.get("gpu") if isinstance(m, dict) else None
    if not isinstance(gpu, dict):
        return None, None, ""
    total, used = gpu.get("total_mib"), gpu.get("used_mib")
    if not isinstance(total, (int, float)) or total <= 0:
        return None, None, ""
    free = int(total - used) if isinstance(used, (int, float)) else None
    return int(total), free, "engine /v1/status machine.gpu"


def _vram_nvidia_smi() -> tuple[int | None, int | None, str]:
    text = _run(["nvidia-smi", "--query-gpu=memory.total,memory.free", "--format=csv,noheader,nounits"])
    for line in text.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
            return int(parts[0]), int(parts[1]), "nvidia-smi"
    return None, None, ""


@dataclasses.dataclass
class Machine:
    """What this host says about itself. Every field may be None: nothing here is required."""
    ram_total_mb: int | None = None
    ram_free_mb: int | None = None
    vram_total_mb: int | None = None
    vram_free_mb: int | None = None
    sources: dict = dataclasses.field(default_factory=dict)

    @classmethod
    def read(cls, engine=None, vram: bool = True) -> "Machine":
        total, free, src = read_ram()
        m = cls(ram_total_mb=total, ram_free_mb=free)
        if src:
            m.sources["ram"] = src
        if vram:
            vt, vf, vsrc = _vram_from_engine(engine)
            if not vsrc:
                vt, vf, vsrc = _vram_nvidia_smi()
            m.vram_total_mb, m.vram_free_mb = vt, vf
            if vsrc:
                m.sources["vram"] = vsrc
        return m

    def summary(self) -> dict:
        d = {k: v for k, v in dataclasses.asdict(self).items() if k != "sources" and v is not None}
        if self.sources:
            d["sources"] = dict(self.sources)
        return d

    def line(self) -> str:
        """One line of evidence, for the banner and for `check`."""
        def gib(mb):
            return "%.1f GiB" % (mb / 1024.0) if isinstance(mb, int) else "unknown"

        out = "RAM %s total, %s available" % (gib(self.ram_total_mb), gib(self.ram_free_mb))
        if self.ram_total_mb is None:
            out = "RAM unknown on this platform"
        if self.vram_total_mb is not None:
            out += "; VRAM %s total, %s free" % (gib(self.vram_total_mb), gib(self.vram_free_mb))
        else:
            out += "; VRAM not known (optional: no nvidia-smi and the engine does not report it)"
        if self.sources:
            out += " [%s]" % ", ".join("%s: %s" % kv for kv in sorted(self.sources.items()))
        return out


# --- the decisions ----------------------------------------------------------------------------------

@dataclasses.dataclass
class Decision:
    """One configuration value and why it has that value. `changed` False = reported, not touched."""
    field: str
    value: object
    why: str
    override: str = ""
    changed: bool = True
    # 0.3.23 (friction A8): this choice was NOT applied because the data folder already has conversations.
    # `check` counts these in one summary line and leaves the reasons to `--explain-config`.
    kept_existing: bool = False

    def line(self) -> str:
        mark = "auto" if self.changed else "kept"
        return "%s %s = %s: %s" % (mark, self.field, fmt(self.value), self.why)

    def summary(self) -> dict:
        d = {"field": self.field, "value": self.value, "why": self.why, "changed": self.changed}
        if self.kept_existing:
            d["kept_existing"] = True
        if self.override:
            d["override"] = self.override
        return d


def fmt(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, float):
        return "%g" % v
    return str(v)


def state_file_gib(window: int, kb_per_token: float) -> float:
    """Estimated size of one saved session file for a full window."""
    return max(0.0, float(window) * float(kb_per_token) * 1024.0 / GIB)


def state_kb(cfg) -> float:
    """`state_kb_per_token` from the config, with the module default as the fallback."""
    try:
        value = float(getattr(cfg, "state_kb_per_token", STATE_KB_DEFAULT) or STATE_KB_DEFAULT)
    except (TypeError, ValueError):
        return STATE_KB_DEFAULT
    return value if value > 0 else STATE_KB_DEFAULT


def version_tuple(version: str | None) -> tuple:
    """"0.1.51" -> (0, 1, 51); "6123 (abc1234)" -> (6123,); anything unparsable -> ().

    Used only to compare with a known floor, so it stops at the first part that does not start with digits.
    """
    out = []
    for part in str(version or "").strip().lstrip("vV").replace("-", ".").split("."):
        digits = ""
        for c in part.strip():
            if not c.isdigit():
                break
            digits += c
        if not digits:
            break
        out.append(int(digits))
    return tuple(out)


# Strata grew session files in 0.1.40 (engines.SAVED_STATE_LATER says so to the user)
STRATA_SAVED_STATE_SINCE = (0, 1, 40)


def saved_state_gate(cfg, engine, machine: Machine | None) -> tuple[bool, str, str]:
    """May the proxy switch the saved-state options on by itself? -> (ok, why, how to force it).

    Every "no" names the one thing that is missing, so the answer is actionable.
    """
    from .engines import saved_state_later, upstream_is_local
    force = 'set "slot_save": true (and "mask_anchor"/"autosave") in the config to force it'
    kind = getattr(engine, "kind", "")
    if not getattr(engine, "slot_save", False):
        return False, "the engine (%s) does not offer saved state" % (kind or "unknown"), saved_state_later(kind)
    ver = version_tuple(getattr(engine, "version", None))
    if kind == "strata" and ver and ver < STRATA_SAVED_STATE_SINCE:
        return (False, "Strata %s is older than %s, which is where session files start"
                % (engine.version, ".".join(map(str, STRATA_SAVED_STATE_SINCE))),
                "update Strata, or build the session-files branch (README, Engines)")
    par = int(getattr(engine, "parallel", 0) or 0)
    if kind == "strata" and par > 1:
        # verified in Strata's serve/server.py slot_action(): with batch slots it answers 501 before touching
        # the engine ("slot save/restore is not available with parallel requests"). llama-server has no such
        # limit: with --parallel N the proxy pins id_slot and save/restore keep working.
        return (False, "Strata serves %d requests in parallel and refuses save/restore in that mode" % par,
                "start Strata with a single slot (no \"parallel\" / --batch)")
    if not upstream_is_local(getattr(engine, "upstream", "") or ""):
        return (False, "the engine runs on another host (%s): a path written here is not its --slot-save-path"
                % (getattr(engine, "upstream", "") or "remote"),
                "run the proxy next to its engine, or share the folder and " + force)
    if not getattr(cfg, "slot_dir", ""):
        return (False, "slot_dir is not set, so old state files could never be cleaned up",
                "set \"slot_dir\" to the engine's --slot-save-path folder and the proxy turns saved state on by "
                "itself")
    kb = state_kb(cfg)
    need = state_file_gib(int(getattr(cfg, "window", 0) or 0), kb)
    free = (machine.ram_free_mb / 1024.0) if machine is not None and machine.ram_free_mb is not None else None
    if free is not None and need > free:
        return (False, "a session file for a %d-token window is about %.1f GiB (%.1f KB/token, %s) and only "
                "%.1f GiB of RAM are available"
                % (int(getattr(cfg, "window", 0) or 0), need, kb, STATE_KB_SOURCE, free),
                "free some RAM, use a smaller window, or " + force)
    where = "%.1f GiB available" % free if free is not None else "RAM unknown on this platform"
    return (True, "the engine saves its state, slot_dir is on this host, and the estimated session file "
            "(%.1f GiB at %.1f KB/token, %s) fits: %s" % (need, kb, STATE_KB_SOURCE, where),
            "set the saved-state options to false in the config to keep them off")


def _disk_free_gib(path: str) -> float | None:
    try:
        return shutil.disk_usage(path).free / GIB
    except OSError:
        return None


def zstd_available() -> bool:
    try:
        from .kvarchive import zstd
    except Exception:  # noqa: BLE001
        return False
    return zstd is not None


def existing_install(data_dir) -> bool:
    """True when the data folder already holds conversations (archive.sqlite with at least one archived message).

    An upgrade must not change what a running setup does: automatic choices are only *applied* on a fresh data
    folder, or when the user writes "auto_config": true. Read-only; an unreadable archive counts as existing.
    """
    if not data_dir:
        return False
    p = os.path.join(data_dir, "archive.sqlite")
    if not os.path.isfile(p):
        return False
    try:
        con = sqlite3.connect(Path(p).resolve().as_uri() + "?mode=ro", uri=True)
        try:
            return con.execute("SELECT 1 FROM archive LIMIT 1").fetchone() is not None
        finally:
            con.close()
    except sqlite3.Error:
        return True


EXISTING_WHY = ("not applied: this data folder already has conversations, so an upgrade keeps how they ran "
                "(automatic choice: %s, because %s)")
EXISTING_FIX = 'write "auto_config": true in the config to apply the automatic choices, or set "%s" yourself'


def decide(cfg, engine, explicit=(), machine: Machine | None = None, exact: bool = False,
           existing: bool = False) -> list[Decision]:
    """Pick the engine/machine-dependent defaults the user did not write. -> the decisions, in report order.

    `cfg` is modified in place for the fields marked `changed`. `explicit` is the set of field names the user
    wrote (config file or command line): those are reported and never changed. `existing` (see
    `existing_install`): the data folder already has conversations; unless "auto_config" was written, the
    choices are reported as what the proxy would pick and `cfg` is left as it was.
    """
    explicit = set(explicit or ())
    out: list[Decision] = []
    if not getattr(cfg, "auto_config", True):
        return out
    before = {f: getattr(cfg, f, None) for f in AUTO_FIELDS}
    out = _decide(cfg, engine, explicit, machine, exact)
    if existing and "auto_config" not in explicit:
        for d in out:
            if d.changed:
                setattr(cfg, d.field, before.get(d.field))
                d.why = EXISTING_WHY % (fmt(d.value), d.why)
                d.override = EXISTING_FIX % d.field
                d.value = before.get(d.field)
                d.changed = False
                d.kept_existing = True
    return out


def _decide(cfg, engine, explicit, machine, exact) -> list[Decision]:
    out: list[Decision] = []
    window = int(getattr(cfg, "window", 0) or 0)

    # --- saved state: slot_save, mask_anchor, autosave (engine state files only, never the prompt)
    ok, why, override = saved_state_gate(cfg, engine, machine)
    shown = False
    for field in AUTO_SAVED_STATE:
        if field in explicit:
            out.append(Decision(field, getattr(cfg, field, None), "written in the config", changed=False))
            continue
        reason = why if not shown else "same reason as %s above" % AUTO_SAVED_STATE[0]
        fix = override if not shown else ""      # the same override line, once
        if not ok:
            if getattr(cfg, field, False):
                continue        # a default that is already on: engines.apply decides whether to disable it
            out.append(Decision(field, False, reason, fix, changed=False))
            shown = True
            continue
        if field == "autosave":
            # autosave keeps up to autosave_keep files of this size: it needs the disk, not just the RAM
            need = state_file_gib(window, state_kb(cfg))
            floor = float(getattr(cfg, "autosave_min_free_gb", 8.0) or 0.0)
            free = _disk_free_gib(getattr(cfg, "slot_dir", "") or ".")
            if free is not None and free < need + floor:
                out.append(Decision(field, False, "only %.1f GiB free on the slot_dir disk: one session file is "
                                    "about %.1f GiB and autosave keeps %.1f GiB free" % (free, need, floor),
                                    "free some disk or set \"autosave\": true in the config", changed=False))
                continue
        if getattr(cfg, field, False):
            out.append(Decision(field, True, "already on by default", changed=False))
            continue
        setattr(cfg, field, True)
        out.append(Decision(field, True, reason, fix))
        shown = True

    # --- exact token count from the engine, when it has one and no --tokenizer was given
    if "engine_tokenize" not in explicit and getattr(engine, "tokenize", False) and not exact \
            and not getattr(cfg, "engine_tokenize", False):
        cfg.engine_tokenize = True
        out.append(Decision("engine_tokenize", True, "the engine counts tokens exactly (POST /tokenize): used "
                            "instead of the chars/%.1f estimate" % float(getattr(cfg, "chars_per_token", 3.5) or 3.5),
                            "set \"engine_tokenize\": false, or pass --tokenizer for a local exact count"))

    # --- window-sized budgets the README recommends, scaled to the window that was actually found.
    # Only from MIN_WINDOW up: below 16384 the proxy already refuses to run without an explicit opt-in, and
    # the documented choice there is the scaled defaults (README, "Small windows"); a floor computed for a
    # 128K setup changes the measured search/switch trade-off of that scenario
    # (docs/dev-notes/SEARCH-KEEPS-EVIDENCE-RESULT.md).
    from .engines import MIN_WINDOW
    if window >= MIN_WINDOW:
        if "response_floor" not in explicit and not int(getattr(cfg, "response_floor", 0) or 0):
            value = max(1024, window // 8)
            cfg.response_floor = value
            out.append(Decision("response_floor", value, "an eighth of the %d-token window: a short replay no "
                                "longer forces a segment switch (16384 at 131072, the README value)" % window,
                                "set \"response_floor\" in the config (0 = no floor)"))
        if "notes_max_tokens" not in explicit:
            value = max(1024, window * 3 // 32)
            if value != int(getattr(cfg, "notes_max_tokens", 0) or 0):
                cfg.notes_max_tokens = value
                out.append(Decision("notes_max_tokens", value, "three thirty-seconds of the %d-token window for "
                                    "the handoff notes (12288 at 131072, the README value)" % window,
                                    "set \"notes_max_tokens\" in the config"))

    return out


def explain(cfg, engine, machine: Machine | None, decisions: list[Decision], full: bool = False) -> list[str]:
    """The human-readable report: what was found, what was chosen, how to force it. -> lines."""
    lines = []
    kind = getattr(engine, "kind", "?")
    ver = getattr(engine, "version", None)
    lines.append("engine: %s%s%s, window %s, saved state %s, %s" % (
        kind, " %s" % ver if ver else " (version not reported)",
        " router" if getattr(engine, "router", False) else "",
        getattr(engine, "n_ctx", None) or getattr(cfg, "window", "?"),
        "available" if getattr(engine, "slot_save", False) else "not available",
        "on this host" if _local(engine) else "on another host (%s)" % (getattr(engine, "upstream", "") or "?")))
    if machine is not None:
        lines.append("machine: " + machine.line())
    if not getattr(cfg, "auto_config", True):
        lines.append("automatic configuration off (\"auto_config\": false): every default is used as written")
        return lines
    shown = [d for d in decisions if full or d.changed]
    if not shown:
        lines.append("nothing to choose: the config already decides every engine/machine-dependent option")
    for d in shown:
        lines.append(d.line())
        if d.override and (full or d.changed):
            lines.append("    -> " + d.override)
    return lines


def _local(engine) -> bool:
    from .engines import upstream_is_local
    return upstream_is_local(getattr(engine, "upstream", "") or "")
