# Troubleshooting

Start with `mnemonic-proxy check` (add `--live` to exercise the engine): every failing line says what to change.

| symptom | cause | fix |
|---|---|---|
| `check`: port … is in use | another proxy (or another program) on that port | stop it, or pass a different `--port` (and change the client's base URL) |
| `check`: engine answers 503 / `GET /health` says `loading` | llama-server is still loading the model | wait for `listening on …` in its log |
| `check`: engine not reachable | wrong `--upstream`, or the engine is not running | start the engine; `curl http://127.0.0.1:8095/health` must answer |
| proxy exits at startup: `refusing to start, the context window is too small` / `check`: context window … minimum is 16384 | engine started with a window below 16K (`-c 8192`), or `"window"` below 16384 in the config | restart the engine with `-c 32768` (ds4-server: `--ctx 32768`); remove `"window"` from the config (it is read from the engine). See [Requirements](install.md#requirements) |
| llama-server: `failed to allocate` / killed | not enough memory for model + context | a smaller quantisation or a smaller model; the window can go down to 16384 (`-c 16384`), not below |
| HTTP 400 `exceeds the available context size` | the client asked for more than the window | proxy started with a config whose `window` is larger than the engine's `-c`? The proxy reads `n_ctx` from llama-server at startup: restart the proxy after changing `-c` |
| the model describes the command instead of calling a tool, or says it "cannot run commands" | chat template without tools, or a model weak at tool calling | llama-server with `--jinja`; `check --live` shows whether a tool call works; try a stronger model (see [Tested combinations](engines.md#tested-combinations)) |
| `check`: saved state supported by the engine, not enabled in the proxy | config without `slot_save` | nothing to fix unless you want the fast resume: it is optional, and the line says how to add it later |
| `check --live`: the state file is not in `slot_dir` | `slot_dir` ≠ llama-server `--slot-save-path`, or the engine runs on another host / in a container with another path | the same folder, as seen from the proxy's host |
| `check`: engine detected as `openai` | llama-server too old, or another engine | saved state needs llama-server with `--slot-save-path`, or Strata ≥ 0.1.40 with `--slot-save-path`; everything else works |
| Strata has no saved state | Strata older than 0.1.40, or started without `--slot-save-path` | optional: base mode keeps working. For the fast resume, update to Strata ≥ 0.1.40 and start it with `--slot-save-path DIR` |
| `check`: `slot_dir … does not exist and the proxy could not create it` | the path is not writable by the user running the proxy | run the `mkdir` command the message prints, or point `slot_dir` at a writable folder (the engine's `--slot-save-path`) |
| answers get shorter and shorter / summaries appear in the history | the client's own compaction is on as well | turn it off: pi `"compaction": {"enabled": false}`, opencode `"compaction": {"auto": false}`, Claude Code `DISABLE_AUTO_COMPACT=1`, Hermes `compression.enabled: false`, Codex: [threshold](agents.md#codex-openai-responses) |
| Windows: `\u27ea` in the output of `demo` / `check`, or `?` where `·` `—` `«»` should be | before 0.3.24 a non-UTF-8 console got `errors="backslashreplace"` | fixed in 0.3.24: on a console that is not UTF-8 the proxy prints ASCII stand-ins (`<<`, `>>`, `--`). Nothing sent to the engine or written to disk changes; `chcp 65001` (UTF-8) brings the real characters back |
| Codex: `wire_api = "chat"` rejected | Codex ≥ 0.160 only speaks the Responses API | `wire_api = "responses"` |
| `kv_archive` refused | needs Python ≥ 3.14 (`compression.zstd`) | Python 3.14, or leave `kv_archive` off (default) |
