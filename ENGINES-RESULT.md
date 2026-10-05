# ENGINES-RESULT — il proxy davanti a motori diversi da Strata, e l'archivio via MCP

Card t_0ebd2f7a, branch feature/engines. Misure del 2026-10-05.

## Che cosa è cambiato

- `ctxproxy/engines.py` (nuovo): descrive il motore e lo riconosce all'avvio.
  - `engine: "auto"` (di serie) prova in ordine: `GET /v1/status` con `activity` -> Strata; `GET /props` con
    `default_generation_settings` -> llama.cpp; altrimenti OpenAI generico. `--engine strata|llama.cpp|openai`
    (o `engine` nella configurazione) lo forza.
  - llama.cpp: per sapere se i salvataggi ci sono si manda un'azione slot non valida: 400 "Invalid action" = server
    partito con `--slot-save-path`, 501 = no. Lo slot non viene toccato (verificato sul server vero: `id_task` e
    `n_prompt_tokens` invariati).
  - `apply()` spegne con un avviso (stampato e nel giornale, evento `engine_warning`) slot_save, mask_anchor,
    autosave/autorestore, seal_experimental, kv_archive quando il motore non ha i salvataggi.
  - Con llama.cpp la finestra viene da `n_ctx` dello slot (se `window` non è scritto in configurazione) e le soglie
    di serie pensate per 128K si scalano in proporzione (evento `engine_window`).
  - Stato del motore nella forma di `/v1/status` di Strata, ricavato da `GET /slots` (id_task = contatore,
    is_processing = in corso): Tracker (autorestore), AutoSaver e kv_archive restano invariati.
    Differenza vera: `id_task` di llama-server cresce di più di 1 per richiesta, quindi dopo ogni chiamata il
    Tracker rilegge il contatore invece di fare +1.
  - `engine_tokenize: true` conta i token col `POST /tokenize` del motore (solo se manca `--tokenizer`).
- `upstream.py`: slot configurabile (`slot_id`; con llama.cpp ogni richiesta porta `id_slot` e `cache_prompt`),
  l'errore 400 di llama-server su un file mancante diventa 404 come in Strata (il ripristino lo tratta come
  "file assente", non come errore del motore).
- `autosave.py`, `kvarchive.py`: lo stato del motore passa da `engines.normalize_status`.
- `server.py`: `--engine`, rilevamento all'avvio, `GET /v1/strata/engine`, riga di avvio con motore e salvataggi.
- `ctxproxy/mcp_server.py` (nuovo): l'archivio come server MCP in sola lettura, stdio o HTTP streamable.
- Test: `tests/test_engines.py` (26 test, motore finto con comportamento llama-server/OpenAI in
  `tests/fake_engine.py`). Banchi reali: `bench/engines_llama.py`, `bench/engines_live_noslot.py`.

## Tabella: che cosa funziona con quale motore

| funzione | Strata + file di sessione | Strata ufficiale | llama-server --slot-save-path | llama-server senza | OpenAI generico (vLLM, online) |
|---|---|---|---|---|---|
| masking a frontiera stabile, archivio, strata_recall | sì | sì | sì | sì | sì |
| segmenti con note di passaggio, 📌/🗑, streaming | sì | sì | sì | sì | sì |
| conteggio token esatto | --tokenizer | --tokenizer | --tokenizer o /tokenize | --tokenizer o /tokenize | --tokenizer |
| ancora di masking (save/restore alla frontiera) | sì | no | sì | no | no |
| segmento A salvato su disco | sì | no | sì | no | no |
| autosave / autorestore | sì | no | sì (misurato) | no | no |
| kv_archive (archivio freddo) | sì, parser per blocchi | no | sì, blocchi fissi da 1 MiB (misurato) | no | no |
| stato del motore | /v1/status | /v1/status | /slots | /slots | nessuno |
| finestra | configurazione | configurazione | n_ctx del server | n_ctx del server | configurazione |

"no" = spento all'avvio con avviso. Il server MCP non dipende dal motore.

## Misure reali: llama-server con --slot-save-path

PC4070TI, llama.cpp b1-6c5afc8 (build-cuda-gcc15) in sola CPU (`CUDA_VISIBLE_DEVICES=""`, `-ngl 0`, 8 thread,
nice 19: la GPU era occupata da Strata), Qwen3-0.6B Q8_0, `-c 16384 -np 1`. Il proxy gira nel processo del banco
(`bench/engines_llama.py`), conversazione di prova ~8.2K token. Rilevamento: llama.cpp, salvataggi sì, n_ctx 16384,
/tokenize sì; finestra 16384 presa dal server (avviso e soglie scalate).

| passo | tempo totale | prompt_tokens | letti davvero | dalla cache |
|---|---|---|---|---|
| 1. prima lettura | 29.7 s | 8249 | 8249 | 0 |
| 2. autosave (AutoSaver) | 0.89 s, 948 MB, n_saved 8269 | | | |
| 3. riavvio di llama-server + turno con autorestore (restore 0.21 s) | 1.9 s | 8288 | 43 | 8245 |
| 4. riavvio + stesso turno senza file (rilettura) | 28.6 s | 8288 | 8288 | 0 |
| 5a. altra conversazione nello stesso slot | 4.1 s | 1985 | 1508 | 477 |
| 5b. ritorno alla prima (autorestore "other_state", restore 0.12 s) | 2.0 s | 8321 | 37 | 8284 |

kv_archive sul file di llama-server (952 MB): archiviato in 1.08 s, ricostruito in 0.77 s, sha256 verificato e
identico byte per byte. Rapporto 1.08: il formato di llama-server non è quello di Strata, il parser ricade sui
blocchi fissi e la KV f16 si comprime poco. Funziona, ma il risparmio di spazio è piccolo.

Lettura: ripresa dopo riavvio 1.9 s invece di 28.6 s (15x), come con Strata (2.9 s contro 33.1 s a 69.6K).
Il file di sessione di llama-server per 8.2K token pesa ~950 MB (KV f16 di un modello piccolo, ~115 KB/token):
con modelli grandi e contesti lunghi lo spazio su disco va controllato (`autosave_max_gb`, `kv_archive`).

## Misure reali: llama-server senza --slot-save-path e --engine openai

`bench/engines_live_noslot.py`, stesso server senza `--slot-save-path`, configurazione con slot_save, autosave,
mask_anchor accesi:
- auto: rilevato llama.cpp, salvataggi no (nota "avviare llama-server con --slot-save-path DIR"), le tre funzioni
  spente con avviso, finestra 4096 dal server; richiesta via proxy 200 OK in 0.81 s; stato da /slots.
- `--engine openai` sullo stesso server: nessun endpoint extra usato, funzioni spente, richiesta 200 OK in 0.16 s.

## Server MCP: prove reali

Archivio usato: copia di `nibble2.sqlite` (sessione reale del gioco, 1683 messaggi, 461K token), file in sola
lettura (chmod 444), sha256 identico prima e dopo tutte le prove.

| client | registrazione | prova |
|---|---|---|
| stdio a mano | — | initialize, tools/list (3 strumenti), conversations, recall con query, mode=tree, journal (assente: messaggio chiaro) |
| Claude Code 2.1.287 | `claude mcp add -s project ctxarchive ...` (.mcp.json) | `claude -p` con solo gli strumenti MCP: trovata `SNAKE_BASE_SPEED = 5.4`, con id del pezzo e messaggio, in 6 turni |
| Claude Code, HTTP | `--http 18133`, configurazione `type: http` | `conversations` -> `cfbb3f8ae1bc8` |
| Codex CLI 0.160.0 | `codex mcp add` (in CODEX_HOME temporaneo) e `-c mcp_servers...` | `codex exec`: `ctxarchive/recall` chiamato, stessa risposta (5.4, id del pezzo, messaggio 83) |
| Hermes | `hermes mcp add` in HERMES_HOME temporaneo | `hermes mcp test`: connesso in 802 ms, 3 strumenti |

Configurazioni utente (`~/.codex/config.toml`, `~/.hermes/config.yaml`, Claude Desktop) non toccate.
Claude Desktop / Cowork: la configurazione dei server MCP locali è `mcpServers` in
`~/.config/Claude/claude_desktop_config.json` (oggi senza server); non provato in questa card.

## Limiti e cose da sapere

- llama-server con `--parallel N`: il proxy usa un solo slot (`slot_id`); gli altri restano liberi per altri
  client, ma il proxy resta "una sequenza".
- Il contatore di llama-server (`id_task`) non distingue un riavvio: dopo un riavvio il Tracker se ne accorge
  dal prompt (prefisso diverso) e ripristina col motivo `strata_restart`/`other_state` come visto sopra.
- kv_archive comprime poco i file di llama-server (1.08x): servirebbe un parser del loro formato.
- Motori OpenAI online: niente stato, niente salvataggi; il masking riduce i token inviati ma la cache del
  fornitore (se c'è) si invalida a ogni pacchetto come con Strata ufficiale.
- Non provato: vLLM vero (stesso percorso del motore OpenAI generico, provato con llama-server forzato a openai).

## Test

    python3 -m unittest discover -s tests -t .      # 135 test (109 + 26 nuovi), OK, 5 saltati come prima
                                                    # PC4070TI, g3 venv (3.14.6): 135 OK, 4 saltati

`tests/test_kvarchive.py`: T0 era calcolato all'import (+10 s); con i 26 test nuovi prima in ordine alfabetico la
suite superava i 10 s e due test di integrazione fallivano. Ora T0 si calcola in setUp.
