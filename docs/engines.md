# Engines and tested combinations

## Engines

| client \ engine | Strata | llama-server | ds4-server | any OpenAI-compatible |
|---|---|---|---|---|
| Chat Completions (pi, omp, Hermes, Continue…) | tested · full | tested on CPU · full | tested with pi (GLM 5.3, Qwen3.8) · engine keeps its own state | base (no saved state) |
| opencode (Chat Completions) | **tested** (1 h 32 min live session, 554 requests) | experimental | experimental | experimental |
| Claude Code (Anthropic Messages) | **tested** (1 h 41 min live session) | experimental | experimental | experimental |
| Codex (Responses) | **tested** (live, exact recall) | experimental | experimental | experimental |
| MCP recall server (Claude Code, Codex, Hermes) | tested read-only on a real archive | | | |

"Tested": real client, real engine, measured; reports in `docs/dev-notes/`. "Experimental": tested end to end
against a mock engine and with short real client runs, not yet on long real sessions.

- **llama.cpp** — start `llama-server` with `--slot-save-path DIR` for saved state (masking anchor, segment saved
  before a switch, autosave/autorestore, `kv_archive`). The window comes from the server's `n_ctx` unless `window` is
  set; with `--parallel N` the proxy pins one slot (`slot_id`, default 0). Without `--slot-save-path` the proxy runs
  in base mode.
- **llama.cpp in router mode** (0.3.7, llama-server started without `-m`, models from `--models-preset` or
  `--models-dir`, loaded on demand) — detected automatically. The client's `model` picks the model; the proxy reads
  that model's n_ctx (its slot, `GET /slots?model=…`, loading the model if needed) **before** building the prompt, so
  the window always fits the model that will run it (thresholds scale with it). If the n_ctx cannot be read the
  request is refused with a clear error; it is never sent with the config window. Saved files are bound to the model
  that wrote them: a file is never restored into another model. Start every model with `--slot-save-path` (put it
  in the `[*]` section of the preset) and `parallel = 1`, and give each model a `ctx-size` of at least 16K.
  **Loading takes time** (on AI395 a 35B-A3B Q4 model took ~73 s from a USB disk, 12 s from the page cache): with
  `stream: true` the proxy sends an SSE comment (`: loading model <id>`; Anthropic API: a `ping` event) every
  `router_keepalive_s` (10 s) until the model is loaded and its n_ctx read, so a client with a read timeout does not
  give up; if the client has gone away the request is dropped before anything is generated (journal
  `router_client_gone`). **Non-stream requests get nothing until the answer: the client's timeout must be longer
  than the model's load time** (plus the answer). `router_load_timeout_s` (600) bounds the wait on the proxy side.
  **One history per conversation**: when a smaller model needs masking to fit, the masked history is what the bigger
  model gets afterwards too (it can read the masked results again with its tools; its earlier saved file no longer
  matches). The disk check before an autosave uses the bytes/token measured on the last save of the same model
  (×1.25; 20000 until the model has saved once). Tested on CPU with tiny models and on AI395 (Radeon 8060S,
  Vulkan) with Qwen3.6-35B-A3B and Gemma 4 E2B (`docs/dev-notes/ROUTER-MODE-RESULT.md`). Ready to copy:
  `examples/preset.router.example.ini` (start with `llama-server --models-preset preset.router.example.ini
  --models-max 1 --port 8095`) and `examples/config.llama-server-router.json`.
- **Strata** — works in base mode on any version. **Saved state: Strata ≥ 0.1.40**
  ([PR #668](https://github.com/Niko1221/Strata/pull/668), released in
  [v0.1.40](https://github.com/Niko1221/Strata/releases/tag/v0.1.40)), started with `--slot-save-path DIR`. Session files
  must be trusted: the hashes detect corruption, not tampering. Our measurements are from the session-files build
  (0.1.38 + PR #668, `8d8f664`); on 0.1.40 the proxy ran the AgentLongBench benchmark and the recall tests, but
  restore times were not re-measured there (see [Saved state, measured](benchmarks.md#saved-state-measured)).
**Saved state that the engine does not support.** After a reply that means "not supported", the proxy stops
saved-state work for that model only (anchors and their one-token calls, segment seal/save, SAVE/RESTORE,
autosave/autorestore, kv_archive). Masking, the text archive, `recall` and notes keep working.

| Reply to a slot call | What the proxy does |
|---|---|
| HTTP 500 "the engine answered a session … the server ended the engine" (PolyStrata `strata-poly`) | suspended until restart or reset |
| HTTP 501 with a documented permanent text (no `--slot-save-path`, parallel requests, strata-poly on PolyStrata v0.1.45-poly.7 or later) | suspended until restart or reset |
| any other HTTP 501 | paused for 15 minutes, then one call retries |
| 404, 400, 409, 503, 507, generic 500, timeouts | not a refusal |

`GET /v1/strata/saved-state` lists the suspended models. `POST /v1/strata/saved-state/reset` with `{}` (all models)
or `{"model": "<exact id>"}` clears them; it answers `{"cleared": [...]}`, 400 for a bad body, and **403 unless the
request comes from this machine** (the proxy has no client authentication). The check looks at the direct TCP
peer: a local reverse proxy can make remote requests look local, so block this endpoint at that proxy if remote
access must be denied. After a reset the next save is sent again, so on a strata-poly model with a PolyStrata
release older than v0.1.45-poly.7 it ends the engine once more.

The proxy warns once per suspension: after a reset or a successful retry, a new refusal is reported again. The
retry after the 15-minute pause happens only when the proxy next needs saved state, so `GET` may show
`retry_after_s: 0` until then. The suspension adds no new requests to the engine's status endpoint.

**PolyStrata v0.1.45-poly.7 and later** answer a save or restore on a `strata-poly` model with HTTP 501 and keep the
engine running (VecSzn/PolyStrata#114): update with `UPDATE.bat` / `./update.sh` and restart PolyStrata. The proxy
recognises that 501 as permanent and suspends saved state for that model on the first reply. On **older PolyStrata
releases** the first SAVE to a `strata-poly` model still ends the engine (the proxy then suspends saved state for
that model); to avoid even that, set `mask_anchor`, `slot_save`, `autosave`, `autorestore`, `seal_experimental` and
`kv_archive` to `false` for that model.
  `setup.sh` and `run-strata-proxy.sh` in the repository root (a venv, then the proxy in front of a running
  Strata, `slot_dir` read from the Strata config) were contributed and are covered by unit tests; they are not yet
  tested by the maintainer on a real Strata install.

  `examples/config.example.json` is the configuration used with session files;
  `examples/config.official-strata.json` the one for Strata without `--slot-save-path` (or older than 0.1.40).
- **Generic OpenAI-compatible** (vLLM, online APIs, …) — base mode: masking, archive + `recall`, segments with
  handoff notes, 📌/🗑, streaming. No engine state, no saved files. Not tested on vLLM itself.
- **Unsloth** (`unsloth run`, Unsloth Studio) — recognised as llama-server through Unsloth's API (`/props`,
  `/slots`), window read from it, API key passed with `--api-key`. No saved state: the API does not pass on slot
  save/restore. Setup in [Install](install.md#install), results in [Unsloth, measured](#unsloth-measured).
- **ds4-server** ([antirez/ds4](https://github.com/antirez/ds4)) — automatic detection, tool-call ids untouched, no
  duplicate saving (ds4 has `--kv-disk-dir`). Tested with pi, see below.

### ds4-server

ds4-server keeps its own state: with `--kv-disk-dir DIR` it checkpoints the KV cache to disk by text prefix and
resumes a conversation after a tool call by recognising the tool-call id (exact replay of the sampled call). So the
proxy does less than with other engines:

<!-- readme-test: real-engine -->
```sh
./ds4-server -m MODEL.gguf --ctx 65536 --port 8110 --kv-disk-dir ./kv
mnemonic-proxy --upstream http://127.0.0.1:8110 --port 8096 --data ./data --config examples/config.ds4.json
```

- Detection: `GET /v1/models` with `owned_by: "ds4.c"`; the window is taken from `context_length` (= `--ctx`).
  `engine: "ds4"` forces it.
- The proxy does not save, restore or archive engine state (`slot_save`, `mask_anchor`, `autosave`, `kv_archive`
  are turned off): ds4 does it itself.
- `mask_tool_args` is turned off: ds4 re-inserts the sampled call text by id, so shortened arguments would not
  reach the model. Set `ds4_exact_tool_replay: false` only if ds4-server runs with
  `--disable-exact-dsml-tool-replay`.
- Tool-call ids pass through unchanged in all three protocols (the id the engine generated reaches the client and
  comes back identical); internal `recall` calls keep the engine's id and are re-inserted identically next turn.
- Old reasoning is masked as usual; the last turn with tool calls keeps its reasoning.
- `checkpoint_align_tokens` (off by default): when set to ds4's checkpoint interval (by default 10240 tokens,
  `--kv-cache-continued-interval-tokens` rounded to `--kv-cache-boundary-align-tokens`), a masking block starts just
  after a multiple of it, so fewer tokens are re-read after the last disk checkpoint. Needs an exact `--tokenizer`
  to be meaningful. In one long session at 32K (below) it changed no masking decision.
- Not proven: Claude Code and Codex in front of ds4; parallel sessions (`--batched-session N`) — the proxy still
  serialises requests.

mnemonic-proxy is a separate tool, compatible with ds4-server and tested with it; it is not part of the ds4 project.

**Measured with pi** (4 tasks in one session on a 140-line `server.log`; the last one asks, without re-running
anything, for the request id and shard of the *third* error, whose output the proxy had already removed from the
prompt). Hardware: Ryzen AI Max+ 395 "Strix Halo", 128 GB unified memory, ROCm gfx1151.

| model, engine | window | tasks 1–3 | third error (rq-2290, shard 3) | `recall` | time |
|---|---|---|---|---|---|
| GLM 5.3 Flash Q2, ds4 `0aaea5a`, SSD streaming, MTP | 8K | ok | **right**, plus an invented timestamp | 5, by id + offset | 10 min 34 s |
| Qwen3.8 Flash Next Q2, ds4 local ROCm port | 8K | ok | not found; said so, invented nothing (0.2 recall budget: fixed in 0.3.0) | 5 | 6 min 05 s |
| Qwen3.8 Flash Next Q2, ds4 local ROCm port | 32K | ok | **right** (still in the prompt, no masking) | 0 | 3 min 30 s |
| same, **proxy 0.3.0**, first prompt forcing a plain `cat server.log` | 8K | ok | **right**, exact line (no invented detail) | 1 (id + query) | 7 min 07 s |

The 8K rows were measured to stress recall; since 0.3.0 the proxy refuses windows below 16K (see
[Requirements](install.md#requirements)).
- GLM: after each `recall` ds4 read only the new tokens (614–634), never the whole prompt again: it resumes by
  tool-call id. Prompt reading 24–29 tok/s, generation ~6 tok/s — enough to verify the mechanism, too slow for work.
- Qwen3.8 at 8K: the model used `recall` correctly (by id, then by offset), but 0.2's proxy cut the 2nd–4th results
  to almost nothing (`room` < 0) and the third error, at character 6,183 of 9,282, never arrived. 0.3.0 fixes the
  two causes (see the changelog). With 0.3.0 one `recall` by id + query brought the line back (360 tokens). One
  successful live run, with a stricter first prompt: in two runs with the original prompt the model ran
  `cat server.log | grep -c ERROR` instead, never saw the lines, and said so.
- Qwen3.8 runs on a local ROCm port of ds4's Qwen path (not the ds4 release, which supports Qwen3.8 on Metal/CUDA
  only); prompt reading ~81 tok/s and generation 16–19 tok/s at 1.6K–7.8K tokens without MTP.

Reports (Italian): [DS4-GLM-RESULT.md](https://github.com/lastreload/mnemonic-proxy/blob/main/docs/dev-notes/DS4-GLM-RESULT.md),
[DS4-QWEN-COMPARE-RESULT.md](https://github.com/lastreload/mnemonic-proxy/blob/main/docs/dev-notes/DS4-QWEN-COMPARE-RESULT.md) (also Qwen3.8 quality on ds4 vs Strata),
[RECALL-8K-030-RESULT.md](https://github.com/lastreload/mnemonic-proxy/blob/main/docs/dev-notes/RECALL-8K-030-RESULT.md) (the 0.3.0 run),
[DS4-RESULT.md](https://github.com/lastreload/mnemonic-proxy/blob/main/docs/dev-notes/DS4-RESULT.md) (the adapter).

#### Long sessions on ds4: proxy vs pi's own compaction

Same hardware, ds4 `fc80bd6` (+ the local ROCm port for Qwen3.8), proxy 0.3.2, pi 1.0.3, window 32K, one run per
side. Without the proxy pi compacts by itself (`reserveTokens 8192`, `keepRecentTokens 8000`: our choice for 32K).
Every answer below is graded by tests and regexes written before the runs.

| task, model | | with proxy | pi alone |
|---|---|---|---|
| 16 steps (`farmastock`), Qwen3.8 Flash Next Q2 | steps done (2 h limit) | **16/16** in 85 min | 14/16 |
| | re-reads from token 0 | **0** | 23 |
| | tokens read by the engine | **167K** | 267K (with fewer steps) |
| | early facts recalled at the end | **5/5** | 4/5, one "I don't know" (asked after the limit) |
| 29 steps (`salepren`), same model | steps done (4 h limit) | 29/29 | 10/29 |
| | re-reads from token 0 | **1** | 38 |
| | tokens read / prefill time | **362K / 4,447 s** | 511K / 6,368 s |
| | memory probes at steps 8, 14, 20 | 17/18 | 5/6 (only step 8 reached) |
| | hidden tests on the final code | 87/87 ⚠ | 59/87 |
| 16 steps, DeepSeek V4 Flash Q2 (official ds4, no patches) | steps / re-reads / facts | 16/16 in 61 min / 0 / 7 of 8, one "I don't know" | not run |

Prompt reading (~82 tok/s) and generation (~19 tok/s) were the same with and without the proxy.

**⚠ The 29-step run is not a clean quality result.** Both sides ran with pi's `maxTokens 4096`, and that limit
includes the model's reasoning. Qwen3.8 often thought for more than 4,096 tokens before acting, so the answer was
cut before the tool call (`finish_reason: length`): 26 times with the proxy, 8 without. pi stores a cut answer
without an action and sends the next step, so unanswered requests pile up. With the proxy they reached 13 in a
row; the model tried to do all of them in one turn, ran out of tokens again, and kept looping. It wrote no code
from step 2 to step 25, then rewrote the whole package from memory at step 26 (which passes all 87 tests). Without
the proxy, pi's summaries happened to clear the backlog. The first cut came before the proxy had changed anything,
and it also happened without the proxy: the cause is the limit, but the proxy keeps the history faithfully and so
does not break the loop.

What this means in practice:
- **give reasoning models room**: set the client's max output tokens well above their usual reasoning length
  (8192 or more for Qwen3.8 with reasoning on);
- the 16-step runs did not show this loop (with the proxy all 16 steps produced code; 57 of the agent's own tests
  passed at the end, 40 without the proxy) and are the cleaner comparison;
- a rerun of the 29-step task with 8192, and a proxy note when the last answer was cut, are planned.

Other findings: once (16-step run with `checkpoint_align_tokens`) a `recall` found the right chunk but the result
was cut for lack of room in the window, and the model answered "never discussed" — a weak point seen once, not yet
reproduced. With DeepSeek V4 Flash, ds4's default q8→f16 cache left only ~9 GB of free RAM on a 128 GB machine;
the run used `DS4_CUDA_NO_Q8_F16_CACHE=1`.

Reports (Italian): [DS4-LONG-RESULT.md](https://github.com/lastreload/mnemonic-proxy/blob/main/docs/dev-notes/DS4-LONG-RESULT.md) (16 steps, Qwen3.8),
[DS4-LONG2-RESULT.md](https://github.com/lastreload/mnemonic-proxy/blob/main/docs/dev-notes/DS4-LONG2-RESULT.md) (29 steps, Qwen3.8),
[DS4-FLASH-RESULT.md](https://github.com/lastreload/mnemonic-proxy/blob/main/docs/dev-notes/DS4-FLASH-RESULT.md) (16 steps, DeepSeek V4 Flash).

`GET /v1/strata/engine` shows what was detected. Measurements: [docs/dev-notes/ENGINES-RESULT.md](https://github.com/lastreload/mnemonic-proxy/blob/main/docs/dev-notes/ENGINES-RESULT.md) (Italian).

## Tested combinations

### Unsloth, measured

**0.3.24, Unsloth 2026.11.3** (2026-11-09): AI395 (Ryzen AI Max+ 395, Radeon 8060S, Vulkan). Unsloth installed with
its own script (`--no-torch`; prebuilt llama.cpp `b12209`, Vulkan), started with `unsloth run --model
Qwen3.6-35B-A3B-MTP-UD-Q4_K_XL.gguf --context-length 32768 --api-only -p 8888 --disable-tools` and Unsloth's
defaults for the rest (4 slots, MTP drafting, reasoning on). Proxy 0.3.24 from PyPI, no config, no `--tokenizer`,
`--upstream http://127.0.0.1:8888 --api-key …`. Client: a small Python agent loop, not an agent CLI, with the tools
`list_files`, `read_file` and `calc`. Task: read 12 files of about 3.8K tokens each, add up their totals, then quote
a code written only in file 1.

| | |
|---|---|
| `check` | `READY`: engine detected as llama.cpp, window 32768 read through Unsloth, saved state off |
| `check --live` | `READY with warnings`: the tool-call probe ran out of its 128 tokens while the model was still reasoning (same answer straight from Unsloth: `finish_reason: length`) |
| requests | 30 through the proxy, all HTTP 200; 0 `upstream_error`, 0 `proxy_error` |
| history vs prompt | 73.9K tokens archived; largest prompt 28,358 tokens (window 32,768) |
| masking | 4 masks, 17 blocks, 64.6K tokens taken out of the prompt; the first was an emergency mask, because the model read all 12 files in one turn (22.8K tokens) |
| `recall` | 24 calls by the model, served inside the proxy; after the second segment switch it brought file 1 back verbatim by id (3,821 tokens). One `path` call came back empty |
| segments | 2 switches; both times the model answered the notes request with a tool call instead of notes (`finish_reason: tool_calls`), so the proxy used its fallback note ("use recall") |
| answers | the sum of the 12 totals right (82,885); the code from file 1 quoted exactly, with no new `read_file` |
| streaming, `/v1/messages`, `/v1/responses` | 499 chunks; HTTP 200; `completed` |
| pause and resume (separate run) | four conversations of about 9.7K tokens each, through the proxy, in the order A, B, A, C, D, A: every cold one took 10.6–14.5 s, every return to A 0.44–0.91 s with 9.7K tokens cached. llama-server's log shows A brought back from its RAM prompt cache (`found better prompt with f_keep = 1.000`, 33 ms), about 520 MiB per conversation against the 8192 MiB default of `--cache-ram`. It lasts as long as Unsloth runs |

The empty notes do not come from Unsloth: with the proxy pointed straight at Unsloth's private `llama-server` (same
process, same model) the notes request got the same `tool_calls` answer. Not shown: an agent CLI on Unsloth, long
sessions, Unsloth's server-side tools, the desktop app, Windows and macOS.

### opencode on Strata, measured

**0.3.21, opencode 1.19.5 on Strata** (2026-11-01): PC4070TI, Strata 0.2.6 / engine 0.1.50 with Qwen3.8 Flash Next
GSQ-RCO IQ3_XXS, `--max-context 131072 --kv int8 --kv-resident 32768`, proxy window 131072 (mask trigger ~80K,
target ~40K), `--tokenizer` from the pack. opencode installed in an isolated prefix with its own `XDG_*` dirs,
auto-compaction off, one session, eight consecutive turns driven with `opencode run` / `run -c` on a throwaway copy
of 29 Python modules (13,911 lines): write one markdown document per group of modules, then write and test a new
module, then answer questions about the session from memory.

| | |
|---|---|
| session | 1 h 32 min 30 s, 554 requests, 235,380 tokens generated |
| history vs prompt | real history up to **1,171,531** tokens; physical prompt median 77,027, max **108,332** |
| prefill | 1,866,843 tokens actually read, 40,031,102 reused from saved state (**97.8 % reuse**) |
| TTFT (proxy-side prefill), all requests | median **1,902 ms**, max 30,279 ms |
| TTFT on the 51 requests where masking happens | median **7,023 ms**, max 19,574 ms |
| TTFT on the 51 requests right after a mask | median **1,930 ms**, max 17,188 ms |
| generation | median 49.5 tok/s, max 90.5 tok/s |
| masking | 51 masks, from `93,470 → 57,659` (3 pieces) down to 1-piece emergency batches |
| segments | 3 switches; `seal` 1.8–2.8 s, `save` 0.80–0.84 s per file |
| anchor | 39 `anchor_save`, 36 `anchor_restore`, 2 `anchor_skip` |
| `recall` | **44 calls by the model; 19 returned archived text** (8 by `query`, 11 by arguments id as 700-char excerpts), 25 returned none (21 output ids with `mode=output`, which by design answers only with a pointer to the arguments id; 4 `path` calls empty because of the filter fixed in 0.3.24). All 32 id calls asked for blocks masked inside the current turn: `mode=output` served them without that filter, but a plain `recall(id=…)` of the same blocks would have been refused by 0.3.23 (see CHANGELOG). `recall_use` marks 35 of 44 as used |
| tool calls | 669 (`read` 434, `bash` 190, `edit` 33, `write` 10, `grep` 2); 0 empty results |
| errors | 0 `proxy_error`, 0 `upstream_error`, 0 `client_disconnect`, 0 non-200; 5 `edit` calls rejected by opencode (stale `oldString`), 1 turn with no final answer (`final_answer_missing`) |

What it shows: masking costs about **5 s of extra prefill on the request where it happens** (7.0 s against a 1.9 s
median) and **nothing on the next one** (1.93 s against 1.886 s for requests far from a mask), because the anchor is
saved and restored instead of re-prefilling the shortened prompt. The model asked for `recall` on its own whenever
it needed a masked tool result, and the three memory questions at the end of the session were answered correctly
after seven `recall` calls. What it does NOT show, and what a re-reading of this journal found later: the id calls
of this session all pointed at blocks masked inside the *current* turn. They all used `mode=output`, which in
0.3.23 skipped the archive filter: each output id got only a pointer to its arguments id, and the arguments id gave
700-char excerpts, never the verbatim text. A plain `recall(id=…)` of those blocks would have been refused
(`idx < max_idx`), and the 4 `path` calls came back empty for that reason. The filter is fixed in 0.3.24; these
numbers are the 0.3.23 behaviour. Also not shown: the
interactive TUI, `opencode serve`/`web`/`attach`, MCP servers
or sub-agents, opencode's own compaction running *together* with the proxy, and the client-side TTFT (we measured
the proxy's `prompt_ms`). One turn out of eight ended with no final answer: the model spent the whole turn reading.

**0.3.8, Ollama and LM Studio: window read from the engine** (2026-10-19): same machine, engines and model as the
0.3.7 run below (Ollama with the integrated GPU, `OLLAMA_IGPU_ENABLE=1`, context set with `OLLAMA_CONTEXT_LENGTH`;
LM Studio `lms load … [--context-length 32768]`), no `--tokenizer`. Report: `docs/dev-notes/ENGINE-WINDOW-038-RESULT.md`.

| test | Ollama | LM Studio |
|---|---|---|
| context read by the proxy (`/api/ps`, `/api/v0/models`) | 4096, 32768 | 8192, 32768 |
| small context, `check` without config / with `window` 32768; proxy start | NOT READY: 4096 < 16384 / also "config window=32768 > Ollama context 4096"; start refused | same with 8192 |
| small context, proxy allowed below the minimum (development switch), prompts of 5.4K and 10.8K estimated | window 4096: segment switches, 0.97K / 0.93K sent, codeword right, nothing cut | window 8192: 6.0K and 1.1K sent, notes 6.5K, codeword right, nothing cut |
| forced cut: engine hidden behind a plain `/v1` forwarder, config `window` 32768 | `engine_truncated` ×2 (11.2K counted → 3.7K read; 22.0K → 3.2K), 1 warning; `check --live` fails ("request (29473 tokens) exceeds … 4096") | ×2 (11.2K → 3.8K; 22.0K → 3.2K), 1 warning; `check --live` fails (… 8192) |
| context 32768: `check --live` full-window probe | 29,473 tokens sent, 29,473 read | 29,473 / 29,473 |
| context 32768: the notes request that 0.3.7 cut (prompt ≈ 35.7K real) | 21 of 28 messages kept, notes request 27.7K real tokens, not cut; codeword right | 27.7K, not cut; codeword right |
| context 32768, no `window` in the config: tool conversation (12 files) | — | 21 requests all 200, 19/19 valid tool calls, 1 mask (21.8K → 7.3K), 1 `recall` served, 0 `engine_truncated` |

**0.3.7, Ollama and LM Studio** (2026-10-19): Linux, Ryzen AI Max+ 395 (Radeon 8060S integrated GPU, Vulkan), both
engines installed in user space (Ollama 0.40.6 official tarball; LM Studio headless `llmster` 0.0.25 with its
llama.cpp Vulkan runtime 2.41.0), same model in both: Qwen3-4B-Instruct-2507 Q4_K_M. Proxy from `main` with
`config.openai-32k.json`, one request at a time from a small Python client (no agent).

| test | Ollama | LM Studio |
|---|---|---|
| `check` | `READY` (exit 0); without a config `READY with warnings`: window unknown, 131072 | `READY` (exit 0) |
| engine context by default | 4096 (CPU; with `OLLAMA_IGPU_ENABLE=1`: 262144) | 8192 |
| longer prompts at the default context (Ollama ≈ 6–36K real tokens, LM Studio ≈ 8.6–21K), straight to the engine and through the proxy | HTTP 200, prompt cut to 2.7–3.7K tokens, a codeword from the first message lost | HTTP 200, prompt cut to 2.7–3.8K tokens, beginning and end kept |
| engine context 32768: tool conversation over 12 files (`list_files`, `read_file`, `calc`) | 23 requests, all 200; 21 tool calls, all valid; 1 mask (prompt 19.4K → 7.4K); largest prompt 19.4K; nothing cut | 23 requests, all 200; 21 tool calls, all valid; 1 mask (19.3K → 7.3K); largest prompt 21.3K; nothing cut |
| engine context 32768: one prompt of ≈ 35.7K real tokens | straight: cut to 29.8K, codeword lost; proxy: segment switch, 6.3K sent, codeword right | straight: cut to 14.8K; proxy: segment switch, 6.2K sent, codeword right |
| the proxy's own notes request at that switch (no `--tokenizer`) | estimated 31.3K, cut by the engine to 30.5K | estimated 31.3K, real 33.4K, cut to 15.1K; with `--tokenizer`: 30.8K, not cut |
| streaming, `/v1/messages`, `/v1/responses` | streamed (11 chunks), 200, 200 | streamed (11 chunks), 200, 200 |

The model stopped before reading every file (10 of 12 on Ollama, 11 of 12 on LM Studio; the sums it made were right
for the files it read) and, asked about a file that was still in view, called `recall` for the wrong piece and said
it could not quote it: the proxy served the piece the model asked for. These are limits of the 4B model, not of the
engines.

**0.3.0, engine `-c 32768`** (2026-10-06): T1 run as written above, on the CPU of a Ryzen 7 5700G (16 threads, no
GPU, llama-server at `nice 19`), in a fresh folder with its own `HOME`: llama.cpp `b11430` CPU build, pi 1.0.3,
Node 24.14, Python 3.14, `config.llama-server-32k.json`. Requests sent with `pi -p` / `pi -c -p`, one at a time.

| model | `check --live` | tool calls | masking | recall (step 5) | engine restart |
|---|---|---|---|---|---|
| Qwen3-4B-Instruct-2507 Q4_K_M (Unsloth) | READY (exit 0) | yes; steps 1–4 done, 4/4 tests pass | yes: 1 mask (the 13.4K-token log, prompt 17.3K → 4.0K) | **no**: answered "cannot determine" instead of calling `recall` | autorestore 4112 tokens in 349 ms; next prompt 4157/4176 reused |

Times on this CPU: steps 2–4 took 7, 5 and 10 minutes (reading 13K tokens of log, then generating at a 16K prompt);
the whole exercise about 25 minutes. Memory: llama-server RSS 8.9 GB at `-c 32768`; one saved state of 4.1K tokens
is 600 MB in `./slots`. A first run with a 150-line log (≈3K tokens, the 0.2.1 exercise) never reached
the masking threshold at 32K (largest prompt 10K): step 5 was answered right from the prompt, not tested. Hence the
650-line log above.

**0.2.1, engine `-c 8192`** (windows below 16K are refused since 0.3.0): run literally as written in T1 at the time,
in a clean `ubuntu:24.04` container (12 CPU cores, no GPU, user without sudo), llama.cpp `b11430` CPU build, pi
1.0.3, Node 22.23.3, Python 3.12, the 8K example config of 0.2.1 (2026-10-06).

| model | `check --live` | tool calls | masking | recall (step 5) | engine restart |
|---|---|---|---|---|---|
| Qwen3-4B-Instruct-2507 Q4_K_M (Unsloth) | READY | yes | yes: 5 masks, 4 segment switches | **no**: answered "cannot determine" instead of calling `recall` | autorestore 2450 tokens in 42 ms; next prompt 2486/2505 reused |
| Qwen2.5-Coder-7B-Instruct Q4_K_M (Qwen) | READY with warnings (no tool call) | **no**: writes the call as JSON text, same straight to llama-server | — | — | autorestore 3698 tokens in 17 ms |

What T1 proves: the proxy in front of a real llama-server — window read from the engine, thresholds scaled to it,
masking under a real agent (and, at 8K, segment switches), state saved when idle and restored after an engine
restart without re-reading. What it does not prove: that a 4B model uses `recall` reliably (it did not, at 8K or at
32K), or that the agent always finishes the task — at 8K, in step 4 the 4B model looped on read/edit until the
timeout. Those depend on the model; use T2 for real work.

Found while testing, fixed in 0.2.1: with the extended recall tool (`recall_struct`, `recall_multi`) in the tool
list, Qwen3-4B made 0/6 tool calls (4–6/6 without): the example configs keep the short `recall` tool.
