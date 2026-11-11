# Tools

## Tools: audit the archive and measure restore

Two stand-alone tools, Strata-only for now. Both are read-only against the archive and
never restart the engine; run them with the Python that has the archive support
(`.venv314`).

**`tools/check_kv_archive.py`** — is the cold archive healthy, and what is it saving?

<!-- readme-test: real-engine -->
```sh
.venv314/bin/python tools/check_kv_archive.py                 # snapshot now
.venv314/bin/python tools/check_kv_archive.py --restore-check  # rebuild each archive, compare sha256
.venv314/bin/python tools/check_kv_archive.py --watch          # follow until it archives
```

It reports the `sessions/` files, the `kv-archive/` usage with dedup, and how far the
archiver is from firing (engine idle, file age, file size). It never creates the archive
directories and never writes to disk: `--restore-check` restores into a private temp dir.
Exit code 0 when everything is fine, 1 on a problem (a corrupt block, a failed restore,
a `--watch` that never archived), 2 on a usage error.

**`tools/measure_restore.py`** — how much does the automatic restore save?

<!-- readme-test: real-engine -->
```sh
.venv314/bin/python tools/measure_restore.py --allow-shared-engine
```

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

### Dashboard (optional; needs the git clone; UI in Italian)

The dashboard is not part of the pip package: it runs from a clone of this repository, and its interface is in
Italian.

<!-- readme-test: real-engine -->
```sh
python3 mnemonic-proxy/dashboard/ctxdash.py --data ./data --strata http://127.0.0.1:8095 --proxy http://127.0.0.1:8096 --port 8097
```

If Strata requires an API key, add `--api-key-file` with the host path to its key file. The dashboard reads the key
locally and sends it only to Strata.

Read-only, on `127.0.0.1:8097`: engine status, physical vs. virtual context over time, masking/segment/recall events,
and the live physical prompt (with `live_dump: true`).

### Proxy endpoints

Besides the client APIs (`/v1/chat/completions`, `/v1/messages`, `/v1/responses`), all other paths are passed to the
engine. The proxy's own:

| endpoint | what |
|---|---|
| `GET /health` | proxy + engine health: `{"status": "ok" \| "loading" \| "engine_unreachable", "engine", "version", "upstream"}`; HTTP 200 only when the engine is ready |
| `GET /v1/engine` | what was detected: `kind`, `slot_save` (saved state supported and on), `n_ctx`, `model`, `notes`. `GET /v1/strata/engine` is the same (0.1 name, kept) |
| `GET /v1/strata/conversations/<id>` | one conversation: archived messages, masks, segments |
| `GET /v1/strata/archive/<id>` | one archived block, verbatim |
| `GET /v1/strata/journal` | the last 200 journal events |
