# How it works

Archived content stays verbatim; the model writes handoff notes at segment switches. No vector database is required.

1. **Hide — mask, don't summarize.** Old tool outputs and reasoning leave the prompt in stable blocks and become
   one-line receipts with an id. The prefix stays byte-identical between blocks, so the engine's KV cache keeps
   working. A receipt says what the call did and how it ended, how big the output was (lines and bytes), the exit
   code when there is one, the error type and the **first** error line — everything read off the archived bytes, no
   model involved, so the same output always produces the same receipt:
   `⟪ctx-archive id=t1a2b… · output of bash "pytest -q" · this agent · hidden to save space (3120 tokens)⟫ Receipt: test: exit code 1, 412 lines/38.2 KB, passed 86, failed 1 (tests/test_a.py::test_adds). Error type: AssertionError. First error line: «E AssertionError: assert 3 == 4». Exact text: recall id=t1a2b…`
2. **Recall — structured, exact.** A `recall` tool searches the verbatim archive: first write of a file, its full
   edit timeline, command → output, the reasoning behind an edit — every hit tagged with message number and segment.
   The proxy runs these calls itself; the client never sees them, even when streaming.
3. **Segment — handoff notes.** When even the masked window fills up, the model writes its own notes, the segment is
   sealed and a fresh one starts. Pinned lines (`!!` or `📌`) survive every switch; lines starting with `🗑` (or `~`)
   mark a message as disposable once answered.
   If the model answers the notes request with a tool call and no text, its reasoning and the call it was about to
   make become the notes, marked as rebuilt by the proxy (journal: `handoff_status` clean / recovered / fallback).
   *A copy is labelled a copy.* Several of these blocks — handoff notes, the verbatim first request, the 📌 block,
   an automatic-recall insertion — are themselves `role:"user"` messages, so a client's own control frame carried
   inside them would read as a fresh delivery. Clients mark out-of-band messages with frames the model is told to
   trust (Hermes: `[OUT-OF-BAND USER MESSAGE … delivered once at this position] … [/OUT-OF-BAND USER MESSAGE]`);
   in every copy the proxy writes, such a frame is relabelled `[quoted OUT-OF-BAND USER MESSAGE …]` — same words,
   whole text, visibly a quotation. The message the client actually delivered stays byte for byte as it was, in its
   own place (issue #61).
4. **Save — engine state on disk.** On engines that support it (llama-server, Strata with session files) the KV
   state is saved to disk: resuming after a pause, a restart or another client takes 1–3 s instead of a full
   re-read.
5. **Archive — deduplicated cold storage.** Old state files are rebuilt bit-for-bit from content-addressed zstd
   blocks: −36% disk on our sessions, sha256-verified before every restore.
6. **Lean — tools on demand.** Agents ship tens of thousands of tokens of tool definitions. The proxy keeps the core
   ones and loads the rest by name: 25.6K → 6.2K tokens on every request.

> "In the courier business you don't read the payload. You carry it, and you give it back exactly as it was." — the
> design rule. Hidden content is never paraphrased; it is either in the prompt or retrievable verbatim.
