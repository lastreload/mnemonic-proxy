# The recall of the current turn: measured impact of the `idx < max_idx` filter (0.3.23 → 0.3.24)

Read-only re-reading of our own journals, with their archives, done when the fix was written. The script is
`tools/measure_recall_filter.py`; it opens every archive with `mode=ro` and writes nothing.

## What was measured, and how

`server.py` passed `max_idx = last_user` (the index of the last human message of the request) to every recall, and
the archive filter was `idx < max_idx`. So a block was refused exactly when its index was `>= last_user`.

For each `recall` event in a journal the script resolves:

- the index of the requested id, from the archive (`archive.rid → idx`);
- the index of the human message recorded in that event. The `recall` event stores `last_user` as **text**
  (truncated to 300 characters), so the text is matched back to the archived `user` message and its index is read.
  This matters: the archive keeps growing, so the *last* human message of the conversation is not the `max_idx`
  that applied to a call made earlier. Using the conversation's final human index gives 0 blocked calls; using the
  per-call one gives 32.

`is_human_user` is reimplemented verbatim (a `user` message whose text is wrapped in `<tool_response>…` is a tool
result, not a human turn), so the reconstruction uses the same definition as the proxy.

## Sources

136 journals: the 0.3.23 production journal (PC4070TI, `/mnt/mneme-nvme/mnemonic-proxy-039/data`, copied with
`scp`), 31 syntaxis measurement sessions and the 104 journals of the AgentLongBench bench run
(`main-run-0319`). `receipts-ab/raw/` holds no proxy journal (320 `.jsonl` files, all driver-side `calls.jsonl` /
`engine_calls.jsonl`), so it is not measurable.

## Result

| | all 136 sources | production 0.3.23 | AgentLongBench (104) |
|---|---|---|---|
| `recall` calls | 490 | 48 | 277 |
| by id | 205 | 32 | 140 |
| **by id, for a block past `last_user`** (a plain `recall(id=…)` refuses it) | **32** | **32** | **0** |
| by id, unknown id (invented by the model) | 1 | 0 | 0 |
| by `query` | 256 | 12 | 116 |
| by `query`, empty *only* because the matches were past `last_user` | 1 | 0 | 0 |
| by `path` | 9 | 4 | 1 |
| **by `path`, empty *only* because the operations were past `last_user`** | **4** | **4** | **0** |
| `mask` events | 1 440 | 51 | 1 252 |
| of which `emergency=current_tool` | 122 | 10 | 112 |
| masked blocks | 7 459 | 643 | 3 103 |

The contrast is the most informative number here. The AgentLongBench driver sends **every round as a new user
message**, so `last_user` always sits at the end and masked blocks stay below the cut: 140 id calls, 0 refused —
even though that run has 112 `emergency=current_tool` masks whose receipts were unreachable by construction and
simply never asked for. The defect belongs to the **single-prompt** shape, which is why it surfaced in production
and in the Windows user's report and not on the bench.

Every one of the 32 id calls past `last_user` comes from the single opencode session the README quotes
(`cf88233028d5d`, 1 h 32 min, 554 requests). The three turns involved had `last_user` at index 1, 503 and 592; the
requested blocks sat at indices 4 to 690 — always inside the turn that was running.

## What those 32 calls actually got

All 32 used `mode=output`, which goes through `Recall2.around`, and in 0.3.23 `around` applied **no chain filter
at all**. So in this session the filter refused none of them; the defect shows up as what a *plain*
`recall(id=…)` would have answered. Of those 32 calls:

- **21 asked for an output id (`r…`) and got no text.** For a `tool` row `mode=output` returns, by design, only
  the pointer *"output id=… comes from call id=…"*
  (134-136 characters). A plain `recall(id=r…)` would have gone through `_recall_page` and answered
  `recall: id … not found`.
- **11 asked for the arguments id (`a…`) and got text** — 700-character excerpts of the linked outputs, which is
  what `mode=output` returns for an arguments id.

So the model's visible strategy — ask for the output id, get a pointer, follow it to the arguments id — was the
`mode=output` design at work, not the filter. The text it never reached verbatim in those 21 calls totals
**195 636 characters** of archived tool output; a plain `recall(id=…)` would have paged it, had the filter let it
through. The filter did empty the 4 `path` calls.

`emergency=current_tool` is the worst case and needs no reconstruction: that mask archives the **last** message of
the request, so its index is always `>= last_user`. All 10 occurrences in production handed the model a receipt id
that plain `recall(id=…)` could not resolve for the rest of the turn.

## What the journals do NOT show

- No `recall: id … not found` string appears in these journals: in this session the model always used
  `mode=output`, which bypassed the filter. The no-text answers are pointers, not refusals.
- The journal records no `max_idx`, so the reconstruction depends on matching the `last_user` text. 0 events failed
  to match in these 32 sources.
- `recall_use` marks 35 of the 44 calls of that session as "used": it compares words of the model's next answer
  with the recall result, so a pointer answer can count as used. That counter is why the README said "all 44
  served and used"; the README now reports 19 calls with archived text.

## After the fix

The filter becomes `(idx < ? OR rid IN cur_chain)` (`core.chain_ok`), `cur_chain` being the archive ids of the
prefix sent in the current request minus the current question. Re-running the same script against the same
journals is meaningless — the calls are already recorded — so the fix is held by
`tests/test_recall_current_turn.py`: 12 tests on a single-prompt agent session, covering ids, the emergency mask,
`query`, `path`, the structured engine, output/arguments symmetry, the exclusion of the current question, the
exclusion of an abandoned sibling branch on both paths, and one end-to-end request through `Proxy.chat` where the
model calls recall in the middle of its own turn. 10 of the 12 fail on `main`.

## Adjacent defects found and NOT fixed here

Listed for separate cards; none of them is the filter this change is about.

- `Recall2._row` and the leaf selector fall back to `store.get(rid)` **without the conversation**, so an id that
  exists in another conversation of the same archive can resolve. The chain filter does not close this: a foreign
  row with `idx < max_idx` still passes.
- The index cut never isolated sibling branches *below* `max_idx` either: an abandoned branch whose blocks sit at
  low indices was already reachable before this change, and still is. Only the newly admitted region
  (`idx >= max_idx`) is chain-checked.
- `mode=output` on an output id answers with a pointer to the arguments id instead of the text, and the arguments
  path then shortens every linked output to 700 characters (`_short(..., 700)`). Reachability is now correct, but
  the model still cannot get the verbatim text through `mode=output`; plain `recall(id=…)` is the path that pages
  the full text.
- `tests/test_current_tool_emergency.py` calls `mgr.recall` without `max_idx`, so it never exercised the real
  server call. The new file covers that case; the old test was left as it is.
