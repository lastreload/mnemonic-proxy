# virtual-context-proxy

An OpenAI-compatible HTTP proxy that sits between a coding agent and a local
[Strata](https://github.com/Niko1221/Strata) server and gives the agent a **virtual context**:
the conversation can grow far beyond the physical window (e.g. 400K+ tokens of history on a
128K window) while the prompt that Strata actually reads stays bounded, stable, and cheap to
reuse from its prefix cache.

The agent keeps sending its full, unmodified history. The proxy decides what Strata sees.

## What it does

- **Gradual masking with a stable frontier.** When the physical prompt crosses a trigger
  (default 80K tokens), old tool outputs, old reasoning and large old tool-call arguments are
  replaced, in one batch, by short placeholders with provenance (tool, file, size, first
  line, archive id). Masking decisions are persisted, so between two batches the physical
  prompt only grows at the tail and Strata's prefix cache keeps working. A monotonic
  frontier plus an optional slot "anchor" (save/restore of the KV state at the frontier)
  limits what has to be re-read after a batch.
- **Exact archive + `strata_recall`.** Everything masked goes into a SQLite archive with
  FTS5. The proxy injects a `strata_recall` tool (by `id`, by text `query`, or by file
  `path` history) and executes it server-side; the client never sees these calls, even
  when streaming.
- **Segments with handoff notes.** When even a masked prompt would not fit, the proxy asks
  the model for handoff notes at the end of the current segment (A), optionally saves A to
  disk, and starts a new segment B = system + tools + notes + recall index + recent tail.
  Later requests from the client are mapped onto B transparently.
- **📌 pins and 🗑 disposable messages.** User lines starting with `📌` (or `!!`, or
  `[[importante]]…[[/importante]]` blocks) are carried verbatim into every new segment.
  User messages with a line starting with `🗑` (or `~`) are dropped from the physical prompt
  once answered. Both can also be toggled from the dashboard.
- **Autosave / autorestore.** When the active conversation is idle (default 180 s) and
  Strata is free, the proxy saves Strata's state to disk. If Strata later restarts or serves
  another conversation, the proxy restores the file before forwarding the next request,
  provided the saved text is an exact prefix of the new prompt.
- **Exact token counting** (optional): renders the Strata chat template in pure Python and
  tokenizes with the Strata pack tokenizer (`tokenizers` package), matching Strata's
  `prompt_tokens` exactly. Without it, a calibrated chars/3.5 estimate is used.
- **Real streaming**, client-disconnect propagation (cancelling the generation upstream),
  a JSONL journal of every decision, and a read-only live **dashboard**.

## Who it is for

People who already run Strata locally and use a tool-calling agent (tested with
[pi](https://github.com/badlogic/pi-mono)) for long sessions, and would rather not have the
agent compact (summarize and discard) its history every time the window fills up.

## Quick start

Requirements: Python ≥ 3.10 with `sqlite3` FTS5 (standard in CPython builds). No other
dependency; `tokenizers` is optional for exact counting.

1. Run Strata as usual (here on `127.0.0.1:8095`).
2. Start the proxy:

   ```sh
   git clone https://github.com/maverde73/virtual-context-proxy
   cd virtual-context-proxy
   python3 -m venv .venv && . .venv/bin/activate
   pip install -e '.[exact]'          # or: pip install -e .   (no exact counting)
   cp examples/config.official-strata.json config.json   # see "Compatibility"
   ctxproxy --upstream http://127.0.0.1:8095 --port 8096 --data ./data --config config.json \
            [--tokenizer /path/to/strata/packs/<pack>/tokenizer]
   ```

   `--tokenizer` accepts either a Hugging Face `tokenizer.json` or the `tokenizer/` folder of
   a Strata pack (it is converted on the fly; `tools/make_hf_tokenizer.py` does the same
   offline).
3. Point the agent at `http://127.0.0.1:8096/v1` instead of Strata. For pi, add a provider
   in `~/.pi/agent/models.json` with that base URL and **disable pi's auto-compaction**
   (`"compaction": {"enabled": false}` in `settings.json`), otherwise two context managers
   fight over the same history.
4. Optional dashboard:

   ```sh
   python3 dashboard/ctxdash.py --data ./data --strata http://127.0.0.1:8095 \
           --proxy http://127.0.0.1:8096 --port 8097
   ```

   Open `http://127.0.0.1:8097`. It shows Strata status, physical vs virtual context over
   time, masking/segment/recall events, and the live physical prompt (with `live_dump`).

`--mode off` turns the proxy into a pure pass-through (useful as a baseline). An example
systemd user unit with placeholder paths is in `examples/ctx-proxy.service`.

## Configuration

`--config` takes a JSON object with any field of `ctxproxy.core.Config`. Main ones:

| field | default | meaning |
|---|---|---|
| `window` | 131072 | physical context of the Strata server |
| `mask_trigger` / `mask_target` | 80000 / 40000 | start a masking batch above trigger, mask oldest-first down to target |
| `min_batch_tokens` | 16000 | a batch runs only if it frees at least this much |
| `keep_recent_tokens` / `min_age_turns` | 16000 / 2 | protected tail, never masked |
| `mask_reasoning` / `mask_tool_args` | false / false | also mask old reasoning and large old tool-call arguments (recommended: true) |
| `mask_anchor`, `anchor_min_tokens`, `slot_dir` | false, 24000, "" | save/restore a KV anchor at the masking frontier (needs session files) |
| `segments_enabled`, `reserve`, `tail_max`, `notes_max_tokens` | true, 8192, 24000, 8192 | segment switch and handoff notes |
| `slot_save` / `seal_experimental` | false / false | save segment A to disk before switching (needs session files) |
| `inject_recall`, `max_recall_rounds`, `recall_max_tokens` | true, 4, 6000 | `strata_recall` tool |
| `pins_max_tokens` | 8192 | cap for the carried-over 📌 block |
| `autosave`, `autosave_idle_s`, `autosave_keep`, `autosave_max_gb`, `autosave_min_free_gb` | false, 180, 10, 25, 8 | idle autosave (needs session files) |
| `kv_archive`, `kv_archive_idle_s`, `kv_archive_min_age_s`, `kv_archive_threads` | false, 600, 1800, 4 | cold archive of session files: content-addressed zstd blocks with cross-file dedup, original deleted only after sha256-verified rebuild, rebuilt on demand before a restore (`ctxproxy/kvarchive.py`, CLI `python3 -m ctxproxy.kvarchive`) |
| `autorestore`, `autorestore_min_gain` | true, 8192 | restore before forwarding when it saves at least this many tokens |
| `response_floor` | 0 | minimum response size assumed in the window check |
| `live_dump` | false | write `data/live/last_request.json` for the dashboard |

Extra endpoints: `GET /v1/strata/archive/<id>`, `GET /v1/strata/conversations/<conv>`,
`GET /v1/strata/journal`, `GET|POST /v1/strata/marks[/<conv>]` (📌/🗑). Every response carries
a `strata_context` field and an `X-Strata-Context` header. An optional
`X-Strata-Conversation` header (or `prompt_cache_key`) binds a request to a conversation
explicitly; otherwise conversations are recognised by a hash chain over the messages.

## Using it with Claude Code / Codex

Besides OpenAI Chat Completions (`/v1/chat/completions`), the proxy accepts:

- **Anthropic Messages** — `POST /v1/messages` (streaming and not) and
  `POST /v1/messages/count_tokens` (same counter the proxy uses for its own window checks);
- **OpenAI Responses** — `POST /v1/responses` (streaming and not; stateless: `store=false`,
  no `previous_response_id`).

Both are converted to Chat Completions before the context manager and back on the way out, so
masking, archive, `strata_recall`, segments, receipts and on-demand tools work the same; the
proxy's own tools never reach the client.

**Claude Code** (tested with 2.1.287):

```sh
export ANTHROPIC_BASE_URL=http://127.0.0.1:8096      # no /v1
export ANTHROPIC_AUTH_TOKEN=local                    # any value; the proxy does not check it
export ANTHROPIC_MODEL=qwen-local                    # any name; Strata serves its loaded model
export ANTHROPIC_SMALL_FAST_MODEL=qwen-local
export CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1
claude                                               # or: claude -p "…"
```

Disable Claude Code's auto-compaction (`export DISABLE_AUTO_COMPACT=1`, or `/config` →
Auto-compact off), otherwise two context managers fight over the same history.

**Codex** (tested with codex-cli 0.160.0). Codex 0.160 only speaks the Responses API:
`wire_api = "chat"` is rejected ("`wire_api = "chat"` is no longer supported"). Add a provider
to `~/.codex/config.toml` (or to a separate `CODEX_HOME`):

```toml
model = "qwen-local"
model_provider = "strata"

[model_providers.strata]
name = "Strata via virtual-context-proxy"
base_url = "http://127.0.0.1:8096/v1"
wire_api = "responses"
env_key = "STRATA_API_KEY"          # export STRATA_API_KEY=local (any value)
requires_openai_auth = false
supports_websockets = false
```

Codex sends `prompt_cache_key` (its session id), which the proxy uses as the conversation key.
Codex warns "Model metadata for `qwen-local` not found": harmless; set `model_context_window`
to Strata's window if you want Codex's own accounting to match.

Conversion details and limits:

- reasoning ↔ `thinking` blocks / `reasoning` items; Anthropic `signature` is a local
  placeholder (hash of the text), incoming signatures and `encrypted_content` are ignored;
- tool ids: Anthropic gets `toolu_<internal id>`, Responses keeps `call_id` unchanged;
  several tool calls per turn, `tool_result` with block content and `is_error` are supported;
- Codex `custom` tools (e.g. `apply_patch`) become a function with one `input` string and
  come back as `custom_tool_call`; hosted tools (web search etc.) are dropped;
- images in user messages are rejected with a 400 in the client's error format; images inside
  tool results are replaced by a text note; `cache_control` is ignored;
- `x-anthropic-billing-header` system blocks (they change every request) are dropped so the
  hash chain and Strata's prefix cache stay stable.

## Compatibility

| Strata build | what works |
|---|---|
| Official Strata (no session files) | Reduced mode: masking with stable frontier, archive + `strata_recall`, segments with handoff notes, 📌/🗑, streaming, exact counting, dashboard. Use `examples/config.official-strata.json` (`mask_anchor`, `slot_save`, `autosave` off). After each masking batch Strata re-reads from its last valid prefix checkpoint. |
| Strata with session files (`/slots/0?action=save|restore`, proposed in [Niko1221/Strata#668](https://github.com/Niko1221/Strata/pull/668)) | Everything above, plus: masking anchor (fewer tokens re-read per batch), segment A saved to disk, and idle autosave/autorestore — resuming a 69.6K-token conversation after a Strata restart took 2.9 s instead of 33.1 s. Use `examples/config.example.json`. |

The proxy does not modify Strata and uses only its public HTTP API
(`/v1/chat/completions`, `/v1/status`, and the slot endpoints when present).

## Measured results

Hardware: RTX 4070 Ti 12 GB, 64 GB RAM, NVMe. Strata 0.1.38 (+ session files where noted),
model Qwen3.8-Flash-Next GSQ-RCO IQ3_XXS with MTP speculative decoding, 128K context
(131072), int8 KV. Agent: pi 0.84.2 (49 tools). Numbers are from single runs on one machine
and one kind of workload (an agent writing a small browser game); treat them as indicative.

**Strata primitives (session files build)**

| context | save | restore | full re-read | decode |
|---|---|---|---|---|
| 16K | 0.36 s | 0.34 s | 7.0 s | 53 tok/s |
| 64K | 0.60 s | 0.87 s | 25.3 s | 50 tok/s |
| 125K | 0.92 s | 1.52 s | 50.7 s | 52 tok/s |

Prefill is flat at ~2.3–2.5K tok/s from 16K to 125K. Session file size ≈ 236 MB + 15.3 KB/token.

**Full replay of a real 3h50 pi session** (362 requests, history kept whole instead of
compacted, virtual history ~400K tokens): 362/362 OK, physical prompt ≤ ~106K, 15 masking
batches, 1 segment switch; median request 2.1 s, p95 3.3 s.

Recall probe at the end of that session (26 questions with known answers):

| arm | correct |
|---|---|
| proxy (masking + segments + `strata_recall`) | 22/26 |
| pi's own compaction summary | 20/26 |
| plain truncation to the last ~96K tokens | 16/26 |

The proxy's advantage is on exact old facts recovered through `strata_recall`; it missed
two where text search did not find the right block.

**Live session with pi** (414 requests, 2h05): 13 masking batches (median ~22 s each,
re-reading 17–62K tokens), 2 segment switches (notes generation 68–78 s), normal requests
re-read a median of 155 tokens (p95 1,268). After the post-session fixes, the exact counter
matched Strata's `prompt_tokens` on 200/200 replayed requests, and anchor restores succeeded
6/6 (0.56–0.88 s).

**Masking anchor** (150 replayed requests, session files build): tokens re-read per batch
40.6K → 22.6K, total re-read −34%, total time 446 s → 402 s.

**Autosave** (session files build, ~69.6K-token conversation):

| resume after Strata restart | time | tokens re-read |
|---|---|---|
| with autorestore (restore 0.93 s) | 2.9 s | 38 |
| without (cold re-read) | 33.1 s | 69,361 |

## Known limitations

- One sequence: the proxy serialises requests, and the anchor/autosave logic assumes it is
  the only client of Strata. Another client hitting Strata directly costs re-reads (with
  autosave enabled it is detected through `/v1/status` and the cached state is invalidated).
- Each masking batch still costs a re-read of the span between two frontiers (~9–12 s with
  anchors, 12–60 s without). A segment switch costs the time to generate handoff notes
  (~70–100 s in our runs).
- `strata_recall` search is lexical (FTS5 + substring), not semantic. In the live session the
  model often preferred re-reading files with its own tools, which is reasonable for
  current file state.
- With default Strata settings outputs are not bit-reproducible across runs (independent of
  this proxy); restores are exact with respect to the saved state.
- Restore needs an exact prefix: if the client rewrites an old message, the proxy falls back
  to a cold read (correct, but slow).
- Images are counted as a fixed 1,024 tokens and never masked.
- Code comments, placeholders shown to the model, and the dashboard UI are currently in
  Italian.

## Tests

```sh
python3 -m unittest discover -s tests -t .
```

Uses a fake OpenAI-compatible engine (`tests/fake_engine.py`, also runnable stand-alone with
`python3 -m tests.fake_engine --port 18095`). Two optional groups are skipped unless their
inputs are present:

- exact-count tests: put a Strata pack `tokenizer/` folder in `./tok` (or set `CTX_TOKDIR`)
  and install `tokenizers`;
- Strata mock-engine integration: set `STRATA_DIR` to a Strata checkout and install `jinja2`
  (uses Strata's real chat template and tool-call parser in-process).

`tools/replay_dry.py` and `tools/replay_exact.py` replay a pi session JSONL through the
context manager offline, to compare policies without a GPU.

## Acknowledgements

This project exists because of [Strata](https://github.com/Niko1221/Strata) by Niko1221 and
the Strata contributors, which makes a large model usable on a single 12 GB GPU. All the
measurements above are of Strata doing the actual work. Thanks also to the authors of pi.

If you use this proxy in your work, please cite Strata, and optionally this repository:

```
@software{verde2026virtualcontextproxy,
  author = {Verde, Maurizio},
  title  = {virtual-context-proxy: a virtual context for Strata},
  year   = {2026},
  url    = {https://github.com/maverde73/virtual-context-proxy}
}
```

## License

MIT — see [LICENSE](LICENSE).
