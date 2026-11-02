# Changelog

## 0.3.24 — unreleased

- **The recall of the current turn: the proxy handed out ids it then refused.** A single human prompt followed by
  hundreds of tool calls — `opencode run`, a Hermes worker, any headless agent — is the shape where masking does
  the most work, and it was exactly the shape where recall was blind. `server.py` passed `max_idx=last_user` (the
  index of the last human message) to every recall and the archive filter was `idx < max_idx`, so **every block
  masked after that message was unreachable for the rest of the turn**: its receipt was in the prompt, its id was
  valid, its text was in the archive, and `recall(id=…)` answered `not found`. It became reachable only when a new
  user message arrived — i.e. once the turn it belonged to was over. The same filter also hid the turn's own
  `query` hits and the turn's `path` timeline.
  - **Measured on our own journals, after the fact** (31 syntaxis sessions + the 0.3.23 production journal, read
    only, indices compared against the human message recorded in each `recall` event): of 65 id calls, **32 asked
    for a block past the last human message and all 32 came from one opencode session** — the one the README
    quotes as "44 recall calls all used". 21 of them (output ids, `r…`) returned no text at all; 4 `path` calls
    came back empty although the file had been read in that very turn. The 10 `emergency=current_tool` masks of
    that session are unreachable by construction: that mask archives the LAST message of the request.
  - **Why the arguments id seemed to work.** Two code paths, two different rules. `Recall2.around`
    (`mode=output/call/neighbors`) resolved the id with **no chain filter at all**, so an arguments id (`a…`)
    answered; `_recall_page`/`recall_legacy`/`search`/`fileops` applied `idx < max_idx`, so the output id (`r…`)
    of the same message did not. The model felt the asymmetry and groped for the working id — and the one that
    worked gave 700-char excerpts, never the verbatim text. Both paths now apply the same rule.
  - **The fix is a chain filter, not a wider window.** `idx < max_idx` was doing two jobs with one line: keeping
    out sibling branches (edits, regenerations) and the current question, which is right, and keeping out the
    current agentic turn, which is not. New `chain_ok()` (`core.py`) turns the filter into
    `(idx < ? OR rid IN (SELECT rid FROM cur_chain))`, where `cur_chain` is a TEMP table holding the archive ids
    of the prefix sent **now**, minus the current question (which the model sees in full). Same parameter list, so
    every query keeps its signature. A sibling branch has different ids (`rid` = hash of index + content), so it
    stays out; a test regenerates a turn and proves the abandoned text is still unreachable.
  - **No change to the prompt.** The filter only decides what a recall answer may contain; masking, segments,
    anchors and the prompt sent to the engine are untouched. `tests/test_recall_current_turn.py` (10 tests) covers
    ids, `emergency=current_tool`, `query`, `path`, the structured engine, the arguments/output symmetry, the
    exclusion of the current question and the exclusion of sibling branches.

- **The prompt head is measurable: one journal line with the names and the weight of the tool definitions.** The
  head — template scaffolding + tool definitions + system prompt — is the largest single occupant of an
  interactive prompt (19 166 tokens median, 24.8 % of the prompt in the measurement of card `t_d1134087`), yet it
  was **recorded nowhere**: the `request` event carries aggregate counters only and the archive holds messages,
  not tools. Consequence: the two levers that act on the head (`tooldefs_desc_max`/`tooldefs_param_max` and
  `tools_paging`) could not be measured even after the fact, and a production journal with `tools_paging` on but
  **0** `tool_not_loaded` and **0** `tools_search` over 599 requests did not say whether the pager had ever run.
  New event **`tool_heads`** (`ctxproxy/toolheads.py`, switch `journal_tool_heads`, on by default), written **once
  per conversation and once per change of the tool list** — not per request: fingerprint of the list, number of
  tools, for each tool its **name** plus the tokens of the full definition and of its `description` alone, the
  counted head total (`head_tokens` = `defs_tokens` + `system_tokens` + `frame_tokens`) and, with the pager on,
  which tools stayed in the head (`base`) and which went to the catalog (`catalog`). `counting` declares `exact`
  (pack tokenizer + real template) or `estimated` (chars/`chars_per_token`); the counter is the one the proxy
  already uses for the window.
  - **Names and numbers only.** No definition text, no `description` text, no system prompt. The guarantee is
    structural, not a promise: `toolheads.payload()` never receives the tools, only their names and lists of
    integers, so it has no text to copy; the only text-dependent value is the non-invertible fingerprint. Two
    predicates (`toolheads.leaks`, `toolheads.unknown_strings`) prove it in `tests/test_tool_heads.py`.
  - **Why the pager looks silent in production.** `ToolPager.split` and its call site in `core.py` journal
    nothing; `tools_search` and `tool_not_loaded` are written in `server.py` only when the **model** calls
    `tools` or an unloaded tool. So an empty journal could not distinguish "the pager never ran" from "the pager
    ran and the model never touched the catalog". `tool_heads` now records the split itself. The split also
    depends entirely on the client's naming: with opencode's documented tools, `tools_core` keeps 5 in the head
    (`bash edit write read grep`) and sends 6 to the catalog; with Hermes' tool names (`read_file`, `terminal`,
    `search_files`, …) **no** name matches `tools_core`, so the whole list goes to the catalog and the head keeps
    only `recall` + `tools` — a test covers both.
  - **Measured cost, no behaviour change.** About 450 bytes per line, one line per 200 requests in the test, and
    the line's size does not grow with the length of the definitions; the prompt sent to the engine is
    byte-identical with the event on or off (identity test, pager on and off). `render.prompt_head` factors the
    head out of `render_pieces` with no change to the rendered text.

- **A version string is not a protocol: llama-swap gateways are no longer taken for Ollama.** A gateway that
  exposes `GET /api/version` but answers HTTP 404 to `GET /api/ps` was identified as Ollama, reported "no model
  loaded" and suggested `ollama run`. Recognition now requires both the version answer and a coherent `/api/ps`
  models array (an empty array is valid: no model loaded); an unsupported, failed or malformed answer leaves the
  engine generic instead of empty. `check` now distinguishes an empty runtime list, unavailable runtime metadata
  and a loaded model whose context is not reported, uses the runtime metadata — never the `/v1/models` catalog —
  as loaded-state evidence for Ollama and LM Studio, and reports a generic gateway's catalog as model
  availability with the loaded state unverified. A generic window is labelled unverified whether it comes from
  the config or from the default: the proxy cannot discover a gateway's per-model contexts. The minimum-window
  and known-context mismatch guards are unchanged.

## 0.3.23 — 2026-11-02

- **Pictures in pack mode: forwarded to the engine and budgeted, no longer refused (issue #60, patch by
  @lupus0).** With `--tokenizer` on a Strata pack, a request with a picture used to fail with a misleading 400
  ("prompt 131072 + margin 64 leaves no room in the window"). Now the template writes one vision marker per
  picture and the guard prices each one at `image_tokens`, an upper bound (`image_tokens >= vision.max_tokens + 2`;
  default **1026**, Strata's GPU clamp of 1024 plus the two markers that stay; 302 for a CPU reader clamped to 300).
  `window_guard` events for such prompts carry `guarantee=image_bounded`; text-only prompts stay `exact` and
  byte-identical. Fail-closed where the count could not be honest: a picture in a `system` message, a picture with
  no readable source, a template without a vision branch, video and audio are refused with the real reason. Picture
  bytes never reach disk (`live_dump` and the KV sidecar name a picture by hash), and saved-state identity includes
  the pictures. Tested live on Strata 0.1.50 with a CPU picture reader: 0.3.22 returned the 400; with the patch the
  model read the test picture correctly, at about 230 prompt tokens per picture. Review fix on top: the default was
  1024, which broke the rule on a GPU reader (an under-count of 2 per picture).
- **When a count fails, the client gets the real reason** instead of the window message above (same patch).

- **The proxy no longer re-delivers a client's out-of-band message (issue #61).** Reported by @akohlsmith with
  Hermes over Slack: a mid-turn steering message he sent **once** ("there is no vision model running on port
  8080; the proxy is 8001 …") reached the model **5+ times**, in new positions, sometimes after a segment
  change, while Hermes' own session database held exactly one copy of it. The model kept acknowledging the
  rule because every copy still looked like a fresh delivery.
  - **Why.** Hermes delivers a `/steer` as a standalone `role:"user"` message wrapped in a frame the model is
    told to trust: `[OUT-OF-BAND USER MESSAGE — a direct message from the user, delivered once at this
    position; …] … [/OUT-OF-BAND USER MESSAGE]` (`agent/prompt_builder.py`, `steer_user_row`). The proxy
    re-presents archived text at **new** positions — handoff notes (the notes instruction asks the model for
    the user's constraints "almost verbatim", so a compliant model copies the frame too), `keep_first_user`'s
    *First user request (verbatim)*, the 📌 pinned-points block, automatic recall pieces, a `recall` result —
    and the frame came along intact. Three of those blocks are themselves `role:"user"` messages, so the copy
    was indistinguishable from the real thing. With `auto_recall: true` the same frame appeared **twice in one
    physical prompt**; with the default configuration it appeared once per prompt but moved to a new position
    at every segment change, which is the same thing seen one turn at a time.
  - **Fix** (`core.quote_control_frames`, pure and idempotent): in every copy the proxy itself writes, a
    client control frame is relabelled — `[OUT-OF-BAND USER MESSAGE …]` becomes
    `[quoted OUT-OF-BAND USER MESSAGE …]`, `[/OUT-OF-BAND USER MESSAGE]` becomes
    `[quoted /OUT-OF-BAND USER MESSAGE]`. Same words, whole text, visibly a quotation. This is the convention
    Hermes itself uses when it republishes model text as `role=user` (`[member-quoted …]`). Also covered:
    `CONTEXT COMPACTION`, `CONTEXT SUMMARY`, `PRIOR CONTEXT`.
  - **The real delivery is never touched**: in its own place in the conversation the user's message stays byte
    for byte what the client sent. Existing conversations keep their prefix: notes, pinned-points blocks and
    automatic-recall insertions are stored when they are built, and stored text is replayed unchanged — only
    newly built blocks carry the new label. Masked tool-output placeholders and receipts are deliberately left
    alone, because recomputing them would change the prefix of every live conversation.
  - Tests: `tests/test_issue61_steer_echo.py` (notes, verbatim first request, pinned points, automatic recall,
    `recall` result, and the invariant "at most one intact frame per physical prompt").
- **The first run of a new user, fixed where it actually broke.** A trial of the README by a fresh user on a
  clean machine (`docs/dev-notes/README-NEWUSER-RESULT.md`) got stuck before the proxy ever ran. Eight frictions,
  all of them now closed; `main` also reports its version as `0.3.23.dev0` instead of claiming to be the last
  release.
  - **A `--config` file that does not exist is a sentence, not a traceback.** The Install block told the user to
    pass `--config config.json` and never to create it; the proxy answered with a raw `FileNotFoundError`
    traceback and `check` stopped at the first line advising *"fix the JSON file"* about a file that was not
    there. Now the proxy prints one line with the three real options (create it, with the command; or drop
    `--config`: the defaults work), and `check` reports it as one `❌ config` line **and goes on with every other
    check** against the defaults — the engine is what the user came to verify.
  - **`mnemonic-proxy init-config --engine strata|llama-server|ds4|openai-32k`** writes `config.json` from the
    example that now ships *inside* the package (`ctxproxy.examples`, mapped from the repository's `examples/`).
    No clone, no network, nothing to copy by hand; `--dir`, `--name` and `--force` are there, and an existing
    file is never overwritten without `--force`. `pip install mnemonic-proxy` does not create an `examples/`
    folder, so every hint that names an example now prints either this command or the full
    `raw.githubusercontent.com` URL **of the version that is installed** (`vX.Y.Z` for a release, `main` for a
    `.dev` build, so a copied link is never a 404).
  - **The configuration hint follows the engine that was detected.** `check` without a `--config` used to say
    *"for llama-server use examples/config.llama-server.json"* in front of any engine, Strata included. It now
    says what to do for Strata, llama-server, ds4-server or a generic OpenAI engine — and stays generic when the
    engine never answered.
  - **`check`'s three outcomes are documented**: `READY` (exit 0), `READY with warnings` (exit 2) and
    `NOT READY` (exit 1); the README only mentioned the first.
  - **The automatic-configuration summary stays one line** on a data folder that already has conversations:
    *"N choices not applied: this data folder already has conversations, so an upgrade keeps how they ran"*,
    with each value and reason left to `check --explain-config`, instead of repeating the same sentence once per
    field.
  - **The fake engine is reachable the way the README says**: `python3 -m ctxproxy.fake_engine` (it moved into
    the package in 0.3.2; `tests/fake_engine.py` is only an import shim), and the Install section now shows how
    to try `check` and the proxy with no engine installed at all.
  - Smaller documentation fixes from the same trial: how to get `uv` when it is missing, that
    `pip install git+…` needs `git` on PATH, and that `$XDG_CONFIG_HOME/opencode/opencode.json` is usually
    `~/.config/opencode/opencode.json`.
  - The shutdown test `test_sigint_exits_promptly_with_idle_keepalive_connection` no longer fails on a loaded
    machine: it waits for the proxy to really serve (up to 60 s) and then gives the shutdown a budget scaled to
    that same machine's measured start-up time. What it proves is unchanged — an idle keep-alive connection must
    not block shutdown, and a hang still fails.

- **Automatic configuration: the proxy now chooses the engine/machine-dependent options, out loud.** Reported
  on r/LocalLLM: *"why is the default config not the recommended config? It would be so nice if it could detect
  the engine version, context size, VRAM + RAM and apply the best settings by itself"*, by a user who had
  enabled saved state by hand and then found a 2 GB state file eating the RAM he needed for the model. The
  answer was "because those values depend on your engine and your machine, and the proxy did not look". It
  looks now (`ctxproxy/autoconfig.py`), and nothing is silent: every choice carries its reason and the way to
  force it otherwise.
  - **What is read**, all from endpoints and files already in use, no new dependency and no telemetry: the
    engine version (Strata `/v1/status` → `engine`, e.g. `0.1.51`; llama-server `/props` → `build_info`), the
    context window, saved-state support, how many requests the engine serves at once (Strata `concurrency`,
    llama-server `total_slots`), whether the engine is on this host, RAM total and available (Linux
    `/proc/meminfo`, macOS `sysctl` + `vm_stat`, Windows `GlobalMemoryStatusEx`), the free disk of `slot_dir`,
    and — only when it comes for free — VRAM from the GPU figures Strata already reports, otherwise
    `nvidia-smi` if it happens to be on PATH. Nothing is required: an unknown figure never blocks a choice.
  - **What is decided**, when the user did not write it: `slot_save`, `mask_anchor` and `autosave` (the engine
    offers saved state, it is on this host, `slot_dir` is set, and one state file fits in the available RAM —
    `autosave` also needs the disk); `response_floor` = `window / 8` and `notes_max_tokens` =
    `window * 3 / 32` (16384 and 12288 at 131072, 4096 and 3072 at 32768), from a 16384 window up;
    `engine_tokenize` when the engine counts exactly and no `--tokenizer` was given. So the recommended
    saved-state setup is now reached by writing **one** field, `slot_dir`.
  - **The sizing number is explicit and has a source**: a state file costs about 19.5 KB per token of window
    (`state_kb_per_token` — the 20000 bytes/token `autosave.py` already uses for its own disk check, so the two
    agree), so 131072 tokens ≈ 2.4 GiB. The measured spread is wide (37–73 KB/token for Qwen3.5 35B-A3B,
    7.6–9 KB/token for gemma E2B, ~115 KB/token for llama-server with Qwen3-0.6B), which is why it is a field
    and why the rule is a floor and not a promise. With less RAM available than that, the saved-state options
    stay off *and say why*, instead of filling the RAM the model needs. That is the r/LocalLLM case, decided
    for the user.
  - **Nothing that changes the prompt is touched.** An old conversation must be replayed byte for byte, so the
    recommendations that change what the proxy writes into the prompt (`mask_reasoning`, `mask_tool_args`,
    `recall_struct`, `recall_flex`, `recall_multi`, `tools_paging`) keep their default and the README now says
    *why*, together with the three that stay opt-in because of what they spend (`seal_experimental`,
    `kv_archive`, `live_dump`). Making them defaults needs the same per-conversation storage as the receipts.
  - **Seen without starting anything**: `mnemonic-proxy check --explain-config` prints what was read, every
    option chosen, every option left alone, each with its reason and its override; `--json` carries the same
    under `auto_config`. The startup log prints the applied choices as `[mnemonic-proxy] auto config: …` and
    `journal.jsonl` gets one `auto_config` event. `"auto_config": false` turns the whole mechanism off.
  - **Upgrades keep what was running**: the choices are applied only on a fresh data folder. When the data folder
    already has archived conversations they are reported (`kept`, with the value that would be picked and why)
    and nothing changes, unless `"auto_config": true` is written. The proxy cannot tell a PolyStrata release
    before v0.1.45-poly.7 from Strata, and there the first save on a `strata-poly` model ends the engine: an
    upgrade must never turn saved state on by itself (`tests/test_autoconfig.py`, `TestExistingInstall`).
  - Because `check` now reports the configuration the server would really run with, a Strata or llama-server
    setup with `slot_dir` goes from *"supported by the engine, not enabled in the proxy config"* to
    *"supported by the engine, configured in the proxy (slot_save, mask_anchor, autosave)"* with no edit.
    Below a 16384 window nothing is decided: the proxy already refuses to start there without an explicit
    opt-in, and the documented choice is the scaled defaults.
  - Measured output engine by engine, the Strata 0.1.51 source reads behind the `concurrency` rule, and what is
    deliberately *not* decided: `docs/dev-notes/AUTOCONFIG-RESULT.md`.

## 0.3.22 — 2026-11-01

- **Guards on the calibration of the token estimate.** The factor that corrects the chars/3.5 estimate from the
  engine's `usage.prompt_tokens` could be moved to its full extent by a single sample, in either direction; a
  sample that pulls it *down* makes the proxy estimate less than the real prompt, mask too late, and risk a prompt
  the engine cuts in silence. Now a sample is accepted only above 1,000 estimated tokens and with a real/estimated
  ratio inside 0.25–4.0 (outside it the sample is rejected and journalled as `calibration_rejected`), and until 5
  samples have been accepted the factor may only grow, never shrink. Masking decisions are unchanged on the
  language scenario: only the token counts the proxy prints move, by under 5% on blocks masked during the warm-up
  (`tests/test_calibration_guards.py`), and the 0.3.0 byte-identity lock still holds for the old rule
  (`TokenCounter(min_samples=1)`).

  The lower clamp stays **0.5**, deliberately. Replaying 68 recorded local sessions (3,446 first-round requests
  counted by estimate) the factor ran below 1.0 in 525 of them (15.2%), down to 0.514; the warm-up guard alone
  brings that to 409 (−22%). Raising the clamp to 1.0 would remove them all, but it only moves the share of
  requests whose estimate is below the real prompt from 66.5% to 63.6%, while the 95th percentile of
  estimated/real goes from 1.047 to 1.211: window given away for almost no protection. chars/3.5 is already a
  pessimistic baseline for prose (unlike the 4.0 of the tool this guard was modelled on), so a factor below 1.0 is
  a legitimate correction, and prudence against a cut prompt belongs to `window_estimate_slack` and to the
  `engine_truncated` detection, which already excludes a cut prompt from calibration (0.3.8).

- **Setup: saved state is optional, and the session folder is the proxy's job.** Reported on r/LocalLLM: the
  setup looked like it required `--slot-save-path` in the Strata configuration and a session folder created by
  hand. It never did, and now nothing suggests it does.
  - The proxy **creates `slot_dir` itself** (with its parents) when it is missing, both in `check` and at
    startup, and says so (`slot_dir … (created by the proxy)`; `created the session folder …`). When it cannot,
    the message carries the exact `mkdir -p` command. It does **not** create it when the engine is not on this
    host: the local path would not be the engine's `--slot-save-path`, so `check` keeps failing with that
    reason instead of hiding the mismatch behind an empty folder. `check --live` still verifies that the state
    file really appears in `slot_dir`.
  - `check` no longer calls base mode "disabled": with an engine that supports saved state but a config that
    does not enable it, the line now reads *supported by the engine, not enabled in the proxy config: the proxy
    works without it* and names what keeps working (masking, the verbatim archive, `recall`, segments with
    handoff notes) and the one thing that is lost (the fast resume of a long session), plus how to turn it on
    later. The old hint pointed Strata users at a llama-server example file.
  - The **startup banner** reports the configuration, not just the engine: `saved state yes` only when a
    save-producing option is on, otherwise `saved state off (optional)` followed by one line saying what still
    works and how to enable it later. Before, a Strata engine printed `saved state yes` even with every
    saved-state option set to `false`.
  - **README**: a new *Strata in two commands* section — `pip install` plus one `mnemonic-proxy` line, no
    configuration file, no folder to create, no engine flag — with saved state as a clearly optional later
    step that gives the exact Strata command line and the three config fields. The engine table, Requirements
    and Troubleshooting no longer present `--slot-save-path` and `slot_dir` as part of the basic setup, and T1
    no longer tells the user to `mkdir` the proxy's own data folder.
  - New tests: `tests/test_setup_optional_saved_state.py` (folder creation, the remote-engine refusal, the
    unwritable-parent message, the `check` and startup wording, and the README's minimal path executed against
    the fake Strata engine).

- **README: opencode** — opencode 1.19.5 tested live against the proxy on Strata (Qwen3.8 Flash Next IQ3_XXS),
  one 1 h 32 min session: 554 requests, real history up to 1.17 M tokens against a physical prompt of at most
  108K, 51 masks, 3 segment switches, 44 `recall` calls by the model all served and used, 669 tool calls with no
  empty result and no HTTP error. Masking costs ~5 s of extra prefill on the request where it happens (median
  7.0 s against 1.9 s) and nothing on the next one. New *opencode (Chat Completions)* section with the minimal
  copyable configuration (`provider` + `npm`, not the newer plural `providers`), the real auto-compaction switch
  (`"compaction": {"auto": false}`) and the limits found; new *opencode on Strata, measured* table under *Tested
  combinations*; opencode moved from the untested clients to the tested ones in the engines table.

- **Richer receipts for a hidden tool output.** A receipt said what the call did and how it ended; it did not say
  how big the output was, which kind of error it was, nor where the failure *started*. It now also carries the
  output's size in lines and UTF-8 bytes, the error type (`AssertionError`, `command not found`, …) and the
  **first** error line, cut at 160 characters — the last lines are still there for a failure. An output whose kind
  is not recognised gets a receipt too (size, error fields and its first line) instead of only the first 140
  characters. Every field is read off the archived bytes with fixed rules: no model, no clock, no counter, so the
  same output always gives the same receipt and the placeholder stays a fixed point inside the cacheable prefix.

  Measured cost (with `rich_receipts: true`) on the regression fixture of an oversized tool result: the prompt after
  masking goes from 8,885 to 8,897 estimated tokens, +12 tokens for one receipt. The receipt is in the stable prefix, so it is paid once per
  masked block.

  Like the receipt language in 0.3.1, the choice is made at a conversation's first request and remembered: a
  conversation that already has an archive keeps the receipts it was masked with, so its prefix stays byte-identical
  and the engine's KV cache is not invalidated.

  **Off by default** (`rich_receipts: false`): the cost is measured, the benefit is not. A richer receipt could also
  make a model reason on the first error line instead of recalling the full output, so it stays opt-in until an
  A/B run shows it does not hurt. Set `rich_receipts: true` to try it on new conversations.

## 0.3.21 — 2026-11-01

- Saved state: the HTTP 501 that PolyStrata v0.1.45-poly.7 returns on `strata-poly` models ("slot save/restore is not
  available with strata-poly, this model's engine", VecSzn/PolyStrata#114) is recognised as permanent, so saved
  state stays suspended for that model instead of pausing and retrying every 15 minutes. README: with poly.7 or later
  the engine is no longer ended; older releases still need the saved-state options off for those models.

- **The README is under test** (three levels, standard library only). A documented command that
  no longer exists is a worse bug than a missing feature: this README once showed
  `mnemonic-proxy check` before the subcommand existed. Every fenced block now carries an
  invisible label in an HTML comment above the fence — `ci`, `windows`, `real-engine` or
  `illustrative` — and JSON blocks also say whether they are a full proxy configuration, a
  partial one, or another program's (`tools/readme_blocks.py` lists them).
  - level 1, `tests/test_readme_static.py`: every executable block is labeled; every
    `mnemonic-proxy` / `mnemonic-mcp` command line in the README parses with the parser the
    program really builds; every proxy-configuration example uses only real `Config` fields and
    is accepted by `Config.from_dict` (the shipped `examples/*.json` too); every internal
    `#anchor` link resolves to a heading.
  - level 2, `tests/test_readme_run.py`: the `ci` blocks are executed for real against
    `ctxproxy.fake_engine` in both the llama-server and the Strata flavor — `demo` prints PASS,
    `check` says READY, `check` with no engine says NOT READY and *start the engine first*, the
    proxy answers a Chat Completions request, `forget --list` runs, `mnemonic-mcp` answers an
    MCP `initialize` and lists `recall` / `conversations` / `journal`. `tools/readme_run.sh`
    builds the wheel, installs it in a clean venv and runs them from a folder with no source
    tree, so `import ctxproxy` can only resolve to the installed package; the new CI job
    `readme-run` does the same on Python 3.10 and 3.14.
  - level 3, `tests/test_readme_coverage.py`: no subcommand, command-line option or `Config`
    field may exist in the code without being documented in the README or declared internal in
    `tools/readme_internal.txt` with a reason. It found 39 undocumented `Config` fields and six
    undocumented options, now documented: `--mode`, `--version`, `check --json`, `demo --keep`,
    `mnemonic-mcp --journal` and `--max-tokens`.
  - `check`, `demo`, `forget` and `mnemonic-mcp` expose their parser builder
    (`build_check_parser`, `build_demo_parser`, `build_forget_parser`, `build_mcp_parser`) so
    the tests use the real parsers instead of a copy. No behaviour change.
  - new README sections: «Every command-line option», «Every configuration field» (the 39
    remaining `Config` fields with their real defaults) and «This README is tested».
    `tools/readme_newuser.sh` is the "new user" walk-through for a clean container; developer
    notes in `docs/dev-notes/README-TESTS.md`.

## 0.3.20 — 2026-10-31

- Saved engine state on engines that do not support it (#28, #46, #47). After a reply that means "not supported",
  the proxy stops saved-state work (anchors and their one-token calls, segment seal/save, SAVE/RESTORE,
  autosave/autorestore, `kv_archive`) for that model only; masking, the text archive, `recall` and notes keep
  working. The documented PolyStrata strata-poly HTTP 500 ("the server ended the engine") and documented permanent
  HTTP 501 texts (no `--slot-save-path`, parallel requests) suspend it until restart or
  `POST /v1/strata/saved-state/reset` (accepted only from this machine); any other HTTP 501 pauses it for 15 minutes,
  then a single call retries. `GET /v1/strata/saved-state` lists suspended models. One stderr warning and one journal
  event per suspension. Missing files, temporary storage or memory failures, generic HTTP errors and timeouts do not
  suspend it. `check --live` sends no SAVE/RESTORE when all six saved-state options are false. Limit: on current
  PolyStrata releases the first save to a strata-poly model still ends the engine once; set the six saved-state
  options to `false` for that model to avoid it.
- The saved-state model is now selected under the proxy lock, so a request waiting for the lock no longer changes the
  model of the one in flight.
- Router mode: slow metadata reads during streaming send a neutral keep-alive instead of falsely announcing a model
  load (#50).
- `mnemonic-proxy demo`: the closing hint works after `uvx mnemonic-proxy demo` (uvx installs nothing): it suggests
  `uvx mnemonic-proxy check --upstream ...` or `uv tool install mnemonic-proxy`, and says that `check` needs a running
  engine (#51).
- README: clearer introduction with the one-minute demo first and a table of measured results (#48); handoff notes
  and T1 recall expectations described precisely (#49).

## 0.3.19 — 2026-10-28

- Context fitting with fine-tunes that ship their own chat template: when the tokenizer directory contains
  `chat_template.jinja`, the proxy now counts the prompt with that template (sandboxed Jinja, `[exact]` extra) instead
  of the built-in Qwen rendering. A Tiel/Sharp setup was under-counting by a few hundred tokens and sent requests the
  engine rejected (#28, #43, #44). Startup reports `template=pack` or `builtin-qwen`; on the stock Qwen3.8 pack the two
  counts are identical. Prompts that leave no room to answer are refused locally; a recognised engine context
  rejection is retried once with smaller output limits. Short stderr summaries of key events (window guard, clamps,
  masking, segment switches) with request phase labels; client disconnects no longer print a BrokenPipe traceback.
  Pack-template mode is text-only: requests with images are refused explicitly.
- A single fresh tool result that leaves no room for an answer is now archived behind the usual `recall` placeholder
  (only when `recall` is exposed and only if this restores a useful answer budget), instead of the request being
  forwarded with `max_tokens` clamped to 1. If nothing fits, the proxy returns an explicit context error (#40, #42).
- Tests: zstd fixtures no longer depend on the interpreter's `sitecustomize` (fixes 7 failures on Ubuntu with
  Python 3.14) (#39, #41).

## 0.3.18 — 2026-10-28

- A closed search whose final reply is only an internal `recall` call written as text (`<tool_call><function=recall>…`)
  is no longer returned as a successful answer. When the search is closed the proxy removes its own tools, and engines
  that parse only declared tools (Strata) then return such a call as plain text with `finish_reason: stop`; it now goes
  through the same final-answer retry and explicit notice as an empty reply (#36, #38). In streaming, a possible call
  at the very start of the answer is held back until it is known to be ordinary text. Quoted examples, prose and
  client tool calls are unchanged.
- Docs: tested Hermes Agent setup (`docs/hermes-agent.md`): custom provider, `compression.enabled: false`, tools and
  `recall` through the proxy, 18-turn session over a 32K window with masking and recall, 99.4% prefix reuse (#37).

## 0.3.17 — 2026-10-28

- Refuse startup if an explicitly supplied `--tokenizer` cannot load (missing optional dependency, bad path or
  invalid format), rather than silently replacing exact counting with an estimated guard. Tokenizer-free usage
  remains supported. Add CPU startup regressions and an observed-counter budget-gate diagnostic fixture.

## 0.3.16 — 2026-10-28

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
