# Changelog

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
