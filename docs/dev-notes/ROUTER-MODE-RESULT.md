# mnemonic-proxy 0.3.7 — llama-server in router mode (issue #12): risultato
Card t_a23ce44a, 19/10/2026. Host MVLINNA, **solo CPU** (nice 19, 2 thread per modello). Niente GPU, niente
PC4070TI, niente AI395. Log completi: `~/projects/flash-next-local/workspaces/proxy-ideas/out/router-mode/`.

## In breve
- **Prima (main 6fc8824) il router era rotto, non solo "generico".** Il proxy lo prendeva per un llama-server a
  modello singolo (`/props` del router ha `default_generation_settings` con `n_ctx: 0`), con finestra di
  configurazione 131072, salvataggi spenti (`/slots` senza modello → 400). Con `engine_tokenize` **tutte** le
  richieste falliscono (`/tokenize` senza modello → 400 → 500 al client: 7 su 7). Senza, il proxy manda prompt più
  grandi della finestra del modello: **5 richieste su 5 respinte dal motore** con `exceed_context_size_error`
  (34196 token contro 32768, 24546 contro 16384).
- **Dopo (ramo `router-mode-012`, 0.3.7)**, stessa sequenza, stesso router, stessi modelli: **5 su 5 richieste
  valide servite, 0 sforamenti** (0 righe `exceeds the available context` nel log del motore), finestra giusta per
  ogni modello prima di costruire il prompt, salvataggi accesi e legati al modello, i due casi d'errore respinti dal
  proxy con un messaggio chiaro senza mandare nulla al motore.
- Suite verde: 489 test su Python 3.14, 456 su Python 3.10 (i moduli `kv_archive` richiedono 3.14), 17 nuovi in
  `tests/test_router.py`. CI GitHub: vedi PR.

## Motore e modelli
- llama.cpp release **b11723** (commit `31288c9c6`), binario ufficiale `llama-b11723-bin-ubuntu-x64.tar.gz`
  (sha256 `8f5652ca…d069`, verificato col digest della release), in `~/.hermes/cache/scratch/llama-router/`.
- Modelli (`ggml-org/models`, `tinyllamas/`): `stories260K.gguf` (1,1 MB) e `stories15M-q4_0.gguf` (18,2 MB).
  Il contesto di addestramento è 128 (stories15M) / 512 token: llama-server **taglia `-c` al contesto di
  addestramento** (`the slot context (1024) exceeds the training context of the model (128) - capping`). Per avere
  finestre ≥ 16K (minimo del proxy) il preset usa `override-kv = llama.context_length=int:N`. I modelli producono
  testo senza senso a queste lunghezze: la prova misura finestre, salvataggi e instradamento, non la qualità.
- Preset (router avviato con `--models-preset preset.ini --parallel 1 --slot-save-path …`):

  | modello | file | ctx-size | n_ctx reale dello slot |
  |---|---|---|---|
  | `tiny-a` | stories260K | 32768 | 32768 |
  | `tiny-b` | stories15M-q4_0 | 16384 | 16384 |
  | `tiny-broken` | file inesistente | 16384 | — (caricamento fallisce) |

## API del router verificata sul binario (differenze dal README di llama.cpp)
- `GET /props` senza modello: props del router, `"role": "router"`, `"model_path": "none"`,
  `default_generation_settings: {"params": null, "n_ctx": 0}`. Con `?model=` e modello non caricato e
  `autoload=false`: 400 `model is not loaded`.
- `GET /models` (= `/v1/models`): `status.value` unloaded|loaded, `status.args` (riga di comando dell'istanza, da
  cui si legge `--slot-save-path` senza caricare nulla), `meta.n_ctx` **solo quando caricato**.
  **`context_length` non è affidabile**: vale il contesto di addestramento del GGUF (2048 per stories260K, 128 per
  stories15M), non il `ctx-size` del preset né quello reale. Il proxy non lo usa.
- `GET /slots`, `/metrics`, `/props` senza `?model=`: 400 `model name is missing from the request`.
  `GET /slots?model=X` (autoload di default) **carica il modello** e risponde con lo slot (`n_ctx` reale).
- POST: il modello va nel corpo (`"model"`); `?model=` nella query **non** basta per `/slots/0?action=save`
  (400). `?autoload=false` funziona anche sui POST.
- `POST /tokenize` senza `"model"`: 400. Con `"model"`: tokenizer di quel modello (stessa frase: 3001 token con
  tiny-a, 2101 con tiny-b — i conteggi vanno tenuti per modello).
- `POST /models/load|unload`: `{"success": true}`; doppio unload: 400 `model is not running`; modello
  sconosciuto al load: 404; caricamento fallito: 500 `model name=tiny-broken failed to load`.
- **Il motore ripristina senza protestare un file di slot salvato da un altro modello** (`restore` di
  `probe-a.bin`, salvato da tiny-a, con `"model": "tiny-b"` → 200, `n_read: 36`). Il controllo deve stare nel proxy.
- Non provati: `/models/sse` (non serve), `sleeping`, `--models-max`, `--sleep-idle-seconds`.

## Cosa fa il proxy (0.3.7)
1. **Riconoscimento**: `/props` con `role: router` (o, per build vecchie, `model_path: none` e n_ctx 0) + `/models`
   con oggetti `status` → motore `llama.cpp`, `router=True` (anche con `engine: llama.cpp` forzato). Il
   rilevamento non carica nessun modello. Salvataggi accesi solo se **ogni** modello ha `--slot-save-path`.
2. **A ogni richiesta, prima di costruire il prompt** (sotto il lock del proxy): `GET /models` → modello della
   richiesta (id o alias; altrimenti `router_model` della configurazione; altrimenti 400 chiaro). Se non è caricato,
   o è stato ricaricato, o il suo `meta.n_ctx` è cambiato: `GET /slots?model=…&autoload=true` (lo carica, attesa
   fino a `router_load_timeout_s`, di serie 600 s) → n_ctx dello slot. Finestra = n_ctx (o `window` della
   configurazione se più piccola); soglie non scritte in configurazione scalate dai valori di partenza con la stessa
   formula di `engines.apply`. n_ctx illeggibile → 503 chiaro, **niente invio** con la finestra di configurazione.
3. **Ogni chiamata al modello della richiesta**: chat (anche chiamate interne: note, ancora, recall), `/tokenize`
   (cache dei conteggi per modello), `/slots/0?action=save|restore` con `"model"` nel corpo e `autoload=false`.
4. **Salvataggi legati al modello**: `<data>/router-slot-models.json` (file → modello). Restore con un altro
   modello, o di un file non salvato dal proxy in router mode → 409 prima del motore. L'autorestore sceglie solo
   file del modello corrente (evento `autorestore_other_model` quando ne scarta uno che combacerebbe); un
   autosalvataggio non cancella come "superato" il file di un altro modello.
5. **Scaricamento/ricaricamento**: stato del motore per il Tracker = `modello#generazione`; un modello visto non
   caricato passa alla generazione successiva → stato perso (`strata_state_lost`), nessuna ipotesi di cache calda,
   ripristino solo da un file dello stesso modello. Nessun thread in più, niente SSE.
6. **Percorso di default invariato**: Strata, ds4, llama.cpp a modello singolo e OpenAI generico non passano dal
   codice nuovo (summary del motore e journal identici; `autorestore_other_model` non compare mai fuori dal router).

## Prova vera: sequenza e numeri
Script `run_router.py`: una conversazione di 60 giri di strumenti (~83K caratteri; ~43K token con tiny-a, ~20K con
tiny-b), poi passi A → B → A → scaricamento di A → A → modello sconosciuto → modello rotto → B. `max_tokens` 32.
Configurazione: `examples/config.llama-server.json` + autosalvataggio rapido (`autosave_idle_s` 5,
`autosave_poll_s` 1, `autosave_min_tokens` 500, `autorestore_min_gain` 200). Nessuna `window` in configurazione.

### main 6fc8824 (run0-main)
| passo | con `engine_tokenize` | senza |
|---|---|---|
| 1 A | 500 `tokenize HTTP 400` | 400 motore: 34196 > 32768 |
| 2 B | 500 | 400 motore: 24546 > 16384 |
| 3 A | 500 | 400 motore: 34316 > 32768 |
| 4 A dopo scaricamento | 500 | 400 motore: 34378 > 32768 |
| 5 modello sconosciuto | 500 | 400 del motore (`model 'gpt-4o' not found`) |
| 6 modello rotto | 500 | 500 del motore |
| 7 B | 500 | 400 motore: 24663 > 16384 |
Avvio: `engine llama.cpp, saved state no … window=131072`.

### ramo 0.3.7 (run3-branch)
| passo | esito | finestra | prompt token | riusati | tempo |
|---|---|---|---|---|---|
| 1 A (carica tiny-a) | 200 | 32768 | 19892 (dopo masking da 42842 stimati) | 16466 (ancora) | 17,7 s |
| 2 B (carica tiny-b) | 200 | 16384 | 3223 (cambio di segmento: 16340 stimati non entravano) | 33 | 32,9 s* |
| 3 A | 200 | 32768 | 5720 | 51 | 1,6 s |
| scarica A | 200 | | | | |
| 4 A (ricaricato, generazione 2) | 200 | 32768 | 5782 | **5720** (autorestore del file di A) | 0,4 s |
| 5 `gpt-4o` | 400 dal proxy, nulla inviato | | | | 0,0 s |
| 6 `tiny-broken` | 503 dal proxy, nulla inviato | | | | 0,02 s |
| 7 B | 200 | 16384 | 3340 | 3223 (autorestore del file di B) | 0,3 s |
\* 30 s sono la scrittura delle note di passaggio (1024 token di tiny-b su CPU).

- Sforamenti: **0** (log del motore), `window_guard` violazioni 0.
- Lettura n_ctx: 42 ms (tiny-a, compreso caricamento), 180 ms (tiny-b), 41 ms dopo lo scaricamento.
- Salvataggi (`router-slot-models.json`): 6 file, ognuno col suo modello. Passo 3 (A): scartato il file di B che
  combaciava (`autorestore_other_model`), nessun restore. Passo 7 (B): scartato il file di A, restore del file di B.
  Passo 4: restore del file di A dopo lo scaricamento (`n_restored` 5751, 2,5 ms).
- `check`: READY; "router mode: models loaded on demand"; finestra letta per richiesta.

### Difetto trovato e corretto durante la prova (run2)
Nella seconda corsa l'autosalvataggio di A ha cancellato come "superato" il file di B (il testo di B era prefisso
di quello di A: stessa conversazione). Nessun ripristino sbagliato, ma B perdeva il suo stato. Corretto
(`AutoSaver.prune` salta i file di altri modelli), test `test_prune_keeps_other_models_files`, verificato in run3
(il file di A resta dopo l'autosalvataggio di B). Log in `run2-branch-prune-bug/`.

## Test
- `tests/test_router.py` (17 test, contro `FakeRouter` in `ctxproxy/fake_engine.py`: ogni modello è un
  FakeEngine llama avviato al caricamento e fermato allo scaricamento, file di slot condivisi come sul motore vero,
  finestra stretta che respinge ogni richiesta oltre n_ctx): rilevamento (anche forzato, senza
  `--slot-save-path`, modello singolo invariato), finestra che segue il modello, passaggio a un modello più piccolo
  senza sforare, chiamate interne e `/tokenize` al modello giusto, modello sconosciuto/`router_model`, scaricamento
  fra due richieste, stato con `autoload=false`, ricaricato con altro n_ctx senza essere visto scaricato, n_ctx ignoto
  → errore chiaro, restore rifiutato fra modelli, file sconosciuto, autorestore solo stesso modello, pulizia che non
  tocca i file di altri modelli.
- Controprova (mutazioni): togliere il rifiuto del restore fa fallire 3 test; togliere la finestra per modello ne
  fa fallire 3 (compreso "nessuno sforamento"); togliere le generazioni ne fa fallire 2; togliere la correzione della
  pulizia ne fa fallire 1.
- CI: nuovo passo `check` contro `python -m ctxproxy.fake_engine --flavor router`.

## Limiti (non provato)
- Modelli veri grandi (caricamenti di minuti: durante il caricamento il proxy non manda keep-alive a un client in
  streaming), GPU.
- `--parallel` > 1 (il proxy usa lo slot `slot_id`; l'n_ctx è quello dello slot), `--models-max` con espulsioni
  sotto carico, `sleeping` / `--sleep-idle-seconds`, `/models/sse`.
- `check --live` in router mode: non implementato (avviso).
- Un modello scaricato e ricaricato **con lo stesso n_ctx** fra due richieste senza che il proxy lo veda scaricato:
  la generazione non cambia; il contatore dello slot riparte e il controllo esistente del Tracker dovrebbe vederlo
  come richiesta estranea (stato perso). Non provato né dal vivo né con un test dedicato.
- Lo stesso discorso dei motori a modello singolo: col tokenizer del motore il conteggio usa il template del proxy,
  non quello del modello (margine `window_margin`).
- `--tokenizer` locale in router mode: un solo tokenizer per tutti i modelli (sbagliato se i modelli differiscono:
  usare `engine_tokenize`).
