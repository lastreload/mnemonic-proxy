# `init-config`: a minimal file, and one click — result

Author: Maurizio Verde — LastReload. Version 0.3.24.

Reported by the first Windows user of mnemonic-proxy (u/derbidrd, after the 0.3.23 Reddit announcement): the
configuration `init-config` wrote was too big, and he had written his own `.bat` by hand to start the proxy.

## 1. The bug behind "too big"

`mnemonic-proxy init-config --engine strata` copied `examples/config.example.json` out verbatim — sixteen
explicit fields:

```json
{"window": 131072, "mask_reasoning": true, "mask_tool_args": true, "mask_anchor": true,
 "anchor_min_tokens": 24000, "slot_dir": "/path/to/strata/sessions", "slot_save": true,
 "seal_experimental": true, "notes_max_tokens": 12288, "response_floor": 16384, "live_dump": true,
 "autosave": true, "autosave_idle_s": 180, "autosave_keep": 10, "autosave_max_gb": 25,
 "autosave_min_free_gb": 8}
```

This is not a cosmetic problem. **A field written in the config always wins over the automatic
configuration** (`autoconfig.decide`, `engines.apply`), so that file *switched the automatic configuration
off* — from the very command `check` suggests. Concretely, on a fresh install it:

* pinned `window` to 131072 even on an engine running with 115K or 32K, instead of reading `n_ctx` /
  `/v1/status` and scaling the thresholds to it;
* turned `slot_save`/`mask_anchor`/`autosave` on past `autoconfig.saved_state_gate`, which exists precisely
  to refuse them when the engine cannot save state, when it runs on another host, when a session file would
  not fit in RAM, or when the `slot_dir` disk is too full;
* left `slot_dir` as `/path/to/strata/sessions`, a path that cannot work;
* pinned `response_floor` 16384 and `notes_max_tokens` 12288, the values *computed for a 131072 window*;
* turned `live_dump` on — every prompt written to disk — and `seal_experimental`, which the README keeps
  opt-in on purpose.

## 2. What the minimal file holds, engine by engine

Reviewed one by one against `autoconfig.AUTO_FIELDS`, `autoconfig.saved_state_gate` and
`engines.apply`. What the proxy already discovers must not be written; what it cannot discover must be.

| `--engine` | minimal file | why exactly this |
|---|---|---|
| `strata` | `{}` | window from `/v1/status`; saved state, exact count and budgets from `autoconfig` |
| `llama-server` | `{}` | window from `n_ctx` (`/props`) |
| `llama-server-32k` | `{}` | same; the thresholds scale to whatever `-c` the engine runs with |
| `llama-server-router` | `{}` | per-model window and thresholds come from the engine (`router.py`) |
| `ds4` | `{}` | ds4 reports its `--ctx` and keeps its own state (`--kv-disk-dir`) |
| `openai-32k` | `{"window": 32768}` | a generic OpenAI-compatible engine reports no context at all |
| `official-strata` | `{"slot_save": false, "autosave": false}` | the engine *can* save state, so base mode is a user decision |

Plus, on the four engines whose saved state lives in a folder the user picked, `--slot-dir DIR` adds
`"slot_dir"` **and nothing else**. That field is the one thing the proxy cannot discover, and the one thing
`saved_state_gate` names as missing before it switches saved state on by itself:

```
slot_dir is not set, so old state files could never be cleaned up
  -> set "slot_dir" to the engine's --slot-save-path folder and the proxy turns saved state on by itself
```

Without `--slot-dir` **no placeholder is written**. A `/path/to/...` in there is worse than an absent field:
it reads like a working setting, and it is the field the whole saved-state gate hangs on.

Verified end to end against a fake Strata (`tests/fake_engine.py`, flavor `strata`), with a one-field config:

```
$ cat config.json
{"slot_dir": ".../sessions"}
✅ config         config.json: 1 fields, all known
✅ context window window from engine n_ctx=32768; thresholds scaled: mask at 20000 -> 10000, protected tail 4000
ℹ️  auto config    chosen for this engine and this machine: slot_save=true, mask_anchor=true, autosave=true,
                  response_floor=4096, notes_max_tokens=3072 (RAM 26.8 GiB total, 20.5 GiB available; VRAM 12.0 GiB)
✅ saved state    supported by the engine, configured in the proxy (slot_save, mask_anchor, autosave)
```

One field in, five decisions taken by the proxy from the engine and the machine — each with its reason, and
each overridable by writing it.

### `"auto_config": true` is deliberately never written

It would look like the honest thing to write, since the automatic configuration is exactly what the file
leaves in charge. It is wrong for two reasons:

1. it is already the default (`Config.auto_config = True`), so on a new data folder it adds nothing;
2. on a data folder that **already holds conversations** it *removes* a protection. `autoconfig.decide`
   only *applies* its choices there when `auto_config` is explicit (`EXISTING_WHY`), so that an upgrade
   cannot silently change how a running setup behaves — including switching saved state on where it was off.

### `mask_reasoning` and `mask_tool_args` are left out too

They are not in `AUTO_FIELDS`: the proxy does not decide them. They are also not engine or machine facts —
they are feature choices, documented in the README's Configuration section, with their own defaults
(`False`). A *starting* file should not make them silently, and the README already claims masking works on
Strata with no config at all. `--full` spells them out for anyone who wants them pinned.

## 3. The thresholds now scale behind a written `window` too

While reviewing `openai-32k` it became clear that `{"window": 32768}` alone was **not** a working 32K
configuration. `engines.apply` scaled the window-sized defaults only when the window came *from the engine*
(`engine.n_ctx and "window" not in explicit`). With a generic engine, which reports none, a written
`"window": 32768` left `mask_trigger` at 80000, `tail_max` at 24000, `default_response` at 16384 — so
proactive masking never fired on a 32K window, and only the hard window guard kept it alive.

The scaling loop is now one function, `engines._scale(cfg, explicit, ratio)`, with two callers: the existing
engine-`n_ctx` branch, and a new one for `not engine.n_ctx and "window" in explicit and window < BASE_WINDOW`.
A threshold the user wrote still wins; a startup warning says it happened.

That the two agree is not an assumption — `examples/config.openai-32k.json`, written by hand long before,
spells out *exactly* this scaling, and the test asserts it field by field:

```
mask_trigger 20000, mask_target 10000, keep_recent_tokens 4000, min_batch_tokens 4000, tail_max 6000,
reserve 2048, default_response 4096, pins_max_tokens 2048, recall_struct_max_tokens 1024,
auto_recall_max_tokens 1000
```

Measured startup with the minimal file in front of a generic fake engine:

```
[mnemonic-proxy] warning: window 32768 written in the config and the engine reports none;
                 thresholds scaled x0.250 from the 131072 defaults
[mnemonic-proxy] 127.0.0.1:18299 -> ... (engine openai, saved state no) mode=tools window=32768
[mnemonic-proxy] auto config: response_floor = 4096, an eighth of the 32768-token window ...
[mnemonic-proxy] auto config: notes_max_tokens = 3072, three thirty-seconds of the 32768-token window ...
```

The blast radius is deliberately narrow (`not engine.n_ctx`): every engine the proxy recognises keeps the
behaviour it had, and the full 1029-test suite passes unchanged.

## 4. `--full`, kept

`init-config --engine X --full` writes the complete documented example from the copy inside the wheel, as
before, and the shipped `examples/*.json` are **unchanged** — they are the reference the README links to and
other work in flight edits. Two additions: `--slot-dir` is substituted into the `slot_dir` placeholder when
given, and the output says in one line that every explicit field is one the automatic configuration no
longer decides.

## 5. `--launcher`: one click, any engine

`init-config --launcher` writes `start-mnemonic-proxy.bat` on Windows, `start-mnemonic-proxy.sh` elsewhere,
next to the config, with the `--upstream`, `--port`, `--data`, `--config` and optional `--tokenizer` that
were actually passed. Style taken from `examples/start-windows.bat`.

It starts **only the proxy**. `examples/start-windows.bat` also launches `llama-server.exe`, which is why it
has four paths to edit at the top; a Strata (or vLLM, or online API) command line is the user's own and
guessing it is how that file became four placeholders. Order, with a real message at every stop:

1. `mnemonic-proxy` not in the venv -> the two commands that install it, then stop;
2. the engine does not answer (`curl /v1/models`, then `/health`) -> "start it first", then stop. The proxy
   is never launched against a dead engine;
3. `mnemonic-proxy check` once: exit 1 -> stop, repeating that a taken port is reported above and how to
   pick another. Exit 0 or 2 -> go on. The port check is `check`'s own (`_port_state`: free / a
   mnemonic-proxy already there / taken by another program), so the message is the one `check` maintains;
4. the proxy, in the foreground. On Windows, `pause` at the end, so a console opened by a double-click does
   not vanish with the error still in it.

Every value is a variable at the top (`UPSTREAM`, `PORT`, `DATA`, `CONFIG`, `VENV`, `TOKENIZER`) and can be
overridden from the environment without editing the file; the `TOKENIZER` line is present but commented out
unless `--tokenizer` was given. No personal path anywhere: `%~dp0` / `$HERE` and `%USERPROFILE%` / `$HOME`.

Verified on Linux against a fake Strata, all four paths:

| run | result |
|---|---|
| `UPSTREAM=...:19999 ./start-mnemonic-proxy.sh` | `The engine at ... does not answer.` / `Start it first ...`, exit 1 |
| engine up, first run | `check` READY with warnings, proxy answers on `/v1/engine`, `data/journal.jsonl` created |
| second run, same port | `check`: `a mnemonic-proxy is already running there (engine strata)`; then one line, `data directory is in use by a proxy or maintenance command`, exit 1 — no traceback |
| `PORT=18297` held by another program | `check`: `❌ port 127.0.0.1:18297 is taken by another program -> pick a free --port`, then `NOT READY`, then the launcher's own `If port 18297 is taken, pick a free one: PORT=8097 ./start-mnemonic-proxy.sh`, exit 1 |

## 6. Tests

* `tests/test_init_config_minimal.py` (new, 35 tests): every alias's minimal content; **no** minimal file may
  carry a field from `AUTO_FIELDS`, `live_dump`, `seal_experimental`, `auto_config` or a `/path/to`
  placeholder (checked by iterating `ENGINES`, so a new engine cannot be added without an answer); every
  minimal file parses as a real `Config`; `--slot-dir` refused where it means nothing; `--full` unchanged and
  placeholder substitution; the launcher's content, CRLF on Windows, `pause` last, variables at the top, the
  commented-out tokenizer, engine-checked-before-proxy ordering, the taken-port message, `sh -n` on the
  generated script, executable bit, never overwritten without `--force`; and the generic-window scaling
  asserted against `config.openai-32k.json`.
* `tests/test_init_config.py`: the pre-existing tests that asserted the *old* full-copy behaviour now pass
  `--full`, and every alias is exercised both ways.
* `tests/test_readme_run.py`: new level-2 test that really executes every README `ci` block calling
  `init-config`, then asserts the file on disk holds nothing the proxy decides, and `sh -n`s the launcher.
* `.github/workflows/ci.yml`, `test-windows`: `init-config --launcher`, assert `config.json == {}` and the
  `.bat`'s shape, then run the `.bat` four times — with `VENV` pointing nowhere (must print the install hint),
  without an engine (must refuse), with a fake Strata (proxy must answer on 18196 and create `--data`), and
  with `PORT` on the engine's own port (must stop with `taken by another program`). `VENV` and `PORT` are set
  through the environment, which also proves the variables at the top are overridable. Stdin is an empty
  file, because the failure paths end in `pause`.
* Full suite: 1029 tests, OK (66 skipped — live/model-dependent), Python 3.14 locally; README level 1 (27)
  and level 2 (11) green.

## 7. Not done, on purpose

* `examples/*.json` untouched, including `live_dump: true` in `config.example.json`. It is the *documented
  full* example and the README links every file by name; changing their content is a separate decision and
  would collide with the other 0.3.24 work in flight. The minimal path no longer goes through them.
* No new static `.bat` committed. The launcher is generated so it carries real values; a second committed
  file would be a second source of truth for the same thing. `examples/start-windows.bat` stays as the
  llama-server-plus-engine variant.
* `mask_reasoning` / `mask_tool_args` left out of the minimal files (§2).
* The generic-window scaling is gated on `not engine.n_ctx`: widening it to a window written for a
  *recognised* engine (where `n_ctx == window` also skips the scaling today) is a real but separate
  behaviour change, and no user has reported it.
