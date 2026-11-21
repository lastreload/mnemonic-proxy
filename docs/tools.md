# Tools

The administrative utilities that come with the repository — the cold-archive audit, the restore
measurement, the dashboard — and the proxy's own HTTP endpoints. Everything on this page is for the
operator: none of it is a tool the model can call.

The tools the **agent** exposes are a different subject. How the proxy keeps the core ones in the prompt and
loads the rest by name (`tools_paging`, `tools_core`, `tools_load_max`) is in
[Configuration](configuration.md#every-configuration-field), with the experimental
[native tool-call split](configuration.md#native-tool-call-split-experimental-llamacpp) for llama.cpp; the
names of the proxy's own `recall` and `tools` are in
[Tool names and older conversations](configuration.md#tool-names-and-older-conversations). Read-only archive
search from an MCP client is the [MCP recall server](agents.md#mcp-recall-server-claude-code-codex-hermes).

## Archive audit and restore measurement

Two stand-alone programs, Strata-only for now. They run from a clone of this repository (the pip package
does not install them) and neither restarts the engine. They need **Python 3.14 or newer**, because the cold
archive uses the standard library's `compression.zstd`: `.venv314` below is only the name of our own virtual
environment, not a required one — any interpreter that can import `compression.zstd` works. Run them from
the clone's root, which is what the relative `tools/…` paths assume.

The first one is a passive audit; the second sends real requests to your engine. Read its paragraph before
running it.

**`tools/check_kv_archive.py`** — is the cold archive healthy, and what is it saving?

<!-- readme-test: real-engine -->
```sh
.venv314/bin/python tools/check_kv_archive.py                 # snapshot now
.venv314/bin/python tools/check_kv_archive.py --restore-check  # rebuild each archive, compare sha256
.venv314/bin/python tools/check_kv_archive.py --watch          # follow until it archives
```

It reports the `sessions/` files, the `kv-archive/` usage with dedup, and how far the
archiver is from firing (engine idle, file age, file size). What it touches, exactly:

- it never modifies the archive and never creates the archive root — a missing archive is reported, not
  materialised. Inside a root that already exists it does recreate the empty `blocks/` and `manifests/`
  folders;
- `--restore-check` writes: it rebuilds every archive into a private temporary directory
  (`tempfile.mkdtemp`), compares each sha256 with the manifest, deletes each rebuilt `.bin` and removes the
  directory at the end. The stored archive is left as it was, but the run needs free temporary space for one
  rebuilt state file at a time;
- it polls the engine's `GET /v1/status` to see whether the engine is idle. That is the only request it
  sends: no completion, nothing written to the engine.

Exit code 0 when everything is fine, 1 on a problem (a corrupt block, a failed restore,
a `--watch` that never archived), 2 on a usage error.

**`tools/measure_restore.py`** — how much does the automatic restore save?

<!-- readme-test: real-engine -->
```sh
.venv314/bin/python tools/measure_restore.py --allow-shared-engine
```

This one is a benchmark, not an audit, and it is active on both sides. It starts a **private proxy**
(`--port`, default 8196) with its own data directory in front of the engine you already have running, sends
real completion requests, and fills the engine with a second conversation on purpose. It writes its own data
directory (an existing `--data` is refused, never deleted, unless it carries the marker this tool writes) and
the saved-state files its own conversations produce. It does not touch the proxy you normally run and does
not restart the engine. `--dry-run` prints the plan and exits without contacting anything.

It sends **the same request twice**, each time after B has evicted A from the engine:
**control** (`autorestore` off) gives the real re-read, **restore** (`autorestore` on)
gives `restore_ms` plus the few new tokens — both read from the journal, so the speed-up
is `control_ms / (restore_ms + new_ms)`, not an extrapolation. `--two-phases` is the
earlier method: it runs a whole A/B cycle per phase and re-sends A with one extra
message, so its restore path also carries those new tokens — a slightly different
question, measured with the same journal timings.

Measured with **Strata 0.1.41** and **swift-1.5-iq3_xxs** (window 131072, prompt
~31.4K tokens) on an **RTX 5060 Ti 16 GB** (PCIe 3.0 x8), an **Intel Xeon E5-2699 v3**
and **64 GB DDR4-2667 running at 2133 MT/s** (the CPU's memory limit), with three runs
of each method (six runs in total): the same request gave **~10–12×** (median 11.6×) —
re-read ~30 s vs restore + new tokens ~2.7 s — and the two-phase method gave ~6.7×
under the same conditions.

It needs `--allow-shared-engine` because filling the engine with conversation B evicts
other sessions; it removes only the files it created itself. Exit code 0 when a restore
was measured, 1 when nothing was restored, 2 on a usage error.

### Dashboard (optional; needs the git clone)

The dashboard is not part of the pip package: it runs from a clone of this repository. Its interface is
translated into Italian, English and French; it picks one from the browser's language (anything that is
neither Italian nor French gets English) and a selector in the page switches it. Run it from the clone's
root:

<!-- readme-test: real-engine -->
```sh
python3 dashboard/ctxdash.py --data ./data --strata http://127.0.0.1:8095 --proxy http://127.0.0.1:8096 --port 8097
```

`--data` is required: it is the proxy's data directory (`journal.jsonl`, `archive.sqlite`, `live/`). If
Strata requires an API key, add `--api-key-file` with the host path to its key file. The dashboard reads the
key locally and sends it only to Strata. If the proxy requires a client key, add `--proxy-api-key-file`
explicitly; even when sharing the same key file, supply both flags. The dashboard is local-only: use its
listener's port and `localhost` or `127.0.0.1` as Host (the gate also recognizes `[::1]`, without changing the
existing IPv4-only bind implementation). Custom hosts, reverse proxies and forwarding to a different local
port are not supported. Headless writes to `/api/marks` must use JSON, not text/plain; browser writes must
be same-origin. These dashboard guards are automatic: no new key configuration is required for local no-auth
setups. Key files are read at startup; restart to change them. This is not HTTPS or safe public exposure.
See [Security & privacy](security.md#security--privacy).

On `127.0.0.1:8097`: engine status, physical vs. virtual context over time, masking/segment/recall events,
and the live physical prompt (with `live_dump: true`). It reads Strata and the data directory without
changing them, and has one write path: the 📌 / 🗑 buttons, which it forwards to the proxy's
`POST /v1/strata/marks` (pin a line, mark a message disposable). Its history export reads the archive only.

### Proxy endpoints

Besides the client APIs — `/v1/chat/completions`, `/v1/messages`, `/v1/messages/count_tokens` and
`/v1/responses`, each also accepted without the `/v1` prefix — every other path is passed to the engine, and
only for `GET` and `POST`: the proxy implements no other HTTP method.

The proxy's own:

| endpoint | what |
|---|---|
| `GET /health` | proxy + engine health: `{"status": "ok" \| "loading" \| "engine_unreachable", "engine", "version", "upstream"}`; HTTP 200 only when the engine is ready |
| `GET /v1/engine` | what was detected: `kind`, `slot_save` (saved state supported and on), `n_ctx`, `model`, `notes`. `GET /v1/strata/engine` is the same (0.1 name, kept) |
| `GET /v1/strata/conversations/<id>` | one conversation's counters (`archived`, `archived_tokens`, `masked`, `masked_tokens`, `masked_saved_est`, `segments`) and `segment_list`, each segment with its cut index, kind and handoff notes — not the messages themselves |
| `GET /v1/strata/archive/<id>` | one archived block, verbatim |
| `GET /v1/strata/journal` | the last 200 journal events still in memory; the full log is `<--data>/journal.jsonl` |
| `GET /v1/strata/marks/<id>` | that conversation's pinned lines and messages marked disposable, with the pin token budget |
| `POST /v1/strata/marks` | `{"conv", "action": pin \| unpin \| update_pin \| drop \| undrop, …}` — what the dashboard's 📌 / 🗑 buttons call |
| `GET /v1/strata/saved-state` | the models whose saved state the proxy has suspended after a refusal from the engine |
| `POST /v1/strata/saved-state/reset` | clears that suspension (`{}` for all models); **403 unless the request comes from this machine**. Details and the refusal table: [Engines](engines.md#engines) |
