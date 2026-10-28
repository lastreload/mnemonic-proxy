# Hermes Agent

Hermes can use Mnemonic Proxy through its custom OpenAI-compatible provider.
Use Chat Completions, point Hermes at the proxy rather than the engine, and turn
off Hermes' own automatic context compression. No Hermes plugin is required.

## Configure an isolated profile

Use a new profile: do not experiment in your normal assistant's profile.
These commands apply only to `mnemonic-test`; they do not change the sticky
active profile or copy any existing credentials, memories, or skills.

```sh
hermes profile create mnemonic-test --no-alias --no-skills
hermes -p mnemonic-test config set model.provider custom
hermes -p mnemonic-test config set model.base_url http://127.0.0.1:8096/v1
hermes -p mnemonic-test config set model.default YOUR_ENGINE_MODEL_ID
hermes -p mnemonic-test config set model.api_mode chat_completions
hermes -p mnemonic-test config set model.context_length 131072
hermes -p mnemonic-test config set compression.enabled false
hermes -p mnemonic-test config get compression.enabled
```

Get `YOUR_ENGINE_MODEL_ID` from `curl http://127.0.0.1:8096/v1/models`.
Do not use the engine's URL as `model.base_url`. The `/v1` suffix is required.
If your local server does not require authentication, a non-secret dummy key
can be passed to Hermes when it requires a key; never reuse a cloud provider's
key for an unrelated local endpoint.

Start the proxy as in the README, with a separate data directory if another
proxy is already running. For exact counts, install the optional tokenizer
support and pass the model's actual tokenizer file:

```sh
pip install 'mnemonic-proxy[exact]==0.3.17'
mnemonic-proxy --upstream http://127.0.0.1:8095 \
  --host 127.0.0.1 --port 8096 --data ./mnemonic-hermes-data \
  --tokenizer /path/to/model/tokenizer.json
```

The startup log should contain `tokens=esatto` rather than an estimated counter.
The default proxy window is 131072; set `window` and appropriately scaled
thresholds in a JSON config if your engine has a smaller window. The proxy's
window must never be larger than the engine's actual context capacity.

Then run Hermes:

```sh
hermes -p mnemonic-test chat
```

If the engine runs on another host, bind the proxy there to loopback and forward
it over SSH, for example `ssh -N -L 18196:127.0.0.1:8196 HOST`. In that example,
Hermes uses `http://127.0.0.1:18196/v1`, while the remote proxy listens on 8196.

## Why disable Hermes compression?

`compression.enabled=false` is the Hermes configuration key, not
`compaction.enabled` (used by some other agents). It makes Hermes keep sending
the original history, so the proxy owns masking and archive recall. Otherwise
Hermes may summarize history before the proxy can archive or retrieve it.

Keep Hermes' declared model context large enough for the unmodified history
you intend to send; it is not the same thing as the proxy's reduced physical
window. Hermes versions with a minimum-context check can reject a custom
provider declared below 64K. Do not claim that your underlying engine has a
larger context than it really does: a smaller proxy window is the intentional
virtualization boundary, not an engine capacity upgrade.

## Tools, streaming and recall

Hermes sends its ordinary function definitions (`terminal`, `read_file`,
`write_file`, etc.) to the proxy. The proxy adds its own `recall` definition on
the engine-facing request and executes those calls internally. Do not register
a second Hermes tool with that name. A successful proxy recall should not show
up as an unknown tool in Hermes; a final response containing literal
`<tool_call>` markup is not a successful final answer.

Inspect `GET /v1/strata/journal` or the proxy's `journal.jsonl`. For each
`request`, compare `reused` with `prompt_tokens`; check masking and recall
events as well as the final answer. HTTP 200 alone is not an acceptance test.
A masking batch deliberately changes old prompt content and can reduce cache
reuse; distinguish it from a system prompt changing on every turn.

## Measured integration (2026-10-28)

One local integration run used Hermes Agent 0.22.2 (local build `d923a5eb487a`),
Mnemonic Proxy 0.3.17 with the exact tokenizer, and Qwen3.8-Flash-Next IQ3_XXS
served by Strata 0.2.6 / engine 0.1.50. Hermes ran on a separate Linux host
through an SSH tunnel. This is not a claim about every Hermes or model version.

- 18 real user turns in one `AIAgent.run_conversation` session: file reads,
  terminal commands, writing a CSV auditor and its tests, and executing them.
  All 18 had final answers; all 24 client tool results succeeded.
- 38 Hermes Chat Completions requests: 17 streaming and 21 non-streaming;
  the proxy journal recorded 39 engine requests including one internal recall.
- The proxy window was intentionally 32768 while the engine served 131072.
  Unmodified virtual history reached 51117 tokens; the largest journaled
  physical prompt was 24271. Two masking batches saved 11710 and 12308 tokens,
  and two proxy-managed segment switches occurred.
- One proxy-owned `recall` recovered the exact original reference of an older
  CSV record. The final answer matched the source file verbatim; Hermes did
  not see an unknown tool or literal `<tool_call>` as its final answer.
- The serialized Hermes system message had the same SHA-256 in every captured
  client request. Across the first ten turns, `reused/prompt_tokens` for each
  turn's first request ranged from 99.14% to 99.52% (99.39% weighted).
  This run started with a warm prefix from a preparatory probe; the earlier
  cold request had `reused=0`. These figures measure prefix stability, not
  cold-start latency or a guarantee of reuse across masking batches.
- The generated auditor passed four `unittest` cases and its six batch totals
  matched an independent CSV calculation. A separate invocation through the
  real `hermes -p ... chat -Q --oneshot` CLI also read the specification,
  executed the tests and printed a final answer (exit 0).
- No API, proxy, tool-execution or final-answer errors in this accepted run.
  Preliminary oversized-output and headless-approval probes are excluded and
  their limitations are described below, not hidden as successes.

For the reduced-window test the proxy config was:

```json
{
  "window": 32768,
  "mask_trigger": 22000,
  "mask_target": 13000,
  "keep_recent_tokens": 5000,
  "min_batch_tokens": 4000,
  "reserve": 4096,
  "default_response": 8192,
  "tail_max": 8000,
  "notes_max_tokens": 4096,
  "mask_reasoning": true,
  "mask_tool_args": true,
  "live_dump": true,
  "engine": "strata"
}
```

This is a deliberately small test window, not a production tuning recommendation.
No Hermes or proxy source changes, no plugin, and no explicit thinking-budget
override were needed. The test used only the file and terminal toolsets, with
memory, background review and project-context loading disabled for isolation.
An enabled-versus-disabled Hermes compression A/B was not run; the accepted
setup used `compression.enabled=false`, verified in config and on the live
agent instance.

## Operational limits

Bound individual tool outputs as well as the total context. In a 32768-token
proxy window, a single approximately 24K-token `read_file` result plus Hermes'
system prompt and the proxy's response reserve left no useful output room:
the proxy clamped one response to one token and Hermes requested a continuation
that answered an earlier task. That probe was stopped, not counted as a pass.
Read large files in chunks (roughly 6K tokens per result worked in the test).
Masking old results cannot make an oversized current result free.

Hermes' approvals still apply. In headless `-q` / Python-harness execution,
commands such as `python -c` can be rejected by policy even when the proxy and
tool parser are working. Prefer ordinary script files and keep interactive
approval enabled for normal use. An isolated, trusted test fixture can opt into
`hermes -p mnemonic-test config set approvals.single_query_mode approve`;
this allows flagged commands in that disposable profile's single-query mode,
so it is not a general recommendation and must never be applied casually to
your normal profile.

## Configuration references

- [Hermes configuration](https://hermes-agent.nousresearch.com/docs/user-guide/configuration)
- [Hermes profiles](https://hermes-agent.nousresearch.com/docs/user-guide/profiles)
- [Hermes model configuration](https://hermes-agent.nousresearch.com/docs/user-guide/configuring-models)
- Hermes implementation: `agent/agent_init.py` applies the configured
  compression settings to `agent.compression_enabled`; `agent/turn_preflight.py`
  gates automatic compression on that value.

## Cleanup

Stop your test proxy and SSH tunnel, and remove only the disposable profile:

```sh
hermes profile delete mnemonic-test
```

The proxy data directory contains the verbatim conversation archive. Keep or
remove it deliberately; deleting the Hermes profile does not delete it.
