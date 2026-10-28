# Changelog

## Unreleased

- Enforce `tool_choice=none` on the last bounded closed-search answer attempt (#30), keeping tool schemas
  and earlier attempts unchanged. A model that still supplies no public answer now returns an explicit
  context-manager failure notice (also in streaming), with `strata_context.final_answer_missing=true`
  and a `final_answer_missing` journal event. Private reasoning is never promoted into an answer.
  Client tool calls, the window guard, answer floor and two-retry limit are unchanged.

## 0.3.15 — 2026-10-28

- Fix a tokenizer-backed window-guard undercount for recent Strata/PolyStrata (#28): special-token spellings
  quoted in message text, tool schemas or decoded call arguments now receive a conservative ordinary byte-BPE
  reserve. Both streaming and non-streaming requests use the same guard, including notes, seal, anchor and retries.
  Uncountable prompts with a loaded tokenizer are refused before HTTP; output-limit aliases remain synchronized.
- Warn and adapt oversized small-window notes/response budgets and masking-anchor thresholds at startup;
  `check` now reports the same adaptations and Strata's discovered window. Never honor a Strata window override
  larger than the reported engine context. Document 32K settings and the limits of local token-count guarantees.
- Run synthetic exact-tokenizer regressions in Linux and Windows CI; no model or GPU is used.

## 0.3.14 — 2026-10-27

- Share the available result budget across distinct id recalls in the same batch (#25), returning exact
  pages instead of letting the first id consume the remaining window. Reuse unused shares and, when
  feasible, reserve room for one more search round. Single-id recalls retain the 0.3.13 output; the
  window guard, answer floor and per-result ceiling are unchanged.

## 0.3.13 — 2026-10-27

Measured on 20 AgentLongBench questions picked for heavy paging (Strata 0.1.40, 32K window, 0.3.12 vs this
release, alternating order): recall rounds 69 → 33, recall calls 77 → 40, end-to-end time −5%, correct answers
16 vs 16, no window overflows or HTTP errors. Correctness is unchanged on this sample, not improved.

- Avoid artificial id-recall pages when the complete output/remainder fits: `recall_max_tokens` stays a
  per-result ceiling instead of scaling down with the engine window. Explicit ceilings still apply; the
  window guard still reserves the answer floor and enforces the remaining context on every request.
- Fit id pages with their headers and continuation cursors inside the result/window budget. When a query
  is supplied and the full text does not fit, center a verbatim span on a query hit, with exact character
  offsets, total length and cursors for omitted text (including single-line JSON).
- Honor `mode=output` with query/offset on an output id; reject that combination on call-argument ids with
  an explanation rather than silently ignoring it. Receipts and the stable physical prefix are unchanged.

## 0.3.12 — 2026-10-27

- Fix typed receipts for Claude Code (`Read`, `Write`, `Edit`, `MultiEdit`, `Bash`) and Codex
  (`shell`, `exec_command`, `apply_patch`), including their argument names and argv commands (#21).
  Unknown tools keep the existing fallback; pi receipts are unchanged.
  Receipts are rebuilt on every request, not stored in the masks table: already-hidden outputs change too.
  An ongoing Claude Code/Codex conversation re-reads its prompt once after upgrading, because the engine
  cache no longer matches from the first changed receipt.

## 0.3.11 — 2026-10-27

- **`mnemonic-proxy forget`: delete one conversation in one step** (#19, suggested by a review on Radyz.cz).
  `forget --data DIR --conv ID` removes its rows from every table that has a `conv` column (read from the schema,
  so future tables are included), its per-conversation `kv` entries, the lazy structured-recall index (rebuilt on
  demand), then rebuilds FTS and vacuums; it deletes the conversation's saved-state files and prompt sidecars,
  its `kv-archive` manifests and unreferenced blocks, rewrites `journal.jsonl` without its lines (atomically) and
  clears matching live/router metadata. `--list` shows ids, sizes and timestamps; `--dry-run` shows what would be
  removed and changes nothing. Paths come from `--slot-dir`/`--kv-archive`, else `--config`, else
  `MNEMONIC_CONFIG`; if saved-state records exist and the state folder is unknown, it refuses. Exit code is non-zero
  if anything could not be removed. Not a forensic wipe (SSD, snapshots, backups, engine memory).
- The proxy now holds a non-blocking lock on its data folder for as long as it runs; a second proxy or `forget`
  on the same folder is refused. Proxies older than 0.3.11 do not take it: stop them before running `forget`.
- On Ctrl-C the proxy now stops its background workers (autosave, kv-archive) before exiting.

## 0.3.10 — 2026-10-27

- **`check --live` works on a fresh Strata 0.2.x session.** Skip the initial backup and final restore when the engine
  refuses the backup with `no complete session`; still verify save/restore after generation. The slot's
  `n_prompt_tokens` is not used for this: Strata 0.2.x reports 0 right after a restore although the slot holds the
  restored session (checked on a real engine), so trusting it would overwrite that session.
  Non-empty sessions are preserved as before, and other backup errors still stop the check before generation.
  Disable thinking for the short Strata generation probe so its token budget can produce a visible reply.
  Add a CPU-only fake-engine mode (`--reject-empty-save`) and regressions for empty/non-empty sessions and harmless,
  logged runtime save failures (autosave, segment and anchor). Fixes #17.

## 0.3.9 — 2026-10-25

- **Dashboard no longer crashes on load.** Five copy/export buttons used by the script (`copyAll`, `expMd`, `expJson`,
  `expHist`, `expHistJ`) were missing from `dashboard/index.html`, so the page stopped before rendering. Restored, and a
  missing control no longer breaks the rest of the page; a test checks that every id the script uses exists.
  Reported by DarkJezter.

- **Oversized output limits no longer force segment switches.** Treat `max_completion_tokens` (preferred when both
  fields exist) / `max_tokens` as a ceiling, cap it to the available context after preparing the prompt, and send
  the reduced value to the engine. A genuinely large prompt still triggers a switch to preserve a useful response
  budget (`response_floor`, or up to `default_response`, bounded to a quarter of the window). Each reduction is
  recorded as `response_clamped` in the journal, with one log warning per conversation (persisted across restarts).
  The final window guard still enforces prompt + response + margin and refuses prompts with no room for a token.
  Fixes repeated switches with large client output ceilings reported by DarkJezter (pi.dev + Strata).

## 0.3.8 — 2026-10-19

- **Ollama and LM Studio: the window is read from the engine** (`ctxproxy/enginectx.py`). Both run in base mode and
  both cut an over-long prompt in silence (HTTP 200): with 0.3.7 the proxy used the config `window` (or 131072) and
  `check` said `READY` with an engine of 4096. Now, in the detection of a generic engine:
  - Ollama: recognised by `GET /api/version`; context = `context_length` of the loaded model in `GET /api/ps` (the
    context it runs with, = `OLLAMA_CONTEXT_LENGTH`; `POST /api/show` gives the training context, 262144 for
    Qwen3-4B, and is not used). LM Studio: `GET /api/v0/models`, `loaded_context_length` of the loaded model.
  - the context is the window (thresholds scaled as for llama-server) and it is read again before every request,
    before the prompt is built; a model that is not loaded is loaded with a 1-token request and read again; if it
    still cannot be read the request is refused (503), never sent with a guessed window. Below the minimum window the
    request is refused (400).
  - a config `window` bigger than the engine context: the proxy refuses to start and `check` fails, with the fix
    (`OLLAMA_CONTEXT_LENGTH=… ollama serve` / `lms load … --context-length …`); if the engine shrinks later the
    window is lowered to it with a warning. Never over the engine.
  - other generic engines: unchanged.
- **Engine truncation detected**: on every generic engine, `usage.prompt_tokens` < 0.8 × the window guard's count
  (≥ 1024 tokens) → `engine_truncated` in the journal (each time) and one warning per conversation in the log. Answers
  are not changed; a cut prompt is not used to calibrate the token estimate. `check --live` on a generic engine sends
  a prompt of ~0.9 × window (sized on the engine's own `prompt_tokens`) and fails with "engine context smaller than
  window" if it is cut or refused; it also uses the engine's model name (Ollama answered 404 to `local-model`).
- **Estimate margin on generic engines**: without `--tokenizer`, `window_estimate_slack` defaults to 0.15 (measured:
  chars/3.5 was 6.5% low on a 33K notes request, ~10% on code with digits); a value in the config wins. `check` warns
  "no tokenizer: the window guard is estimated". Strata, llama.cpp (single and router), ds4: unchanged (slack 0, no
  truncation watch, same journal and prompts).
- Fake engine: flavors `ollama` and `lmstudio` (context endpoints, model loaded on demand, silent truncation like
  the real ones). Tests: `tests/test_engine_window.py`.
- Real run on AI395 (Ollama 0.40.6, LM Studio llmster 0.0.25, Qwen3-4B-Instruct-2507 Q4_K_M):
  `docs/dev-notes/ENGINE-WINDOW-038-RESULT.md`.

## 0.3.7 — not released separately (in 0.3.8)

- **llama-server in router mode** ([#12](https://github.com/lastreload/mnemonic-proxy/issues/12)): llama-server
  started without `-m` (several models, loaded and unloaded on demand). Before, the router was taken for a generic
  OpenAI engine (or, with `engine_tokenize`, every request failed on `/tokenize` without a model): no saved state and
  the window from the configuration, not from the model — a model loaded with a smaller `ctx-size` got prompts it
  could not hold (verified on llama.cpp b11723: HTTP 400 `exceed_context_size_error`). Now:
  - **detection**: `GET /props` says `role: router` and `GET /models` lists models with a `status` → engine
    `llama.cpp` in router mode (also with `engine: llama.cpp` forced). Nothing is loaded to detect it.
  - **window per model, before the prompt is built**: at every request the proxy reads `GET /models`; if the model
    is not loaded (or was reloaded) it reads the n_ctx of its slot with `GET /slots?model=…&autoload=true`, which
    loads it. Window = that n_ctx (or `window` from the config if it is smaller); unset thresholds scaled as for a
    single-model llama-server. If the n_ctx cannot be read (model missing, load failed) the request is refused with
    a clear error — never sent with the config window. An unknown model name is refused too, unless `router_model`
    names the model to use.
  - **every call to the model of the request**: chat (internal calls included), `/tokenize` (token counts and their
    cache per model), `/slots/{id}?action=save|restore` (`"model"` in the body, `autoload=false`).
  - **saved files bound to their model** (`<data>/router-slot-models.json`): a restore with another model, or of a
    file the proxy did not save in router mode, is refused (409) before it reaches the engine — llama-server itself
    restores another model's file without complaining. Autorestore picks only files of the current model; an
    autosave never prunes another model's file as superseded.
  - **unload / reload**: the engine state is `model#generation`; a model seen unloaded gets a new generation, so the
    proxy drops any warm-cache assumption and restores only a file of that same model.
  - Default path unchanged: Strata, ds4, single-model llama.cpp and generic engines behave exactly as before.
  - Real run on CPU (llama.cpp b11723, two tiny models with n_ctx 32768 / 16384, one broken): A → B → A → unload A
    → A, 0 requests over the window, A's file restored only on A. Report: `docs/dev-notes/ROUTER-MODE-RESULT.md`.
  - Real run on AI395 (Vulkan, Qwen3.6-35B-A3B 64K + Gemma 4 E2B 32K, `--models-max 1`): 23/23 requests served, 0
    over the window, 19/19 valid tool calls; main 0.3.6 on the same router: 500 on `/tokenize` or 400 from the engine
    (41134 tokens > 32768). Three issues found there, fixed below.
  - **streaming while a model loads**: before, the client got the SSE headers and then nothing for the whole load
    (73 s for qwen from a USB disk: a client with a 60 s read timeout gave up, and the proxy generated the answer
    anyway). Now, with `stream: true`, an SSE comment `: loading model <id>` (Anthropic API: `ping`) every
    `router_keepalive_s` (10 s) until the model is loaded and its n_ctx read; the window is still settled before the
    prompt is built. A keep-alive that cannot be written = client gone: nothing is generated, journal
    `router_client_gone`, the lock is released when the load ends. Non-stream: unchanged (README: the client's
    timeout must exceed the load time).
  - **disk estimate per model before an autosave**: `tokens × bytes/token` of the last save of the same model
    (`bytes / n_saved`, ×1.25), 20000 until the model has saved once (measured on AI395: qwen 37–73 KB/token, not
    constant with the length; gemma 7.6–9 KB/token; 20000 was too low for qwen). It is an estimate: a longer prompt
    can need more per token than the last save (`autosave_min_free_gb` stays as the margin). The `autosave` event
    carries `est_bytes`, `est_bpt`, `est_from` in router mode only. Single-model engines: unchanged (same 20000,
    same journal).
  - **own journal reasons**: state lost through a model change is `router_model_change`, through an unload/reload
    `router_unload` (was `strata_restart` / `unloaded`); an autorestore in router mode carries the same reason.
    Non-router paths: unchanged.
  - Second run on AI395 with these fixes (a9bfb87): stream to qwen not loaded (cold, 73 s load) with client read
    timeouts of 60 s and 300 s: both got `: loading model qwen` every 10 s (7 comments, first at 10 s) and the answer
    at 74 s; client gone at 20 s: `router_client_gone`, nothing generated; restore from the same model after an
    unload (`autorestore`, reason `router_unload`, 23689 tokens, 155 ms; next answer correct, 23689 cached tokens);
    gemma does not restore qwen's file (`autorestore_other_model`); estimate for qwen's second save 1.08 GB vs
    0.95 GB written; 0 requests over the window.
- **Docs**: the README starts with a short **Install** section (`#install`): one table engine → engine command →
  example config (Strata with and without saved state, llama-server, router mode, ds4-server, Ollama / LM Studio
  marked "not tested yet", any OpenAI-compatible server), one table agent → base URL → what to turn off, and the
  OS. The benchmark and measurement sections moved after Quick start / Uninstall, unchanged; every heading (and so
  every link anchor) is kept. New examples: `examples/config.llama-server-router.json` +
  `examples/preset.router.example.ini` (router mode) and `examples/config.openai-32k.json` (base mode at 32K: the
  proxy cannot read the window from a generic engine, these are the thresholds it uses itself at 32K); `check`
  READY on each against the fake engine, and the preset parsed by llama-server b11723 in router mode. The line
  "0.1.40 has not been re-measured" now says what was run on 0.1.40 (AgentLongBench, recall tests) and what was not
  (restore times). Project page: router mode and a link to Install.
- **Docs: Ollama and LM Studio tested** (Ollama 0.40.6, LM Studio `llmster` 0.0.25, Qwen3-4B-Instruct-2507 Q4_K_M,
  Linux): base mode works (masking, `recall`, segment switch, tool calls, streaming, Anthropic and Responses APIs)
  with the engine's context set to 32768 and `config.openai-32k.json`. With their default context (4096 and 8192 on
  the test machine) both engines cut longer prompts silently (HTTP 200); without `--tokenizer` the proxy's own notes
  request at a segment switch went past 32768 real tokens and was cut too. Install section and Tested combinations
  updated; no code change.

## 0.3.6 — not released separately (in 0.3.8)

- **Fix: the final attempt is not left empty when the model calls `recall` again.** With 0.3.5 a final round of
  internal calls only got one new attempt, but Flash Next called `recall` again in it too (it reasoned up to the
  thinking budget, then called the tool that was not offered): the call was dropped and the answer was empty (2 of 19
  D runs at 64K in the AgentLongBench confirmation run, both `round_limit`). Now, if that attempt also returns only
  internal calls, there is **one last** attempt with the reasoning off (`chat_template_kwargs.enable_thinking=false`,
  the switch Strata and llama.cpp honour; it wins over `reasoning_effort`; no `reasoning_budget_tokens`; answer T does
  not turn the reasoning back on) and the calls answered by an explicit instruction (`ANSWER_NOW`: search closed,
  no tools, answer now in the requested format, or say the information is missing). Same check
  `P + F + margin <= W`, counted with the reasoning-off template; the answer room is never less than the previous
  final call's; only verified copies and finished reasoning pay for it. If the calls + instruction do not fit, the
  last attempt goes **bare** (the calls leave; the instruction is appended to the last result only if it fits).
  If attempt 1 does not fit, the last attempt is tried at once. If the last attempt too calls only internal tools,
  the answer stays empty (nothing invented). Never more than two new attempts. Journal: `final_recall_retry` gains
  `attempt` (1|2), `thinking_off`, `bare`; `recall_dropped` gains `attempts`, `thinking_off`. Streaming: a reasoning
  line "last answer attempt, reasoning off". Cost: with the reasoning off the template drops the reasoning
  instruction from the system head, so the engine's prefix cache is lost for that one call (full prefill).
  See `docs/dev-notes/FINAL-ATTEMPT-036-RESULT.md`.

## 0.3.5 — not released separately (in 0.3.8)

- **Fix: a search round never consumes the evidence already acquired (window guard, correction 2).** On a small
  window (AgentLongBench 16K) the guard made room for one more `recall` round by removing results the model had
  already read: 0.3.4 only changed the order. Now, during the search, only verified copies and finished reasoning
  are removed; removal goes by bands (verified copies → reasoning → results that are not copies → results that are
  not copies and carry the question's identifiers; the x0.25 duplication discount is gone; reasoning is never
  `protected`). A result is a verified copy only if its delivered text is identical to an older result still
  present, or if its certified coverage (exact character spans or line sets of the same record, recorded in the
  extended `meta`; never after a cut, never for grouped provenance, never with empty sources) is inside the coverage
  of older results still present; receipts are never witnesses. A search round is admitted only if
  `P + max(S, 256 + 1024 + F) + H + margin <= W` (S = max(2048, largest complete generation of the previous rounds +
  256), never decreasing; R = 1024 per round; H = result frame + closing note). If a new group does not fit, it is
  reduced or cancelled first (tool protocol coherent) and the round becomes final; dropping older evidence (C2) is an
  emergency, journaled with cause and sources (`evidence_lost`). `_fit_result` frees up to what the result needs,
  replaces a result that does not fit in < 256 tokens with a "no room" notice (or drops the call when not even that
  fits) and closes the search; the other calls of the round do not search. Tool definitions and receipt-guard notices
  pass the same check. At `max_recall_rounds` the round is final directly (no `recall` offered only to say "limit").
  The closing note no longer pins the last result (`_keep`); answerfix K retries with the tools of the final round.
  `P0 + F + margin <= W` is checked before the loop (`floor_short`); in exact mode a count error is journaled
  (`count_error`), never a silent fallback. Sweep (2,000 conversations, exact tokenizer, real ids and repeated calls,
  engine-side loss check): evidence seen by the model and then lost 669 → 0, false deduplications 0 on 392 copies
  removed, 0 over the window; cost: 6,450 recalls instead of 9,547 (more searches end for space). See
  `docs/dev-notes/SEARCH-KEEPS-EVIDENCE-RESULT.md`.
- **Fix: no empty answer when the model calls `recall` anyway in the final round.** In the final round (search
  closed for space or at `max_recall_rounds`) `recall` is not offered, but a model that saw it used may call it
  anyway; the call was dropped and the client got an empty answer (`finish_reason=stop`; 2 of 8 questions in an
  AgentLongBench 32K pilot). Now such a round (internal calls only, no content, no client call) gets **one** new
  final attempt: the calls go into the loop with their reasoning reduced to the conclusion line and each is answered
  by the closing notice (`FINAL_NOTE` for space, `LIMIT_MSG` for the round limit; nothing is searched). The attempt
  passes the same `P + F + margin <= W` check, paying only with verified copies and finished reasoning (never
  evidence); if it does not fit, or the model calls `recall` again, the answer is empty as before. Mixed calls and
  normal final answers are unchanged; streaming never shows the internal calls. Journal: `final_recall_retry`
  (`round`, `reason`, `used`, prompt tokens), `recall_dropped` gains `retry` / `retry_no_room`. Sweep (2,000
  conversations, exact tokenizer, the fake model calls `recall` when not offered with p = 0.3): avoidable empty
  answers 254 → 0, empty answers 254 → 58 (model called `recall` again, or no room), 0 over the window, 0 evidence
  lost.

## Earlier in this cycle (0.3.3, 0.3.4; not released separately)

- **Fix: a piece carrying identifiers of the question is never the cheapest thing to drop.** The removal rule
  discounts x0.25 a piece whose identifiers had all appeared in an older piece; the recall results that answer the
  question look like duplicates of one another, so the question's own evidence was removed first (real journal,
  ALB 16K `38_i13_j22_a_0031`: 5 of the 6 results carrying `0031` removed, 4 of them as the first choice). Now
  `window.piece_value` never discounts a piece that carries identifiers of the current question and
  `window.removal_order` puts those pieces last (whatever the policy: `age`, `reasoning`, `value`). The window
  invariant does not change: a protected piece is still removed when nothing else can free the room, and the
  journal records it (`protected` in the `window_guard` events, next to `policy`). With the same reconstructed
  candidates the removed pieces carrying `0031` go from 5 to 1, and the one left is the event where every
  remaining candidate carried it. Suite: 312 ok on 3.10, 338 ok on 3.14; sweep 2,000 scenarios (8K–128K, exact
  tokenizer, 27,411 requests): 0 over the window. See `docs/dev-notes/GUARD-PROTECT-RESULT.md`.
- **Fix: the proxy never sends a request larger than the window (window guard, on by default).** On a small window
  (AgentLongBench at 16K/32K) the recall loop could grow the prompt round after round and send
  `prompt + max_tokens` over the window: the engine refused the request and the client got an error or an empty
  answer. Now one check runs before EVERY call to the engine (answer, recall rounds, handoff notes, answerfix
  retries, streaming and not): `prompt + max_tokens + window_margin ≤ window` (INV-1). If it does not fit,
  `max_tokens` is lowered, never below the answer floor; a thinking budget always leaves `answer_min_tokens` of
  answer (INV-2); a new search round starts only if the worst case fits (INV-3), otherwise the round is final:
  the proxy's own tools are not offered and the model is told there is no room for more searches. To make room
  the proxy removes what the recall loop added, by value per token (results become one-line receipts recallable
  by id, old reasoning becomes its last conclusion line, never a generated summary); removing all of it gives back
  the prepared prompt, so the final call always fits. If even that is impossible the client gets HTTP 400 and
  nothing is sent. Exact with `--tokenizer`; without it the guarantee is that of the estimate
  (`guarantee=estimated` in the journal). `"window_guard": false` restores 0.3.2.
- Measured on CPU with a strict fake engine (rejects every request over the window, always generates the maximum):
  2,000 random scenarios (8K–128K, streaming and not, answerfix and selector on/off), 27,411 requests, **0 over
  the window** (0.3.2 on the same scenarios: 1,021 of 3,867). Replay of the failed AgentLongBench cases
  (`13_i3_b` 16K, `9_i2_b` and `35_i9_b` 32K): 0.3.2 ends in an engine error on all 4, the guard answers all 4.
- The handoff-notes request is also kept in the window: the source part is shortened from the end (whole groups,
  same prefix). This is the only change of the 0.3.0 language golden (`tests/test_lang.py`); at 128K the requests
  are byte-identical with and without the guard (`tests/test_default_identity.py`).
- New journal events: `window_guard` (`clamp`, `budget_cap`, `remove`, `final`, `notes_source`, `refused`) and,
  measure only, `mask_reuse` (prefix the engine was expected to reuse after a mask vs what it reused).
- A final round (for space or for the round limit) no longer offers the proxy's tools: a recall there was dropped
  and could leave the answer empty.

- **Fix: `recall` by id lost data between pages.** When a page was cut by `recall_max_tokens`, the truncation notice
  stayed inside the page, the header counted it as data and the next `offset` skipped as many real characters as the
  notice is long (146 per page in English). Now the notice is removed: the header and the `[continues: … offset=N]`
  line say where the page ends, and reading page after page returns every character exactly once
  (`tests/test_recall_paging.py`). Found while preparing the AgentLongBench runs (long id lists). This changes the
  recall output when a page is cut: the golden file of `tests/test_default_identity.py` is regenerated (53 of 157
  outputs, all of them cut pages, differ only by the removed notice and the corrected offsets).

- **`recall_leaf_select`** (off by default): on structured outputs `recall` returns the record that matches the query
  or the user's last question, with the key lines of its ancestors, verbatim, `⋮` markers and the offsets of what was
  left out. Falls back to the normal recall when the parse is uncertain, when `offset`/`path`/`mode` is given, or on
  `recall(id)` without a strong match. Same behaviour in `recall_struct`/`flex`/`multi` and in the MCP server. With the
  option on, the recall cap also counts the proxy's own header and warnings.
- **`answer_thinking_after_recall`** and **`truncation_guard`** (off by default): thinking only for the answer after a
  recall, and one replay with thinking for a short answer cut in the middle of a recalled value. The reasoning never
  reaches the client and the prompt prefix stays stable.
- Measured (one machine, Qwen3.8 Flash Next IQ3_XXS on Strata, 32K): complete evidence in the first recall 66% → 99%
  (CPU replay, 1,512 turns); 80 sealed turns 70/80 → 80/80 with the selector and thinking after recall. Details:
  `docs/dev-notes/LEAF-SELECT-RESULT.md`.
- With the default config, what reaches the engine is byte-identical to 0.3.2 (`tests/test_default_identity.py`).

## 0.3.2 — 2026-10-11

Runs natively on Windows (no WSL), with saved engine state. Available on PyPI.

- **Available on PyPI**: `pip install mnemonic-proxy`, or `uvx mnemonic-proxy demo` with uv
  (`pip install git+https://github.com/lastreload/mnemonic-proxy` still installs the development version). Releases
  are published by `.github/workflows/publish.yml` with PyPI Trusted Publishing (no token in the repository). Package
  metadata completed (Python 3.10–3.14, Linux and Windows classifiers, SPDX license); README images and links are
  absolute URLs so they render on the PyPI page.
- **Windows without WSL.** The `kv_archive` lock used `fcntl`, which does not exist on Windows: `ctxproxy/compat.py`
  takes an exclusive lock with `msvcrt.locking` there (`fcntl.flock` elsewhere, unchanged). Atomic renames retry for
  ~2.5 s when Windows reports the destination as busy (an antivirus scan, a reader of `last_request.json`). Files
  opened with `os.open` are binary (`O_BINARY`). The read-only archive of the MCP server is opened with a URI that
  works with drive letters (`file:///C:/...`); a `%` in the path is now escaped on Linux too (a directory name such as `p%2f…` made the
  read-only open fail). On Windows, manifest names are also refused if they contain `\`, `:`
  or another character Windows does not allow in a file name.
- **Console output never crashes the proxy.** On a Windows console (cp1252 and similar) characters the console cannot
  show (`—`, `⟪`, `📌`) are printed as escapes (`\u27ea`) instead of raising `UnicodeEncodeError`: `mnemonic-proxy
  demo` used to fail there.
  The archive, the journal and the prompt are untouched (UTF-8 files).
- **Client disconnect on Windows.** A client that drops a stream shows up as `ConnectionAbortedError`
  (WinError 10053) on Windows: it is now handled like a broken pipe (journal `client_disconnect`, upstream stream
  closed) instead of ending up as a proxy error.
- **CI on `windows-latest`**: unit tests (Python 3.10 and 3.14), `mnemonic-proxy demo` with a cp1252 console,
  `check` against the fake engine; on demand, the official llama.cpp `b11430` Windows CPU build (`llama-server.exe`,
  zip checksum verified) with Qwen3-0.6B: `check --live` READY, chat round trip, `examples\start-windows.bat`
  (engine + check + proxy in one file), and `tools/state_e2e.py`.
- **`tools/state_e2e.py`** (any OS): conversation through the proxy, autosave in `slot_dir`, engine restart,
  autorestore from disk, then the same turn with the state file hidden as the re-read baseline; with `--kv-archive`
  the state file is archived, deleted, rebuilt and restored by the engine. Windows CI, 3.2K tokens: restore 95 ms,
  turn 2.3 s instead of 35.7 s. Linux CI runs it as well.
- **Strata: the context window is read from the engine** (`context.native` of `/v1/status`), so masking and segment
  thresholds follow the window Strata actually runs with. Tested by the author with Strata 0.1.41 on an RTX 5060 Ti
  16 GB. Thanks to @erycsonero for the first external contribution (#2).
- CI (Linux): the cached llama.cpp build is no longer `-march=native` (`GGML_NATIVE=OFF`); a build cached on one
  runner crashed with "Illegal instruction" on a runner with a different CPU.

Maurizio Verde — LastReload

## 0.3.1 — 2026-10-08

The texts the model reads are in English for new conversations.

- **`prompt_language` (default `"en"`).** Everything the proxy writes into the prompt — placeholders and typed
  receipts of hidden outputs, the «disposable» exchange note, the note on shortened call arguments, the handoff-note
  request, the segment block (archived messages, first request, handoff notes, archive index), the pinned-points
  block, automatic recall, recall results (simple and structured, MCP server included), the receipt guard, the recall
  limit, tools paging results, the image note and the description of Codex custom-tool input — has an English text.
  `"it"` keeps the Italian texts of 0.3.0.
- **Existing conversations are unchanged.** The language is stored per conversation at its first request (like the
  tool names in 0.2.0). A conversation already in the archive without a stored language was born before 0.3.1 and
  keeps Italian: its physical prompt is byte-identical to 0.3.0, so saved sessions, anchors and KV caches stay
  valid. Checked on a long fake-engine session (26 engine requests, `tests/test_lang.py`, `tools/lang_e2e.py`).
- **Markers recognized in both languages** in every conversation: placeholders, receipts, automatic recall,
  handoff-note request, tools results, shortened arguments, the guard on copied receipts.
- HTTP/API error messages of the Anthropic and Responses layers, of `kvarchive` and of the MCP server are in English.
- Recall `order` accepts `relevance`/`time` besides `pertinenza`/`tempo`.

Maurizio Verde — LastReload

## 0.3.0 — 2026-10-06

ds4-server as an engine, Claude Code and Codex measured live, and two fixes found in those live runs.

- **Strata ≥ 0.1.40 saves sessions without patches.** Our session files (PR #668) were released in Strata v0.1.40,
  with a fix by the maintainer (RESTORE grows the elastic K/V, `--kv-grow`, before the copy). Docs now point to the
  release instead of the `session-files` branch; our Strata measurements remain those of the 0.1.38 + PR #668 build.

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
