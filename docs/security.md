# Security & privacy

- **Everything stays local.** The proxy talks only to the `--upstream` you give it; no telemetry, no update checks,
  no other network access. The dashboard and the MCP server bind to `127.0.0.1`. The MCP server is read-only; the
  dashboard reads too, except for its 📌 / 🗑 buttons, which it forwards to the proxy
  ([Tools](tools.md#dashboard-optional-needs-the-git-clone)).
- **The archive is plaintext.** `data/archive.sqlite` holds everything the agent saw — file contents, command
  outputs, reasoning — secrets included. `data/journal.jsonl` holds request metadata and recall queries;
  tool names and token counts as well (`tool_heads`), but never the text of a tool definition or of the system
  prompt. `data/live/last_request.json` (with `live_dump`) the whole last prompt - pictures named by hash, never as base64.
  Saved engine state files (`slot_dir`) and `kv-archive/` hold the conversation's tokens.
- The proxy binds to `127.0.0.1` by default and has no authentication; do not expose it (`--host`) on a shared
  network. The MCP server's HTTP mode also binds to `127.0.0.1`.
- **Requiring a key** (`--client-key-file`) closes that hole: every request must then
  carry the client key as `Authorization: Bearer <key>` (what the OpenAI clients send) or `x-api-key` (what the
  Anthropic ones send), compared in constant time, and anything else gets HTTP 401 on every path, including `/health`
  and the `/v1/strata/*` ones. Both keys are read from files (`--client-key-file` for the client key,
  `--api-key-file` for the engine key), so neither is in the command line or the process list, and the two can be
  different secrets: handing clients the engine key would let them reach the engine directly, bypassing the proxy.
  Outside this machine the proxy starts open and logs a warning; add `--client-key-file` to close it. The dashboard
  reads the same two files (`--api-key-file` for Strata, `--proxy-api-key-file` for the proxy), so its 📌 / 🗑 calls
  keep working against a proxy that requires a key.
- Protect the folders: `chmod 700 ./data /path/to/slots`.
- **Forget one conversation:** stop the proxy, then run:

  <!-- readme-test: ci -->
  ```sh
  mnemonic-proxy forget --data ./data --list
  mnemonic-proxy forget --data ./data --config config.json --conv <id> --dry-run
  mnemonic-proxy forget --data ./data --config config.json --conv <id>
  ```

  `--list` shows ids, logical SQLite row sizes and first/last UTC timestamps. `--dry-run` lists table rows,
  files and bytes without changing stored data. `--slot-dir DIR` and `--kv-archive DIR` override the config;
  otherwise they are resolved by the same loader as the proxy (`--config`, then `MNEMONIC_CONFIG`, then Config
  defaults; relative paths are relative to the working directory). The command prints the paths and their source.
  With a `slot_dir` but no `kv_archive_dir`, the cold archive defaults to its sibling `kv-archive/`, even if cold
  archiving is currently disabled. Supply the directories actually used by the engine, including remote state
  mounted locally: the command does not contact the engine or discover unknown directories.

  It removes conversation rows from every table with a `conv` column, namespaced `kv` entries and the tracker
  state if owned by that conversation; drops the derived structured-recall index (rebuilt on demand for survivors),
  rebuilds archive FTS, enables SQLite `secure_delete`, runs `VACUUM` and truncates the WAL. It also deletes segment,
  anchor and autosave files, full-prompt `.bin.json` sidecars, cold manifests and unreferenced blocks (shared blocks
  survive), filters the journal atomically, clears a matching live dump, and filters router slot ownership metadata.
  The cold archive cleanup needs no compression module, so forgetting works on Python 3.10 too.

  The server and maintenance command hold the same non-blocking, canonical-data-directory lock: on POSIX it locks
  the directory inode without creating a file; on Windows it locks a persistent sibling
  `.mnemonic-proxy-<sha256-of-canonical-data-path>.lock` (control metadata only, never deleted). `forget`, including
  listing/previewing, refuses while a proxy or another maintenance command owns the lock. Older proxy versions do
  not take this lock: stop them first. Stop other writers too (including engine clients and archive CLI commands);
  a read-only MCP/dashboard process can retain already-read content in memory, so close it when that matters.

  Exit status is 0 on success, 1 for an unknown id, busy folder, unreadable/corrupt data or incomplete removal, and
  2 for invalid arguments. A filesystem failure may leave a partial deletion: the report names the errors; fix
  them and retry the same command (orphan state/journal data can be removed even after SQLite rows are gone).
  Backups, snapshots, engine memory and copies outside the selected folders are not erased. `VACUUM`/unlink are not
  a secure-wipe guarantee on SSDs, copy-on-write filesystems or backups. For reference only:
  [manual deletion checklist](manual-forget.md).
