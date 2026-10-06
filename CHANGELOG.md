# Changelog

## 0.3.0 — 2026-10-06

ds4-server as an engine, Claude Code and Codex measured live, and two fixes found in those live runs.

- **Breaking — engine windows below 16K are refused.** If the engine's window (`n_ctx` / `context_length`) or a
  `window` written in the config is below 16384 tokens, the proxy exits at startup (exit 1) with the window found,
  the minimum (16384), the recommended size (≥ 32768) and the command to restart the engine (`llama-server -c 32768`,
  `ds4-server --ctx 32768`); `check` and `check --live` report it as a failure (NOT READY). Reason: at 32K the ds4
  recall trap was answered right with both ds4 builds, at 8K it passed once, after the recall fix below; small windows
  stay excluded until proven on more runs. `examples/config.llama-server-8k.json` is replaced by
  `config.llama-server-32k.json` and the T1 quick start uses `-c 32768`. For development only (internal tests, small
  window experiments): `MNEMONIC_PROXY_ALLOW_SMALL_WINDOW=1` lets a smaller window through.
- **ds4-server** ([antirez/ds4](https://github.com/antirez/ds4)): detected from `GET /v1/models` (`owned_by: "ds4.c"`),
  window from `context_length`; `--engine ds4`. The proxy does not save engine state (ds4 has `--kv-disk-dir`),
  passes tool-call ids through unchanged in all three protocols (ds4 resumes by id) and turns `mask_tool_args` off.
  `examples/config.ds4.json`; `check` knows ds4. Tested with pi: GLM 5.3 Flash Q2 and Qwen3.8 Flash Next Q2 on a
  Ryzen AI Max+ 395 (see README, Engines). mnemonic-proxy is a separate tool, not part of the ds4 project.
- `response_floor` is limited to 1/4 of the window (small engine windows no longer switch segment on every request).
- **Fix — copied shortened arguments** (live with Claude Code: 3 writes out of 140 ended with the `…` line of a masked
  old argument; one caused a `NameError`). `receipt_guard` now also blocks a write/edit/shell call containing a line
  made only of `…`, and it knows Claude Code's and Codex's tool names (`Write`, `Edit`, `MultiEdit`, `NotebookEdit`,
  `Bash`, `apply_patch`, `exec_command`, `shell`; names compared case-insensitively). The masked form keeps its `…` line.
- **Fix — recall at small windows** (live with Qwen3.8 on ds4, 8K window: the 2nd–4th `recall` results were cut to
  almost nothing and the data never arrived). `recall` with `id` + `query` now returns the lines of that output that
  contain the query (one line of context, line number and character offset) instead of ignoring the query; before
  cutting a recall result the proxy first lowers the space reserved for the answer, down to `default_response`
  (journal event `response_shrunk`). Live (Qwen3.8, ds4, 8K): one `recall` by id + query brought the right line
  back; one run, with a first prompt that forces a plain `cat`.
- README and project page: Claude Code and Codex tested live on Strata (1 h 41 min, 92.6% / 93.9% of prompt tokens
  from the engine cache), ds4 measurements, what is not proven (reasoning masking, scattered facts at ≥ 64K).
- Reports in `docs/dev-notes/`: LIVE020-RESULT.md, DS4-RESULT.md, DS4-GLM-RESULT.md, DS4-QWEN-COMPARE-RESULT.md.

## 0.2.1 — 2026-10-06

First-run onboarding, tested literally in a clean Ubuntu 24.04 container.

- `mnemonic-proxy demo`: offline demo (fake model behind the real proxy: archive, masking, exact recall), no model,
  no GPU, ~1 minute. The fake engine now ships in the package (`ctxproxy/fake_engine.py`).
- `mnemonic-proxy check [--live]`: reads the same options/config as the server and says what is wrong (config fields,
  port, engine reachable/loaded/type, window, saved state, `slot_dir` vs `--slot-save-path`); `--live` runs a real
  generation, a tool call and a save/restore. Exit 0 ready, 2 ready with warnings, 1 not ready; `--json`.
- `/health` combines proxy and engine health (llama-server answers 503 while loading); new `/v1/engine`.
- Small windows: scaled limits have floors (recall/notes/default response ≥ 1024, auto-recall/pins ≥ 512).
- `examples/config.llama-server.json` (saved state, autosave) and `config.llama-server-8k.json` (thresholds for an
  8K engine window). Both keep the short `recall` tool: with `recall_struct`/`recall_multi` Qwen3-4B made 0/6 tool
  calls instead of 4–6/6.
- README: quick start in tiers (T0 demo, T1 llama.cpp + Qwen3-4B on CPU + pi, T2 real use), tested combinations,
  troubleshooting, platforms, uninstall.
- CI workflow (unit tests on 3.10/3.14, demo, check against the fake engine; real-model job on demand).
- `tests/test_kvarchive.py` skips on Python < 3.14 (no `compression.zstd`).

## 0.2.0 — 2026-10-05

Renamed: **virtual-context-proxy → Mnemonic Proxy** (`mnemonic-proxy`; future repository
`github.com/lastreload/mnemonic-proxy`). The Python package is still `ctxproxy`; the `ctxproxy` command is kept as an
alias of `mnemonic-proxy`.

Added

- **Tools on demand** (`tools_paging`): only the core tools + `recall` + `tools` stay in the prompt; the others are
  loaded by name through the `tools` tool, with the definition in the result (the prefix does not change).
  25.6K → 6.2K fixed tokens per request with pi's 49 tools.
- **Typed receipts + output guard**: hidden outputs become one-line receipts by kind (write/edit with byte count and
  sha256, shell ok/error, test summary, read range); write/edit/bash calls that copy a receipt or placeholder are
  blocked before they reach the client (`typed_receipts`, `receipt_guard`, both on).
- **Structured recall** (`recall_struct`, `recall_flex`, `recall_multi`): passage index, file `first`/`timeline`
  modes, `neighbors`/`output`/`reasoning` around an id, session `tree`, role/segment/range filters, split
  identifiers and light stemming, several queries per call. Held-out benchmark: evidence within 5 calls 90% → 100%,
  tokens per question 5,940 → 2,737.
- **Auto-recall hint** (`auto_recall_hint`): a one-line hint with ids instead of injected pieces (same coverage,
  ≈9× fewer tokens).
- **kv_archive**: cold archive of saved engine state, content-addressed zstd blocks with cross-file dedup; originals
  deleted only after a sha256-verified rebuild; rebuilt on demand before a restore (needs Python ≥ 3.14).
- **Engines**: detection at startup (`--engine auto|strata|llama.cpp|openai`); llama.cpp `llama-server
  --slot-save-path` with saved state (anchor, autosave/autorestore, kv_archive), window from `n_ctx`; generic
  OpenAI-compatible servers in base mode. Unsupported features are turned off with a warning.
- **Client ingress**: Anthropic Messages (`/v1/messages`, `/v1/messages/count_tokens`, Claude Code) and OpenAI
  Responses (`/v1/responses`, Codex), streaming and not.
- **MCP recall server** (`mnemonic-mcp`, `python3 -m ctxproxy.mcp_server`): read-only `recall`, `conversations`,
  `journal` over the archive, stdio or streamable HTTP.

Changed

- The proxy's tools are now **`recall`** and **`tools`** (were `strata_recall` and `strata_tools`). Conversations
  already in the archive keep the old names (the tool list is part of the prompt prefix); calls to the old names are
  always resolved. If the client declares a tool with the same name, the proxy uses an alternative
  (`history_recall`, `load_tools`, …). New options `recall_tool_name`, `tools_tool_name`.
- CLI help and startup log in English; `--version`.
- A clear error at startup when `kv_archive` is on and Python has no `compression.zstd`.

## 0.1.0 — 2026-10-04

First public release as `maverde73/virtual-context-proxy`: stable-frontier masking, exact archive + `strata_recall`,
segments with handoff notes, 📌/🗑, autosave/autorestore, exact token counting, streaming, dashboard.
