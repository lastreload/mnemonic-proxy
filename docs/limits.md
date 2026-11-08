# What we have not proven

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
