# Install and platforms

## Install

The first time: install, watch it work with no model, check your engine, start. No configuration file is needed
to begin — these five lines are the whole first run.

<!-- readme-test: ci -->
```sh
python3 -m venv mnemonic-venv && . mnemonic-venv/bin/activate
pip install mnemonic-proxy
mnemonic-proxy demo      # 1 minute, no model: archive, masking, recall -> PASS
mnemonic-proxy check --upstream http://127.0.0.1:8095 --port 8096 --data ./data
mnemonic-proxy --upstream http://127.0.0.1:8095 --port 8096 --data ./data
```

`check` ends with one of three outcomes: **`READY` (exit 0)**, **`READY with warnings` (exit 2)** — it works, but
something optional is off (typically saved state) — or **`NOT READY` (exit 1)**, and then it says, line by line,
what to change; `--live` adds a real generation, a tool call and a save/restore. `--upstream` is the engine's
address. Then [connect your agent](agents.md#connect-your-agent) to the proxy. The engine window must be at least
16K tokens, 32K or more recommended ([Requirements](#requirements)). Step by step with a small model on CPU:
[Quick start](#quick-start).

### No engine yet, a configuration file, one click

**No engine running yet?** The package ships the fake engine the test suite uses, so `check` and the proxy can be
tried with nothing installed but this package:

<!-- readme-test: ci -->
```sh
python3 -m ctxproxy.fake_engine --flavor llama --n-ctx 32768 --port 8095 &
mnemonic-proxy check --upstream http://127.0.0.1:8095 --port 8096 --data ./data
```

**A configuration file, when you want one** (saved state, or an engine that needs explicit thresholds). It is a
separate, explicit step — the proxy runs on its defaults without it:

<!-- readme-test: ci -->
```sh
mnemonic-proxy init-config --engine strata        # or llama-server, ds4, openai-32k
mnemonic-proxy check --upstream http://127.0.0.1:8095 --port 8096 --data ./data --config config.json
```

`init-config` writes a **minimal** `config.json` (0.3.24): only what the proxy cannot work out by itself, so the
[automatic configuration](configuration.md#automatic-configuration) keeps deciding the rest. For Strata, llama-server and
ds4-server that is *nothing at all* (`{}`) — the window comes from the engine, and the saved-state options, the
exact token count and the window-sized budgets are chosen from the engine and the machine. The exceptions are the
only two things nobody can discover: `--slot-dir DIR` (the engine's own `--slot-save-path`, for saved state) and,
for a plain OpenAI-compatible engine that reports no context, `window`.

<!-- readme-test: illustrative -->
```sh
mnemonic-proxy init-config --engine strata --slot-dir /path/to/sessions   # {"slot_dir": "/path/to/sessions"}
mnemonic-proxy init-config --engine strata --full                         # every field, documented
mnemonic-proxy init-config --engine strata --launcher                     # + the one-click starter, below
```

`--full` writes the complete documented example from the copy inside the package (no network, no clone) — every
field explicit, which is also what *switches the automatic configuration off*, so use it as a reference and delete
the lines you do not mean to pin. `init-config` prints which file it used and never overwrites a file you have
already edited (`--force` does). It never writes `"auto_config"`: it is already the default, and writing it would
remove a protection — on a data folder that already holds conversations the automatic choices are only *applied*
when `auto_config` is explicit, so that an upgrade cannot change how a running setup behaves. The complete
examples are browsable at [`examples/`](https://github.com/lastreload/mnemonic-proxy/tree/main/examples); `check`
prints the full `raw.githubusercontent.com` URL of the one for *your* version whenever it suggests one. Fields:
[Configuration](configuration.md#configuration).

**One click** (`--launcher`, 0.3.24; asked for by the first Windows user). Writes `start-mnemonic-proxy.bat` on
Windows and `start-mnemonic-proxy.sh` elsewhere, next to the config and with the values you passed baked in:

<!-- readme-test: ci -->
```sh
mnemonic-proxy init-config --engine strata --launcher --dir ./one-click \
  --upstream http://127.0.0.1:8095 --port 8096 --data ./data
```

It starts **only the proxy** — your engine's own command line stays yours — and in this order: it refuses to go on
if the engine does not answer, runs `check` once (a port already taken is reported there, with the fix), then runs
the proxy. Every value is a variable at the top of the file (`UPSTREAM`, `PORT`, `DATA`, `CONFIG`, `VENV`, and a
commented-out `TOKENIZER` unless `--tokenizer` was given), so it can be edited or overridden from the environment;
on Windows it ends with `pause`, so a console opened by a double-click does not vanish with the error in it. For
llama-server *and* its engine in one file, see
[`examples\start-windows.bat`](https://github.com/lastreload/mnemonic-proxy/blob/main/examples/start-windows.bat)
([Platforms](#platforms)).


### Strata in two commands

Strata already running? Then there is nothing to prepare: no folder to create, no extra engine flag, no
configuration file.

<!-- readme-test: ci -->
```sh
pip install mnemonic-proxy
mnemonic-proxy --upstream http://127.0.0.1:8095 --port 8096 --data ./data
```

The proxy creates `./data` itself and reads the window from Strata's `/v1/status`. Point your agent at
`http://127.0.0.1:8096/v1` instead of 8095 and turn off its own auto-compaction ([Connect your agent](agents.md#connect-your-agent)). Everything but
one feature is already working: masking, the verbatim archive, `recall`, segments with handoff notes, 📌/🗑 and
tools on demand. Run `mnemonic-proxy check --upstream http://127.0.0.1:8095 --data ./data` first if you want the
setup verified line by line; the startup log says in one line what is on and what is off.

**Saved state (optional, and only later).** Saved state buys back one thing: the *fast resume* of a long
session (1.4 s instead of ≈50 s on a 110K-token conversation, [measured](benchmarks.md#saved-state-measured)). It needs
Strata ≥ 0.1.40 restarted with one extra flag — this exact line, added to the Strata start command you already
use:

<!-- readme-test: real-engine -->
```sh
strata --slot-save-path /path/to/sessions        # plus your usual Strata options
```

and the same folder named in the proxy's config (the proxy creates it if it does not exist, so there is no
`mkdir` step) — one command writes exactly that file and nothing else:

<!-- readme-test: ci -->
```sh
mnemonic-proxy init-config --engine strata --slot-dir /path/to/sessions --dir ./saved-state
```

<!-- readme-test: illustrative; config: partial -->
```json
{
  "slot_dir": "/path/to/sessions"
}
```

Then start the proxy with `--config config.json`. `slot_save`, `mask_anchor` and `autosave` are **not** written:
the proxy switches them on by itself once `slot_dir` is there and the engine, the RAM and the disk allow it, and
it says so in one line at startup and in `check` ([Automatic configuration](configuration.md#automatic-configuration)); writing
them by hand only matters to force them, or to keep them off. `check` reports `saved state: supported by the
engine, configured in the proxy`, and `check --live` verifies that `slot_dir` really is Strata's
`--slot-save-path` (same folder, same host) by saving and restoring a real state file. Nothing else changes.

**Your engine.** Start it as below. The `config` column is what `init-config` writes for that engine: minimal by
default, the complete example with `--full`.
[`examples/`](https://github.com/lastreload/mnemonic-proxy/tree/main/examples) holds the complete ones. `slot_dir`
and the engine's `--slot-save-path` are only for saved state: skip both to run in base mode, add them later (the
shortest Strata path is [above](#strata-in-two-commands)).

| engine | engine command (minimum) | config | saved state |
|---|---|---|---|
| Strata, any version | Strata's own start | none; base mode on an engine that *could* save state: `--engine official-strata` | no (base mode) |
| Strata ≥ 0.1.40, with saved state | Strata's own start, plus `--slot-save-path DIR` | `--engine strata --slot-dir DIR` (one field; `--full` = `config.example.json`) | yes |
| llama-server | `llama-server -m model.gguf -c 32768 --parallel 1 --jinja --port 8095` (add `--slot-save-path ./slots` for saved state) | `--engine llama-server --slot-dir ./slots` (`--full` = `config.llama-server.json`) | yes with `--slot-save-path` |
| llama-server, router mode (several models) | `llama-server --models-preset preset.ini --models-max 1 --port 8095`; preset: `[*]` with `slot-save-path` and `parallel = 1`, a `ctx-size` (`c`) ≥ 16384 per model | `--engine llama-server-router --slot-dir ./slots` + `preset.router.example.ini` | yes, per model |
| ds4-server | `ds4-server -m MODEL.gguf --ctx 65536 --kv-disk-dir ./kv --port 8110` | none (`--full` = `config.ds4.json`) | kept by ds4 itself |
| Ollama, LM Studio | engine context set to 32768 (see below); `--tokenizer` recommended | none: the window is read from the engine | no (base mode) |
| Unsloth (`unsloth run`) | `unsloth run --model MODEL --context-length 32768 --api-only -p 8888 --disable-tools` (see below); it prints an `sk-unsloth-…` API key | none: the window is read from the engine; `--upstream http://127.0.0.1:8888 --api-key sk-unsloth-…` | no (base mode) |
| any OpenAI-compatible (vLLM, online APIs) | — | `--engine openai-32k` at 32K (just `{"window": 32768}`); at 131072 none is needed | no (base mode) |

The proxy reads the window from Strata, llama-server (`n_ctx`, per model in router mode), ds4-server (`--ctx`),
Ollama and LM Studio (the context of the loaded model, 0.3.8), and scales its thresholds to it, unless `window` is
written in the config. From any other engine it cannot: write the engine's real context in `window`, and since
0.3.24 the thresholds scale from it exactly as they would from an engine-reported one (so
`{"window": 32768}` is a complete 32K configuration — `config.openai-32k.json` spells the same numbers out).
A `window` written below the engine's own context (for example `{"window": 32768}` in front of a Strata started
with 131072) scales the thresholds the same way since 0.3.26; before, they stayed at their 131K values and every
request switched segment.
Details per engine: [Engines](engines.md#engines).

**Ollama and LM Studio** (tested on Linux, small model, details in [Tested combinations](engines.md#tested-combinations)). Both
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
  (`loaded_context_length`). Ollama is recognised only when `GET /api/version` **and** a coherent `/api/ps` models
  array both answer (0.3.24): a gateway such as llama-swap that exposes the version but not `/api/ps` runs as a
  generic engine, and its `/v1/models` catalog is reported as model availability with the loaded state and the
  window unverified. It reads it again before every request (one cheap GET; a model that is not loaded is
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

**Unsloth** (tested on Linux with Unsloth 2026.11.3, details in [Tested combinations](engines.md#unsloth-measured)).
`unsloth run` starts llama.cpp's `llama-server` on a private port and serves it through Unsloth's own API, which
needs an API key. Point the proxy at Unsloth's port (8888 by default), not at the private `llama-server` port: that
one changes at every start and has a different key.

<!-- readme-test: real-engine -->
```sh
unsloth run --model /path/to/model.gguf --context-length 32768 --api-only -p 8888 --disable-tools
# --model also takes a Hugging Face repo, e.g. unsloth/<model>-GGUF:UD-Q4_K_XL
# when the model is loaded it prints:  API Key: sk-unsloth-...
mnemonic-proxy check --upstream http://127.0.0.1:8888 --api-key sk-unsloth-...
mnemonic-proxy --upstream http://127.0.0.1:8888 --api-key sk-unsloth-... --data ./data
```

The agent talks to the proxy as usual (`http://127.0.0.1:8096/v1`) and does not need the key. What to know:
- **The API key.** `unsloth run` prints it once the model is loaded; the web UI also creates keys (Settings → API).
  Without it Unsloth answers 401 to every request. The proxy sends it as `Authorization: Bearer`.
- **The window.** Unsloth answers llama-server's `/props` and `/slots`, so the proxy recognises it as llama-server
  and reads the context from it. Set `--context-length` yourself (32768, at least 16384): without it Unsloth picks
  one for the model, and its default `auto` GPU memory mode may shrink it to fit the GPU.
- **No saved state, by design.** Unsloth starts `llama-server` with `--slot-save-path` but keeps slot save/restore
  for itself: its API forwards `GET /slots` and answers 404 to `POST /slots/0?action=…`, and `--slot-save-path` is
  refused as an extra argument ("Studio manages that itself",
  [unsloth#11705](https://github.com/unslothai/unsloth/issues/11705)). Unsloth's own save only runs when it unloads
  an idle model (off by default) or makes room for an image/video model, and restores when the same model loads
  again; a restart still re-reads the whole conversation
  ([unsloth#9037](https://github.com/unslothai/unsloth/issues/9037), open). The proxy runs in base mode: masking,
  archive, `recall`, segment switches, streaming and all three APIs worked. If you stop the agent and come back
  while Unsloth is still running, the session is taken from llama-server's cache instead of being read again
  (0.4–0.9 s instead of 11–14 s for 9.7K tokens, [measured](engines.md#unsloth-measured)). After a restart of
  Unsloth the prompt is read again: at most one window (about 39 s for 28K tokens on our machine).
- **Unsloth's own tools.** We tested with `--disable-tools`. On `127.0.0.1` Unsloth turns its server-side tools (web
  search, code execution) on by default; that mode has not been tested with the proxy.
- `check` before 0.3.25 shows `/v1/models 401` on the "engine reachable" line even with `--api-key`: that probe did
  not send the key. Detection did, so nothing else is affected.

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

## Requirements

- **Engine window: at least 16K tokens (16384), 32K or more (32768) recommended.** Below 16K the proxy refuses to
  start, and `mnemonic-proxy check` reports NOT READY, with the command to restart the engine (`llama-server -c
  32768`, `ds4-server --ctx 32768`). Why: at 32K the recall trap of the ds4 runs (an error line removed from the
  prompt, asked for later) was answered right with both ds4 builds; at 8K it passed **once**, after the 0.3.0 recall
  fix, and failed before it. Small windows stay excluded until they are proven on more runs. Reports (Italian):
  [DS4-QWEN-COMPARE-RESULT.md](https://github.com/lastreload/mnemonic-proxy/blob/main/docs/dev-notes/DS4-QWEN-COMPARE-RESULT.md),
  [RECALL-8K-030-RESULT.md](https://github.com/lastreload/mnemonic-proxy/blob/main/docs/dev-notes/RECALL-8K-030-RESULT.md).
- Python ≥ 3.10 (`kv_archive`: ≥ 3.14). An OpenAI-compatible engine. **Saved state is optional**: without it the
  proxy still masks, archives, recalls and switches segments, and only the fast resume of a long session is lost.
  With it: llama-server with `--slot-save-path`, or Strata ≥ 0.1.40 with `--slot-save-path` (see
  [Engines](engines.md#engines)). The proxy creates that folder itself when it is missing and the engine is on this host.

## Quick start

Three levels. Each one stands on its own.

| | what you see | needs | time |
|---|---|---|---|
| **T0 — see it work** | archive, masking, exact recall, with a scripted fake model | Python ≥ 3.10 | 1 minute |
| **T1 — real model on CPU** | a real agent (pi) writing code through the proxy, masking, a recall probe (the tested 4B model did not call `recall`), resume after an engine restart | + cmake, a C++ compiler, Node.js ≥ 22.19, ~9 GB RAM (the engine at `-c 32768`), 3.5 GB disk | ~45 minutes |
| **T2 — real use (GPU)** | your own model and agent, long sessions | a GPU engine | — |

Commands are for Linux; see [Platforms](#platforms) for macOS and Windows (native or WSL).

### T0 — see it work (1 minute, no model)

<!-- readme-test: ci -->
```sh
python3 -m venv mnemonic-venv && . mnemonic-venv/bin/activate
pip install mnemonic-proxy
mnemonic-proxy demo
```

With [uv](https://docs.astral.sh/uv/), no venv to create: `uvx mnemonic-proxy demo`. `uvx` runs the command from a
temporary environment and installs nothing, so keep the prefix for the next commands (`uvx mnemonic-proxy check ...`)
or install the command once with `uv tool install mnemonic-proxy`. The development version (the
`main` branch, newer than the last PyPI release): `pip install git+https://github.com/lastreload/mnemonic-proxy`
(needs `git` on PATH: `sudo apt install git`). Its `--version` ends in `.dev0` — a `main` build is not the next
release, and the examples it points at are the ones on `main`.

The demo starts the real proxy in a temporary folder, in front of a scripted fake model. An agent reads a long
`build.log` that contains one build id; the session grows until the proxy hides the log; the demo prints what the
engine receives in its place (a one-line receipt); then the user asks for the build id, the model calls `recall`,
the proxy answers it internally and the archived text is compared byte for byte with the original. It ends with
`PASS` (exit 0) or `FAIL` (exit 1).

It proves the proxy's own logic: archive, masking, recall. It does not prove anything about a real model, its tool
calling, or saved engine state — T1 exercises those with a real model and reports its limitations.

### T1 — a real model on CPU

Tested end to end in a clean Ubuntu 24.04 container (see [Tested combinations](engines.md#tested-combinations)). A 4B model on
CPU is slow (seconds per answer) and limited: the point is to exercise the proxy with a real model, not to do real
work. A successful recall is not guaranteed; see the measured [Tested combinations](engines.md#tested-combinations).

**0. Prerequisites** (Ubuntu/Debian; other systems: [Platforms](#platforms)):

<!-- readme-test: real-engine -->
```sh
sudo apt install git cmake build-essential python3-venv curl
```

and Node.js ≥ 22.19 for pi (<https://nodejs.org/en/download>, or your package manager).

**1. Folder, proxy, example config.**

<!-- readme-test: real-engine -->
```sh
mkdir mnemonic-trial && cd mnemonic-trial
git clone --depth 1 https://github.com/lastreload/mnemonic-proxy
python3 -m venv .venv && . .venv/bin/activate
pip install ./mnemonic-proxy
cp mnemonic-proxy/examples/config.llama-server-32k.json .
mkdir -p slots
```

**2. llama.cpp, the tested release (CPU build, ~2 minutes on 12 cores).**

<!-- readme-test: real-engine -->
```sh
git clone --depth 1 --branch b11430 https://github.com/ggml-org/llama.cpp
cmake -S llama.cpp -B llama.cpp/build -DCMAKE_BUILD_TYPE=Release
cmake --build llama.cpp/build --config Release -j 8 --target llama-server
```

**3. The model**: Qwen3-4B-Instruct-2507, Q4_K_M (2.5 GB). Qwen publishes no GGUF for this model
([model card](https://huggingface.co/Qwen/Qwen3-4B-Instruct-2507)); this is the
[Unsloth conversion](https://huggingface.co/unsloth/Qwen3-4B-Instruct-2507-GGUF), pinned to the tested revision:

<!-- readme-test: real-engine -->
```sh
mkdir -p models
curl -L -o models/Qwen3-4B-Instruct-2507-Q4_K_M.gguf \
  https://huggingface.co/unsloth/Qwen3-4B-Instruct-2507-GGUF/resolve/a06e946bb6b655725eafa393f4a9745d460374c9/Qwen3-4B-Instruct-2507-Q4_K_M.gguf
sha256sum models/Qwen3-4B-Instruct-2507-Q4_K_M.gguf
# 3605803b982cb64aead44f6c1b2ae36e3acdb41d8e46c8a94c6533bc4c67e597
```

**4. The engine** (terminal 1, inside `mnemonic-trial`):

<!-- readme-test: real-engine -->
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

<!-- readme-test: real-engine -->
```sh
mnemonic-proxy check --live --upstream http://127.0.0.1:8095 --port 8096 --data ./data --config config.llama-server-32k.json
mnemonic-proxy --upstream http://127.0.0.1:8095 --port 8096 --data ./data --config config.llama-server-32k.json
```

`check` must end with `READY` (exit 0): engine recognised as llama.cpp, model loaded, window taken from `n_ctx`
(32768) with the thresholds scaled to it, saved state supported and configured, and — because of `--live` — one
real generation, one tool call, one save/restore whose file appeared in `./slots`. Each failing line says how to fix
it; see also [Troubleshooting](troubleshooting.md#troubleshooting). The proxy then prints
`[mnemonic-proxy] 127.0.0.1:8096 -> http://127.0.0.1:8095 (engine llama.cpp, saved state yes) ... window=32768`.

**6. pi, the coding agent** (terminal 3). Install the tested version:

<!-- readme-test: real-engine -->
```sh
npm install -g --ignore-scripts @earendil-works/pi-coding-agent@1.0.3
```

Create `~/.pi/agent/models.json` (if it exists, add the `mnemonic` entry to its `providers`):

<!-- readme-test: illustrative; config: client -->
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

<!-- readme-test: illustrative; config: client -->
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

<!-- readme-test: real-engine -->
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

What this proves and what it does not, for this model: see [Tested combinations](engines.md#tested-combinations).

### T2 — real use (GPU)

Use the engine and model you already have; the only requirements are an OpenAI-compatible endpoint and, for saved
state, an engine that can save its KV state to disk:

<!-- readme-test: real-engine -->
```sh
llama-server -m your-model.gguf -c 131072 --parallel 1 --jinja --slot-save-path /path/to/slots --port 8095
mnemonic-proxy check --upstream http://127.0.0.1:8095 --port 8096 --data ./data --config config.json
mnemonic-proxy --upstream http://127.0.0.1:8095 --port 8096 --data ./data --config config.json
```

with `config.json` = `examples/config.llama-server.json` and `slot_dir` = the same `/path/to/slots` (see
[Configuration](configuration.md#configuration) for the recommended fields). With an engine that has no saved state (vLLM, online
APIs) the proxy runs in base mode (window: see [Install](#install)). The measurements in
[One session, measured](benchmarks.md#one-session-measured) (Qwen3.8 Flash Next IQ3_XXS, 128K window) come from
Strata (our session-files build, 0.1.38 + PR #668) on a 12 GB RTX 4070 Ti — see [Engines](engines.md#engines); they say nothing about
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
that cannot load or render is never silently replaced by the built-in one. Pictures are forwarded to the engine and
budgeted, not refused: the template writes one vision marker per picture (never its bytes), and the guard counts each
marker as `image_tokens` tokens. That number is an **upper bound**, and it has to be: no engine reports the real
patch count before the request (Strata has no `/tokenize`, and its `/v1/messages/count_tokens` counts the marker
alone). The rule is **`image_tokens >= vision.max_tokens + 2`**: the reader clamps one picture's expansion to
`vision.max_tokens` (Strata's `vision.max_tokens`, 1024 on GPU and 300 on CPU by default; without it a picture once
became 93,700 tokens), while the marker itself is three special tokens of which two - `<|vision_start|>` and
`<|vision_end|>` - stay in the prompt around the expansion the allowance replaces. So a pack clamped to 300 needs
**302**, and Strata's GPU default clamp of 1024 needs **1026** - which is the shipped default; a round 1120 is the
same rule with a little free headroom. An over-reservation costs a little context, an under-reservation would send a
prompt over the window.
`image_tokens` has to be a non-negative integer (config load rejects anything else), and a request that carries a
picture while the bound is `0` is refused with `image_tokens=0` in its message: a picture cannot be priced at
nothing, and the startup line says the same before the first request.

Everything else about pictures is fail-closed, because a counted prompt that is not the prompt the engine builds is
a window nobody can guarantee. The markers the template wrote must be exactly the pictures being priced - a template
without a vision branch (a fine-tune that prints `content` as text) cannot serve pictures and is refused, never
tolerated. A picture in a `system` message is refused: Strata hands that message its text parts and the pack template
raises on its images, so its bytes would never reach the model. A picture with no readable source (a `file_id`, an
empty url) is refused instead of turning into an empty image, and a picture carried under a content type the adapter
does not read is refused instead of disappearing from the prompt. Video and audio parts stay refused: no supported
engine reads them, so their marker would have no honest budget. `window_guard` events for a prompt with pictures
carry `guarantee=image_bounded` with `images` and `image_allowance`, text-only prompts keep `guarantee=exact` and
their journal lines are byte-identical to before, and a refusal carries the reason in its 400 message. The structural
part of that holds on the estimated path too: without a tokenizer the count is only `guarantee=estimated`, but a
picture the adapter would not read is still a lost turn, so it is refused there as well.

A marker **quoted in text** is not a picture. Someone pasting `<|vision_start|><|image_pad|><|vision_end|>` into a
message - asking about the template, or pasting a log - puts the real spelling of the marker in the prompt, and
Strata shields such spellings instead of reading a picture (#28). The guard subtracts quoted spellings before it
checks markers against pictures and before it prices them (their characters stay reserved as literal slack), and the
saved-state stamp counts only pictures, so a quote cannot steal a picture's identity. The quote still changes the
prompt's identity: it is part of the prompt.

Picture bytes never reach disk. `live_dump` and the KV sidecar name a picture by hash the way the journal does, and
they fold a `data:` URI or a long base64 run found inside *text* too - a tool result that prints a screenshot used to
write megabytes of base64 into `live/last_request.json`. The prompt forwarded to the engine is not folded: the
picture gets there whole.

Engine state (autosave checkpoints, masking anchors) is keyed by the prompt's hash, so a picture joins that identity
only where the proxy can name its bytes: a `data:` URI is hashed by content and stamped at the marker the template
wrote, so the same text with another picture is a different state and can never be restored under the previous
image's embeddings. A picture referenced from *outside* the prompt - an `http(s)` url or a file path - can hold
different bytes on the next turn under the same name, so a prompt carrying one records no state, restores none and
seals no anchor (`anchor_skip reason=volatile_picture`); its tokens are budgeted the same way. Send `data:` URIs
(OpenCode does) when a conversation should keep its state reuse. The proxy cannot make the *engine's* own prefix
cache safe for a url whose content changed under the same pad count - a second reason to prefer `data:`.
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

The calibration factor is learned from the `usage.prompt_tokens` the engine reports, with guards against a
single misleading sample: samples under 1,000 estimated tokens are ignored, a sample whose real/estimated ratio
falls outside 0.25–4.0 is rejected and journalled (`calibration_rejected`), and until 5 samples have been accepted
the factor may only grow, never shrink — overestimating costs a little window, underestimating masks too late. A
prompt the engine cut is already excluded (0.3.8). The clamp stays 0.5–2.0: chars/3.5 is a deliberately pessimistic
baseline for prose, so a factor below 1.0 is a legitimate correction, and `window_estimate_slack` is the knob for
extra prudence. Exact counting with a tokenizer is never calibrated.

**Context diagnostics.** The CLI prints startup/version/window/template and concise runtime events to **stderr**:
`window_guard` (including every checked call, clamp, count failure, local refusal and engine-count mismatch),
`response_clamped`, `mask`, `switch`, `switch_skipped`, and `client_disconnected`. No prompt/notes/error-body text
is printed by this summary. Capture it with `2> proxy-events.log` (or use your service manager's journal).
The full structured log stays at `<--data>/journal.jsonl`; recent events are also available at
`GET /v1/strata/journal`. The optional [dashboard](tools.md#dashboard-optional-needs-the-git-clone-ui-in-italian) reads the data directory; do not confuse its
port with the proxy's. `where=answer|recall_round|final|notes|seal|anchor|answerfix` identifies the upstream phase;
a client continuation is a new answer request, not a distinct server-side phase. `seal` and `anchor` deliberately
request one token to establish saved state; this differs from a client answer clamped to one token.

**The prompt head is measured, not guessed (`tool_heads`, 0.3.24).** The head — the template scaffolding, the tool
definitions and the system prompt — is the largest single occupant of an interactive prompt, and until 0.3.24 it was
recorded nowhere: the `request` event carries aggregate counters only, and the archive holds messages, not tools.
`journal_tool_heads` (on by default) writes **one** `tool_heads` line **per conversation and per change of the tool
list** — not per request — with the list's fingerprint, the number of tools, for every tool its **name**, the tokens
of the full definition and the tokens of its `description` alone, the counted head total (`head_tokens` =
`defs_tokens` + `system_tokens` + `frame_tokens`), and, when `tools_paging` is on, which tools stayed in the head
(`base`) and which went to the loadable catalog (`catalog`). `counting` says whether the numbers are `exact` (pack
tokenizer + real template) or `estimated` (the chars/`chars_per_token` fallback); the counter is the one the proxy
already uses for the window. **Names and numbers only: no definition text, no `description` text and no system
prompt ever enter the journal** — the record is built from names and integers, so it cannot carry text, and
`tests/test_tool_heads.py` proves it (`toolheads.leaks`, `toolheads.unknown_strings`). One line is about 450 bytes
and its size does not grow with the length of the definitions. This makes `tooldefs_desc_max`, `tooldefs_param_max`
and `tools_paging` measurable after the fact. Note that the pager itself journals nothing: `tools_search` and
`tool_not_loaded` only appear when the **model** calls `tools` or an unloaded tool, so before this event a journal
without them could not tell "paging never ran" from "paging ran and the model never touched the catalog" — this line
tells them apart. `tool_heads` is a measurement, not a decision: the prompt sent to the engine is byte-identical
with it on or off.

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

## Platforms

- **Linux**: the reference. Every tested combination below ran on Linux.
- **macOS**: the proxy is plain Python and should work as on Linux (not yet tested). Build llama.cpp the same way
  (Metal is on by default on Apple Silicon) or install it with Homebrew (`brew install llama.cpp`), then use the same
  `llama-server` command; `sha256sum` is `shasum -a 256`.
- **Windows, native** (since 0.3.2; tested on the `windows-latest` CI runner, not yet on a desktop): Python ≥ 3.10
  from python.org, the official llama.cpp Windows build (`llama-b11430-bin-win-cpu-x64.zip` or a GPU one). In
  PowerShell:

  <!-- readme-test: windows -->
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
  you start the proxy in, so start the engine and the proxy from the same folder), and
  `mnemonic-proxy init-config --engine llama-server --slot-dir .\slots` writes the minimal version of it.
  **One click, any engine** (0.3.24): `mnemonic-proxy init-config --engine strata --launcher` writes
  `start-mnemonic-proxy.bat` next to the config, with your `--upstream`, `--port`, `--data`, `--config` and
  (optional) `--tokenizer` already in it — start Strata yourself, then double-click the `.bat`. It checks that the
  engine answers, runs `check` once (a taken port is reported there), starts the proxy and ends with `pause`.
  [`examples\start-windows.bat`](https://github.com/lastreload/mnemonic-proxy/blob/main/examples/start-windows.bat)
  is the llama-server variant that also *starts the engine*: it runs
  `llama-server.exe` in its own window with `--slot-save-path .\slots`, waits for it, runs `check`, then the proxy on
  port 8096 (edit the four paths at the top: `LLAMA`, `MODEL`, `CONFIG`, `VENV`). What CI proves on Windows: unit
  tests, demo on a cp1252 console, `check --live` READY, chat through the proxy, `start-windows.bat`, the generated
  `start-mnemonic-proxy.bat` against a fake engine, autosave and
  autorestore after an engine restart (3.2K tokens restored in 95 ms; the turn takes 2.3 s instead of 35.7 s),
  `kv_archive` round trip.
  Not tested: real agents (pi, Claude Code, Codex) on Windows, GPU builds, the dashboard. `sha256sum` is
  `Get-FileHash -Algorithm SHA256`.
  **Console**: a PowerShell window whose code page is not UTF-8 cannot carry the characters the proxy prints
  (`⟪ ⟫ · — «»`). Since 0.3.24 the commands detect it and print ASCII stand-ins instead (`<<ctx-archive id=… -
  output of bash …>>`, `--`, `"…"`), so the report stays readable and nothing arrives mangled; accents in paths are
  folded (`Projet par défaut` -> `Projet par defaut`). `chcp 65001` before running the command brings the real
  characters back. This is the console view only: the text sent to the engine, the archive, the journal and
  `last_request.json` are byte for byte the same on every platform.
- **Windows, WSL 2**: follow the Linux commands inside WSL. Keep the engine, the proxy and the `slots` folder all
  inside WSL, so that `slot_dir` and `--slot-save-path` are the same path. Not yet tested.
- **Android (Termux)** (0.3.8; tested in Waydroid 1.6.2 on Linux — Android 13, x86_64 — with Termux 0.118.3 and its
  Python 3.14.8; not yet on an ARM phone). The proxy is plain Python, nothing is compiled:

  <!-- readme-test: real-engine -->
  ```sh
  pkg update -y && pkg upgrade -y
  pkg install -y python git
  pip install git+https://github.com/lastreload/mnemonic-proxy    # 0.3.8 (about 25 s)
  mnemonic-proxy demo                                              # PASS
  ```

  Use case: an agent on the phone in front of an online API (no model on the phone), base mode:

  <!-- readme-test: real-engine -->
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

<!-- readme-test: illustrative -->
```sh
pip uninstall mnemonic-proxy        # inside the venv you installed it in, or delete the venv
rm -rf ./data                       # the archive, the journal: your conversations in plaintext
rm -rf ./slots                      # saved engine state (large files), and kv-archive/ if you used kv_archive
```

`./data` and `./slots` are separate on purpose: deleting the state files keeps the archive (recall still works, only
resuming without re-reading is lost); deleting `./data` loses the archive and the conversations' state becomes
unreachable. Undo the client changes as well (pi `models.json`/`settings.json`, the Claude Code variables, the Codex
provider).
