# Development

## Tests

<!-- readme-test: illustrative -->
```sh
python3 -m unittest discover -s tests -t .
```

Uses a fake OpenAI-compatible engine (`ctxproxy/fake_engine.py`, shipped with the package and runnable stand-alone
with `python3 -m ctxproxy.fake_engine --port 18095`; `tests/fake_engine.py` is a one-line import shim kept for
older command lines). Optional groups are skipped unless their inputs are present: exact
counting (a tokenizer in `./tok` or `CTX_TOKDIR`, plus `tokenizers`), Strata mock integration (`STRATA_DIR`, plus
`jinja2`), and `kv_archive` (Python ≥ 3.14). `tools/replay_dry.py` and `tools/replay_exact.py` replay a pi session
through the context manager offline. `tools/state_e2e.py --server PATH/llama-server --model M.gguf --work DIR`
checks saved state end to end with a real llama-server on any OS (restart, restore from disk vs re-read, and
`kv_archive` with `--kv-archive`). CI runs the unit tests on Linux and on native Windows.

The two Strata tools have their own tests: `tests/test_kv_archive.py` and
`tests/test_check_kv_archive.py` build a real archive with the same in-memory helper (no engine
needed) and check that the audit detects a corrupt block, exits non-zero, and touches nothing;
`tests/test_measure_restore.py` covers the pure logic (journal filtering, the speed-up from
`prompt_ms` / `restore_ms`, the CLI guards). The `kv_archive` group needs Python ≥ 3.14.

### This README is tested

The document you are reading is under test, on three levels, because a command
that no longer exists is a worse bug than a missing feature: `mnemonic-proxy check` was once
written here before the subcommand existed.

Since 0.3.25 "the README" means the whole documentation: the short landing page `README.md`
plus every page in `docs/` (`docs/dev-notes/` excluded, because those are raw experiment logs,
partly in Italian). `tools/readme_blocks.doc_files()` is the one definition of that set, and the
three levels read it, so a block that moved from the README into `docs/install.md` is tested
exactly as it was before, with its page named in any failure. Two consequences of the split are
checked as well: every page must be reachable by following links from the landing page, and every
link must resolve across pages — `docs/agents.md#codex-openai-responses` from the README, or
`install.md#t1--a-real-model-on-cpu` from inside `docs/`. `python3 tools/readme_blocks.py --links`
runs that check alone, over every page.

Every fenced block carries an invisible label (an HTML comment above the fence) that says how
it is tested: `ci` (executed for real), `windows`, `real-engine` (needs a model, an engine or a
third-party program) or `illustrative` (nothing to run). `python3 tools/readme_blocks.py` lists
them.

| level | what it checks | where |
|---|---|---|
| 1, static | every block is labeled; every documented `mnemonic-proxy` / `mnemonic-mcp` command line parses with the parser the program really builds; every configuration example uses only real `Config` fields; every `#anchor` link resolves | `tests/test_readme_static.py`, part of the normal suite |
| 2, executed | the `ci` blocks are run against the fake engine: `demo` prints PASS, `check` says READY, `check` without an engine says NOT READY and *start the engine first*, the proxy answers a Chat Completions request, `forget --list` works, `mnemonic-mcp` lists its tools | `tests/test_readme_run.py`, run by `tools/readme_run.sh` and by the `readme-run` CI job, from the built wheel in a clean venv |
| 3, exhaustive | no subcommand, option or `Config` field exists in the code without being documented here or declared internal in `tools/readme_internal.txt`, with a reason | `tests/test_readme_coverage.py`, part of the normal suite |

Levels 1 and 3 need nothing but the standard library and take under a second.
`tools/readme_run.sh` does level 2 the way a new user meets the package: it builds the wheel,
installs it in a fresh virtual environment and runs the README's own commands from a folder
with no source tree in it, so `import ctxproxy` can only resolve to the installed package
(`tools/readme_run.sh --source` skips the build and uses the working tree).
It stages `README.md` and `docs/*.md` together, as the `readme-run` CI job does.

`python3 tools/docs_coverage_check.py` is the proof that the 0.3.25 split lost nothing: it reads
the 2,020-line README as it was before (`git show ea640b7:README.md`) and checks every non-empty
line of it against the current pages, ignoring heading level, link targets and whitespace. Lines
1 to 66 — the old opening the new landing page replaces on purpose — are reported separately.

Partly in Italian: code comments, the dashboard UI and the developer notes in `docs/dev-notes/`. Since 0.3.1 every
text the proxy writes into the model's prompt — receipts and placeholders of hidden content, the handoff-note
request, the segment and pinned-points blocks, automatic recall, recall results, guard and limit messages — is in
English for new conversations (`prompt_language`, default `"en"`). Conversations that already existed before 0.3.1
keep the Italian texts they were born with: those strings are part of their stable prompt prefix (archive, session
files, KV cache), so changing them would invalidate saved sessions. The language is stored per conversation at its
first request; the proxy recognizes its own markers in both languages in every conversation.
