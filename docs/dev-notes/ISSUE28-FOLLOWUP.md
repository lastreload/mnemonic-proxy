# Issue #28 follow-up: a vocabulary is not a chat template

## Evidence and limits

Base: mnemonic-proxy 0.3.18 (`f846831edd182ce16b02178ef83134d02997b6d5`).
Independent encoder: PolyStrata `bb94827d92575280463f8193beddbe607c29389b`,
`Service` + `MockEngine`, CPU only. No GPU or real generation was used.

Tiel metadata was taken from `peculiar-ragdoll/Tiel-Coder-35B-A3B-GGUF-MTP`,
revision `bbe9e566f39e4fc9652ac66b71968289a03c520a`, file
`Tiel-Coder-35B-A3B-MTP-UD-Q4_K_XL.gguf` (the revision pinned by PolyStrata setup).
Only a bounded 16 MiB prefix was downloaded, not the full model. Its own Sharp
`qwen3.8-froggeric-v22.5.0` template has 30,505 characters and SHA256
`a41d3a6495c7531e85a45b665ae99c0251a68eaef5fada66cfa283827d3f6422`.
PolyStrata's tokenizer extractor writes that template into the pack directory.
The proxy previously ignored it and rendered a different, built-in Qwen prompt.

These are synthetic padded prompts calibrated to the public report's token
counts, NOT the user's captured requests. The original prompts, journal,
request effort, precise proxy/PolyStrata versions and active model settings
were not supplied. The startup `window from engine n_ctx ... thresholds scaled`
line exists in much older history too; it does not establish a release number.
The report says the user updated after 0.3.15, but an exact version cannot be
inferred from the log. Likewise, the logs cannot establish whether a particular
400 originated in an answer, recall, notes, final retry, or client continuation.
All those calls share the upstream guard; the new per-call diagnostics identify
that phase on the next occurrence.

## Independent CPU replay

With one terminal tool and `reasoning_effort=low`, the real Tiel encoder reads
561 tokens for a small prompt; the old renderer counts 275. The difference is
286 without any special-token spelling or restored hidden reasoning. Padding
that request to the reported size gives:

| Window | Old guard | Actual engine prompt | Requested output | Before | After |
|---:|---:|---:|---:|---|---|
| 32768 | 30937 | 31223 | 1767 | identical reported 400 | 1481 output, accepted |
| 65536 | 65408 | 65848 | 289* | prompt-only 400 | local refusal, no send |
| 65536 | 65128 | 66719 | 289* | prompt-only 400 | local refusal, no send |

The 64K synthetic histories contain five/forty failed tool calls respectively.
Sharp adds warnings for failed tools; the old renderer misses those too. The
measured undercounts are 440 and 1591, so a fixed 286-token reserve would not
solve this class of failure. These histories are demonstrations, not a claim
that the user had those failures. `289` is a synthetic ceiling taken from the
preceding log, not a known field of the rejected request. For the prompt-only
400 wording the replay enables `fit_max_tokens` at 64K; that is an explicit
replay assumption, not evidence about the user's final configuration.

For 32K, `30937 + 1767 + 64 = 32768` passes the old local guard. But the real
prompt gives `31223 + 1767 = 32990`, 222 over the window, or 230 over the
engine's usable window with `CTX_SLACK=8`. The correct proxy ceiling, preserving
its own 64-token margin, is `32768 - 31223 - 64 = 1481` (the engine suggests
1537 using its smaller 8-token margin). A missing 8-token margin does not explain
this undercount; `window_estimate_slack` is not used with a loaded tokenizer.

To repeat with an existing extracted Tiel pack and a CPU-only PolyStrata source
checkout at the revision above (dependencies: `mnemonic-proxy[exact]`, `regex`):

```sh
python tools/replay_issue28.py --polystrata /path/to/PolyStrata \
  --tokenizer /path/to/tiel-pack/tokenizer --out ./issue28-replay.json
```

The script calls the independent service's encoder and `prepare`, never `run`.
The ordinary unit suite uses the stdlib HTTP fake engine and labeled count
fixtures. A separate exact-template CI job tests a tiny local HF tokenizer and
an independently authored custom template; it downloads no models.

## What changed

- Discover the adjacent pack `chat_template.jinja` and render it in a sandbox
  for the planning total and every final guard count. Keep Qwen per-message
  allocation for planning heuristics. Without a template file, preserve the
  historical built-in renderer and identify it at startup. A found-but-invalid
  template fails closed. Pack-template counting currently refuses images
  explicitly rather than inventing their dynamic patch count.
- Preserve the request's `preserve_thinking` flag in template kwargs. This was
  ignored before; ignoring `false` can overcount old reasoning, so this separate
  parity correction is not presented as the cause of the reported undercount.
- A tightly recognized pre-generation context rejection supplies an authoritative
  prompt/window count: one bounded retry may lower only output/reasoning limits.
  An already oversized prompt is refused locally without retry. Do not retry
  unrelated errors, network errors, or failures after streaming starts.
- Handle BrokenPipe/ConnectionReset while writing response headers/body; mark
  the connection closed and record a concise event, with no expected traceback.
  This client-disconnect symptom is independent of token counting; it does not
  identify who sent SIGKILL to the engine.
- Startup and runtime summaries go to stderr. The full journal and recent-event
  API remain available; README documents the dashboard and event locations.
  Label notes/seal/anchor/answerfix separately, including nested calls through
  upstream wrappers. The library Journal remains quiet unless explicitly enabled.

## Other hypotheses and boundaries

PolyStrata can restore missing reasoning from its private answer memory when
`preserve_thinking` is enabled. A local vocabulary/template cannot see that
memory; it remains a server-side mismatch possibility, not needed by the Tiel
reproduction. Server `effort_end` and alternate templates/settings also remain
outside an unconditional local exact-count guarantee. Engine rejection feedback
is a safety net for such cases, not a guarantee that the first call never fails.

A reasoning-budget wrap-up is inserted during generation, after `prepare`'s
initial context check. Prior reasoning explicitly sent in the next request is
part of the counted history. This does not explain the independently reproduced
286-token initial-prompt mismatch, and the report lacks the payloads needed to
attribute any further mismatch to the wrap-up.

A current tool result that leaves no useful answer room is a separate defect,
tracked independently (the oversized-tool follow-up). This patch does not claim
to fix that policy or the subsequent off-task continuation. The user's initial
config also enables `seal_experimental`: seal and anchor requests deliberately
use one token, so `max_tokens=1` alone cannot distinguish a clamp from saved-state
maintenance. New phase logs make that distinction visible. SIGKILL (-9) remains
unattributed: no evidence here establishes who killed the user's engine.
