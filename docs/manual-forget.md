# Manual conversation deletion (reference)

Prefer `mnemonic-proxy forget --data ./data --config config.json --conv <id>`: it discovers tables from the
schema and reports errors. This checklist is for older versions or recovery, not an additional step after the
command. Backups, filesystem snapshots, remote engine memory and separately copied files are outside its scope.

1. Stop the proxy and other writers; close read-only MCP/dashboard processes if their in-memory copies matter.
   Identify the id with the MCP `conversations` tool or
   `sqlite3 data/archive.sqlite 'SELECT conv,count(*) FROM archive GROUP BY conv'`.
2. Inspect `sqlite_master` and `PRAGMA table_info` for EVERY table with a `conv` column. Delete only that id's rows
   using bound SQL parameters. Current primary tables: `archive chains masks segments inserts anchors pins
   autosaves drops fileops`; the derived `passages` table also has `conv`.
3. Delete `kv` keys ending in `:<id>` (currently `tool_names:`, `prompt_language:`, `response_clamp_warned:`).
   Delete the global `strata_state` key only when its JSON value belongs to that conversation. Inspect any newer
   metadata as well.
4. Drop the derived, lazily rebuilt structured-recall index:
   `DROP TABLE IF EXISTS passages; DROP TABLE IF EXISTS passages_done; DROP TABLE IF EXISTS p_raw;
   DROP TABLE IF EXISTS p_norm;`.
   Enable `PRAGMA secure_delete=ON`, rebuild `archive_fts` with
   `INSERT INTO archive_fts(archive_fts) VALUES('rebuild')`, commit, `VACUUM`, then
   `PRAGMA wal_checkpoint(TRUNCATE)` (verify it is not busy).
5. In the configured `slot_dir`, delete that id's segment state files: `<id>-seg<N>-A.bin`,
   `<id>-seg<N>-anchor<N>.bin`, `<id>-seg<N>-auto-<hash>.bin` (also inspect historical variants).
   Delete corresponding full-prompt `<state>.bin.json` sidecars in the data folder / configured `data_dir`.
6. In `kv-archive/manifests`, delete only the matching `.kva` manifests, then run
   `python3 -m ctxproxy.kvarchive --root /path/to/kv-archive gc`. Do NOT remove blocks referenced by other manifests.
7. Atomically filter `journal.jsonl`, including nested `conv` references and events naming only the conversation's
   state files. Preserve other lines byte for byte. Clear `live/last_request.json` only if its `conv` matches;
   filter matching filenames from `router-slot-models.json`. Include a configured external `data_dir` if used.
8. Verify all tables, FTS (search a unique word), filenames and journal. Verify an unrelated conversation still
   recalls exactly. Treat any error as incomplete deletion, not a successful forget.

Unlink and SQLite compaction are not a forensic secure wipe on SSDs, copy-on-write filesystems or backups.
