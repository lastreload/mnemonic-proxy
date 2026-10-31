<p align="center">
  <img src="https://raw.githubusercontent.com/lastreload/mnemonic-proxy/main/docs/assets/logo.png" alt="Mnemonic Proxy logo" width="120">
</p>

<h1 align="center">Mnemonic Proxy</h1>

<p align="center"><em>Like Johnny, your agent carries more than its head can hold — and keeps working for hours.</em></p>

<p align="center">
  <a href="LICENSE"><img alt="License: MIT" src="https://img.shields.io/badge/license-MIT-blue.svg"></a>
  <img alt="Python ≥ 3.10" src="https://img.shields.io/badge/python-%E2%89%A53.10-blue.svg">
  <a href="https://lastreload.github.io/mnemonic-proxy/"><img alt="Project page" src="https://img.shields.io/badge/page-lastreload.github.io-ff2bd6.svg"></a>
</p>

<p align="center">
  <img src="https://raw.githubusercontent.com/lastreload/mnemonic-proxy/main/docs/assets/hero.jpg" alt="Mnemonic Proxy" width="100%">
</p>

A transparent HTTP proxy that sits between a coding agent and a local model (llama.cpp's `llama-server`,
[Strata](https://github.com/Niko1221/Strata), [ds4-server](https://github.com/antirez/ds4), or any
OpenAI-compatible server). The agent keeps sending its whole,
unmodified history; the proxy decides what the model actually reads. Old material leaves the prompt but is never
summarized away: every message, tool call and output stays on disk, verbatim, and the model can pull any of it back.

It speaks OpenAI Chat Completions, Anthropic Messages and OpenAI Responses. Python ≥ 3.10, standard library only.

Developed and benchmarked primarily with [Strata](https://github.com/Niko1221/Strata), by Niko1221 and contributors.
mnemonic-proxy is an independently maintained project that also supports llama-server, ds4-server and other
OpenAI-compatible backends; saved engine state depends on what the backend supports (see [Engines](#engines)).

Project page: <https://lastreload.github.io/mnemonic-proxy/>

Measured on our hardware (single runs, see [What we have not proven](#what-we-have-not-proven)): a 3 h 15 min coding
session on one 12 GB GPU with 448K tokens of history and at most 105K in the prompt
([One session, measured](#one-session-measured)); AgentLongBench in a 32K window, 86/100 right with the proxy vs 47
with an LLM summary and 69 with the full history in a larger window
([benchmark](#benchmark-agentlongbench-with-a-32k-window)); a 110K-token conversation resumed from disk in 1.4 s
instead of ≈50 s ([Saved state, measured](#saved-state-measured)).

## Install

The first time: install, watch it work with no model, check your engine, start.

```sh
python3 -m venv mnemonic-venv && . mnemonic-venv/bin/activate
pip install mnemonic-proxy
mnemonic-proxy demo      # 1 minute, no model: archive, masking, recall -> PASS
mnemonic-proxy check --upstream http://127.0.0.1:8095 --port 8096 --data ./data --config config.json
mnemonic-proxy --upstream http://127.0.0.1:8095 --port 8096 --data ./data --config config.json
```

`check` ends with `READY` (exit 0) or says, line by line, what to change; `--live` adds a real generation, a tool
call and a save/restore. `--upstream` is the engine's address. Then point the agent at the proxy (port 8096)
instead of the engine. The engine window must be at least 16K tokens, 32K or more recommended
([Requirements](#requirements)). Step by step with a small model on CPU: [Quick start](#quick-start).

**Your engine.** Start it as below, copy the config from
[`examples/`](https://github.com/lastreload/mnemonic-proxy/tree/main/examples) and set `slot_dir` to the engine's
save folder:

| engine | engine command (minimum) | config | saved state |
|---|---|---|---|
| Strata ≥ 0.1.40 | Strata's own start, plus `--slot-save-path DIR` | `config.example.json`, `slot_dir` = DIR | yes |
| Strata older, or without `--slot-save-path` | Strata's own start | `config.official-strata.json` | no (base mode) |
| llama-server | `llama-server -m model.gguf -c 32768 --parallel 1 --jinja --slot-save-path ./slots --port 8095` | `config.llama-server.json` | yes |
| llama-server, router mode (several models) | `llama-server --models-preset preset.ini --models-max 1 --port 8095`; preset: `[*]` with `slot-save-path` and `parallel = 1`, a `ctx-size` (`c`) ≥ 16384 per model | `config.llama-server-router.json` + `preset.router.example.ini` | yes, per model |
| ds4-server | `ds4-server -m MODEL.gguf --ctx 65536 --kv-disk-dir ./kv --port 8110` | `config.ds4.json` | kept by ds4 itself |
| Ollama, LM Studio | engine context set to 32768 (see below); `--tokenizer` recommended | `config.openai-32k.json`, or no `window` (read from the engine) | no (base mode) |
| any OpenAI-compatible (vLLM, online APIs) | — | `config.openai-32k.json` at 32K; at 131072 none is needed (the defaults are for 131072) | no (base mode) |

The proxy reads the window from Strata, llama-server (`n_ctx`, per model in router mode), ds4-server (`--ctx`),
Ollama and LM Studio (the context of the loaded model, 0.3.8), and scales its thresholds to it, unless `window` is
written in the config (the two Strata examples set 131072: change it if Strata runs with another window). From any
other engine it cannot: `window` in the config is used as it is (131072 when missing), so write the engine's real
context there; `config.openai-32k.json` has the thresholds the proxy itself uses at 32K. Details per engine:
[Engines](#engines).

**Ollama and LM Studio** (tested on Linux, small model, details in [Tested combinations](#tested-combinations)). Both
serve an OpenAI-compatible API (Ollama `http://127.0.0.1:11434/v1`, LM Studio `http://127.0.0.1:1234/v1`;
`--upstream` is the same address without `/v1`) and the proxy runs in base mode: masking, archive, `recall`, segment
switches, tool calls, streaming, `/v1/messages` and `/v1/responses` worked, no saved state. What to know:
- **Give the engine a context of 32768 (at least 16384).** With their defaults on our machine (Ollama 4096 on CPU;
  LM Studio 8192) both answer HTTP 200 to longer prompts after dropping part of them, without telling the client:
  Ollama drops the oldest messages, LM Studio keeps the beginning and the end and drops the middle.
  Ollama: `OLLAMA_CONTEXT_LENGTH=32768 ollama serve`; `ollama ps` shows the context in use (column `CONTEXT`).
  LM Studio: `lms load <model> --context-length 32768`, then `lms server start`; `lms ps` shows it.
- **The proxy reads that context** (0.3.8): Ollama `GET /api/ps` (`context_length` of the loaded model: the context
  it runs with, not the training context of `POST /api/show`), LM Studio `GET /api/v0/models`
  (`loaded_context_length`). It reads it again before every request (one cheap GET; a model that is not loaded is
  loaded first with a 1-token request), so the window follows the engine and a prompt never goes over it. Below
  16384 the proxy refuses to start; a `window` in the config bigger than the engine context also refuses the start
  and fails `check`, with the command that fixes it. You can leave `window` out of the config.
- **`--tokenizer` is still recommended** (the model's `tokenizer.json`, `pip install 'mnemonic-proxy[exact]'`).
  Without it the proxy estimates chars/3.5, which was up to ~10% low on code with many digits; on generic engines
  the estimate now gets a 15% margin by default (`window_estimate_slack` 0.15) and `check` warns. With 0.3.7 the
  notes request at a segment switch was estimated at 31.3K tokens but was 33.4K real tokens and both engines cut it;
  with 0.3.8 and no tokenizer it was 27.7K real tokens on Ollama and nothing was cut (numbers in Tested
  combinations).
- **A cut prompt is reported**: when the engine says (`usage.prompt_tokens`) it read less than 0.8× what the proxy
  counted, the journal gets `engine_truncated` and the log one warning per conversation; `check --live` sends a
  prompt of 0.9× the window and fails with "engine context smaller than window" if it does not come back whole.
- Ollama ignores an integrated GPU by default and runs on the CPU (much slower); `OLLAMA_IGPU_ENABLE=1` makes it use
  it. With it, Ollama chose a default context of 262144 from the shared memory: set `OLLAMA_CONTEXT_LENGTH` anyway.

**Your agent.** Turn off the agent's own auto-compaction, otherwise two context managers fight over the same history:

| agent | point it at | turn off | setup |
|---|---|---|---|
| pi, omp | `http://127.0.0.1:8096/v1` (`openai-completions`) | `"compaction": {"enabled": false}` | [T1 step 6](#t1--a-real-model-on-cpu), [omp](#pi-hermes-continue--chat-completions) |
| Hermes Agent | `http://127.0.0.1:8096/v1` (custom provider) | `compression.enabled: false` | [Hermes setup and test notes](docs/hermes-agent.md) |
| Continue, other Chat Completions clients | `http://127.0.0.1:8096/v1` | the client's own context compaction | [Chat Completions](#pi-hermes-continue--chat-completions) |
| Claude Code | `ANTHROPIC_BASE_URL=http://127.0.0.1:8096` (no `/v1`) | `DISABLE_AUTO_COMPACT=1` | [Claude Code](#claude-code-anthropic-messages) |
| Codex | `base_url = "http://127.0.0.1:8096/v1"`, `wire_api = "responses"` | no switch: `model_auto_compact_token_limit = 100000000` | [Codex](#codex-openai-responses) |
| any MCP client (Claude Code, Codex, Hermes) | `mnemonic-mcp --db ./data/archive.sqlite` (read-only search of the archive) | — | [MCP](#mcp-recall-server-claude-code-codex-hermes) |

**Your OS.**
- **Linux**: the reference; the commands on this page, and all the measured sessions.
- **Windows, native**: PowerShell commands and a one-click `examples\start-windows.bat` in [Platforms](#platforms)
  (tested on the CI runner, not yet on a desktop). **WSL 2**: the Linux commands, with engine, proxy and `slots` all
  inside WSL.
- **macOS**: not tested yet; follow the Linux commands (llama.cpp from Homebrew or built with Metal), see
  [Platforms](#platforms).
- **Android (Termux)**: `pkg install python git`, then `pip install git+https://github.com/lastreload/mnemonic-proxy`;
  tested in Waydroid (Android 13, x86_64) in front of an online API, not yet on an ARM phone, see
  [Platforms](#platforms).

## How it works

No summaries written by a model, no vector database required.

1. **Hide — mask, don't summarize.** Old tool outputs and reasoning leave the prompt in stable blocks and become
   one-line receipts with an id. The prefix stays byte-identical between blocks, so the engine's KV cache keeps
   working.
2. **Recall — structured, exact.** A `recall` tool searches the verbatim archive: first write of a file, its full
   edit timeline, command → output, the reasoning behind an edit — every hit tagged with message number and segment.
   The proxy runs these calls itself; the client never sees them, even when streaming.
3. **Segment — handoff notes.** When even the masked window fills up, the model writes its own notes, the segment is
   sealed and a fresh one starts. Pinned lines (`!!` or `📌`) survive every switch; lines starting with `🗑` (or `~`)
   mark a message as disposable once answered.
4. **Save — engine state on disk.** On engines that support it (llama-server, Strata with session files) the KV
   state is saved to disk: resuming after a pause, a restart or another client takes 1–3 s instead of a full
   re-read.
5. **Archive — deduplicated cold storage.** Old state files are rebuilt bit-for-bit from content-addressed zstd
   blocks: −36% disk on our sessions, sha256-verified before every restore.
6. **Lean — tools on demand.** Agents ship tens of thousands of tokens of tool definitions. The proxy keeps the core
   ones and loads the rest by name: 25.6K → 6.2K tokens on every request.

> "In the courier business you don't read the payload. You carry it, and you give it back exactly as it was." — the
> design rule. Hidden content is never paraphrased; it is either in the prompt or retrievable verbatim.

## Requirements

- **Engine window: at least 16K tokens (16384), 32K or more (32768) recommended.** Below 16K the proxy refuses to
  start, and `mnemonic-proxy check` reports NOT READY, with the command to restart the engine (`llama-server -c
  32768`, `ds4-server --ctx 32768`). Why: at 32K the recall trap of the ds4 runs (an error line removed from the
  prompt, asked for later) was answered right with both ds4 builds; at 8K it passed **once**, after the 0.3.0 recall
  fix, and failed before it. Small windows stay excluded until they are proven on more runs. Reports (Italian):
  [DS4-QWEN-COMPARE-RESULT.md](https://github.com/lastreload/mnemonic-proxy/blob/main/docs/dev-notes/DS4-QWEN-COMPARE-RESULT.md),
  [RECALL-8K-030-RESULT.md](https://github.com/lastreload/mnemonic-proxy/blob/main/docs/dev-notes/RECALL-8K-030-RESULT.md).
- Python ≥ 3.10 (`kv_archive`: ≥ 3.14). An OpenAI-compatible engine; saved state needs llama-server with
  `--slot-save-path`, or Strata ≥ 0.1.40 with `--slot-save-path` (see [Engines](#engines)).

## Quick start

Three levels. Each one stands on its own.

| | what you see | needs | time |
|---|---|---|---|
| **T0 — see it work** | archive, masking, exact recall, with a scripted fake model | Python ≥ 3.10 | 1 minute |
| **T1 — real model on CPU** | a real agent (pi) writing code through the proxy, masking, recall, resume after an engine restart | + cmake, a C++ compiler, Node.js ≥ 22.19, ~9 GB RAM (the engine at `-c 32768`), 3.5 GB disk | ~45 minutes |
| **T2 — real use (GPU)** | your own model and agent, long sessions | a GPU engine | — |

Commands are for Linux; see [Platforms](#platforms) for macOS and Windows (native or WSL).

### T0 — see it work (1 minute, no model)

```sh
python3 -m venv mnemonic-venv && . mnemonic-venv/bin/activate
pip install mnemonic-proxy
mnemonic-proxy demo
```

With [uv](https://docs.astral.sh/uv/), no venv to create: `uvx mnemonic-proxy demo`. The development version (the
`main` branch, newer than the last PyPI release): `pip install git+https://github.com/lastreload/mnemonic-proxy`.

The demo starts the real proxy in a temporary folder, in front of a scripted fake model. An agent reads a long
`build.log` that contains one build id; the session grows until the proxy hides the log; the demo prints what the
engine receives in its place (a one-line receipt); then the user asks for the build id, the model calls `recall`,
the proxy answers it internally and the archived text is compared byte for byte with the original. It ends with
`PASS` (exit 0) or `FAIL` (exit 1).

It proves the proxy's own logic: archive, masking, recall. It does not prove anything about a real model, its tool
calling, or saved engine state — that is T1.

### T1 — a real model on CPU

Tested end to end in a clean Ubuntu 24.04 container (see [Tested combinations](#tested-combinations)). A 4B model on
CPU is slow (seconds per answer) and limited: the point is to see every mechanism work, not to do real work.

**0. Prerequisites** (Ubuntu/Debian; other systems: [Platforms](#platforms)):

```sh
sudo apt install git cmake build-essential python3-venv curl
```

and Node.js ≥ 22.19 for pi (<https://nodejs.org/en/download>, or your package manager).

**1. Folder, proxy, example config.**

```sh
mkdir mnemonic-trial && cd mnemonic-trial
git clone --depth 1 https://github.com/lastreload/mnemonic-proxy
python3 -m venv .venv && . .venv/bin/activate
pip install ./mnemonic-proxy
cp mnemonic-proxy/examples/config.llama-server-32k.json .
mkdir -p slots data
```

**2. llama.cpp, the tested release (CPU build, ~2 minutes on 12 cores).**

```sh
git clone --depth 1 --branch b11430 https://github.com/ggml-org/llama.cpp
cmake -S llama.cpp -B llama.cpp/build -DCMAKE_BUILD_TYPE=Release
cmake --build llama.cpp/build --config Release -j 8 --target llama-server
```

**3. The model**: Qwen3-4B-Instruct-2507, Q4_K_M (2.5 GB). Qwen publishes no GGUF for this model
([model card](https://huggingface.co/Qwen/Qwen3-4B-Instruct-2507)); this is the
[Unsloth conversion](https://huggingface.co/unsloth/Qwen3-4B-Instruct-2507-GGUF), pinned to the tested revision:

```sh
mkdir -p models
curl -L -o models/Qwen3-4B-Instruct-2507-Q4_K_M.gguf \
  https://huggingface.co/unsloth/Qwen3-4B-Instruct-2507-GGUF/resolve/a06e946bb6b655725eafa393f4a9745d460374c9/Qwen3-4B-Instruct-2507-Q4_K_M.gguf
sha256sum models/Qwen3-4B-Instruct-2507-Q4_K_M.gguf
# 3605803b982cb64aead44f6c1b2ae36e3acdb41d8e46c8a94c6533bc4c67e597
```

**4. The engine** (terminal 1, inside `mnemonic-trial`):

```sh
llama.cpp/build/bin/llama-server -m models/Qwen3-4B-Instruct-2507-Q4_K_M.gguf \
  -c 32768 --parallel 1 --jinja --slot-save-path ./slots --port 8095
```

Wait for the line `listening on http://127.0.0.1:8095` (about 2 s once the model file is cached; `curl -s
localhost:8095/health` answers `{"status":"ok"}`). `--slot-save-path ./slots` is what makes saved state
possible, and it must be the same folder as `slot_dir` in the config (`./slots`). `--jinja` is already the default in
this build; it is written out because tool calling depends on it. `-c 32768` is the recommended window (the minimum
is 16384, see [Requirements](#requirements)); the example config starts masking at 14K tokens, so the proxy is at
work after a few long tool outputs.

**5. Check, then start the proxy** (terminal 2, inside `mnemonic-trial`, venv active: `. .venv/bin/activate`):

```sh
mnemonic-proxy check --live --upstream http://127.0.0.1:8095 --port 8096 --data ./data --config config.llama-server-32k.json
mnemonic-proxy --upstream http://127.0.0.1:8095 --port 8096 --data ./data --config config.llama-server-32k.json
```

`check` must end with `READY` (exit 0): engine recognised as llama.cpp, model loaded, window taken from `n_ctx`
(32768) with the thresholds scaled to it, saved state supported and configured, and — because of `--live` — one
real generation, one tool call, one save/restore whose file appeared in `./slots`. Each failing line says how to fix
it; see also [Troubleshooting](#troubleshooting). The proxy then prints
`[mnemonic-proxy] 127.0.0.1:8096 -> http://127.0.0.1:8095 (engine llama.cpp, saved state yes) ... window=32768`.

**6. pi, the coding agent** (terminal 3). Install the tested version:

```sh
npm install -g --ignore-scripts @earendil-works/pi-coding-agent@1.0.3
```

Create `~/.pi/agent/models.json` (if it exists, add the `mnemonic` entry to its `providers`):

```json
{
  "providers": {
    "mnemonic": {
      "baseUrl": "http://127.0.0.1:8096/v1",
      "api": "openai-completions",
      "apiKey": "local",
      "compat": { "supportsDeveloperRole": false, "supportsReasoningEffort": false },
      "models": [
        { "id": "local-model", "name": "local model via mnemonic-proxy", "reasoning": false,
          "contextWindow": 131072, "maxTokens": 2048 }
      ]
    }
  }
}
```

and `~/.pi/agent/settings.json` (if it exists, add these three keys):

```json
{
  "defaultProvider": "mnemonic",
  "defaultModel": "local-model",
  "compaction": { "enabled": false }
}
```

`compaction.enabled: false` matters: pi's own auto-compaction would summarise the history the proxy is managing.
`contextWindow` is the *virtual* window the client sees (131072), not the engine's 32768: pi subtracts the prompt size
from it to choose `max_tokens`, and with the engine's real window it would ask for fewer and fewer tokens as the
history grows, down to 1. Fitting the history
into the engine's real window is the proxy's job. `apiKey` is a placeholder (the proxy does not check it); the model
id is any name, llama-server serves its loaded model.

**7. The exercise.** In terminal 3, a folder with a log whose one interesting line is buried in the middle:

```sh
mkdir -p ~/mnemonic-exercise && cd ~/mnemonic-exercise
python3 -c "for i in range(650): print('[%04d] compiling module_%03d.c ... ok (%d ms)' % (i, i, 80 + i * 7 % 61) if i != 397 else '[0397] build id: BUILD-7f3a9c-ORCHID-42')" > build.log
pi
```

Type these requests one at a time and wait for each answer (or non-interactively, same session:
`pi -p "<request 1>"`, then `pi -c -p "<request 2>"`, …):

1. `Run python3 -c "import uuid; print(uuid.uuid4())" and tell me the value.` — a `bash` tool call; pi repeats the
   value.
2. `Run cat build.log and tell me whether every module compiled.` — a long output (≈13K tokens; on CPU the 4B model
   needs a few minutes to read it).
3. `Write primes.py with a function first_primes(n) that returns the first n prime numbers, and test_primes.py with
   three unittest tests. Run the tests.` — `write` calls, then `bash`. The prompt is now past the masking threshold
   (14K tokens with the 32K config): as soon as enough newer messages follow the log, the proxy hides it behind a
   receipt (here during step 4). Nothing is printed; check with
   `grep -c '"event": "mask"' ~/mnemonic-trial/data/journal.jsonl` (≥ 1).
4. `Add a function is_prime(n) to primes.py and a test for it in test_primes.py, then run the tests.`
5. `What build id is written in build.log? Do not run commands and do not read files.` — the log is no longer in the
   prompt, only its receipt; the expected path is a `recall` call answered by the proxy itself (pi never sees it):
   `grep -c '"event": "recall"' ~/mnemonic-trial/data/journal.jsonl`. Small models often skip it: see the results
   below.

Wait one minute (the proxy saves the engine state after 60 s idle: `"event": "autosave"`), then restart the engine:
Ctrl+C in terminal 1, start it again with the same command, and ask `Which files did you write in this session?`.
The proxy restores the conversation from `./slots` instead of re-reading it:
`grep '"event": "autorestore"' ~/mnemonic-trial/data/journal.jsonl` shows the restored tokens and milliseconds.

What this proves and what it does not, for this model: see [Tested combinations](#tested-combinations).

### T2 — real use (GPU)

Use the engine and model you already have; the only requirements are an OpenAI-compatible endpoint and, for saved
state, an engine that can save its KV state to disk:

```sh
llama-server -m your-model.gguf -c 131072 --parallel 1 --jinja --slot-save-path /path/to/slots --port 8095
mnemonic-proxy check --upstream http://127.0.0.1:8095 --port 8096 --data ./data --config config.json
mnemonic-proxy --upstream http://127.0.0.1:8095 --port 8096 --data ./data --config config.json
```

with `config.json` = `examples/config.llama-server.json` and `slot_dir` = the same `/path/to/slots` (see
[Configuration](#configuration) for the recommended fields). With an engine that has no saved state (vLLM, online
APIs) the proxy runs in base mode (window: see [Install](#install)). The measurements in
[One session, measured](#one-session-measured) (Qwen3.8 Flash Next IQ3_XXS, 128K window) come from
Strata (our session-files build, 0.1.38 + PR #668) on a 12 GB RTX 4070 Ti — see [Engines](#engines); they say nothing about
other hardware.

The proxy recognises the engine at startup and turns off, with a warning, whatever the engine cannot do
(`--engine auto|strata|llama.cpp|ds4|openai` forces it). `ctxproxy` is kept as an alias of `mnemonic-proxy`.
Optional: `--tokenizer` (a Hugging Face `tokenizer.json`, or a Strata pack `tokenizer/` folder; needs
`pip install 'mnemonic-proxy[exact]'`) for exact token counts.
If `--tokenizer` is explicitly supplied but cannot be loaded (missing `tokenizers`, bad path or invalid format),
startup fails before contacting the engine; it never silently switches that run to estimated counting. Install
`mnemonic-proxy[exact]` in the same environment as the proxy and check the path. Omit the option only when an
estimated guard is intended. Without a tokenizer, the proxy uses a chars/3.5 estimate;
with llama-server, `"engine_tokenize": true` counts through its `/tokenize`. A
systemd user unit is in [examples/mnemonic-proxy.service](https://github.com/lastreload/mnemonic-proxy/blob/main/examples/mnemonic-proxy.service).

**Window guard.** Before every call (answer, recall rounds, handoff notes, seal, anchor and retries), the proxy
checks `counted_prompt + max_tokens + window_margin ≤ window`. If it does not fit, it lowers `max_tokens`, removes
what the recall loop added (by value per token: results become one-line receipts, old reasoning becomes its
conclusion line, recallable by id) or returns HTTP 400 instead of sending. Both output-limit aliases are kept in
sync. A prompt that cannot be counted with a loaded tokenizer is refused rather than silently estimated.

With `--tokenizer`, the final guard and planning total now load an adjacent `chat_template.jinja`
(from the pack directory, or beside a supplied HF tokenizer file). This matters for fine-tunes such as Tiel:
its Sharp template adds instructions and tool-error warnings that the built-in Qwen renderer does not write.
Install/update `mnemonic-proxy[exact]` for both the tokenizer and sandboxed Jinja renderer. A discovered template
that cannot load or render is never silently replaced by the built-in one. Current pack-template counting is
text-only: image requests are refused locally rather than claiming to know dynamic image patch counts.
Without that file, the historical built-in Qwen rendering remains (`template=builtin-qwen` at startup);
use it only with a matching server template. Per-message planning costs remain Qwen approximations; the full
pack-rendered total and final guard are what enforce the window.

A matching tokenizer/template still does not validate hidden server settings. Recent Strata/PolyStrata encode special-token spellings
quoted inside content (for example `</think>`) as ordinary text, not single special tokens. The guard reserves an
additional conservative byte-BPE bound for these literals, including tool schemas and decoded call arguments;
`window_guard` events carry `guarantee=tokenizer_bound` and `literal_slack` when this reserve is nonzero
(`guarantee=exact` otherwise). This may lower the output ceiling slightly more than necessary. Arbitrary templates,
a tokenizer for another model, and server-side prompt additions are not covered by a local exact-count claim.

Without a tokenizer the count is the calibrated chars/3.5 estimate (`guarantee=estimated`): on text with many digits
or symbols the real count can be well above it, so use the correct `--tokenizer` (or raise `window_estimate_slack`,
e.g. `0.3`) when the window is small. On generic engines (Ollama, LM Studio, any OpenAI-compatible) without
`--tokenizer` the slack is 0.15 by default (0.3.8); Strata, llama.cpp and ds4 keep 0. This 15% is not an extra reserve
on top of a loaded tokenizer. `"window_guard": false` explicitly disables the protection.

**Context diagnostics.** The CLI prints startup/version/window/template and concise runtime events to **stderr**:
`window_guard` (including every checked call, clamp, count failure, local refusal and engine-count mismatch),
`response_clamped`, `mask`, `switch`, `switch_skipped`, and `client_disconnected`. No prompt/notes/error-body text
is printed by this summary. Capture it with `2> proxy-events.log` (or use your service manager's journal).
The full structured log stays at `<--data>/journal.jsonl`; recent events are also available at
`GET /v1/strata/journal`. The optional [dashboard](#dashboard-optional-needs-the-git-clone-ui-in-italian) reads the data directory; do not confuse its
port with the proxy's. `where=answer|recall_round|final|notes|seal|anchor|answerfix` identifies the upstream phase;
a client continuation is a new answer request, not a distinct server-side phase. `seal` and `anchor` deliberately
request one token to establish saved state; this differs from a client answer clamped to one token.

If PolyStrata rejects a request *before generation* with its explicit context/count error, the proxy records
both counts and retries **once**, unchanged except for lower output/reasoning limits derived from that engine
count. An engine-counted prompt with no answer room becomes a local `window_guard` error without a retry.
Unrelated 400s, transport errors and errors after streaming starts are not retried. This is a safety net, not
proof that the first request always fits arbitrary server-side modifications (for example restored hidden
reasoning or an effort instruction moved by the server). `fit_max_tokens=true` is an additional engine-side
output safety net, but cannot make an already oversized prompt fit. `window_margin` applies in tokenizer mode;
`window_estimate_slack` does not. Increasing the margin is not a substitute for the correct template.

Disable the agent's own auto-compaction in every client below, otherwise two context managers fight over the same
history.

### pi, Hermes, Continue, … (Chat Completions)

Point the client's OpenAI base URL at `http://127.0.0.1:8096/v1`. For pi, the two files of T1 step 6 (with your real
`contextWindow`/`maxTokens`). For other clients: their OpenAI-compatible provider setting, plus their own way to turn
off context compaction.

**omp ([oh-my-pi](https://github.com/can1357/oh-my-pi))** — tested with 18.10.1 on Strata. omp reads YAML from
`~/.omp/agent/` (or `$PI_CODING_AGENT_DIR`). Add the proxy as a provider in `models.yml`:

```yaml
providers:
  mnemonic:
    baseUrl: http://127.0.0.1:8096/v1
    api: openai-completions
    apiKey: none
    auth: apiKey
    compat:
      supportsDeveloperRole: false
      supportsReasoningEffort: false
      maxTokensField: max_tokens
      thinkingFormat: qwen-chat-template   # for Qwen models; drop it for others
    models:
      - id: qwen3.8-flash-next-iq3_xxs     # the id your engine reports at /v1/models
        name: Flash Next via mnemonic-proxy
        reasoning: true
        input: [text]
        contextWindow: 131072              # the engine's real window
        maxTokens: 16384                   # leave room for reasoning (see the ds4 note below)
        cost: {input: 0, output: 0, cacheRead: 0, cacheWrite: 0}
```

and turn omp's own compaction off in `config.yml`, so the proxy manages the window:

```yaml
compaction:
  enabled: false
```

Then `omp --model mnemonic/qwen3.8-flash-next-iq3_xxs`. If you use extensions that also shorten tool output (for
example SoL-Pi's ObservationPack), we have not tested them together with the proxy.

### Claude Code (Anthropic Messages)

Checked against Claude Code 2.1.287; a 1 h 41 min session with 2.1.284 on Strata is measured
[below](#claude-code-and-codex-live-on-strata). Claude Code may print `unrecognized_model local-model`: it works
anyway.

```sh
export ANTHROPIC_BASE_URL=http://127.0.0.1:8096      # no /v1
export ANTHROPIC_AUTH_TOKEN=local                    # any value; the proxy does not check it
export ANTHROPIC_MODEL=local-model                   # any name; the engine serves its loaded model
export ANTHROPIC_DEFAULT_HAIKU_MODEL=local-model     # background tasks (replaces ANTHROPIC_SMALL_FAST_MODEL)
export CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1    # fewer side requests competing for the engine
export DISABLE_AUTO_COMPACT=1                        # turns off auto-compaction; /compact still works
claude
```

`ANTHROPIC_SMALL_FAST_MODEL` is still read by 2.1.287 (it takes precedence), but the documented name is
`ANTHROPIC_DEFAULT_HAIKU_MODEL`. `DISABLE_COMPACT=1` would also turn off the manual `/compact`.
`POST /v1/messages` (streaming and not) and `POST /v1/messages/count_tokens` are supported. Thinking blocks get a
local placeholder signature: a session started through the proxy cannot be continued on Anthropic's API.

### Codex (OpenAI Responses)

Codex 0.160 speaks only the Responses API (`wire_api = "chat"` is rejected). In `~/.codex/config.toml` (or a separate
`CODEX_HOME`):

```toml
model = "local-model"
model_provider = "local"
model_context_window = 131072              # the proxy's window; Codex compacts relative to this
model_auto_compact_token_limit = 100000000 # compaction threshold far above any real session

[model_providers.local]
name = "local engine via mnemonic-proxy"
base_url = "http://127.0.0.1:8096/v1"
wire_api = "responses"
env_key = "LOCAL_API_KEY"          # export LOCAL_API_KEY=local (any value)
requires_openai_auth = false
supports_websockets = false
```

Codex 0.160 has no setting that turns auto-compaction off: `model_auto_compact_token_limit` only moves the
threshold. Set it far above any session, as above, so that in practice the proxy is the only context manager.
Codex's `prompt_cache_key` is used as the conversation key. Stateless only (`store=false`, no
`previous_response_id`), which is what Codex sends.

### MCP recall server (Claude Code, Codex, Hermes)

The archive can also be searched from any MCP client, read-only (sqlite `mode=ro`; the proxy can keep writing). Three
tools: `recall` (same search as the proxy's tool), `conversations`, `journal`.

```sh
mnemonic-mcp --db ./data/archive.sqlite                 # stdio
mnemonic-mcp --db ./data/archive.sqlite --http 8133     # streamable HTTP on 127.0.0.1:8133/mcp
```

```sh
claude mcp add mnemonic -- mnemonic-mcp --db /path/to/data/archive.sqlite
codex mcp add mnemonic -- mnemonic-mcp --db /path/to/data/archive.sqlite
hermes mcp add mnemonic --command mnemonic-mcp --args --db /path/to/data/archive.sqlite
```

`python3 -m ctxproxy.mcp_server` is the same program. Hermes `config.yaml` equivalent:

```yaml
mcp_servers:
  mnemonic:
    command: mnemonic-mcp
    args: [--db, /path/to/data/archive.sqlite]
    enabled: true
```

### Dashboard (optional; needs the git clone; UI in Italian)

The dashboard is not part of the pip package: it runs from a clone of this repository, and its interface is in
Italian.

```sh
python3 mnemonic-proxy/dashboard/ctxdash.py --data ./data --strata http://127.0.0.1:8095 --proxy http://127.0.0.1:8096 --port 8097
```

Read-only, on `127.0.0.1:8097`: engine status, physical vs. virtual context over time, masking/segment/recall events,
and the live physical prompt (with `live_dump: true`).

### Proxy endpoints

Besides the client APIs (`/v1/chat/completions`, `/v1/messages`, `/v1/responses`), all other paths are passed to the
engine. The proxy's own:

| endpoint | what |
|---|---|
| `GET /health` | proxy + engine health: `{"status": "ok" \| "loading" \| "engine_unreachable", "engine", "version", "upstream"}`; HTTP 200 only when the engine is ready |
| `GET /v1/engine` | what was detected: `kind`, `slot_save` (saved state supported and on), `n_ctx`, `model`, `notes`. `GET /v1/strata/engine` is the same (0.1 name, kept) |
| `GET /v1/strata/conversations/<id>` | one conversation: archived messages, masks, segments |
| `GET /v1/strata/archive/<id>` | one archived block, verbatim |
| `GET /v1/strata/journal` | the last 200 journal events |

## Tested combinations

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

## Troubleshooting

Start with `mnemonic-proxy check` (add `--live` to exercise the engine): every failing line says what to change.

| symptom | cause | fix |
|---|---|---|
| `check`: port … is in use | another proxy (or another program) on that port | stop it, or pass a different `--port` (and change the client's base URL) |
| `check`: engine answers 503 / `GET /health` says `loading` | llama-server is still loading the model | wait for `listening on …` in its log |
| `check`: engine not reachable | wrong `--upstream`, or the engine is not running | start the engine; `curl http://127.0.0.1:8095/health` must answer |
| proxy exits at startup: `refusing to start, the context window is too small` / `check`: context window … minimum is 16384 | engine started with a window below 16K (`-c 8192`), or `"window"` below 16384 in the config | restart the engine with `-c 32768` (ds4-server: `--ctx 32768`); remove `"window"` from the config (it is read from the engine). See [Requirements](#requirements) |
| llama-server: `failed to allocate` / killed | not enough memory for model + context | a smaller quantisation or a smaller model; the window can go down to 16384 (`-c 16384`), not below |
| HTTP 400 `exceeds the available context size` | the client asked for more than the window | proxy started with a config whose `window` is larger than the engine's `-c`? The proxy reads `n_ctx` from llama-server at startup: restart the proxy after changing `-c` |
| the model describes the command instead of calling a tool, or says it "cannot run commands" | chat template without tools, or a model weak at tool calling | llama-server with `--jinja`; `check --live` shows whether a tool call works; try a stronger model (see [Tested combinations](#tested-combinations)) |
| `check`: saved state supported by the engine, disabled in the proxy | config without `slot_save` | use `examples/config.llama-server.json` |
| `check --live`: the state file is not in `slot_dir` | `slot_dir` ≠ llama-server `--slot-save-path`, or the engine runs on another host / in a container with another path | the same folder, as seen from the proxy's host |
| `check`: engine detected as `openai` | llama-server too old, or another engine | saved state needs llama-server with `--slot-save-path`, or Strata ≥ 0.1.40 with `--slot-save-path`; everything else works |
| Strata has no saved state | Strata older than 0.1.40, or started without `--slot-save-path` | update to Strata ≥ 0.1.40 and start it with `--slot-save-path DIR` |
| answers get shorter and shorter / summaries appear in the history | the client's own compaction is on as well | turn it off: pi `"compaction": {"enabled": false}`, Claude Code `DISABLE_AUTO_COMPACT=1`, Codex: [threshold](#codex-openai-responses) |
| Codex: `wire_api = "chat"` rejected | Codex ≥ 0.160 only speaks the Responses API | `wire_api = "responses"` |
| `kv_archive` refused | needs Python ≥ 3.14 (`compression.zstd`) | Python 3.14, or leave `kv_archive` off (default) |

## Platforms

- **Linux**: the reference. Every tested combination below ran on Linux.
- **macOS**: the proxy is plain Python and should work as on Linux (not yet tested). Build llama.cpp the same way
  (Metal is on by default on Apple Silicon) or install it with Homebrew (`brew install llama.cpp`), then use the same
  `llama-server` command; `sha256sum` is `shasum -a 256`.
- **Windows, native** (since 0.3.2; tested on the `windows-latest` CI runner, not yet on a desktop): Python ≥ 3.10
  from python.org, the official llama.cpp Windows build (`llama-b11430-bin-win-cpu-x64.zip` or a GPU one). In
  PowerShell:

  ```powershell
  py -m venv mnemonic-venv; .\mnemonic-venv\Scripts\Activate.ps1
  pip install mnemonic-proxy
  mnemonic-proxy demo
  mkdir slots
  llama-server.exe -m model.gguf -c 32768 --jinja --slot-save-path .\slots --port 8095   # in a second window
  mnemonic-proxy check --live --upstream http://127.0.0.1:8095 --data .\data --config config.llama-server.json
  mnemonic-proxy --upstream http://127.0.0.1:8095 --port 8096 --data .\data --config config.llama-server.json
  ```

  `config.llama-server.json` is in `examples\` (`"slot_dir": "./slots"`: relative paths are resolved from the folder
  you start the proxy in, so start the engine and the proxy from the same folder).
  [`examples\start-windows.bat`](https://github.com/lastreload/mnemonic-proxy/blob/main/examples/start-windows.bat) does the same in one double-click: it starts
  `llama-server.exe` in its own window with `--slot-save-path .\slots`, waits for it, runs `check`, then the proxy on
  port 8096 (edit the four paths at the top: `LLAMA`, `MODEL`, `CONFIG`, `VENV`). What CI proves on Windows: unit
  tests, demo on a cp1252 console, `check --live` READY, chat through the proxy, `start-windows.bat`, autosave and
  autorestore after an engine restart (3.2K tokens restored in 95 ms; the turn takes 2.3 s instead of 35.7 s),
  `kv_archive` round trip.
  Not tested: real agents (pi, Claude Code, Codex) on Windows, GPU builds, the dashboard. `sha256sum` is
  `Get-FileHash -Algorithm SHA256`.
- **Windows, WSL 2**: follow the Linux commands inside WSL. Keep the engine, the proxy and the `slots` folder all
  inside WSL, so that `slot_dir` and `--slot-save-path` are the same path. Not yet tested.
- **Android (Termux)** (0.3.8; tested in Waydroid 1.6.2 on Linux — Android 13, x86_64 — with Termux 0.118.3 and its
  Python 3.14.8; not yet on an ARM phone). The proxy is plain Python, nothing is compiled:

  ```sh
  pkg update -y && pkg upgrade -y
  pkg install -y python git
  pip install git+https://github.com/lastreload/mnemonic-proxy    # 0.3.8 (about 25 s)
  mnemonic-proxy demo                                              # PASS
  ```

  Use case: an agent on the phone in front of an online API (no model on the phone), base mode:

  ```sh
  curl -LO https://raw.githubusercontent.com/lastreload/mnemonic-proxy/main/examples/config.openai-32k.json
  mnemonic-proxy --upstream https://openrouter.ai/api --api-key "$OPENROUTER_API_KEY" \
    --port 8096 --data ./data --config config.openai-32k.json
  ```

  then point the agent at `http://127.0.0.1:8096/v1`. Measured run (OpenRouter, `meta/muse-spark-1.3-contributor`, a
  small stdlib client with 3 tools reading ten ~5K-token logs, 15 turns, history 55K tokens at the end): the proxy
  never sent more than 24K tokens; prompt tokens 353K with the proxy (all rounds, the 9 `recall` rounds included) vs
  447K for the same 15 requests sent directly (−21%; −56% counting only the 15 client turns); 12 valid tool calls,
  0 errors; the final question about the first log (masked by then) was answered right through `recall`. Prefix
  caching on the API side made the direct requests cheap, so in dollars the two were close (about 0.03–0.04 USD each).
  Limits: no saved state (online API); `--api-key` is visible in the process list of the Termux user; `check --live`
  tries the first model the API lists, so do not use `--live` against a catalogue like OpenRouter; online APIs can
  answer HTTP 429 (rate limit), which the proxy passes to the client unchanged (the client must retry). A proxy on a
  computer can also be reached from Termux with `adb reverse tcp:8096 tcp:8096` (tested with a fake engine).

## Uninstall

```sh
pip uninstall mnemonic-proxy        # inside the venv you installed it in, or delete the venv
rm -rf ./data                       # the archive, the journal: your conversations in plaintext
rm -rf ./slots                      # saved engine state (large files), and kv-archive/ if you used kv_archive
```

`./data` and `./slots` are separate on purpose: deleting the state files keeps the archive (recall still works, only
resuming without re-reading is lost); deleting `./data` loses the archive and the conversations' state becomes
unreachable. Undo the client changes as well (pi `models.json`/`settings.json`, the Claude Code variables, the Codex
provider).

## One session, measured

A real coding session — a canvas tower-defense game built from scratch by Qwen3.8 Flash Next (IQ3_XXS) on a single 12 GB
RTX 4070 Ti — with the proxy in between.

<p align="center">
  <img src="https://raw.githubusercontent.com/lastreload/mnemonic-proxy/main/docs/assets/replay.gif" alt="Replay: a question answered from three hours earlier via one recall call" width="100%">
</p>

<p align="center"><sub>Replay of a real exchange (animated GIF; <a href="https://raw.githubusercontent.com/lastreload/mnemonic-proxy/main/docs/assets/replay.mp4">MP4</a>).
Three hours and two segment switches into the session, one <code>recall</code> call — resolved inside the proxy in
61 ms — brings back the verbatim first version of the boss block, message 20. No model summarized anything.</sub></p>

| | |
|---|---|
| **448K** | tokens of history archived and recallable |
| **105K** | peak tokens actually in the prompt (window: 128K) |
| **3h15** | continuous agent work · 430 requests · 0 errors |
| **1.4 s** | to resume a 110K-token conversation from disk (≈50 s re-reading it) |

<p align="center">
  <img src="https://raw.githubusercontent.com/lastreload/mnemonic-proxy/main/docs/assets/session-chart.svg" alt="Full history vs. prompt actually read, per request" width="100%">
</p>

Magenta: the full history of the session. Cyan: what the model actually reads on each request. Dashed lines: the two
segment switches (≈2 minutes each, written handoff notes). Data from the proxy journal, 5 Oct 2026.

## Benchmark: AgentLongBench with a 32K window

[AgentLongBench](https://github.com/euReKa025/AgentLongBench) (kf-c, tool_response, Count Frequency and Find
Duplicates), 25 questions per type at 64k (~74K-token history) and 128k (~150K): 100 questions, official scorer.
Qwen3.8 Flash Next IQ3_XXS on Strata 0.1.40, one RTX 4070 Ti 12 GB, seed 1. Four methods: the full history in an
81,920 / 163,840-token window, and three ways to fit it into 32,768 tokens — this proxy (0.3.5, commit
[`b8d4920`](https://github.com/lastreload/mnemonic-proxy/commit/b8d4920)), an LLM summary of at most 2,048 tokens
written by the same model, and truncation. Preregistered test: proxy vs summary, exact McNemar. With the question
clarified (one sentence saying which tool output "round N" means; answers and scorer unchanged):

| correct | full window | proxy | summary | truncation |
|---|---|---|---|---|
| 64k (/50) | 38 | **41** | 27 | 23 |
| 128k (/50) | 31 | **45** | 20 | 26 |
| both (/100) | 69 | **86** | 47 | 49 |

Proxy vs summary: 44 questions right only with the proxy, 5 only with the summary (p = 7.6e-9). With the original,
ambiguous question every method was near chance (51 / 52 / 56 / 44; answering "0 / no" every time scores 50).
Limits: one model, one engine, one seed, 100 questions; the clarified run exists because of the first one; end to
end the proxy took a median 136 s / 286 s per question (64k / 128k) vs 70 s / 102 s for the full window, mostly
replaying the whole history through the proxy before the question.
Per-question results, logs, scripts and analysis: [mverde73/mnemonic-proxy-agentlongbench](https://huggingface.co/datasets/mverde73/mnemonic-proxy-agentlongbench).

## Recall, benchmarked

71 questions about past sessions, each with the known location of the evidence (including 8 with no answer in the
archive). Held-out split:

| | keyword search | structured recall |
|---|---|---|
| evidence found within 5 calls | 90% | 100% |
| tokens returned per question | 5,940 | 2,737 |
| "not in the archive" recognised | 1 / 4 | 4 / 4 |

The benchmark guided development, so read it as a regression suite, not an independent score. Its archives are real
private sessions and are not in this repository: `bench/` runs against your own archives (`bench/data/<name>.sqlite`)
with questions written for them. Details: [docs/dev-notes/RECALL-RESULT.md](https://github.com/lastreload/mnemonic-proxy/blob/main/docs/dev-notes/RECALL-RESULT.md)
(Italian).

## Saved state, measured

Resuming a conversation instead of re-reading it:

| case | saved | re-read |
|---|---|---|
| Strata (session-files branch), 110K tokens, RTX 4070 Ti | 1.4 s | ≈50 s |
| Strata (session-files branch), 69K tokens, after restart | 2.9 s | 33 s |
| llama-server, 8.2K tokens, CPU, Qwen3-0.6B | 1.9 s | 28.6 s |
| llama-server.exe, native Windows (CI runner), 3.2K tokens, CPU, Qwen3-0.6B | 2.3 s | 35.7 s |

Times are for restoring the conversation's context; loading the model is separate.

### Why saved state matters

A long agent conversation is expensive to rebuild: every token of it has to be processed again (prefill) before the
model can answer. Without saved state that cost is paid again each time the engine loses the conversation — after a
restart, when another client or conversation used the engine in between, and when the proxy returns to a sealed
segment. On a single 12 GB GPU that is close to a minute for 100K tokens; restoring the saved state takes a second or
two, and the state lives on disk, not in VRAM.

This is not specific to the proxy: any client that keeps long conversations on a local engine benefits.
llama-server already offers it through `--slot-save-path`. Strata has it since
[v0.1.40](https://github.com/Niko1221/Strata/releases/tag/v0.1.40) ([PR #668](https://github.com/Niko1221/Strata/pull/668)),
opt-in with `--slot-save-path`. The Strata numbers on this page were measured on our session-files build
(0.1.38 + PR #668, `8d8f664`), before the release. The proxy has since run on Strata 0.1.40 in the
[AgentLongBench benchmark](#benchmark-agentlongbench-with-a-32k-window) and the
[recall tests](#does-the-model-recall-on-its-own), but the restore times in the table above have not been
re-measured on 0.1.40; a contributor measured restore on Strata 0.1.41
([Tools](#tools-audit-the-archive-and-measure-restore)).

## Claude Code and Codex, live on Strata

0.2.0 in front of Strata (Qwen3.8 Flash Next IQ3_XXS, RTX 4070 Ti 12 GB, 128K window), with the real clients:

| | Claude Code 2.1.284 (Anthropic Messages) | Codex 0.160 (Responses) |
|---|---|---|
| session | 1 h 41 min, 9 steps, 339 requests, **0 proxy errors** | 3 steps, 20 requests |
| prompt tokens served from the engine cache | **92.6%** of 11.36M | **93.9%** of 351K |
| context | 328K virtual, at most 46K in the prompt; 14 segment switches | — |
| `recall` | 22 calls | exact: 36 tests and 8/8 error messages, word for word |

Claude Code ran on a test config with low thresholds (window 64K, masking at 36K) so that segments would switch
within the session. Found live and fixed in 0.3.0: in 3 writes out of 140 the model copied the shortened form of an
old tool argument into a file (see [What we have not proven](#what-we-have-not-proven)). Report:
[docs/dev-notes/LIVE020-RESULT.md](https://github.com/lastreload/mnemonic-proxy/blob/main/docs/dev-notes/LIVE020-RESULT.md) (Italian).

### Does the model recall on its own?

Recall only helps if the model uses it without being told. We built short scripted tasks where a value the model
needs at the end exists **only** in an old, masked tool output — not on disk, not reproducible — and the final request
never says "remember" or "earlier":

- **A** random output of a setup script (build id, p95, skipped migrations); re-running it gives different values;
- **B** a file read once, then deleted;
- **C** the old version of an overwritten config, no git, no backup;
- **D** an error from a test service that is then shut down;
- **L** a long session: the value appears in turn 0, followed by 12 heavy turns and several segment switches.

Qwen3.8 Flash Next IQ3_XXS on Strata 0.1.40, RTX 4070 Ti 12 GB, proxy 0.3.1 as released (no extra hints):

| client | episodes | right values | via `recall` the model chose to call |
|---|---|---|---|
| Claude Code 2.1.284 | A, B, C, D × 2 | 8 / 8 | 8 / 8 |
| Claude Code 2.1.284 | L: 127K virtual, 3 segment switches | 1 / 1 | yes |
| pi 0.87.1 | A, B, D | 3 / 3 | 3 / 3 |
| pi 0.87.1 | L: 231K virtual, 6 segment switches | 1 / 1 | yes (5 keyword searches) |
| omp 18.10.1 (oh-my-pi) | A, B, C, D | 4 / 4 | 4 / 4 ¹ |
| omp 18.10.1 (oh-my-pi) | L: 82K virtual, 2 segment switches | 1 / 1 | yes (6 calls) |

¹ With omp the model repeated the values in its own one-line answer in B (all three) and D (two of three), so they
stayed visible: there recall was strictly needed only for D's third value. A and C are clean.

A control with the full history in the prompt (no masking) was right in every short episode. In the long one it
re-ran the setup script and wrote the new, wrong values — the proxy run did better. Each episode was checked for the
true values in the final prompt (none were there, except the omp cases in ¹); one file per configuration was also
checked by hand.

Across 18 real sessions (pi, Claude Code, Codex) the need is rare: 5 spontaneous recalls against 108 after a question
about the past; 30 re-reads of a masked, unchanged file (≈37K tokens); 4 edits out of 505 failed because the model
misremembered a masked file. When a file can be re-read, the model re-reads it — which is right when it has changed.

Limits: one model, one run of each long session, scaled-down thresholds (32K window) for the long sessions; Cline
not measured (it cannot resume a session headless without a Cline account). Report:
[docs/dev-notes/RECALL-NEED-RESULT.md](https://github.com/lastreload/mnemonic-proxy/blob/main/docs/dev-notes/RECALL-NEED-RESULT.md)
(Italian).

### Long context on Strata, without the proxy

16 `name=number` facts scattered in a generated Italian story, question at the end, temperature 0, two seeds, from
16K to 122K tokens of prompt: **151/160 exact, 0 wrong** — every miss is an omitted line, all at 64K and above.
Prefill stays at ~2.5K tok/s up to 122K, VRAM flat at 11.6 GB. Same report, section 4.

## User reports

Numbers measured by users on their own hardware and shared with permission. Not reproduced by us.

| | [u/Swimming-Farmer7815](https://www.reddit.com/r/Qwen_AI/comments/1x26aj9/comment/pfb7uu7/) |
|---|---|
| hardware | RTX 5060 Ti 16 GB, Xeon E5-2699 v3, 64 GB DDR4 ECC |
| engine / model | Strata, Swift-Qwen3.8-Flash-Next IQ3_XXS, 128K window |
| client | Cline, with its automatic context compaction disabled |
| session | 13 h; ~280K tokens of total history, ~77K in the last request |
| saved state | ~58K tokens restored in under 4 s (about 1 min 15 s to re-read at the measured prefill rate) |
| `recall` | 8 calls, all 8 results used by the model |

The user reports no compaction pauses during the session. All 8 recalls happened in the last two minutes of the
session, when the user asked for a summary of the day: during normal work the model never called `recall` on its
own. In our tests with Claude Code, pi and omp the model did call `recall` by itself when the value could not be re-read
from disk ([Does the model recall on its own?](#does-the-model-recall-on-its-own)); with Cline we could not test it.

**Share your numbers.** If you run the proxy on your own hardware, a report helps — especially with clients and models
we have not tested (Cline, OpenCode, other models). Useful: GPU/RAM, engine and model, client, session length (total
history and tokens in the last request), restore time, how many `recall` calls and whether the model made them on its
own. `data/journal.jsonl` holds most of it; check it before sharing, it contains your recall queries. Open an issue
with the [user report](https://github.com/lastreload/mnemonic-proxy/issues/new?template=user-report.md) template.

## Written about it

- [Radyz.cz](https://radyz.cz/nazory/mnemonic-proxy-agenticka-pamet-kontextove-okno/) (Czech, 25 Oct 2026):
  *"AI si nepamatuje. Jen si vede spis."* ("AI doesn't remember. It just keeps a file."). An independent review of how
  the proxy works, the session and benchmark numbers with their limits, and the privacy side. It points out that there
  is no command yet to delete a conversation; that is tracked in [#19](https://github.com/lastreload/mnemonic-proxy/issues/19).

## Engines

| client \ engine | Strata | llama-server | ds4-server | any OpenAI-compatible |
|---|---|---|---|---|
| Chat Completions (pi, omp, Hermes, Continue…) | tested · full | tested on CPU · full | tested with pi (GLM 5.3, Qwen3.8) · engine keeps its own state | base (no saved state) |
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
  restore times were not re-measured there (see [Saved state, measured](#saved-state-measured)).
**Saved state that the engine does not support.** After a reply that means "not supported", the proxy stops
saved-state work for that model only (anchors and their one-token calls, segment seal/save, SAVE/RESTORE,
autosave/autorestore, kv_archive). Masking, the text archive, `recall` and notes keep working.

| Reply to a slot call | What the proxy does |
|---|---|
| HTTP 500 "the engine answered a session … the server ended the engine" (PolyStrata `strata-poly`) | suspended until restart or reset |
| HTTP 501 with a documented permanent text (no `--slot-save-path`, parallel requests, strata-poly with VecSzn/PolyStrata#114) | suspended until restart or reset |
| any other HTTP 501 | paused for 15 minutes, then one call retries |
| 404, 400, 409, 503, 507, generic 500, timeouts | not a refusal |

`GET /v1/strata/saved-state` lists the suspended models. `POST /v1/strata/saved-state/reset` with `{}` (all models)
or `{"model": "<exact id>"}` clears them; it answers `{"cleared": [...]}`, 400 for a bad body, and **403 unless the
request comes from this machine** (the proxy has no client authentication). The check looks at the direct TCP
peer: a local reverse proxy can make remote requests look local, so block this endpoint at that proxy if remote
access must be denied. After a reset the next save is sent again, so on a strata-poly model without the PolyStrata
fix it ends the engine once more.

The proxy warns once per suspension: after a reset or a successful retry, a new refusal is reported again. The
retry after the 15-minute pause happens only when the proxy next needs saved state, so `GET` may show
`retry_after_s: 0` until then. The suspension adds no new requests to the engine's status endpoint.

On current PolyStrata releases the **first** SAVE to a `strata-poly` model still ends the engine: the proxy learns
from that reply. To avoid it, set `mask_anchor`, `slot_save`, `autosave`, `autorestore`, `seal_experimental` and
`kv_archive` to `false` (in the configuration, or in that model's profile).
  `setup.sh` and `run-strata-proxy.sh` in the repository root (a venv, then the proxy in front of a running
  Strata, `slot_dir` read from the Strata config) were contributed and are covered by unit tests; they are not yet
  tested by the maintainer on a real Strata install.

  `examples/config.example.json` is the configuration used with session files;
  `examples/config.official-strata.json` the one for Strata without `--slot-save-path` (or older than 0.1.40).
- **Generic OpenAI-compatible** (vLLM, online APIs, …) — base mode: masking, archive + `recall`, segments with
  handoff notes, 📌/🗑, streaming. No engine state, no saved files. Not tested on vLLM itself.
- **ds4-server** ([antirez/ds4](https://github.com/antirez/ds4)) — automatic detection, tool-call ids untouched, no
  duplicate saving (ds4 has `--kv-disk-dir`). Tested with pi, see below.

### ds4-server

ds4-server keeps its own state: with `--kv-disk-dir DIR` it checkpoints the KV cache to disk by text prefix and
resumes a conversation after a tool call by recognising the tool-call id (exact replay of the sampled call). So the
proxy does less than with other engines:

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
[Requirements](#requirements)).
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

## Configuration

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

`--config` takes a JSON object with any field of `ctxproxy.core.Config`. Defaults below are the real ones in the
code. Most features are off by default so that 0.1 behaviour is unchanged; the last column is what we recommend
with an engine that has saved state (`examples/config.example.json` plus the options the benchmarks above support).

| field | default | meaning | recommended |
|---|---|---|---|
| `window` | 131072 | physical context of the engine (llama-server, ds4, Strata, Ollama, LM Studio: taken from the engine if unset; Ollama / LM Studio: a bigger value refuses the start) | 131072 |
| `window_margin`, `window_estimate_slack` | 64, 0 (0.15 on generic engines without `--tokenizer`) | window guard: tokens kept free; inflation of the estimated prompt count | default |
| `mask_trigger` / `mask_target` | 80000 / 40000 | start a masking block above trigger, mask oldest-first down to target | default |
| `min_batch_tokens` | 16000 | a block runs only if it frees at least this much | default |
| `keep_recent_tokens` / `min_age_turns` | 16000 / 2 | protected tail, never masked | default |
| `mask_reasoning` / `mask_tool_args` | false / false | also mask old reasoning / large old tool-call arguments | **true / true** |
| `mask_anchor`, `anchor_min_tokens` | false, 24000 | save/restore engine state at the masking frontier (saved state) | **true**, 24000 |
| `slot_dir` | "" | the engine's `--slot-save-path` folder, same host (anchor cleanup, autosave, `kv_archive`) | set |
| `segments_enabled`, `tail_max`, `notes_max_tokens` | true, 24000, 8192 | segment switch with handoff notes | true, 24000, **12288** |
| `slot_save` / `seal_experimental` | false / false | save the old segment to disk before switching (saved state) | **true / true** |
| `response_floor` | 0 | minimum useful response budget for segment switching (bounded to a quarter of the window) | **16384** |
| `inject_recall`, `max_recall_rounds`, `recall_max_tokens` | true, 4, 6000 | the `recall` tool | default |
| `recall_tool_name` / `tools_tool_name` | "recall" / "tools" | names of the proxy's tools for new conversations | default |
| `prompt_language` | "en" | language of the texts the proxy writes into the prompt, for NEW conversations: `"en"` or `"it"`; stored per conversation at first use; conversations from before 0.3.1 stay Italian | default |
| `recall_struct`, `recall_flex`, `recall_multi` | false, false, false | structured recall: file timelines, links, flexible words, several queries per call (the benchmark above) | **true, true, true** |
| `auto_recall` / `auto_recall_hint` | false / false | `auto_recall` enables automatic retrieval on new user messages; `auto_recall_hint` only selects a one-line hint with ids instead of pieces and requires `auto_recall: true` | false / false (automatic retrieval off) |
| `recall_leaf_select` | false | on structured outputs (yaml/toml (Python ≥ 3.11)/xml/json, tables, diffs, logs, git log, pytest) `recall` returns only the matching record and the key lines of its ancestors, verbatim, with `⋮` markers and offsets for the gaps; falls back to the normal recall when unsure ([dev note](docs/dev-notes/LEAF-SELECT-RESULT.md)) | default (experimental) |
| `answer_thinking_after_recall`, `answer_thinking_budget` | false, 512 | turn thinking on only for the answer that follows a recall (fixes answers cut in the middle of a value) | default (experimental) |
| `truncation_guard` | false | replay once, with thinking, a short answer that ends on a prefix of a recalled token | default (experimental) |
| `tools_paging`, `tools_core` | false, read/bash/edit/write/grep/find/ls | tools on demand: only core tools + `recall` + `tools` in the prompt | **true** |
| `typed_receipts`, `receipt_guard` | true, true | typed one-line receipts for hidden outputs; block write/edit/shell calls (pi, Claude Code, Codex names) that copy a receipt or the shortened form of an old argument | default |
| `pins_max_tokens` | 8192 | cap of the carried-over 📌 block | default |
| `autosave`, `autosave_idle_s`, `autosave_keep`, `autosave_max_gb` | false, 180, 10, 25 | save engine state when the conversation is idle (saved state) | **true**, 180, 10, 25 |
| `autorestore`, `autorestore_min_gain` | true, 8192 | restore before forwarding when it saves at least this many tokens | default |
| `kv_archive`, `kv_archive_idle_s`, `kv_archive_min_age_s` | false, 600, 1800 | cold archive of state files (needs Python ≥ 3.14 for `compression.zstd`) | **true** |
| `engine`, `slot_id`, `engine_tokenize` | "auto", 0, false | engine type, slot, token counting via the engine's `/tokenize` | auto |
| `router_model`, `router_load_timeout_s`, `router_keepalive_s` | "", 600, 10 | llama-server router mode: model used when the client's `model` is not served (empty = refuse), how long a model may take to load, seconds between two SSE comments to a streaming client while the model loads | default |
| `live_dump` | false | write `data/live/last_request.json` for the dashboard | **true** |

A config for llama-server with saved state, as recommended above:

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

### Tool names and older conversations

The proxy's own tools are called `recall` and `tools` (0.1 called them `strata_recall` and `strata_tools`). The tool
list is part of the prompt prefix, so conversations already in the archive from 0.1 keep the old names — renaming
them would force a full re-read and break saved state. New conversations get the new names. A call to the old names
is always resolved. If the client already declares a tool called `recall` (or `tools`), the client's tool is left
alone and the proxy uses `history_recall` (or `load_tools`) instead.

## What we have not proven

- Engine windows below 16K: refused since 0.3.0. At 8K the recall trap passed once (Qwen3.8 on ds4, after the
  0.3.0 fix) and failed in the runs before it; small windows stay excluded until they are proven on more runs.
- Long-range quality is not free. Of six questions about the first hour of a 3-hour session, four were answered
  exactly, two partially (one value missed, one timeline mixed up). None were invented.
- The model does not attend to 448K tokens. It sees at most one window; the rest is recallable, not "in context".
- Segment switches cost ≈2 minutes of note-writing on a 12 GB GPU.
- Saved state on Strata: restore times measured on our 0.1.38 + PR #668 build; on the released Strata 0.1.40 (which
  includes it) the proxy ran the AgentLongBench benchmark and the recall tests, but restore times were not
  re-measured.
- Claude Code and Codex are tested live only on Strata; on llama-server and ds4-server only against mock engines.
- Copied shortened arguments (found live with Claude Code: 3 writes out of 140 ended with the `…` line of a masked
  old argument, one caused a real `NameError`). Since 0.3.0 the proxy blocks a write/edit/shell call whose text
  contains a line made only of `…` and answers the model with an error pointing to `recall`; tested on a mock
  engine, not yet live. Still possible: a copy that drops the `…` line (only the first 160 characters of the old
  argument) passes, and a real file whose content has a line made only of `…` is blocked.
- Old reasoning is masked. On questions whose answer was only in the model's reasoning ("what had you estimated?"),
  a replayed real session went from 3 exact + 1 partial out of 5 (reasoning kept) to 2 out of 5 (reasoning removed)
  — and the model did not say it did not know: it took the final values from the files and presented them as the
  original estimate. Questions about visible content: 7/7 either way. Measured without the proxy's recall loop.
- Facts scattered in a long prompt: 0 wrong out of 160 on Strata, but 9 omissions, all from 64K up (two seeds).
- Recall search is lexical (FTS5 + structure), not semantic: a paraphrase with no shared words can miss.
- Unprompted recall is measured with Claude Code, pi and omp on one model; with Cline a user saw recall only when asking
  about the past, and we could not reproduce that setup.
- One sequence: the proxy serialises requests and assumes it is the engine's only client; another client on the
  same engine costs re-reads.
- llama-server router mode: tested on CPU with two tiny models (stories260K, stories15M) and on AI395 (Vulkan) with
  Qwen3.6-35B-A3B (64K) and Gemma 4 E2B (32K), `--models-max 1`, one slot per model. Not tested: `--parallel` > 1,
  `--models-max` > 1 with evictions under load, `--sleep-idle-seconds`, `/models/sse` (not used: the proxy reads
  `/models` at every request), `check --live`. A client that goes away while a model loads frees the proxy only when
  the load ends (the proxy does not stop a load in progress).
- Numbers come from single runs on one machine and one kind of workload; treat them as indicative.

## Security & privacy

- **Everything stays local.** The proxy talks only to the `--upstream` you give it; no telemetry, no update checks,
  no other network access. The dashboard and the MCP server bind to `127.0.0.1` and are read-only.
- **The archive is plaintext.** `data/archive.sqlite` holds everything the agent saw — file contents, command
  outputs, reasoning — secrets included. `data/journal.jsonl` holds request metadata and recall queries;
  `data/live/last_request.json` (with `live_dump`) the whole last prompt. Saved engine state files (`slot_dir`) and
  `kv-archive/` hold the conversation's tokens.
- The proxy binds to `127.0.0.1` by default and has no authentication; do not expose it (`--host`) on a shared
  network. The MCP server's HTTP mode also binds to `127.0.0.1`.
- Protect the folders: `chmod 700 ./data /path/to/slots`.
- **Forget one conversation:** stop the proxy, then run:

  ```sh
  mnemonic-proxy forget --data ./data --list
  mnemonic-proxy forget --data ./data --config config.json --conv <id> --dry-run
  mnemonic-proxy forget --data ./data --config config.json --conv <id>
  ```

  `--list` shows ids, logical SQLite row sizes and first/last UTC timestamps. `--dry-run` lists table rows,
  files and bytes without changing stored data. `--slot-dir DIR` and `--kv-archive DIR` override the config;
  otherwise they are resolved by the same loader as the proxy (`--config`, then `MNEMONIC_CONFIG`, then Config
  defaults; relative paths are relative to the working directory). The command prints the paths and their source.
  With a `slot_dir` but no `kv_archive_dir`, the cold archive defaults to its sibling `kv-archive/`, even if cold
  archiving is currently disabled. Supply the directories actually used by the engine, including remote state
  mounted locally: the command does not contact the engine or discover unknown directories.

  It removes conversation rows from every table with a `conv` column, namespaced `kv` entries and the tracker
  state if owned by that conversation; drops the derived structured-recall index (rebuilt on demand for survivors),
  rebuilds archive FTS, enables SQLite `secure_delete`, runs `VACUUM` and truncates the WAL. It also deletes segment,
  anchor and autosave files, full-prompt `.bin.json` sidecars, cold manifests and unreferenced blocks (shared blocks
  survive), filters the journal atomically, clears a matching live dump, and filters router slot ownership metadata.
  The cold archive cleanup needs no compression module, so forgetting works on Python 3.10 too.

  The server and maintenance command hold the same non-blocking, canonical-data-directory lock: on POSIX it locks
  the directory inode without creating a file; on Windows it locks a persistent sibling
  `.mnemonic-proxy-<sha256-of-canonical-data-path>.lock` (control metadata only, never deleted). `forget`, including
  listing/previewing, refuses while a proxy or another maintenance command owns the lock. Older proxy versions do
  not take this lock: stop them first. Stop other writers too (including engine clients and archive CLI commands);
  a read-only MCP/dashboard process can retain already-read content in memory, so close it when that matters.

  Exit status is 0 on success, 1 for an unknown id, busy folder, unreadable/corrupt data or incomplete removal, and
  2 for invalid arguments. A filesystem failure may leave a partial deletion: the report names the errors; fix
  them and retry the same command (orphan state/journal data can be removed even after SQLite rows are gone).
  Backups, snapshots, engine memory and copies outside the selected folders are not erased. `VACUUM`/unlink are not
  a secure-wipe guarantee on SSDs, copy-on-write filesystems or backups. For reference only:
  [manual deletion checklist](docs/manual-forget.md).

## Tools: audit the archive and measure restore

Two stand-alone tools, Strata-only for now. Both are read-only against the archive and
never restart the engine; run them with the Python that has the archive support
(`.venv314`).

**`tools/check_kv_archive.py`** — is the cold archive healthy, and what is it saving?

```sh
.venv314/bin/python tools/check_kv_archive.py                 # snapshot now
.venv314/bin/python tools/check_kv_archive.py --restore-check  # rebuild each archive, compare sha256
.venv314/bin/python tools/check_kv_archive.py --watch          # follow until it archives
```

It reports the `sessions/` files, the `kv-archive/` usage with dedup, and how far the
archiver is from firing (engine idle, file age, file size). It never creates the archive
directories and never writes to disk: `--restore-check` restores into a private temp dir.
Exit code 0 when everything is fine, 1 on a problem (a corrupt block, a failed restore,
a `--watch` that never archived), 2 on a usage error.

**`tools/measure_restore.py`** — how much does the automatic restore save?

```sh
.venv314/bin/python tools/measure_restore.py --allow-shared-engine
```

It sends **the same request twice**, each time after B has evicted A from the engine:
**control** (`autorestore` off) gives the real re-read, **restore** (`autorestore` on)
gives `restore_ms` plus the few new tokens — both read from the journal, so the speed-up
is `control_ms / (restore_ms + new_ms)`, not an extrapolation. `--two-phases` is the
earlier method: it runs a whole A/B cycle per phase and re-sends A with one extra
message, so its restore path also carries those new tokens — a slightly different
question, measured with the same journal timings.

Measured with **Strata 0.1.41** and **swift-1.5-iq3_xxs** (window 131072, prompt
~31.4K tokens) on an **RTX 5060 Ti 16 GB** (PCIe 3.0 x8), an **Intel Xeon E5-2699 v3**
and **64 GB DDR4-2667 running at 2133 MT/s** (the CPU's memory limit), with three runs
of each method (six runs in total): the same request gave **~10–12×** (median 11.6×) —
re-read ~30 s vs restore + new tokens ~2.7 s — and the two-phase method gave ~6.7×
under the same conditions.

It needs `--allow-shared-engine` because filling the engine with conversation B evicts
other sessions; it removes only the files it created itself. Exit code 0 when a restore
was measured, 1 when nothing was restored, 2 on a usage error.

## Tests

```sh
python3 -m unittest discover -s tests -t .
```

Uses a fake OpenAI-compatible engine (`tests/fake_engine.py`, also runnable stand-alone with
`python3 -m tests.fake_engine --port 18095`). Optional groups are skipped unless their inputs are present: exact
counting (a tokenizer in `./tok` or `CTX_TOKDIR`, plus `tokenizers`), Strata mock integration (`STRATA_DIR`, plus
`jinja2`), and `kv_archive` (Python ≥ 3.14). `tools/replay_dry.py` and `tools/replay_exact.py` replay a pi session
through the context manager offline. `tools/state_e2e.py --server PATH/llama-server --model M.gguf --work DIR`
checks saved state end to end with a real llama-server on any OS (restart, restore from disk vs re-read, and
`kv_archive` with `--kv-archive`). CI runs the unit tests on Linux and on native Windows.

The two Strata tools have their own tests: `tests/test_kv_archive.py` and
`tests/test_check_kv_archive.py` build a real archive with the same in-memory helper (no engine
needed) and check that the audit detects a corrupt block, exits non-zero, and touches nothing;
`tests/test_measure_restore.py` covers the pure logic (journal filtering, the speed-up from
`prompt_ms` / `restore_ms`, the CLI guards). The `kv_archive` group needs Python ≥ 3.14.

Partly in Italian: code comments, the dashboard UI and the developer notes in `docs/dev-notes/`. Since 0.3.1 every
text the proxy writes into the model's prompt — receipts and placeholders of hidden content, the handoff-note
request, the segment and pinned-points blocks, automatic recall, recall results, guard and limit messages — is in
English for new conversations (`prompt_language`, default `"en"`). Conversations that already existed before 0.3.1
keep the Italian texts they were born with: those strings are part of their stable prompt prefix (archive, session
files, KV cache), so changing them would invalidate saved sessions. The language is stored per conversation at its
first request; the proxy recognizes its own markers in both languages in every conversation.

## Credits & license

MIT — see [LICENSE](https://github.com/lastreload/mnemonic-proxy/blob/main/LICENSE). Built by Maurizio Verde — LastReload.

Thanks to Niko1221 and the [Strata](https://github.com/Niko1221/Strata) contributors for the inference engine that
made our large-model measurements possible on a single 12 GB GPU; to the [llama.cpp](https://github.com/ggml-org/llama.cpp)
contributors for llama-server and its slot save/restore API; and to the authors of pi for the coding agent used in our
experiments.

The name is a nod to William Gibson's courier, not affiliated with the story or the film.

```bibtex
@software{verde2026mnemonicproxy,
  author = {Verde, Maurizio},
  title  = {Mnemonic Proxy: long-running local coding agents with a verbatim archive},
  year   = {2026},
  url    = {https://github.com/lastreload/mnemonic-proxy}
}
```
