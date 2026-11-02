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

32 journals: the 0.3.23 production journal (PC4070TI, `/mnt/mneme-nvme/mnemonic-proxy-039/data`) and 31 syntaxis
measurement sessions. 22 of them contain `recall` or `mask` events.

## Result

| | all 32 sources | production 0.3.23 |
|---|---|---|
| `recall` calls | 213 | 48 |
| by id | 65 | 32 |
| **by id, refused by the filter** (`idx >= last_user`) | **32** | **32** |
| by id, unknown id (invented by the model) | 1 | 0 |
| by `query` | 140 | 12 |
| by `query`, empty *only* because the matches were past `last_user` | 1 | 0 |
| by `path` | 8 | 4 |
| **by `path`, empty *only* because the operations were past `last_user`** | **4** | **4** |
| `mask` events | 188 | 51 |
| of which `emergency=current_tool` | 10 | 10 |
| masked blocks | 4 356 | 643 |

Every one of the 32 refused id calls comes from the single opencode session the README quotes
(`cf88233028d5d`, 1 h 32 min, 554 requests). The three turns involved had `last_user` at index 1, 503 and 592; the
requested blocks sat at indices 4 to 690 — always inside the turn that was running.

## The asymmetry the model felt

Of those 32 calls:

- **21 asked for an output id (`r…`) and got no text.** They all carried `mode=output`, which goes through
  `Recall2.around`; for a `tool` row that path returns only the pointer *"output id=… comes from call id=…"*
  (134-136 characters). A plain `recall(id=r…)` would have gone through `_recall_page` and answered
  `recall: id … not found`.
- **11 asked for the arguments id (`a…`) and got text** — 700-character excerpts of the linked outputs. They
  worked for a reason that had nothing to do with being arguments: `around` applied **no chain filter at all**, so
  any id in the archive resolved there.

So the model's visible strategy — ask for the output id, be refused, ask for the arguments id of the same message —
was it groping around a filter applied on one path and not the other. The text it never reached in those 21 calls
totals **195 636 characters** of archived tool output.

`emergency=current_tool` is the worst case and needs no reconstruction: that mask archives the **last** message of
the request, so its index is always `>= last_user`. All 10 occurrences in production handed the model a receipt id
that plain `recall(id=…)` could not resolve for the rest of the turn.

## What the journals do NOT show

- No `recall: id … not found` string appears in these journals: in this session the model always used
  `mode=output`, which answers with the pointer instead. The refusal is visible as *no text*, not as the message.
- The journal records no `max_idx`, so the reconstruction depends on matching the `last_user` text. 0 events failed
  to match in these 32 sources.
- `recall_use` marks 35 of the 44 calls of that session as "used": it compares words of the model's next answer
  with the recall result, so a pointer answer can count as used. That counter is why the README said "all 44
  served and used"; the README now reports 19 calls with archived text.

## After the fix

The filter becomes `(idx < ? OR rid IN cur_chain)` (`core.chain_ok`), `cur_chain` being the archive ids of the
prefix sent in the current request minus the current question. Re-running the same script against the same
journals is meaningless — the calls are already recorded — so the fix is held by
`tests/test_recall_current_turn.py`: 10 tests on a single-prompt agent session, covering ids, the emergency mask,
`query`, `path`, the structured engine, output/arguments symmetry, the exclusion of the current question and the
exclusion of an abandoned sibling branch.
