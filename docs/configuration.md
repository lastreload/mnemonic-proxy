# Configuration

Output limits in managed mode: `max_completion_tokens` takes precedence over `max_tokens` when both are supplied;
without either field, `default_response` is used (16384 by default). These values are ceilings, not a reason to
switch segments. After masking and any necessary switch, the proxy caps the response to
`window - estimated_prompt - reserve - 8` and sends the reduced limit to the engine (both fields are synchronized
when present). The final window guard may reduce it further for its margin and estimate slack; it refuses a
prompt with no space for even one generated token. Tokenizer-based bounds require the current model's tokenizer
and matching template/settings (see Window guard above); estimates are not proof of actual engine token usage.

A genuinely large prompt still triggers a segment switch when there is not enough room for a useful response:
`max(response_floor, min(requested_output, default_response))`, bounded to at least one token and at most a quarter
of the window. A floor never raises the actual output ceiling requested by the client. Every reduction produces
`response_clamped` in `journal.jsonl` (requested and sent limits, prompt estimate, window and reserve), plus one
warning per conversation in the process log, remembered across restarts. No new option is required; for example,
a client ceiling of 120000 in a 131072 window no longer causes a switch on every short turn.

**Small windows (including Strata/PolyStrata at 32K).** Unset thresholds scale when `window` is read from the
engine's `/v1/status` (`context.native` / `max_positions`). Oversized explicit `response_floor` and
`notes_max_tokens` are capped at one quarter of the window at startup, with warnings also shown by `check`.
With masking anchors enabled below 128K, `anchor_min_tokens` is capped at `24000 * window / 131072` (6000 at 32K)
so a 128K anchor threshold does not prevent useful anchors on a small window. A Strata `window` override larger
than its reported context is lowered to that context with a warning (`check` reports the mismatch).
These are budget/threshold adaptations, not token-count repairs; the final guard still handles every call.
For 32K, prefer removing these three overrides to get scaled defaults, or start conservatively with
`"response_floor": 4096, "notes_max_tokens": 2048, "anchor_min_tokens": 6000`. Keep `window` unset when discovery
works, and check the startup log for `window from engine n_ctx: 32768`. The 128K recommendations below are not
32K settings. Existing values within these bounds are unchanged.

## Automatic configuration

> *"Why is the default config not the recommended config? It would be so nice if it could detect the engine
> version, context size, VRAM + RAM and apply the best settings by itself."* — a user on r/LocalLLM, who had
> turned saved state on by hand and then found a 2 GB state file eating the RAM he needed for the model.

Since 0.3.23 the proxy looks, decides, and says what it decided. At startup (and in `check`, without starting
anything) it reads the engine and the machine, then sets the options that depend on them. **A field written in
the config file or on the command line always wins and is never touched.**

What is read — all of it from endpoints and files the proxy already uses, no new dependency, no telemetry:

| read | where from |
|---|---|
| engine kind, context window, saved-state support | `/v1/status` (Strata: `context.native`), `/props` + `/slots` (llama-server), as before |
| engine version | Strata `/v1/status` → `engine` (e.g. `0.1.51`); llama-server `/props` → `build_info` |
| requests served at once | Strata `/v1/status` → `concurrency`; llama-server `/props` → `total_slots` |
| local or remote engine | the `--upstream` URL |
| RAM, total and available | Linux `/proc/meminfo`, macOS `sysctl hw.memsize` + `vm_stat`, Windows `GlobalMemoryStatusEx` |
| free disk of `slot_dir` | `shutil.disk_usage` |
| VRAM (optional, never required) | the GPU figures Strata already reports in `/v1/status`, otherwise `nvidia-smi` if it happens to be on PATH |

What is decided, and the rule behind it:

| field | chosen when | why that rule |
|---|---|---|
| `slot_save`, `mask_anchor`, `autosave` | the engine offers saved state, it runs on this host, `slot_dir` is set, and one state file fits in the available RAM | these three only move engine state files: they never change a byte of the prompt. `slot_dir` is required because without it old anchors and autosaves could never be cleaned up; `autosave` also needs `state_file + autosave_min_free_gb` of free disk |
| `response_floor` | the window is 16384 or more: `window / 8` (16384 at 131072, 4096 at 32768 — the values this README already suggested) | the recommended value, scaled. Below 16384 the proxy already refuses to start without an explicit opt-in and the documented choice there is the scaled defaults, so nothing is decided |
| `notes_max_tokens` | same range: `window * 3 / 32` (12288 at 131072) | same |
| `engine_tokenize` | the engine has `/tokenize` and no `--tokenizer` was given | an exact count from the engine beats the `chars_per_token` estimate; a local tokenizer, when present, beats both |

The size rule has one number: a saved state file costs about **19.5 KB per token** of window
(`state_kb_per_token`), so a 131072-token window is roughly 2.4 GiB of engine state. That default is the
20000 bytes/token `autosave.py` already uses for its own disk check, so the two parts of the proxy agree; the
measured spread is wide — 37–73 KB/token for Qwen3.5 35B-A3B and 7.6–9 KB/token for gemma E2B
([dev note](dev-notes/ROUTER-MODE-RESULT.md)), ~115 KB/token for llama-server with Qwen3-0.6B
([dev note](dev-notes/ENGINES-RESULT.md)) — which is why it is a field and why this is a floor, not a
promise. With 1 GiB available the saved-state options stay off and say so, instead of filling the RAM the model
needs. Nothing is decided from VRAM: it is printed as evidence, because no rule here has a defensible
VRAM threshold yet.

`check --explain-config` prints the whole picture — what was read, what was chosen, what was left alone, and
how to force each one otherwise — without starting the proxy:

<!-- readme-test: illustrative -->
```
$ mnemonic-proxy check --explain-config --upstream http://127.0.0.1:8095 --config config.json
...
ℹ️  auto config        chosen for this engine and this machine: slot_save=true, mask_anchor=true, autosave=true,
                      response_floor=16384, notes_max_tokens=12288 (RAM 31.3 GiB total, 19.4 GiB available;
                      VRAM 12.0 GiB total, 4.0 GiB free [ram: /proc/meminfo, vram: engine /v1/status machine.gpu])
✅ saved state        supported by the engine, configured in the proxy (slot_save, mask_anchor, autosave)

automatic configuration (a field written in the config always wins):
  engine: strata 0.1.51, window 131072, saved state available, on this host
  machine: RAM 31.3 GiB total, 19.4 GiB available; VRAM 12.0 GiB total, 4.0 GiB free
  data folder: new data folder: the automatic choices are applied and recorded in autoconfig.json (the data
      folder does not exist yet)
  auto slot_save = true: the engine saves its state, slot_dir is on this host, and the estimated session file
      (2.4 GiB at 19.5 KB/token, the 20000 bytes/token of autosave.py) fits: 19.4 GiB available
      -> set the saved-state options to false in the config to keep them off
  auto response_floor = 16384: an eighth of the 131072-token window: a short replay no longer forces a
      segment switch (16384 at 131072, the README value)
      -> set "response_floor" in the config (0 = no floor)

READY (exit 0)
```

The same lines appear in the startup log as `[mnemonic-proxy] auto config: …`, and in `journal.jsonl` as one
`auto_config` event (its `data_folder` field carries the case below). `"auto_config": false` turns the whole
mechanism off: every default is then used exactly as written in the table below.

**Upgrades keep what was running; a restart keeps what this installation chose.** What the automatic
configuration does depends on **where the data folder comes from**, which is recorded in the folder itself
(`autoconfig.json`) and is kept apart from the presence of conversations. `check` and the startup log always
name the case in one line (`data folder origin`):

| the data folder | what happens | the line |
|---|---|---|
| **new** — not there yet, or empty | the choices are applied, and `autoconfig.json` records this version, the window and the values applied | `new data folder: the automatic choices are applied and recorded in autoconfig.json` |
| **created with the automatic configuration** — it carries `autoconfig.json`, or its journal shows that its first start was a 0.3.23 start that applied the choices (0.3.23 wrote no mark; the next start writes it) | the choices keep being applied at every start, conversations or not | `data folder created with the automatic configuration: the choices stay applied` |
| **from an earlier setup** — it holds files but no `autoconfig.json`, or the mark cannot be read | the choices are only reported, marked `kept`, with the value the proxy would pick and why; nothing changes | `data folder from an earlier setup: the automatic choices are reported, not applied` |

Write `"auto_config": true` in the config to apply them on an earlier folder as well; delete `autoconfig.json`
to have a folder treated as an earlier one. The reason an earlier folder is protected: the proxy cannot tell
every engine apart (a PolyStrata release before v0.1.45-poly.7 ends the engine at the first save on a
`strata-poly` model, and it answers like Strata), so an **upgrade** must never turn saved state on by itself.
That is a statement about installations that predate the automatic configuration, so it is now decided by the
provenance of the folder and not by "does this folder have conversations?" — up to 0.3.23 the second question
made the proxy apply `response_floor = 14375` on a brand-new folder and then go back to `0` after the first
archived conversation, so the same installation ran with different values at every restart (reported by the
first Windows user, window 115000).

Three details worth knowing:

- **An old folder whose archive happens to be empty is treated as an earlier folder**, not as a new one: the
  question is where the folder comes from, and an empty archive does not answer it. Same for a folder that
  cannot be listed and for an `autoconfig.json` that cannot be read — the conservative case wins. An
  `auto_config` event in an old `journal.jsonl` is not a consent either.
- **`check` only reads the provenance.** It never writes `autoconfig.json`, so running `check` before the
  first start cannot make an earlier folder look automatic.
- **The choices are recomputed at every start, and may change** — on an "automatic" folder they are then
  applied. Every gate is re-evaluated (engine support and version, local or remote, parallel slots, free RAM,
  free disk), so a machine that has less RAM available than it had yesterday makes `slot_save`/`autosave` go
  back to `false`: switching **off** is always safe, it only costs the saved-state speed-up. The opposite
  direction — switching saved state **on** where it was off — is only ever done on a new folder or on a folder
  this mechanism created itself, never on an earlier one, which is exactly the PolyStrata guarantee above; on
  such a folder it is the same decision the first start already took, with the same gates. The mark records
  the values that were applied, so `autoconfig.json` also tells you what the previous start chose. If your
  engine is one the gates cannot recognise, `"slot_save": false` (or `"auto_config": false`) in the config
  wins over all of this, at every start.

The measured output of all of this, engine by engine, is in the
[dev note](dev-notes/AUTOCONFIG-RESULT.md).

**Why the rest of the recommended config is still not the default.** The proxy guarantees that an old
conversation is replayed byte for byte: the receipts, the tool names and `prompt_language` are stored per
conversation at first use precisely so that a new default cannot rewrite history. Every remaining
recommendation changes what the proxy *writes into the prompt*, so flipping its default would change the
prompt of conversations that already exist. These stay opt-in on purpose, and the reason is now in the table:

- `mask_reasoning`, `mask_tool_args`, `recall_struct`, `recall_flex`, `recall_multi`, `tools_paging`: they
  change the prompt the engine sees. Turn them on when you start a new conversation (that is what the
  benchmarks above use); they need the same per-conversation storage as the receipts before they can become
  defaults.
- `seal_experimental`, `kv_archive`, `live_dump`: no prompt effect, but each one spends something the proxy
  should not spend without being asked — an experimental save trick, a background compression thread, and the
  full prompt written to disk for the dashboard.

`--config` takes a JSON object with any field of `ctxproxy.core.Config`. Defaults below are the real ones in the
code. The last column is what we recommend with an engine that has saved state
(`examples/config.example.json` plus the options the benchmarks above support); **chosen automatically** marks
the ones the proxy now picks for you when the engine and the machine allow it.

| field | default | meaning | recommended |
|---|---|---|---|
| `window` | 131072 | physical context of the engine (llama-server, ds4, Strata, Ollama, LM Studio: taken from the engine if unset; Ollama / LM Studio: a bigger value refuses the start) | 131072 |
| `window_margin`, `window_estimate_slack` | 64, 0 (0.15 on generic engines without `--tokenizer`) | window guard: tokens kept free; inflation of the estimated prompt count | default |
| `image_tokens` | 1026 | tokens charged for **one picture**: the guard budgets each vision marker with this upper bound, because the engine expands it afterwards and reports the real number only in `usage.prompt_tokens`. Must satisfy `image_tokens >= vision.max_tokens + 2` (two of the marker's three special tokens stay in the prompt). A non-negative integer, checked when the configuration is loaded; `0` means "no pictures": a request carrying one is refused, and the startup says so | `vision.max_tokens` + 2 (Strata clamp 300 → **302**; the default 1026 covers the GPU clamp of 1024) |
| `mask_trigger` / `mask_target` | 80000 / 40000 | start a masking block above trigger, mask oldest-first down to target | default |
| `min_batch_tokens` | 16000 | a block runs only if it frees at least this much | default |
| `keep_recent_tokens` / `min_age_turns` | 16000 / 2 | protected tail, never masked | default |
| `mask_reasoning` / `mask_tool_args` | false / false | also mask old reasoning / large old tool-call arguments | **true / true** (opt-in: they change the prompt, see [Automatic configuration](#automatic-configuration)) |
| `mask_anchor`, `anchor_min_tokens` | false, 24000 | save/restore engine state at the masking frontier (saved state) | **chosen automatically**, 24000 |
| `slot_dir` | "" | the engine's `--slot-save-path` folder, same host (anchor cleanup, autosave, `kv_archive`) | set (one field: the saved-state options then follow by themselves) |
| `segments_enabled`, `tail_max`, `notes_max_tokens` | true, 24000, 8192 | segment switch with handoff notes | true, 24000, **chosen automatically** (`window * 3 / 32`) |
| `slot_save` / `seal_experimental` | false / false | save the old segment to disk before switching (saved state) | **chosen automatically** / **true** (opt-in: experimental) |
| `response_floor` | 0 | minimum useful response budget for segment switching (bounded to a quarter of the window) | **chosen automatically** (`window / 8`) |
| `auto_config`, `state_kb_per_token` | true, 19.5 | choose the engine/machine-dependent options at startup and print every choice with its reason (`check --explain-config`); KB of engine state per token of window (the 20000 bytes/token of `autosave.py`), used to check a state file fits in the available RAM | default |
| `inject_recall`, `max_recall_rounds`, `recall_max_tokens` | true, 4, 6000 | the `recall` tool | default |
| `recall_tool_name` / `tools_tool_name` | "recall" / "tools" | names of the proxy's tools for new conversations | default |
| `prompt_language` | "en" | language of the texts the proxy writes into the prompt, for NEW conversations: `"en"` or `"it"`; stored per conversation at first use; conversations from before 0.3.1 stay Italian | default |
| `rich_receipts` | false | opt-in (benefit not yet measured): richer receipt for a hidden tool output, for NEW conversations: output size in lines/bytes, error type and the FIRST error line (160 characters), plus a receipt for outputs whose kind is not recognised; stored per conversation at first use, so a conversation from before 0.3.22 keeps the receipts it was masked with | default |
| `recall_struct`, `recall_flex`, `recall_multi` | false, false, false | structured recall: file timelines, links, flexible words, several queries per call (the benchmark above) | **true, true, true** (opt-in: they change the prompt) |
| `auto_recall` / `auto_recall_hint` | false / false | `auto_recall` enables automatic retrieval on new user messages; `auto_recall_hint` only selects a one-line hint with ids instead of pieces and requires `auto_recall: true` | false / false (automatic retrieval off) |
| `recall_leaf_select` | false | on structured outputs (yaml/toml (Python ≥ 3.11)/xml/json, tables, diffs, logs, git log, pytest) `recall` returns only the matching record and the key lines of its ancestors, verbatim, with `⋮` markers and offsets for the gaps; falls back to the normal recall when unsure ([dev note](dev-notes/LEAF-SELECT-RESULT.md)) | default (experimental) |
| `answer_thinking_after_recall`, `answer_thinking_budget` | false, 512 | turn thinking on only for the answer that follows a recall (fixes answers cut in the middle of a value) | default (experimental) |
| `truncation_guard` | false | replay once, with thinking, a short answer that ends on a prefix of a recalled token | default (experimental) |
| `tools_paging`, `tools_core` | false, read/bash/edit/write/grep/find/ls + read_file/write_file/patch/search_files/terminal | tools on demand: only core tools + `recall` + `tools` in the prompt. The second group is Hermes' base names (0.3.24): without them the whole Hermes tool list went to the catalog and only `recall` and `tools` stayed in the head. A name the client does not have changes nothing | **true** (opt-in: it changes the prompt) |
| `typed_receipts`, `receipt_guard` | true, true | typed one-line receipts for hidden outputs; block write/edit/shell calls (pi, Claude Code, Codex names) that copy a receipt or the shortened form of an old argument | default |
| `pins_max_tokens` | 8192 | cap of the carried-over 📌 block | default |
| `autosave`, `autosave_idle_s`, `autosave_keep`, `autosave_max_gb`, `autosave_min_free_gb` | false, 180, 10, 25, 8 | save engine state when the conversation is idle (saved state) | **chosen automatically**, 180, 10, 25, 8 |
| `autorestore`, `autorestore_min_gain` | true, 8192 | restore before forwarding when it saves at least this many tokens | default |
| `kv_archive`, `kv_archive_idle_s`, `kv_archive_min_age_s` | false, 600, 1800 | cold archive of state files (needs Python ≥ 3.14 for `compression.zstd`) | **true** (opt-in: a background compression thread) |
| `engine`, `slot_id`, `engine_tokenize` | "auto", 0, false | engine type, slot, token counting via the engine's `/tokenize` | auto, 0, **chosen automatically** |
| `router_model`, `router_load_timeout_s`, `router_keepalive_s` | "", 600, 10 | llama-server router mode: model used when the client's `model` is not served (empty = refuse), how long a model may take to load, seconds between two SSE comments to a streaming client while the model loads | default |
| `live_dump` | false | write `data/live/last_request.json` for the dashboard | **true** (opt-in: it writes the full prompt to disk) |

A config for llama-server with saved state, as recommended above:

<!-- readme-test: illustrative; config: proxy -->
```json
{
  "mask_reasoning": true, "mask_tool_args": true,
  "mask_anchor": true, "slot_dir": "/path/to/slots",
  "slot_save": true, "seal_experimental": true,
  "notes_max_tokens": 12288, "response_floor": 16384,
  "recall_struct": true, "recall_flex": true, "recall_multi": true,
  "auto_recall": false,
  "tools_paging": true,
  "autosave": true
}
```

The recommended config above and `examples/config.ds4.json` keep automatic retrieval off
(`auto_recall: false`). The 100-question AgentLongBench confirmation run also used no automatic
retrieval: its journals contain 379 model-requested `recall` calls and zero `auto_recall` events.
`auto_recall_hint: true` alone does not enable hints: it only changes the mode when
`auto_recall: true` is also set. To opt into automatic hints, set both options to `true`.
The previously listed hint setting was inert and has been removed from the recommended examples;
this documentation correction does not enable automatic retrieval or change their effective behavior.

On a generic OpenAI engine drop the saved-state fields (`mask_anchor`, `slot_dir`, `slot_save`,
`seal_experimental`, `autosave`, `kv_archive`); the proxy turns them off anyway, with a warning.

## Every command-line option

Three commands are installed: `mnemonic-proxy` (the proxy, with its `demo`, `check`, `init-config`
and `forget` subcommands; `ctxproxy` is a 0.1 alias of the same program) and `mnemonic-mcp` (the
read-only MCP recall server). `tests/test_readme_coverage.py` fails if an option exists in the code
and is not listed here.

| command | option | what |
|---|---|---|
| `mnemonic-proxy` | `--upstream URL` | the engine's address (default `http://127.0.0.1:8095`) |
| | `--host ADDR` | bind address (default `127.0.0.1`, local only) |
| | `--port N` | listen port (default `8096`) |
| | `--data DIR` | archive, journal and lock (default `./data`) |
| | `--config FILE` | JSON file with the `Config` fields below |
| | `--mode tools\|off` | `tools` = managed context (default); `off` = plain pass-through, nothing archived or masked, useful to compare a session with and without the proxy |
| | `--tokenizer PATH` | Hugging Face `tokenizer.json` (or a Strata pack `tokenizer/` folder) for exact token counts; needs `pip install 'mnemonic-proxy[exact]'` |
| | `--api-key KEY` | key sent to the upstream engine |
| | `--engine auto\|strata\|llama.cpp\|ds4\|openai` | force the engine type instead of detecting it |
| | `--version` | print the version and exit |
| `mnemonic-proxy demo` | `--keep DIR` | keep the demo's archive in `DIR` (`archive.sqlite`, `journal.jsonl`) instead of a temporary folder, to look at it afterwards or to point `mnemonic-mcp --db` at it |
| `mnemonic-proxy check` | all the server options | `check` reads the configuration the server would run with |
| | `--live` | also run a real generation, a tool call and a save/restore on the engine |
| | `--json` | machine-readable report: `{"ready", "exit_code", "checks": [...], "auto_config": {...}}`, for a script or a health probe |
| | `--explain-config` | print the automatic configuration in full: what was read about the engine and the machine, every option chosen (and every one left alone) with its reason and how to force it otherwise ([Automatic configuration](#automatic-configuration)) |
| `mnemonic-proxy init-config` | `--engine NAME` | which engine the configuration is for: `strata`, `llama-server`, `ds4`, `openai-32k`, `official-strata`, `llama-server-32k` or `llama-server-router` (required; `--help` lists what each one is for) |
| | `--dir DIR` | folder to write the file in, created when missing (default `.`) |
| | `--name FILE` | file name to write (default `config.json`) |
| | `--force` | overwrite an existing file; without it nothing is written and the exit code is 1 |
| `mnemonic-proxy forget` | `--data DIR` | the data folder to delete from (required) |
| | `--list` | list conversation ids, logical sizes and UTC timestamps |
| | `--conv ID` | the conversation to remove |
| | `--dry-run` | report what would be removed, change nothing (requires `--conv`) |
| | `--slot-dir DIR`, `--kv-archive DIR` | saved-state and cold-archive folders, when they are not in the config |
| | `--config FILE` | the proxy's config; defaults to `$MNEMONIC_CONFIG` |
| `mnemonic-mcp` | `--db FILE` | the proxy's `archive.sqlite`, opened read-only (required) |
| | `--journal FILE` | `journal.jsonl`; by default the one next to the database |
| | `--max-tokens N` | token cap of one recall result (default `6000`) |
| | `--tokenizer PATH` | tokenizer for those token counts |
| | `--http PORT` | streamable HTTP on `/mcp` instead of stdio |
| | `--host ADDR` | bind address for `--http` (default `127.0.0.1`) |

## Every configuration field

The table above covers what most setups need. These are the remaining fields of
`ctxproxy.core.Config`, with their real defaults; they exist for tuning and for reproducing our
measurements, and changing them is rarely necessary.

<details>
<summary>The remaining configuration fields</summary>

| field | default | meaning |
|---|---|---|
| `mask_enabled` | true | level 1 on/off: with false nothing is ever masked (the archive still records everything) |
| `min_mask_tokens` | 200 | outputs shorter than this always stay in clear text |
| `data_dir` | "" | where the physical prompt of the outgoing segment is written when `slot_save` is on; empty = the `--data` folder |
| `index_max_tokens` | 2000 | cap of the segment index block |
| `keep_first_user` | true | carry the first user message over to a new segment |
| `recall_max_chars` | 24000 | character cap of one recall result (the token cap applies as well) |
| `recall_struct_max_tokens` | 3500 | token cap of a structured recall result (`recall_struct`) |
| `truncation_guard_max_tokens` | 16 | only answers up to this many tokens are replayed by `truncation_guard` |
| `truncation_guard_budget` | 512 | reasoning cap of that replay (0 = no cap) |
| `answer_thinking_strip` | true | the reasoning of those replays is not returned to the client |
| `tooldefs_desc_max` | 0 | shorten each tool's description to this many characters (0 = off) |
| `tooldefs_param_max` | 0 | the same for every parameter description, nested ones included |
| `tooldefs_keep` | `recall`, `read`, `bash`, `edit`, `write`, `grep`, `find`, `ls` | tools whose definitions are never shortened |
| `tools_load_max` | 5 | how many tool definitions one `tools` call may load (`tools_paging`) |
| `journal_tool_heads` | true | journal one `tool_heads` line per conversation and per tool-list change: names and token weight of the definitions in the prompt head (names and numbers only, never text) |
| `auto_recall_on_error` | false | automatic retrieval also after a failed tool result |
| `auto_recall_k` | 4 | pieces at most per injection |
| `auto_recall_max_tokens` | 4000 | cap of the injected message |
| `auto_recall_piece_tokens` | 1200 | cap of each piece |
| `auto_recall_min_score` | 8.0 | threshold on the FTS5 bm25 score |
| `auto_recall_min_terms` | 2 | distinct query terms a piece must contain (common ones excluded) |
| `auto_recall_common_frac` | 0.4 | a term appearing in more than this share of the hits counts as common |
| `auto_recall_max_args` | 1 | tool-call arguments at most per injection |
| `auto_recall_hint_k` | 5 | candidates named in a hint (`auto_recall_hint`) |
| `chars_per_token` | 3.5 | characters per token when there is no tokenizer |
| `msg_overhead` | 6 | tokens the chat template adds per message |
| `image_tokens` | 1026 | tokens assumed for one image (`vision.max_tokens` + 2) |
| `reasoning_last_turn_only` | false | Strata template: keep only the last turn's reasoning |
| `autosave_min_tokens` | 16384 | below this a re-read is cheaper than a state file: no save |
| `autosave_min_free_gb` | 8.0 | no save when the slot disk has less free space than this |
| `autosave_poll_s` | 10.0 | how often the autosaver looks at the engine |
| `kv_archive_min_bytes` | 67108864 | smaller state files are left where they are |
| `kv_archive_poll_s` | 60.0 | how often the cold archiver looks |
| `kv_archive_level` | 3 | zstd compression level |
| `kv_archive_threads` | 4 | zstd threads |
| `checkpoint_align_slack` | 0.25 | accepted waste after a checkpoint multiple (`checkpoint_align_tokens`) |
| `answer_min_tokens` | 512 | a thinking budget always leaves at least this much room for the answer |
| `recall_removal` | "value" | what the recall loop gives up first when space runs out: `value` \| `reasoning` \| `age` |
| `final_drop_internal_tools` | true | a final call made for lack of space goes without the proxy's own tools |
| `notes_thinking` | "auto" | thinking while writing handoff notes: `auto` (on when the engine leaves room) \| `off` \| `on` |

</details>

## Tool names and older conversations

The proxy's own tools are called `recall` and `tools` (0.1 called them `strata_recall` and `strata_tools`). The tool
list is part of the prompt prefix, so conversations already in the archive from 0.1 keep the old names — renaming
them would force a full re-read and break saved state. New conversations get the new names. A call to the old names
is always resolved. If the client already declares a tool called `recall` (or `tools`), the client's tool is left
alone and the proxy uses `history_recall` (or `load_tools`) instead.
