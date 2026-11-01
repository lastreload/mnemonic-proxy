# Automatic configuration — result

0.3.23, `ctxproxy/autoconfig.py`. Author: Maurizio Verde — LastReload.

## The report

From r/LocalLLM, about 0.3.22:

> *"Why is the default config not the recommended config? It would be so nice if it could detect the engine
> version, context size, VRAM + RAM and apply the best settings by itself."*

The same user had enabled saved state by hand and then found a ~2 GB state file eating the RAM he needed for
the model, on a 32 GB machine. Two separate complaints in one sentence:

1. the recommended configuration had to be copied by hand, field by field, from the README;
2. nothing checked whether the machine could actually afford what was being switched on.

The honest answer to (1) was *"because those values depend on your engine and your machine, and the proxy did
not look"*. So it looks now. The answer to (2) is a size estimate checked against the free RAM **before** the
options go on, with the number and its source printed next to the decision.

## What this adds

- `ctxproxy/autoconfig.py` (new): reads the engine and the machine, decides the options that depend on them,
  and returns one `Decision` per option — field, value, reason, how to force it otherwise. Standard library
  only, as the rest of the package.
- `ctxproxy/engines.py`: `Engine` now carries `version`, `parallel`, `upstream` and `machine`; `detect()`
  fills them from endpoints it already called; `setup()` runs the decisions right after `apply()`.
- `ctxproxy/check.py`: a new `auto config` line, the decisions applied to the configuration the rest of the
  report describes, `--explain-config` for the full picture, and `auto_config` in `--json`.
- `ctxproxy/server.py`: the applied choices in the startup log and one `auto_config` event in `journal.jsonl`.
  The session-folder creation moved *after* engine detection, so it sees the options that were just chosen.
- `ctxproxy/core.py`: `auto_config` (true) and `state_kb_per_token` (19.5).
- `tests/test_autoconfig.py`: 37 tests. Full suite: **809 tests, OK (20 skipped)**, Python 3.14.7.

## What is read, and from where

Everything comes from endpoints and files the proxy already uses. No new dependency, no telemetry, no
required external tool.

| read | source | verified on |
|---|---|---|
| engine version | Strata `/v1/status` → `engine`; llama-server `/props` → `build_info` | Strata 0.1.51 source, `serve/server.py:4709` (`info["version"]`) and `:6957` (`build_info`) |
| context window | `/v1/status` → `context.native`, `/props`, `/slots` | unchanged from 0.3.8 |
| requests served at once | Strata `/v1/status` → `concurrency`; llama-server `/props` → `total_slots` | Strata `serve/server.py:4714` |
| local or remote engine | the `--upstream` URL (`engines.upstream_is_local`) | unchanged from 0.3.22 |
| RAM total/available | Linux `/proc/meminfo`, macOS `sysctl hw.memsize` + `vm_stat`, Windows `GlobalMemoryStatusEx` via `ctypes` | Linux measured; the other two are code paths with no CI runner here |
| free disk of `slot_dir` | `shutil.disk_usage` | measured |
| VRAM (optional) | the GPU figures Strata already reports in `/v1/status` (`machine.gpu`), otherwise `nvidia-smi` if on PATH | measured against the fake engine's `machine.gpu` |

**Why the Strata `concurrency` read matters.** Reading Strata 0.1.51 (`/mnt/mneme-nvme/strata-0150/control`,
`VERSION` 0.1.51, commit 562a0c2) confirms `slot_action()` answers **501** before touching the engine in two
cases: `--slot-save-path` not given, and batch slots in use (*"slot save/restore is not available with
parallel requests"*). It answers **400** for an unknown action, also before the engine. So a probe would be
safe on real Strata — but PolyStrata's `slot_action()` rejects an unknown action with 400 regardless (the
ambiguity already documented in `engines.py`), so a probe still could not tell "supported" from "rejected
early". Instead of probing, the `concurrency` field in `/v1/status` is read: more than one slot means saved
state is unavailable, with no request that could change anything. The `batch == 1` corner case stays
undetectable and is left to the existing runtime handling in `savedstate.py`. llama-server is *not* subject to
this rule: with `--parallel N` the proxy pins `id_slot` and save/restore keep working.

## What is decided

Only when the user did not write the field. An explicit value always wins and is reported as `written in the
config`.

| field | rule |
|---|---|
| `slot_save`, `mask_anchor`, `autosave` | engine offers saved state **and** engine is on this host **and** `slot_dir` is set **and** one state file fits in the available RAM. `autosave` additionally needs `state_file + autosave_min_free_gb` of free disk |
| `response_floor` | `window / 8`, from a 16384 window up |
| `notes_max_tokens` | `window * 3 / 32`, from a 16384 window up |
| `engine_tokenize` | the engine has `/tokenize` and no `--tokenizer` was given |

`slot_dir` is deliberately a precondition and not something the proxy invents: without it old anchors and
autosaves could never be cleaned up, and the proxy cannot guess the engine's `--slot-save-path`. The practical
effect is that the recommended saved-state setup is now **one** field in the config file.

### The size number

A saved state file costs about `state_kb_per_token` KB per token of window. The default, **19.5 KB/token**, is
the 20000 bytes/token `autosave.py` already uses for its own disk check (`ctxproxy/autosave.py`, `per_tok =
20000`), so the two parts of the proxy agree instead of carrying two different estimates — a test asserts they
stay in step. The measured spread is wide and is not hidden: 37–73 KB/token for Qwen3.5 35B-A3B and 7.6–9
KB/token for gemma E2B (`ROUTER-MODE-RESULT.md`), ~115 KB/token for llama-server with Qwen3-0.6B
(`ENGINES-RESULT.md`). That is why it is a configuration field and why the rule is a **floor, not a promise**:
at 19.5 KB/token a 131072-token window needs ~2.4 GiB, and with less than that available the saved-state
options stay off *and say why*.

Nothing is decided from VRAM. It is read when it is free and printed as evidence, because no rule here has a
defensible VRAM threshold yet — inventing one would be worse than printing the figure and leaving the choice.

## What is deliberately NOT decided

A conversation that already exists must be replayed byte for byte: the receipts, the tool names and
`prompt_language` are stored per conversation at first use precisely so that a new default cannot rewrite
history. Every remaining README recommendation changes what the proxy *writes into the prompt*, so flipping its
default would change the prompt of conversations that already exist:

- `mask_reasoning`, `mask_tool_args`, `recall_struct`, `recall_flex`, `recall_multi`, `tools_paging` — they
  need the same per-conversation storage as the receipts before they can become defaults. The README now says
  this instead of leaving a bare bold **true** that looked like an oversight.
- `seal_experimental`, `kv_archive`, `live_dump` — no prompt effect, but each spends something that should be
  asked for: an experimental save trick, a background compression thread, and the full prompt written to disk.
  The README's "recommended" column now carries the reason for each.

A test (`test_nothing_that_changes_the_prompt_is_touched`) pins all sixteen of those fields against any future
rule that tried to touch them.

## Measured output

Fake engine (`python -m ctxproxy.fake_engine`), no model. The machine is this host: 26.8 GiB RAM, a GPU the
fake Strata reports as 12 GiB.

### Strata + `slot_dir`: the recommended setup, with no edit

```
$ mnemonic-proxy check --explain-config --upstream http://127.0.0.1:18901 --config config.slotdir.json
✅ engine type        strata (detected)
✅ context window     window from engine n_ctx=131072; thresholds scaled: mask at 80000 -> 40000 tokens, protected tail 16000
ℹ️  auto config        chosen for this engine and this machine: slot_save=true, mask_anchor=true, autosave=true,
                      response_floor=16384, notes_max_tokens=12288 (RAM 26.8 GiB total, 19.1 GiB available;
                      VRAM 12.0 GiB total, 4.0 GiB free [ram: /proc/meminfo, vram: engine /v1/status machine.gpu])
✅ saved state        supported by the engine, configured in the proxy (slot_save, mask_anchor, autosave)

automatic configuration (a field written in the config always wins):
  engine: strata 0.1.51, window 131072, saved state available, on this host
  machine: RAM 26.8 GiB total, 19.1 GiB available; VRAM 12.0 GiB total, 4.0 GiB free
  auto slot_save = true: the engine saves its state, slot_dir is on this host, and the estimated session file
      (2.4 GiB at 19.5 KB/token, the 20000 bytes/token of autosave.py) fits: 19.1 GiB available
      -> set the saved-state options to false in the config to keep them off
  auto mask_anchor = true: same reason as slot_save above
  auto autosave = true: same reason as slot_save above
  auto response_floor = 16384: an eighth of the 131072-token window: a short replay no longer forces a
      segment switch (16384 at 131072, the README value)
      -> set "response_floor" in the config (0 = no floor)
  auto notes_max_tokens = 12288: three thirty-seconds of the 131072-token window for the handoff notes
      -> set "notes_max_tokens" in the config

READY (exit 0)
```

In 0.3.22 the same config gave `⚠️ saved state  supported by the engine, not enabled in the proxy config`
(exit 2) until the user copied five fields from the README.

### Strata without `slot_dir`: nothing on, and the one field that changes it

```
ℹ️  auto config        chosen ...: response_floor=16384, notes_max_tokens=12288 (...)
⚠️  saved state        supported by the engine, not enabled in the proxy config: the proxy works without it ...
                      -> optional, later: restart Strata with --slot-save-path DIR (Strata >= 0.1.40) and add
                         "slot_dir": "DIR" to the config; the proxy then turns slot_save, mask_anchor and
                         autosave on by itself (see `check --explain-config`)
  kept slot_save = false: slot_dir is not set, so old state files could never be cleaned up
      -> set "slot_dir" to the engine's --slot-save-path folder and the proxy turns saved state on by itself
```

### llama-server + `slot_dir`: the same, plus the exact token count

```
  engine: llama.cpp 6123 (fake), window 131072, saved state available, on this host
  auto engine_tokenize = true: the engine counts tokens exactly (POST /tokenize): used instead of the
      chars/3.5 estimate
      -> set "engine_tokenize": false, or pass --tokenizer for a local exact count
```

### A generic OpenAI engine: nothing invented

```
  engine: openai (version not reported), window 131072, saved state not available, on this host
  machine: ... VRAM not known (optional: no nvidia-smi and the engine does not report it) [ram: /proc/meminfo]
  kept slot_save = false: the engine (openai) does not offer saved state
```

### `"auto_config": false`

```
ℹ️  auto config        off ("auto_config": false): every default is used as written
  automatic configuration off ("auto_config": false): every default is used as written
```

### The real server: the banner and the journal

```
$ mnemonic-proxy --upstream http://127.0.0.1:18911 --port 18912 --data ./data --config config.slotdir.json
[mnemonic-proxy] 127.0.0.1:18912 -> http://127.0.0.1:18911 (engine strata, saved state yes) mode=tools window=131072 tokens=chars/3.5 anchor=True
[mnemonic-proxy] auto config: slot_save = true, the engine saves its state, slot_dir is on this host, and the
  estimated session file (2.4 GiB at 19.5 KB/token, ...) fits: 19.2 GiB available (to force it otherwise: ...)
[mnemonic-proxy] auto config: mask_anchor = true, same reason as slot_save above
[mnemonic-proxy] auto config: autosave = true, same reason as slot_save above
[mnemonic-proxy] auto config: response_floor = 16384, an eighth of the 131072-token window: ...
[mnemonic-proxy] auto config: notes_max_tokens = 12288, three thirty-seconds of the 131072-token window ...
```

`journal.jsonl`, one event, machine facts included so a later report can be read without the machine:

```json
{"event": "auto_config",
 "machine": {"ram_total_mb": 27437, "ram_free_mb": 19696, "vram_total_mb": 12288, "vram_free_mb": 4096,
             "sources": {"ram": "/proc/meminfo", "vram": "engine /v1/status machine.gpu"}},
 "decisions": [{"field": "slot_save", "value": true, "why": "...", "changed": true, "override": "..."}, ...]}
```

Note `saved state yes` and `anchor=True` in the banner: the banner and the session-folder creation now run
after the decisions, so the log describes the configuration the proxy really runs with.

## One behaviour change found by the tests, and what was done about it

`tests/test_recall_small_window.py` replays a measured 8K scenario (ds4, `--ctx 8192`,
`SEARCH-KEEPS-EVIDENCE-RESULT.md`). With `response_floor` chosen as `window / 8 = 1024` the scenario changed:
a third `recall` round became possible where the dev note had measured two. Isolating the cause (each decided
field reverted in turn) confirmed `response_floor`, not `notes_max_tokens`, was responsible.

Rather than re-baseline a measured scenario to fit a new default, the rule was bounded: the two window-sized
budgets are decided only from `engines.MIN_WINDOW` (16384) up. Below that the proxy already refuses to start
without `MNEMONIC_PROXY_ALLOW_SMALL_WINDOW`, and the README's documented choice there is the scaled defaults.
At 32768 the rule gives `response_floor` 4096 and `notes_max_tokens` 3072 — 4096 is exactly the value this
README already suggested for 32K, which is a good sign for the rule rather than a coincidence engineered after
the fact.

## What is not proven here

- The macOS and Windows RAM readers are written against their documented APIs and are exercised only on the
  platform the test runs on (`test_ram_is_read_on_this_platform`); they have not been run on macOS or Windows.
  A failure there degrades to "RAM unknown", which does not block any decision.
- 19.5 KB/token is a heuristic, not a per-model measurement; the real figure for a given model can be three to
  six times larger. The RAM check is therefore a floor. A per-model refinement would be
  `router.bytes_per_token`-style measurement reused across restarts: a separate change.
- No rule uses VRAM. If a VRAM rule is wanted, it needs a measurement of what the engine actually keeps on the
  card versus what it spills, which is engine-specific and outside this change.
- `seal_experimental`, `kv_archive` and `live_dump` are still off by default. Their recommendation was
  *corrected* (the reason is now written down), not implemented.
- Making the prompt-shaping recommendations into defaults needs per-conversation storage like the receipts.
  That is the obvious follow-up, and it is not done here.
