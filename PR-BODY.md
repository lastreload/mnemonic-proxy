# Window guard — mnemonic-proxy 0.3.3

## Problem

On a small window the proxy could send the engine a request where `prompt + max_tokens > window`. The engine
(Strata: "requests are never truncated") rejects it, and the client gets an error or an empty answer. This was found
on AgentLongBench at 16K and 32K (`13_i3_b`, `9_i2_b`, `35_i9_b`). The cause was the recall loop. Every round adds
the call, the model's reasoning and the result to the prompt. The budget check in 0.3.2 trimmed the *result* but
not what came before it, and the next round's `max_tokens` stayed the client's.

## What changes

`ctxproxy/window.py` adds one check (`WindowGuard.enforce`) that runs in `Upstream.chat` / `Upstream.chat_stream`.
Every request the proxy sends goes through it: the answer, recall rounds, handoff notes, answerfix retries, both
streaming and not. Three invariants hold:

- **INV-1**: `prompt + max_tokens + window_margin ≤ window`. When it does not fit, `max_tokens` is lowered, never
  below the answer floor. When it still does not fit, the request is **not sent**: the client gets HTTP 400 (a
  `WindowExceeded` `UpstreamError`), and a `window_guard`/`refused` event goes to the journal.
- **INV-2**: a thinking budget (`reasoning_budget_tokens`) always leaves `answer_min_tokens` for the answer.
- **INV-3**: a new search round starts only if the worst case fits. The worst case is a call, a result of at least
  `RESULT_MIN`, and the answer floor. Otherwise the round is **final**. In a final round the proxy's own tools are
  not offered, and when space is the reason, the model is told there is no room for more searches.

To make room, the recall loop (`Proxy._plan_round`) removes only what the loop itself added. Removal goes by value
per token (`recall_removal: value`; `reasoning` and `age` remain available). Signals used, all known before the
decision:

- piece kind
- tokens actually freed
- age
- duplication
- identifiers of the current request found in the piece

`recall_use.overlap` is not a signal. A result becomes a one-line receipt that can be recalled by id. Old reasoning
becomes its last conclusion line, extracted and never generated. Removing everything gives back the prepared
prompt, which the prepare step already fitted with the floor. So the final call always fits: the loop terminates.

The handoff-notes request keeps its source part in the window too. It is shortened from the end, by whole groups,
so it keeps the same prefix.

With `--tokenizer` the guard uses the engine's own count (template + pack tokenizer), so the guarantee is exact
(`guarantee=exact`). Without it the guard uses the calibrated chars/3.5 estimate, and journal events say
`guarantee=estimated`; the README documents this. `"window_guard": false` restores 0.3.2.

Also included (measurement only): a `mask_reuse` event. It compares the prefix the proxy expected the engine to
reuse after a mask with what the engine actually reused, which helps the checkpoint work on hybrid engines.

## Tests (CPU only)

- Full suite green on Python 3.10 and 3.14: 309 and 335 tests (3.14 also runs the exact-tokenizer tests).
- New `tests/test_window_guard.py` uses a **strict fake engine**. It answers HTTP 400 to every request over the
  window and always generates the maximum (reasoning up to the budget). It covers:
  - windows 8K–32K and all three removal policies;
  - long loops where results are never emptied;
  - the final round, both for space and for the round limit;
  - INV-2;
  - enforce/clamp/refuse;
  - removal purity;
  - notes;
  - a guard-off control (it does go over).
- **Sweep** (`tests/sweep_window_guard.py`): 2,000 random scenarios with fixed seeds, exact pack tokenizer.
  - Windows 8K/16K/24K/32K/128K; random histories and recall results; several calls per round; streaming and
    non-streaming; answerfix on/off; selector on/off; thinking budgets.
  - Result: 27,411 requests, **0 over the window**, 0 exceptions, 0 answers below the floor.
  - Same scenarios with the guard off: 1,021 over the window out of 3,867.
- **Replay of the failed ALB cases** on the strict fake engine (worst case: each recall replayed with the reasoning
  length of the real run or the full budget): 0.3.2 ends in an engine error on all 4 runs; the guard answers all 4
  with 0 overflows.
- **Identity**:
  - At 128K the requests are byte-identical with and without the guard.
  - The 0.3.2 golden (`test_default_identity`) and the 0.3.0 language golden are unchanged with the guard off.
  - With the guard on, exactly one request of the language scenario differs: the handoff-notes request at the 40K
    segment step, which was over the window (40,092 > 40,000 − 64) and is now shortened from the end.

Signed: Maurizio Verde — LastReload
