# Prova "utente nuovo" del README di mnemonic-proxy — container podman su AI395

Data: 2 novembre 2026 · Esecutore: worker Kanban `t_bd7fad4e` · Nessuna modifica al repository, nessuna PR.

## 1. Come è stata fatta la prova

- Macchina: AI395, podman 5.7.0 senza root. RAM al momento dell'avvio e durante tutta la prova:
  122 GB totali, **79 GB disponibili** (14 GB "liberi" + 75 GB di cache riutilizzabile), sempre molto sopra
  i 15 GB richiesti dalla card. Nessun servizio di Maurizio toccato (`llama-swap`, `llama-server :10005`,
  `ds4-glm-full`, ComfyUI tutti lasciati in esecuzione), nessuna GPU, nessun modello scaricato.
- Container usa e getta, `--memory 6g --cpus 8`, rete solo verso PyPI/GitHub:
  - **C1** `docker.io/library/python:3.10-slim` — pacchetto PyPI (0.3.22), primo contatto senza nulla installato.
  - **C1b** stessa immagine, con `curl` aggiunto (vedi attrito A5), per le richieste HTTP vere e il percorso llama-server.
  - **C2** `docker.io/library/python:3.14-slim` — versione di sviluppo (`main`, commit `369f5b1`) per `--explain-config`.
  - **C3** `python:3.14-slim` con `curl`+`git` — confronto PyPI/`main`, conversazione con mascheramento vero,
    `--explain-config` su cartella con archivio già presente.
- Motore: **sempre il motore finto del pacchetto** (`python3 -m ctxproxy.fake_engine`, flavor `strata` e `llama`).
  Nessun modello, nessun peso, nessuna GPU.
- Log integrali: `container1.log`, `container1b.log`, `container2.log`, `container3.log` in questa cartella
  (copia su AI395: `/mnt/storage/home-mverde/readme-tests/2026-11-02/`). Script eseguiti: `nu1.sh`, `nu1b.sh`,
  `nu2.sh`, `nu3.sh`.
- Versione del README seguita: ramo `main`, commit `369f5b1` (1792 righe). Versione su PyPI al momento della prova: **0.3.22**.
- Container rimossi a fine prova (`--rm`, verificato con `podman ps`); nessuna immagine di sistema cancellata.

## 2. Esito dei cinque percorsi richiesti

### Percorso 1 — T0, `uvx` e `pip`

| comando, copiato tale e quale dal README | esito reale | tempo |
|---|---|---|
| `uvx mnemonic-proxy demo` (riga 28, macchina pulita) | **`uvx: command not found`, exit 127** | 0,0 s |
| `pip install uv` (non è nel README; dedotto) poi `uvx mnemonic-proxy demo` | **PASS**, 5 controlli `[ok]`, recall byte per byte identico | 2,7 s + 1,5 s |
| `python3 -m venv mnemonic-venv && . mnemonic-venv/bin/activate` (riga 67) | ok | 1,6 s |
| `pip install mnemonic-proxy` (riga 68) | ok, 0.3.22 | 0,7 s |
| `mnemonic-proxy demo` (riga 69) | **PASS in 0,5 s** | 0,6 s |

La demo fa esattamente quello che dichiara: archivia `build.log` (181 righe, 8320 caratteri), lo nasconde dietro
una ricevuta di 331 caratteri, il modello finto chiama `recall`, il proxy risponde da sé e il testo torna
identico all'originale. È la parte del README che funziona meglio: un minuto, zero configurazione.
Su Python 3.14 (C2) la demo passa identica.

### Percorso 2 — `check` senza motore

```
mnemonic-proxy check --upstream http://127.0.0.1:8095 --port 8096 --data ./data --config config.json
```
(riga 70, copiato tale e quale) → **NOT READY (exit 1)** con una sola riga:

```
❌ config             config.json: [Errno 2] No such file or directory: 'config.json'
                      -> fix the JSON file
```

Senza `--config`, invece, il messaggio è buono e dice esattamente cosa manca e cosa fare:

```
❌ engine reachable   http://127.0.0.1:8095 does not answer (ConnectionRefusedError)
                      -> start the engine first (llama-server ... --port N) and pass the same URL to --upstream
```

Quindi: **con l'esempio del README il check non arriva nemmeno a guardare il motore** (si ferma sulla config
inesistente); senza `--config` il messaggio è chiaro e azionabile. Vedi attrito A1.

### Percorso 3 — percorso minimo "Strata in two commands" e percorso llama-server

**Strata in due comandi** (righe 86-87), contro `fake_engine --flavor strata`: funziona come dichiarato,
senza nessuna preparazione.

- `./data` creata dal proxy da sé (nessun `mkdir`), finestra letta dal motore: `window from engine n_ctx=131072`,
  soglie riscalate (`mask at 80000 -> 40000`, coda protetta 16000).
- Avvio: `[mnemonic-proxy] 127.0.0.1:8096 -> http://127.0.0.1:8095 (engine strata, saved state off (optional))`
  con la riga che spiega cosa è spento e perché non è un problema.
- Richiesta vera dell'agente: `POST /v1/chat/completions` → **HTTP 200**, risposta completa con il blocco
  `strata_context` (`archived: 1`, `masked: 0`). Secondo turno con un output di 650 righe:
  `prompt_tokens 9027`, `cached_tokens 286` (cache a prefisso funzionante).
- `check` finale: **READY with warnings (exit 2)** — tutto verde tranne l'avviso "saved state supportato dal
  motore ma non abilitato", che il README stesso dichiara facoltativo.
- In C3, con finestra 32768 e una conversazione lunga, il mascheramento è scattato davvero:
  eventi `mask`, `mask_cost`, `mask_measured`, `mask_reuse` nel giornale; archivio con 37 righe in `archive`,
  5 in `masks`, 28 in `chains`.

**Percorso llama-server** (riga 336), contro `fake_engine --flavor llama --n-ctx 32768` con
`config.llama-server-32k.json`: `check` riconosce `llama.cpp`, modello caricato, `window from engine n_ctx=32768`
con soglie `mask at 14000 -> 8000`, `saved state ✅ configured`, `slot_dir ✅ /root/lab/slots exists, writable`;
la richiesta passa (HTTP 200, `tokens=esatto:/tokenize`, `anchor=True`). Due note:

- il file di configurazione **non arriva con `pip`**: va scaricato a mano da GitHub (attrito A2);
- `check --live` fallisce sul confronto `slot_dir` (`the engine saved mnemonic-check-463.bin but it is not in
  /root/lab/slots`): è un limite del motore finto, che non scrive davvero nella cartella. Non è un difetto del
  README; con llama-server vero questo controllo non è verificabile senza modello e resta fuori da questa prova.
- Se il motore llama **non** è stato avviato con `--slot-save-path`, il messaggio è corretto e azionabile:
  `⚠️ saved state configured in the proxy ... but not supported by the engine: the proxy will turn them off ->
  start llama-server with --slot-save-path DIR (same folder as slot_dir)`.

### Percorso 4 — configurazione opencode

Il JSON delle righe 562-591 è stato copiato tale e quale in `$XDG_CONFIG_HOME/opencode/opencode.json` e validato
contro lo **schema ufficiale** `https://opencode.ai/config.json` (71 447 byte, scaricato nel container):

- JSON sintatticamente valido; chiavi radice `$schema, autoupdate, compaction, model, provider`.
- **Validazione jsonschema: 0 errori.**
- Tutti i campi usati dal README esistono nello schema: `compaction.auto` (fra `auto, buffer, keep,
  preserve_recent_tokens, prune, reserved, tail_turns`), `provider.<x>.npm/name/options/models`,
  `models.<id>.name/tool_call/reasoning/limit/cost`, `limit.context/output`, `cost.input/output/cache_read/cache_write`.
- Il prefisso in `"model": "mnemonic/..."` corrisponde al provider dichiarato (`mnemonic`): coerente.
- opencode **non** è stato installato (binario pesante, fuori dal perimetro "solo CPU, nessun modello").

Unico inciampo: `$XDG_CONFIG_HOME` nel container è **vuoto** (attrito A7).

### Percorso 5 — `check --explain-config`

Novità del 0.3.23, **non ancora su PyPI**: con il pacchetto 0.3.22 il comando della riga 1383 risponde
`mnemonic-proxy check: error: unrecognized arguments: --explain-config` (exit 2). Provato quindi da `main`.

**Cartella dati nuova** (config con solo `slot_dir`): funziona esattamente come il README mostra, e meglio —
basta un campo per ottenere il setup raccomandato:

```
auto slot_save = true: the engine saves its state, slot_dir is on this host, and the estimated session file
    (0.6 GiB at 19.5 KB/token ...) fits: 79.6 GiB available
auto mask_anchor = true / auto autosave = true: same reason as slot_save above
auto response_floor = 4096: an eighth of the 32768-token window ...
auto notes_max_tokens = 3072: three thirty-seconds of the 32768-token window ...
READY (exit 0)
```

**Cartella con archivio già presente** (dopo una conversazione vera con mascheramento): il comportamento
promesso al paragrafo "Upgrades keep what was running" è rispettato alla lettera — nulla viene cambiato e
ogni scelta è riportata come `kept` con il valore che il proxy sceglierebbe, il motivo e come applicarla:

```
kept slot_save = false: not applied: this data folder already has conversations, so an upgrade keeps how they ran
    (automatic choice: true, because ...)
    -> write "auto_config": true in the config to apply the automatic choices, or set "slot_save" yourself
... (idem mask_anchor, autosave, response_floor = 0, notes_max_tokens = 2048)
READY with warnings (exit 2)
```

Nel giornale c'è l'evento `auto_config` come dichiarato. Un difetto di leggibilità: attrito A8.

## 3. Attriti, in ordine di gravità

### A1 — GRAVE: il blocco "Install" fa schiantare il proxy con un traceback Python

**Frase del README** (righe 66-72):
```sh
pip install mnemonic-proxy
mnemonic-proxy demo
mnemonic-proxy check --upstream ... --data ./data --config config.json
mnemonic-proxy --upstream ... --data ./data --config config.json
```
Il README **non dice mai di creare `config.json`**, e nessun comando lo crea. Esito reale per chi copia e incolla:

- `check` → `❌ config config.json: No such file or directory -> fix the JSON file` (consiglio inapplicabile:
  non c'è niente da aggiustare, il file non esiste) e, peggio, il check **si ferma lì**: non dice nulla del motore;
- il proxy → **traceback Python nudo**:
  `FileNotFoundError: [Errno 2] No such file or directory: 'config.json'` da `server.py:1461`.

È il punto in cui un utente nuovo si ferma per davvero: il primo comando "serio" del README muore con un errore
da programmatore, dopo che la demo gli ha appena detto PASS.

**Proposta**: togliere `--config config.json` dal blocco Install (il percorso senza config funziona: lo prova il
percorso minimo Strata) e spostare la config in un passo successivo esplicito, "quando vuoi lo stato salvato,
crea `config.json` con: {...}". In più, due correzioni di prodotto: `check` dovrebbe dire
`-> create it (see examples/) or drop --config: the proxy works with the defaults`, e il proxy dovrebbe
stampare lo stesso messaggio in una riga invece di un traceback.

### A2 — GRAVE: `examples/` non esiste dopo `pip install`, ma README e `check` ci mandano

**Frasi del README**: riga 121-122 "Start it as below and copy the config from `examples/`"; tabella righe
126-134, che nomina `config.llama-server.json`, `config.ds4.json`, `config.openai-32k.json`…; e lo stesso
`check` suggerisce `-> for llama-server use examples/config.llama-server.json`.

Esito reale: il wheel contiene solo il pacchetto `ctxproxy` (`packages = ["ctxproxy"]`), quindi dopo
`pip install mnemonic-proxy` **nessun file `config.*.json` esiste sul disco**:
`find / -name 'config.llama-server.json'` → nessun risultato. L'utente deve capire da sé di andare su GitHub
(il link c'è, ma in mezzo alla frase, e `check` non lo dà affatto). Scaricandoli a mano dal raw funzionano
subito (0,4 s, verificato).

**Proposta**: includere `examples/` come dati del pacchetto e aggiungere un sottocomando
`mnemonic-proxy init-config --engine llama-server|strata|ds4|openai-32k [--dir .]` che scrive il file giusto;
nel frattempo, far stampare a `check` l'URL completo
(`https://raw.githubusercontent.com/lastreload/mnemonic-proxy/main/examples/config.llama-server.json`) invece
del percorso relativo `examples/...`, che per chi ha installato da PyPI non esiste.

### A3 — MEDIA: il comando del motore finto nel README non esiste più

**Frase del README** (riga 1729-1730): "also runnable stand-alone with `python3 -m tests.fake_engine --port 18095`".

Esito reale: `ModuleNotFoundError: No module named 'tests'`. Due motivi: `tests/` non viene installato con il
pacchetto, e `tests/fake_engine.py` oggi è solo uno shim di compatibilità (due righe di import) senza blocco
`__main__`. Quindi il comando **non funziona nemmeno dentro il clone del repository**.
Quello che funziona è `python3 -m ctxproxy.fake_engine --port 18095` (con `--flavor strata|llama|ds4|openai|
router|ollama|lmstudio`, `--n-ctx`, `--no-slot-save`).

**Proposta**: correggere la riga in `python3 -m ctxproxy.fake_engine --port 18095` e citarla **anche nella
sezione Install**: è l'unico modo di provare `check`, il percorso minimo e `--explain-config` senza un motore
vero, ed è esattamente quello che serve a chi vuole capire il proxy prima di investire in un modello.

### A4 — MEDIA: non si può distinguere la versione di sviluppo da quella di PyPI

**Frase del README** (riga 256-257): "The development version (the `main` branch, newer than the last PyPI
release): `pip install git+https://github.com/lastreload/mnemonic-proxy`".

Esito reale: `main` dichiara ancora `version = "0.3.22"` in `pyproject.toml`, identica a PyPI. Dopo
l'installazione da git, `pip show` e `mnemonic-proxy --version` dicono entrambi **0.3.22**, ma solo una delle
due ha `--explain-config` (su PyPI: `unrecognized arguments: --explain-config`). Un utente che segue la sezione
`--explain-config` e vede quell'errore non ha modo di capire se ha la versione giusta.

**Proposta**: su `main` portare la versione a `0.3.23.dev0` (il CHANGELOG già dice "0.3.23 — unreleased"), e
nel README marcare la funzione con "da 0.3.23 (per ora solo da `main`): verifica con `mnemonic-proxy --version`".

### A5 — MEDIA: prerequisiti non dichiarati per i due comandi di installazione

Su una macchina davvero pulita:

- `uvx mnemonic-proxy demo` (riga 28, **il primo comando del README**) → `uvx: command not found`, exit 127.
  Il README linka la pagina di uv ma non dà la riga per installarlo.
- `pip install git+https://github.com/lastreload/mnemonic-proxy` (riga 257) →
  `ERROR: Cannot find command 'git' - do you have 'git' installed and in your PATH?`. Il `git` serve ed è
  nominato solo nei prerequisiti di T1 (riga 278), che un lettore del percorso "versione di sviluppo" non legge.
- `curl` non c'è in `python:*-slim`: lo usano la verifica `curl -s localhost:8095/health` (riga 325) e il
  download del modello (riga 311), ed è nei prerequisiti T1, ma non in Install.

**Proposta**: una riga sotto il primo blocco — "senza uv: `pip install uv` (o
`curl -LsSf https://astral.sh/uv/install.sh | sh`)" — e, accanto al comando `git+`, "(richiede `git`)".

### A6 — MEDIA: `READY with warnings (exit 2)` non è documentato

**Frase del README** (riga 74): "`check` ends with `READY` (exit 0) or says, line by line, what to change".

Esito reale: nel percorso minimo Strata, con tutto in ordine e lo stato salvato volutamente spento,
`check` termina con **`READY with warnings (exit 2)`**. Chi controlla "exit 0" (o uno script di avvio) legge
un fallimento dove non c'è nulla da correggere; e il README non nomina mai il codice 2.

**Proposta**: scrivere i tre esiti nella riga 74: "`READY` (exit 0), `READY with warnings` (exit 2: funziona,
ma qualcosa di facoltativo è spento), `NOT READY` (exit 1: va corretto)".

### A7 — MINORE: `$XDG_CONFIG_HOME` quasi mai definito

**Frase del README** (riga 558): "Put this in `$XDG_CONFIG_HOME/opencode/opencode.json` (global)".
Nel container (e su molti sistemi desktop) la variabile è **vuota**: chi copia il percorso tale e quale finisce
su `/opencode/opencode.json`, dove non ha permessi, e il file non viene letto da opencode.

**Proposta**: scrivere `~/.config/opencode/opencode.json` (oppure `${XDG_CONFIG_HOME:-~/.config}/opencode/...`).

### A8 — MINORE: la riga "auto config" di `check` diventa illeggibile sulle cartelle esistenti

Su cartella con archivio, la riga `ℹ️ auto config` è un paragrafo di circa mille caratteri che ripete quattro
volte la stessa frase ("not applied: this data folder already has conversations, so an upgrade keeps how they
ran") una volta per campo. Il dettaglio è giusto e c'è già, per esteso e ben formattato, nel blocco
`automatic configuration` sotto.

**Proposta**: nel riepilogo una riga sola — "5 scelte non applicate: questa cartella dati ha già conversazioni
(vedi `--explain-config`)" — e lasciare i motivi al blocco esteso.

### A9 — MINORE: suggerimento fuori contesto nel percorso Strata

Con motore Strata correttamente rilevato, `check` stampa comunque
`ℹ️ config no --config: defaults (saved-state features off) -> for llama-server use examples/config.llama-server.json`.
Un utente Strata che ha appena seguito "Strata in two commands" si chiede se deve installare llama-server.

**Proposta**: condizionare il suggerimento al motore rilevato (per Strata: "nessuna config necessaria; per lo
stato salvato aggiungi solo `slot_dir`").

## 4. Cosa invece non ha fatto inciampare nessuno

- La demo `uvx`/`pip` (T0): un minuto, PASS, nessuna configurazione. È il miglior biglietto da visita.
- Il percorso minimo "Strata in two commands": `./data` creata da sola, finestra letta dal motore, richiesta
  200 al primo colpo, nessun `mkdir`, nessuna config. Promessa mantenuta alla lettera.
- I messaggi di `check` **quando non c'è di mezzo un `--config` inesistente**: ognuno dice cosa manca e il
  comando per rimediare (motore spento, `--slot-save-path` mancante, finestra troppo piccola).
- Il JSON di opencode: valido e completo, 0 errori contro lo schema ufficiale.
- `--explain-config`: fa esattamente quello che dichiara, sia su cartella nuova (`auto …` + motivo) sia su
  cartella con archivio (`kept …` + motivo + come applicare), con l'evento `auto_config` nel giornale.
- Python 3.14: installazione, demo, proxy e check identici al 3.10.

## 5. Riepilogo in una riga

Il *percorso di prova* (demo) e il *percorso minimo* (Strata in due comandi) sono ormai facili e onesti; chi si
perde è l'utente che segue il blocco **Install** alla lettera, perché quel blocco chiede un `config.json` che
nessuno gli ha detto di creare (traceback Python) e lo manda in una cartella `examples/` che `pip` non installa.
Sistemati A1 e A2 — una riga di README e un `init-config` — la lamentela "a nightmare to configure" perde il suo
appoggio principale.
