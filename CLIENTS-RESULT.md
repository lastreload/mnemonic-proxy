# CLIENTS-RESULT — ingressi Claude Code (Anthropic Messages) e Codex (OpenAI Responses)

Ramo `feature/clients` (da `feature/paging` @ dfcc506). Solo CPU, motore finto; niente Strata, niente GPU.

## Cosa c'è

- `ctxproxy/api_anthropic.py` — `POST /v1/messages` (stream e no), `POST /v1/messages/count_tokens`.
- `ctxproxy/api_responses.py` — `POST /v1/responses` (stream e no, senza stato).
- `ctxproxy/server.py` — solo 6 righe: instradamento delle due rotte. Cuore (`core.py`) non toccato.
- `tests/test_clients.py` — 18 test (conversione + HTTP contro il motore finto).
- `tests/test_kvarchive.py` — corretto un test fragile: `T0` era fissato all'import del modulo, e con
  più test prima (i miei) due test di integrazione kv_archive fallivano per tempo, non per codice.
  Ora `T0` è calcolato in `setUp`.
- README: sezione "Using it with Claude Code / Codex".

Schema: richiesta client -> Chat Completions interno -> `Proxy.chat` invariato -> chunk OpenAI -> eventi
SSE del client (Anthropic: message_start / content_block_* / message_delta / message_stop / ping;
Responses: response.created / in_progress / output_item.added/done / output_text.delta /
reasoning_text.delta / function_call_arguments.delta/done / content_part.* / completed|incomplete|failed).

## Verificato

Suite completa: `python3 -m unittest discover -s tests -t .` -> 127 test, OK (5 saltati: gruppi
facoltativi tokenizer/Strata mock, come prima). Su questo MVLINNA `compression.zstd` c'è e la suite è
verde; nessun fallimento d'ambiente.

Test unitari (motore finto, testo + ragionamento + chiamate, stream e no):
- id strumenti: interno `call_x` -> Anthropic `toolu_call_x`; in ingresso l'id resta quello del client
  (conversione pura -> catena di hash stabile); Responses: `call_id` invariato.
- più `tool_use` nello stesso messaggio, `tool_result` con blocchi e `is_error` (-> "Error: …").
- thinking <-> reasoning_content; firma generata (`strata-<sha256>`), in ingresso ignorata.
- `cache_control` ignorato; immagini nel messaggio utente -> 400 nel formato del client;
  immagini dentro un `tool_result` -> nota testuale (rifiutarle bloccherebbe la sessione per sempre).
- `max_tokens` obbligatorio (400 `invalid_request_error`).
- `count_tokens` = `Manager.estimate` del proxy sulla stessa richiesta convertita.
- `strata_recall` risolto dal proxy e invisibile al client (nessun tool_use / function_call), sia
  stream sia no, con entrambi i formati.
- catena di hash: secondo giro (storia rimandata + tool_result) -> stessa conversazione.
- Codex: strumenti `custom` (apply_patch) -> funzione con `input`; tornano come `custom_tool_call`.

Prova dal vivo (proxy su 127.0.0.1:18296 davanti a un motore finto su 18295, che al primo giro chiama
lo strumento shell del client e poi risponde col risultato):
- **Claude Code 2.1.287** (`claude -p "stampa un saluto con bash" --allowedTools "Bash(echo:*)"`,
  `CLAUDE_CONFIG_DIR` temporaneo): thinking + tool_use `Bash` -> Claude Code esegue
  `echo ciao-dal-proxy` -> tool_result -> risposta finale "RISULTATO DELLO STRUMENTO: ciao-dal-proxy",
  `stop_reason: end_turn`, rc 0. Il motore ha ricevuto 20 strumenti di Claude Code + `strata_recall`.
  Una sola conversazione nel giornale; al secondo giro `reused` 17.995 token su 18.097 (prefisso stabile).
  Problema trovato e corretto dal vivo: Claude Code 2.1.x manda anche messaggi `role: "system"` dentro
  `messages` (ambiente, `<total_tokens>`): in testa si uniscono al system, dopo diventano un messaggio
  utente "[system]\n…" (test aggiunto).
- **Codex 0.160.0** (`codex exec --skip-git-repo-check --json "stampa un saluto con la shell"`,
  `CODEX_HOME` temporaneo, `~/.codex` non toccato): reasoning + function_call `exec_command` -> Codex
  esegue `/bin/bash -lc 'echo ciao-dal-proxy'` -> function_call_output -> messaggio finale, rc 0.
  Conversazione legata a `prompt_cache_key` (conv `c-…`), secondo giro `reused` 8.473/8.613.
  ("Failed to create stream fd: Operation not permitted" nell'uscita è la sandbox di Codex nella cartella
  di prova, non il proxy.)
- `wire_api = "chat"` su Codex 0.160: rifiutato all'avvio ("is no longer supported", rimando a
  github.com/openai/codex/discussions/7782). Solo `responses`.

## Limiti noti

- Firma del ragionamento: locale e non verificabile; va bene finché il client parla col proxy. Se una
  sessione Claude Code passa da questo proxy all'API Anthropic vera, i blocchi thinking con firma
  `strata-…` verranno rifiutati da Anthropic.
- Immagini/documenti: non inoltrati (il motore riceve solo testo).
- Responses senza stato: `previous_response_id` e `item_reference` -> 400. Codex usa `store=false` e
  rimanda tutto, quindi non serve. Ragionamento `encrypted_content` ignorato.
- Strumenti lato server (Anthropic web_search/…; Responses web_search, local_shell…) scartati.
- Ragionamento in uscita Responses: item `reasoning` con `content` reasoning_text e lo stesso testo come
  `summary` (Codex mostra i summary).
- Argomenti di chiamate intercalate (indice A, poi B, poi di nuovo A) non rappresentabili nello stream
  Anthropic/Responses: scartati. Il cuore inoltra le chiamate in ordine, quindi non succede con Strata.
- Claude Code avvisa `unrecognized_model` per nomi non Anthropic: innocuo. Codex avvisa "Model metadata
  not found": innocuo (impostare `model_context_window`).
- Richieste accessorie di Claude Code (titoli, ecc. con `ANTHROPIC_SMALL_FAST_MODEL`) passano dal proxy
  come conversazioni a sé: occupano la sequenza unica di Strata; `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1`
  le riduce.

## Prova dal vivo su Strata (per l'orchestratore, GPU libera)

Proxy (sul ramo feature/clients):

    cd /home/mverde/src/vcp-wt/clients
    python3 -m ctxproxy.server --upstream http://127.0.0.1:8095 --port 18096 --data ./data-clients \
        --config examples/config.example.json [--tokenizer <pack>/tokenizer]

Claude Code:

    mkdir -p /tmp/cc-try && cd /tmp/cc-try
    ANTHROPIC_BASE_URL=http://127.0.0.1:18096 ANTHROPIC_AUTH_TOKEN=local \
    ANTHROPIC_MODEL=qwen-local ANTHROPIC_SMALL_FAST_MODEL=qwen-local \
    CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1 DISABLE_AUTO_COMPACT=1 \
      claude -p "crea hello.py che stampa ciao, eseguilo e dimmi l'uscita" \
        --allowedTools "Bash(python3:*)" "Write" --output-format stream-json --verbose

Codex (config in una CODEX_HOME separata):

    mkdir -p /tmp/codex-strata && cat > /tmp/codex-strata/config.toml <<'EOF'
    model = "qwen-local"
    model_provider = "strata"
    approval_policy = "never"
    sandbox_mode = "workspace-write"
    [model_providers.strata]
    name = "Strata via virtual-context-proxy"
    base_url = "http://127.0.0.1:18096/v1"
    wire_api = "responses"
    env_key = "STRATA_API_KEY"
    requires_openai_auth = false
    supports_websockets = false
    EOF
    mkdir -p /tmp/cx-try && cd /tmp/cx-try
    CODEX_HOME=/tmp/codex-strata STRATA_API_KEY=local \
      codex exec --skip-git-repo-check --json "crea hello.py che stampa ciao, eseguilo e dimmi l'uscita"

Da guardare: `data-clients/journal.jsonl` (una conversazione per sessione, `reused` alto dal secondo
giro, eventuali `recall`), e che il modello vero produca chiamate nel formato atteso dagli strumenti di
Claude Code (Bash/Write/Edit) e di Codex (exec_command, apply_patch come custom tool).
