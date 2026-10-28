# Estimated-budget gate: scalar CPU reproduction

This is a diagnosis, not a completed accuracy comparison or a full model replay.
The fixture in `tests/data/budget_gate_0938.json` contains observed counters from
AgentLongBench qid `24_i3_j16_a_0938`, visit 1-B, sequence 50, plus source hashes.
It does not contain the outgoing prompt. The test injects the recorded estimate
into the real `WindowGuard`; it does not pretend to retokenize the prompt.

## Cause

The harness supplied `--tokenizer` to both proxy processes. The 0.3.13 environment
had `tokenizers==0.23.3`; the 0.3.14 environment contained only the base proxy
package. B's startup log explicitly says `No module named 'tokenizers'` and
`tokens=chars/3.5`. A says `tokens=esatto:tokenizer.json`.
The proxy silently fell back despite an explicitly requested tokenizer.
Consequently A/B changed both the proxy version and the counting mode.

The observed final estimate was 27021, the native initial prompt 29505, the
requested output 4096, margin 64, context 32768. The estimate fits; the actual
requested envelope exceeds the window by 897. Counting 29505 would cap output
at 3199. The scalar CPU reproduction produces the same result on PyPI releases
0.3.13, 0.3.14, 0.3.15 and 0.3.16 without invoking any batch allocation code.
This establishes a general estimated-mode limitation, not a causal regression
in `_batch_result_cap` or `_free_search`. It does not establish that full
trajectories would be identical across these releases.

0.3.15's literal-special-token reserve applies to loaded-tokenizer counts, not
the failed-tokenizer path. 0.3.16 changes closed-search answer retries, not this
estimate. Neither release fixes this accidental fallback.

## Native counters and HTTP 200

The recorded native engine segments are:

| Segment | Prompt | Cached | Read | Generated |
| --- | ---: | ---: | ---: | ---: |
| initial | 29505 | 0 | 29505 | 3072 |
| after reasoning wrap-up | 32595 | 32576 | 19 | 8 |

The server's initial encoded `ids` length supplies OpenAI `usage.prompt_tokens`.
The final segment's `reused` supplies `cache_n`. The latter includes the already
generated reasoning: it is not a second measurement of the initial prompt.
The injected wrap-up accounts for 18 tokens; 3072 + 18 + 8 = 3098 completion
tokens, and 29505 + 3098 = 32603. Thus the two reported counters can differ in
scope without invalidating the initial prompt count. The final `prompt_n=0`
comes from `max(0, initial_prompt - final_segment_cache)`; it does not mean the
original prompt cost zero prefill tokens. Native logs report the real reads.

The server was configured with `fit_max_tokens=true`: it clamps to the remaining
context with its own 8-token slack (3255 available at the first segment). HTTP
200 therefore does not prove the proxy's requested 4096-token budget was valid.
No physical context overflow is demonstrated.

## Change and limits

Startup now refuses an explicitly supplied tokenizer that did not load, before
upstream setup or listener creation. The error explains the dependency/path
repair and that omitting the option intentionally keeps estimated mode.
No automatic dependency install, default slack increase or estimator rewrite is
included. Intentional tokenizer-free use remains estimated, including generic
OpenAI-compatible servers: a fixed percentage is not an exact guarantee on
arbitrary text. Match the actual server tokenizer/template or use a supported
engine-side tokenizer for strict budgets.

CPU tests:

    python -m unittest tests.test_tokenizer_startup tests.test_budget_gate_fixture -v
    python -m tests.test_budget_gate_fixture

The scalar fixture and checks are independent of GPU/model availability.
