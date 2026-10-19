# 0.3.8 — real window from Ollama / LM Studio, engine truncation detected, estimate slack: result

Date: 2026-10-19. Branch `engine-window-truth`, PR #14. Real run on AI395 (Ryzen AI Max+ 395, Radeon 8060S
integrated GPU, Vulkan), 14:32–14:56 (24 minutes), everything in a user-space folder, `systemd-run --user --scope`
(engine MemoryMax 40G, proxy 4G, client 2G), `nice 19`, `ionice idle`, ≤ 12 CPUs, a guard every 5 s (minimum
MemAvailable 72.6 GB, maximum GTT 11.3 GB; never fired). Engines: Ollama 0.40.6 (official tarball,
`OLLAMA_IGPU_ENABLE=1 OLLAMA_VULKAN=1 OLLAMA_NUM_PARALLEL=1 OLLAMA_DEBUG=1`, context with
`OLLAMA_CONTEXT_LENGTH`), LM Studio headless `llmster` 0.0.25 (llama.cpp Vulkan runtime 2.41.0, `lms load
qwen/qwen3-4b-2507 --parallel 1 --identifier qwen3-4b [--context-length 32768]`). Model in both:
Qwen3-4B-Instruct-2507 Q4_K_M (downloaded, deleted at the end). No `--tokenizer` anywhere.

## What changed (see CHANGELOG 0.3.8)
1. Ollama / LM Studio are recognised in the detection of a generic engine and their runtime context becomes the
   window, read again before every request (`ctxproxy/enginectx.py`).
2. `engine_truncated`: `usage.prompt_tokens` < 0.8 × the window guard's count → journal + one log warning per
   conversation. `check --live`: a ~0.9 × window prompt must come back whole.
3. Generic engines without `--tokenizer`: `window_estimate_slack` 0.15 by default, `check` warning.

## Which field is the real context (verified on the servers)
- Ollama `GET /api/ps` → `models[].context_length`: 4096 with `OLLAMA_CONTEXT_LENGTH=4096`, 32768 with 32768 (=
  `n_ctx_slot` in the log of Ollama's llama-server). `POST /api/show` → `model_info["qwen3.context_length"]` =
  262144 (training context): not used. With no model loaded `/api/ps` lists nothing: the proxy loads the model
  with a 1-token request before the first prompt is built and reads it again.
- LM Studio `GET /api/v0/models` → `loaded_context_length` 8192 (default load) / 32768 (`--context-length 32768`)
  on the entry with `state: loaded` (`qwen3-4b`); the same model under its key `qwen/qwen3-4b-2507` is listed as
  `not-loaded` and `max_context_length` is 262144: matched by the request's model id.

## Results
| test | Ollama | LM Studio |
|---|---|---|
| `check`, no model loaded | READY with warnings: "no model loaded, its context is not known yet: the proxy reads it before the first request" | same |
| `check` at the small context, no config | NOT READY (exit 1): "engine window n_ctx=4096: the minimum is 16384 … -> start Ollama with a bigger context: OLLAMA_CONTEXT_LENGTH=32768 ollama serve" | NOT READY: 8192, "-> lms load <model> --context-length 32768" |
| `check` at the small context, `config.openai-32k.json` (`window` 32768) | NOT READY: also "config window=32768 > Ollama context 4096 … prompts would be cut in silence by the engine" | NOT READY: "… > LM Studio context 8192" |
| proxy start, small context + `window` 32768 | refused (exit 1), both problems printed, `refused_small_window` in the journal | refused |
| small context, `MNEMONIC_PROXY_ALLOW_SMALL_WINDOW=1`, no config, codeword probe 5.4K / 10.8K estimated | window 4096 read; segment switches; 966 / 929 tokens sent; codeword right both times; 0 `truncating` lines in the Ollama log | window 8192; 6,006 sent, then a switch: notes request 6,480, next 1,124; codeword right; 0 new `exceeds` lines |
| forced cut: `hide_api.py` forwards only `/v1/*` (engine looks unknown), `window` 32768, probes 10.8K / 21.6K | `engine_truncated` ×2: 11,170 counted → 3,722 read (0.33), 21,989 → 3,208 (0.15); 1 warning (same conversation); codeword lost (Ollama dropped the oldest messages: 2 `truncating` lines) | ×2: 11,170 → 3,756 (0.34), 21,989 → 3,242 (0.15); 1 warning; codeword right (LM Studio keeps the start) |
| `check --live` through the forwarder | live: full window FAIL "engine context smaller than window … HTTP 400 request (29473 tokens) exceeds the available context size (4096 tokens)" | FAIL, same with 8192 (LM Studio did not cut a single 29K message: it refused it) |
| context 32768, `check --live` | full window: sent ~29,473, read 29,473, whole (50 s); READY with warnings (only the tokenizer warning) | 29,473 / 29,473 (90 s; 2 s on the repeat, prompt cache); READY with warnings |
| context 32768, `window` 32768, codeword probe 10.8K / 32.4K (the 0.3.7 notes scenario) | 12,257 sent; at 32.4K `freeze` est 33,760, `notes_source` kept 21 of 28 (0.3.7: 25 of 28), **notes request 27,667 real tokens, `truncated = 0`** (0.3.7: 33,354 real, cut to 30,487), next request 6,409; codeword right; 0 `truncating` lines | same journal: 21 of 28, notes 27,667 real (`usage`), 6,421; 0 new `exceeds` (0.3.7: 33,354 refused, re-sent cut to 15,051) |
| context 32768, no config at all (window from the engine), tool conversation (12 files, `pot_client.py --mode agent`) | — | 21 requests all 200; 19 tool calls, all valid; 1 mask (5 reads, est 21,844 → 7,310); largest prompt 19,341; 1 `recall` served (`recall_use used=true`); 0 `engine_truncated`; 0 new `exceeds`; answer: 2,859 lines for the 10 files it read (of 12; truth 3,373 for all 12), risks ledger `post_entry()`, money `round_amount()` |

## Slack and threshold choice
- 0.3.7 journal: whole prompts read 1.00–1.10 × the chars/3.5 count (31,319 → 33,354 = +6.5% on the notes request;
  code with digits ~+10%). Slack 0.15 covers both. With it the 0.3.8 notes request came out at 27.7K real tokens,
  4.9K under the window.
- Cut prompts read 0.12–0.48 × the count (0.3.7: 13,005 → 3,020 = 0.23; here 0.15–0.34). Threshold 0.8 is well
  between the two groups; below 1,024 counted tokens nothing is judged.
- Strata, llama.cpp (single and router) and ds4: no slack, no truncation watch, no extra GET: the existing tests
  (journal and prompts) pass unchanged.

## Notes
- A cut prompt is not used to calibrate the token estimate (it would learn a ratio of 0.2–0.5).
- `check --live` on a generic engine now asks the engine's own model name (Ollama answered 404 to `local-model`).
- `check` run right after stopping a proxy on the same port can report "port taken" (the socket is still in
  TIME_WAIT; `_port_state` binds without `SO_REUSEADDR`): seen once here, not changed.
- Answers are never changed on `engine_truncated` (signal only): the engine already answered, and a retry could
  not make the prompt fit an engine whose context the proxy cannot read.

## Tests
`tests/test_engine_window.py` (26 tests, fake engine flavors `ollama`, `lmstudio`, `openai_cut`): window read,
loaded on demand, window follows a shrinking engine, `window` > context refused / lowered, minimum refused, unknown
model not sent blind, `engine_truncated` once per conversation, no calibration on cut prompts, slack only on
generic engines, `check` NOT READY with the fix, `check --live` full window ok / cut.
