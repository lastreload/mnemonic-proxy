# Agents

## Connect your agent

Point the agent at the proxy (port 8096) instead of the engine and turn off the agent's own auto-compaction,
otherwise two context managers fight over the same history:

| agent | point it at | turn off | setup |
|---|---|---|---|
| pi, omp | `http://127.0.0.1:8096/v1` (`openai-completions`) | `"compaction": {"enabled": false}` | [T1 step 6](install.md#t1--a-real-model-on-cpu), [omp](#pi-hermes-continue--chat-completions) |
| opencode | `http://127.0.0.1:8096/v1` (`openai-compatible` provider) | `"compaction": {"auto": false}` | [opencode](#opencode-chat-completions) |
| Hermes Agent | `http://127.0.0.1:8096/v1` (custom provider) | `compression.enabled: false` | [Hermes setup and test notes](hermes-agent.md) |
| Continue, other Chat Completions clients | `http://127.0.0.1:8096/v1` | the client's own context compaction | [Chat Completions](#pi-hermes-continue--chat-completions) |
| Claude Code | `ANTHROPIC_BASE_URL=http://127.0.0.1:8096` (no `/v1`) | `DISABLE_AUTO_COMPACT=1` | [Claude Code](#claude-code-anthropic-messages) |
| Codex | `base_url = "http://127.0.0.1:8096/v1"`, `wire_api = "responses"` | no switch: `model_auto_compact_token_limit = 100000000` | [Codex](#codex-openai-responses) |
| any MCP client (Claude Code, Codex, Hermes) | `mnemonic-mcp --db ./data/archive.sqlite` (read-only search of the archive) | — | [MCP](#mcp-recall-server-claude-code-codex-hermes) |

## pi, Hermes, Continue, … (Chat Completions)

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

## opencode (Chat Completions)

Tested with opencode **1.19.5** on Strata: one 1 h 32 min session, 554 requests, 51 masks, 44 `recall` calls of
which 19 returned archived text, numbers [below](engines.md#opencode-on-strata-measured). Put this in
`$XDG_CONFIG_HOME/opencode/opencode.json`
(global; `$XDG_CONFIG_HOME` defaults to `~/.config`, so that is usually `~/.config/opencode/opencode.json`)
or `opencode.json` in the project:

<!-- readme-test: real-engine; config: client -->
```json
{
  "$schema": "https://opencode.ai/config.json",
  "compaction": { "auto": false },
  "autoupdate": false,
  "provider": {
    "mnemonic": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "mnemonic-proxy",
      "options": {
        "baseURL": "http://127.0.0.1:8096/v1",
        "apiKey": "local",
        "timeout": false,
        "headerTimeout": false,
        "chunkTimeout": false
      },
      "models": {
        "qwen3.8-flash-next-iq3_xxs": {
          "name": "Flash Next via mnemonic-proxy",
          "tool_call": true,
          "reasoning": true,
          "limit": { "context": 131072, "output": 16384 },
          "cost": { "input": 0, "output": 0, "cache_read": 0, "cache_write": 0 }
        }
      }
    }
  },
  "model": "mnemonic/qwen3.8-flash-next-iq3_xxs"
}
```

The model key is the id your engine reports at `/v1/models`; `limit.context` is the engine's real window. Then
`opencode` (TUI) or, non-interactively, `opencode run --model mnemonic/qwen3.8-flash-next-iq3_xxs --auto "…"` and
`opencode run -c --model … --auto "…"` to continue the same session.

**Auto-compaction off**: `"compaction": {"auto": false}` is the real option (`Enable automatic compaction when
context is full`, default `true`, in opencode's own `config.json` schema). With it off, a 537-message session
produced no summary message and no compaction event in opencode's database. Note that opencode counts tokens from
the `usage` the proxy returns — the **physical** prompt (max 108K here), not the real history (1.17 M tokens at the
end) — so it would rarely trigger compaction anyway; turn it off regardless, so only one context manager is at work.

Limits found:

- Use the **`provider`** key with **`npm`**, not the newer plural `providers` + `package`: with `providers` the
  model does not appear in `opencode models` and `opencode run` fails with
  `UnknownError … Unexpected server error`.
- If npm blocks install scripts, `opencode-ai`'s `postinstall` does not fetch the binary: run
  `cd node_modules/opencode-ai && node postinstall.mjs` once.
- `opencode run` needs `--auto`, otherwise it waits for permission approvals that never come.
- Tool calls go through unchanged (669 calls: `read`, `bash`, `edit`, `write`, `grep`; no empty results, no HTTP
  errors). The only failures were 5 `edit` calls rejected **by opencode** with `Could not find oldString in the
  file`: the model rebuilt the old text from memory after its own earlier tool arguments had been masked. It
  recovered with `write`.
- `recall` never appears in opencode's tool list: the proxy injects it and answers it in its own internal rounds.
- The model re-reads a lot (415 `reread` events, often `read … limit=60`); in one turn that became a ~20 minute
  read loop before it wrote the file. That is the model, not the proxy.

## Claude Code (Anthropic Messages)

Checked against Claude Code 2.1.287; a 1 h 41 min session with 2.1.284 on Strata is measured
[below](benchmarks.md#claude-code-and-codex-live-on-strata). Claude Code may print `unrecognized_model local-model`: it works
anyway.

<!-- readme-test: real-engine -->
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

## Codex (OpenAI Responses)

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

## MCP recall server (Claude Code, Codex, Hermes)

The archive can also be searched from any MCP client, read-only (sqlite `mode=ro`; the proxy can keep writing). Three
tools: `recall` (same search as the proxy's tool), `conversations`, `journal`.

<!-- readme-test: ci -->
```sh
mnemonic-mcp --db ./data/archive.sqlite                 # stdio
mnemonic-mcp --db ./data/archive.sqlite --http 8133     # streamable HTTP on 127.0.0.1:8133/mcp
```

<!-- readme-test: real-engine -->
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
