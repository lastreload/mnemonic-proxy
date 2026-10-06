# LIVE020 — mnemonic-proxy 0.2.0 dal vivo su PC4070TI + verifiche di qualità su GPU

Card t_7f685153, notte 5→6/10/2026. Tutti i numeri qui sotto vengono da run reali (giornali e output in `live020/`
in questa cartella e in `/data/mnemonic-proxy/` su PC4070TI).

## In breve
- 0.2.0 è installato dal tag `v0.2.0` (f97a9ef) ed è **acceso su 8096** come servizio utente `mnemonic-proxy`
  (abilitato all'avvio). Il proxy vecchio è fermo, disabilitato e intatto. Strata è sano: PID 3672161, mai riavviato.
- **Claude Code**: 1 h 41 min, 9 passi, 339 richieste, 0 errori HTTP del proxy. Su 11,36 M token di prompt, il
  **92,6 % è stato ripreso dalla cache**. Ci sono stati 28 pacchetti di mascheramento, **14 cambi di segmento** e
  22 `recall`. Contesto virtuale massimo 328 K su un contesto fisico massimo di 45,8 K.
- **Codex** (Responses): 3 passi, 20 richieste, 93,9 % ripreso dalla cache. `recall` ha risposto in modo esatto
  (36 test, 8/8 messaggi). Però gli strumenti di Codex non sono nella base del paging (vedi problemi 1–2).
- **Fatti sparsi a 16–120 K (QSA, kv int8)**: 151/160 fatti esatti, **0 sbagliati**. Gli errori sono tutti
  *omissioni* (una riga saltata). Da 96 K in su: 60/64.
- **Ragionamento vecchio**: le domande su cose visibili non cambiano (7/7 in ogni variante). Le domande la cui
  risposta sta *solo* nel ragionamento: intero 3 esatte + 1 parziale su 5 → nascosto/tolto 2 esatte su 5. Il
  modello non dice "non lo so": ricostruisce dal file i valori **finali** e li spaccia per la stima originale.
- Il problema più serio visto dal vivo: in 3 scritture su 140 il modello ha copiato nel file la forma
  "prime righe + `\n…`" degli argomenti mascherati. È il problema 3, e ha causato un `NameError` reale.

## 1. Installazione
| voce | valore |
|---|---|
| sorgente | `git clone --branch v0.2.0 https://github.com/lastreload/mnemonic-proxy` → `/data/mnemonic-proxy/src` (f97a9ef) |
| venv | `/data/mnemonic-proxy/venv`, Python 3.14.6 (uv), `pip install ./src[exact]`. `compression.zstd` e `tokenizers` OK |
| config | `/data/mnemonic-proxy/cfg.json`: quella del README per llama-server/Strata con salvataggi (window 131072, mask_reasoning/tool_args, mask_anchor, slot_save+seal, notes 12288, response_floor 16384, autosave, recall_struct/flex/multi, auto_recall_hint, tools_paging). In più `tools_core` include anche i nomi di Claude Code e Codex |
| dati | `/data/mnemonic-proxy/data` (nuova). Slot di Strata: la cartella esistente `/data/strata-0138-live/sessions` (è quella di `--slot-save-path`, non ce n'è un'altra) |
| servizio | `~/.config/systemd/user/mnemonic-proxy.service` (MemoryMax 8G, Nice 5, `--tokenizer` sulla cartella tokenizer del pack: conteggio esatto) |
| `check` | non esiste nella 0.2.0 (arriva con la 0.2.1, card t_9eaa630b). Verifica a mano: `/v1/strata/engine` → `kind strata, slot_save true, detected true`; `/v1/messages` di prova → risposta in 2,5 s |
| `kv_archive` | **spento di proposito**. Nella cartella slot ci sono i file della sessione di Maurizio (`caeaa81de2522-*`) e l'archiviatore li comprimerebbe cancellando l'originale (`kvarchive.candidates`). Si può riaccendere solo con una cartella slot dedicata |

Backup (nulla è stato cancellato):
- `/data/ctx-proxy/cfg.json.bak-mnemonic020-20261005`
- `/data/ctx-proxy/data/archive.sqlite.bak-mnemonic020-20261005` (copia coerente)
- `/data/ctx-proxy-bak-mnemonic020-20261005/` (13 MB: data, proxy, cfg, unità systemd)
- `.../sessions-hardlinks/` con i 3 file `caeaa81de2522-seg2-*.bin` (3,1 GB) protetti come hard link.

Lock GPU: `/data/.flash-next-gpu.lock` è tenuto con flock dal server Strata stesso (PID 3672155, per
tutta la sua vita). Copre quindi ogni richiesta a Strata e non ho preso un secondo lock. Ho mandato un solo
client alla volta, sempre in sequenza: Claude Code → Codex → 2a → 2b.

## 2. Sessione lunga con Claude Code (Anthropic Messages)
**Montaggio.** Claude Code 2.1.284 su PC4070TI, `claude -p --resume` per 9 passi in fila (`cc_live.sh`, prompt in
`live020/prompts/`). Variabili: `ANTHROPIC_BASE_URL=http://127.0.0.1:8096`, `DISABLE_AUTO_COMPACT=1`,
`--max-turns 60`. Il progetto `magazzino` (libreria + CLI Python per un magazzino ricambi) sta in
`/data/mnemonic-proxy/work/cc1`.

Per far scattare i segmenti in un'ora e mezza ho usato una **config di prova** (`cfg-livetest.json`, finestra
64000, mask_trigger 36000, mask_target 26000) su un'istanza a parte con dati separati (`data-livetest/`). La
logica è la stessa della config di produzione, con le soglie più basse.

| misura | valore |
|---|---|
| durata | 1 h 41 min (23:30 → 01:11), 9 passi |
| richieste | 339 (332 `tool_calls`, 7 `stop`). **0 errori HTTP** del proxy |
| token di prompt | 11.362.493, di cui **ripresi 10.521.490 (92,6 %)** e calcolati 841.003 |
| generati | 194.816 |
| tempo motore | 6421 s in tutto, di cui prefill 654 s |
| contesto | fisico max 45.828, **virtuale max 328.341**, 222 messaggi nel prompt fisico |
| mascheramento | 28 pacchetti, 187.429 token tolti (misurati). Ancore: 23 salvate, 11 ripristinate, 5 saltate |
| segmenti | **14 cambi**. Note 46–140 s (mediana ~75 s), 2.000–5.100 token. Un caso anomalo: seg 13, note di 0 token (risposta di 124 token, `finish stop`), ma lo switch ha usato comunque 2.408 token di note |
| invalidazioni della cache | 26 "blocco" (pacchetto), 14 "segmento", 1 "client". Letture più grandi ≈ 32–35 K token in 13–14 s, sempre dopo un cambio di segmento |
| `recall` | 22 chiamate, 7 tagliate per mancanza di spazio (`recall_trimmed`, room fino a −6056) |
| altri eventi | 1 `branch` (Claude Code ha riscritto un messaggio), 1 `tools_search`, 1 autosave a fine sessione |

Passi 5, 6 e 7 si sono chiusi per `error_max_turns`: è il tetto di 60 turni che ho messo io al client, non un
errore del proxy. Il passo successivo ha ripreso la sessione normalmente.

**Memoria a lungo termine.** Al passo 8 il modello ha risposto a memoria, al passo 9 ha controllato con
`recall`/file:
- metodi di `Archivio`: corretti in parte (ha inventato `esiste()`);
- numero di test alla prima esecuzione: sbagliato (ha detto "14, tutti verdi", erano 0 e poi 22);
- 12 articoli di esempio: 6 su 12;
- problema di concorrenza: esatto.

Al passo 9 ha corretto da solo tutti e quattro i punti e ha trovato **4 bug reali**. Uno è `NameError: ultimo`
in `prezzo_da_csv`, prodotto dal problema 3. In sintesi la memoria "a mente" dopo 14 segmenti è debole, mentre
quella tramite `recall` funziona.

## 3. Codex (OpenAI Responses), breve
Codex 0.160.0 gira su MVLINNA e passa per un tunnel SSH locale `127.0.0.1:18096 → PC4070TI:8096` (nessuna porta
pubblica). `CODEX_HOME` è dedicato (`live020/` non contiene credenziali), con `wire_api="responses"` e auto-compact
di fatto spento. Ha usato la config di **produzione**.

| passo | compito | richieste/comandi | input (di cui in cache) | output |
|---|---|---|---|---|
| 1 | `orari.py` + test | 5 comandi | 94.454 (88.314) | 4.171 |
| 2 | estensione (secondi, CLI) | 3 comandi | 173.794 (164.536) | 12.047 |
| 3 | domanda sul passo 1 senza rileggere i file | 4 `recall` | 233.182 (221.114) | 13.376 |

Lato proxy: 20 richieste, 351.288 token di prompt, **93,9 % ripresi**. Al passo 3 la risposta è esatta: 36 test
alla v1 e 8/8 messaggi `ValueError` parola per parola, presi dall'archivio con gli id giusti. Test finali: 51,
tutti verdi.

## 4. Fatti sparsi vicino a 128 K (direttamente a Strata, senza proxy)
Script `facts_bench.py`: 16 assegnazioni `Nome=numero` sparse in una storia italiana generata (luoghi, mestieri,
distrattori). La domanda viene alla fine, temperatura 0, thinking spento, `max_tokens` 300, cache azzerata per
ogni richiesta (`cache_n` 0). Il modello è `qwen3.8-flash-next-iq3_xxs-strata0138`, cioè Strata 0.1.38 (sorgente
`strata-session-0138` @ 8d8f664, server `strata-ab/Strata` @ d9ab843) con `--kv int8 --kv-resident 32768`,
`--spec 4` e MTP.

| lunghezza | prompt (seed 1 / 2) | esatti s1 | esatti s2 | sbagliati | TTFT s | prefill tok/s | decode tok/s |
|---|---|---|---|---|---|---|---|
| 16 K | 16.341 / 16.477 | 16 | 16 | 0 | 6,6 / 7,3 | 2.498 / 2.273 | 48 / 98 |
| 32 K | 32.709 / 32.642 | 16 | 16 | 0 | 12,7 / 12,6 | 2.611 / 2.611 | 68 / 106 |
| 64 K | 65.359 / 65.261 | 16 | 15 (manca Fabio) | 0 | 25,4 / 25,4 | 2.601 / 2.593 | 88 / 98 |
| 96 K | 97.962 / 98.065 | 15 (manca Oscar) | 14 (mancano Elena, Irene) | 0 | 38,6 / 38,6 | 2.557 / 2.565 | 87 / 96 |
| 120 K | 122.389 / 122.458 | 15 (manca Giulia) | 16 | 0 | 49,0 / 48,9 | 2.520 / 2.528 | 95 / 91 |

Tutte le risposte sono "Nome=numero" e non c'è mai un numero sbagliato: quando sbaglia, il modello salta una
riga. Il picco di VRAM è 11.613 MiB e resta piatto (KV residente 32 K; il resto va in RAM). Il picco di RSS del
processo Strata è 44,7–45,1 GiB, anche questo piatto. Il prefill è stabile a ~2,5 K tok/s fino a 122 K.
Con 2 seed i campioni sono pochi: la tendenza (omissioni oltre 64 K) è plausibile ma non è misurata con
precisione.

## 5. Ragionamento dei turni vecchi: intero / ricevuta / tolto
Script `reasoning_bench.py`. Conversazione reale: la sessione pi `prova-paging` (= conv `caeaa81de2522`,
gioco tower defense), prefisso dei primi 76 messaggi rigiocato su Strata. Le ultime 2 risposte assistant tengono
sempre il ragionamento. Temperatura 0, una domanda per richiesta. Le risposte attese vengono dall'archivio
(messaggi 20, 38, 39, 61–73).

| variante | ragionamento inviato | prompt | domande "solo ragionamento" R1–R5 | domande visibili V1–V7 |
|---|---|---|---|---|
| intero | 18.560 tok | ~61.070 | **3 esatte + 1 parziale** (R1, R2, R4, R5; R3 parziale) | 7/7 |
| ricevuta 0.1 `[ragionamento omesso: N token — recall:rid]` | 1.650 | ~44.520 | **2 esatte** (R4, R5) + 2 parziali | 7/7 |
| tolto (0.2.0) | 1.165 | ~43.670 | **2 esatte** (R4, R5) + 1 parziale | 7/7 |

- R4 (namespace del server di sviluppo) e R5 (boss all'ondata 10) restano esatte anche senza ragionamento,
  perché lo stesso fatto compare nel testo o nell'uscita di uno strumento.
- R1–R3 vivono **solo** nel ragionamento: 240 oro iniziale stimato, Cannone 12 danni / 0,75 s ≈ 16 DPS,
  ondata 1 con 8 grunt da 55 HP. Senza ragionamento il modello prende dal file i valori **finali** (265 oro,
  15 / 0,70 ≈ 21,4 DPS, 58 HP) e li presenta come stima originale. È una risposta sicura e sbagliata, non un
  "non lo so".
- La ricevuta non aiuta senza `recall`: nel bench non c'era il ciclo del proxy, quindi il modello non poteva
  usare il rid. Il suo vantaggio sta tutto nella possibilità di fare `recall`, che qui non è misurata.
- Costo dell'intero: +17.400 token di prompt (+40 %). TTFT a cache calda 3,4 s contro 3,0–3,2 s; a freddo
  26,0 s contro 15,5 s.

## 6. Problemi trovati e proposte (nessuna modifica al repo)
1. **Paging degli strumenti per Codex 0.160**: lo strumento shell di Codex si chiama `exec_command` (più
   `write_stdin`), non `shell`. Con `tools_paging` il prompt fisico conteneva solo `recall` e `tools`, e la
   prima richiesta è finita con `tool_not_loaded exec_command`. Proposta: nella 0.2.x aggiungere a `tools_core`
   di serie `exec_command`, `write_stdin`, `apply_patch`, `update_plan`, oppure non paginare quando il client
   dichiara meno di N strumenti.
2. **`apply_patch` in Codex**: il modello l'ha chiamato e Codex ha risposto `unsupported call: apply_patch`
   (poi ha ripiegato su heredoc). Ipotesi: Codex 0.160 non dichiarava `apply_patch` in questa sessione
   (`approval never`/sandbox) e il modello l'ha preso dal prompt di sistema; oppure il tipo `custom` non torna
   come `custom_tool_call`. Proposta: test con un Codex reale che registri `tools` in ingresso e il tipo
   dell'item restituito.
3. **Imitazione degli argomenti accorciati** (`masked_calls`: prime 160 caratteri + `\n…`): in 3 `Write`/`Edit`
   su 140 il file scritto finiva con una riga `…`. `receipt_guard` non le blocca, perché `_FAKE_OMIT` riconosce
   solo i segnaposti fra parentesi quadre. Effetto reale: `prezzo_da_csv` troncata → `NameError`. Proposte:
   (a) `receipt_guard` blocca anche write/edit il cui contenuto termina con una riga fatta solo di `…`;
   (b) negli argomenti mascherati non tenere la testa del contenuto, ma solo nome, percorso e dimensione
   (la nota con `recall` resta nel risultato).
4. **`recall_trimmed` con spazio negativo** (fino a −6.056 token, 7 casi su 22): il risultato viene tagliato
   quando la finestra è quasi piena, cioè proprio quando serve di più. Proposta: contare il risultato di
   `recall` nel controllo di finestra e, se non ci sta, anticipare il cambio di segmento invece di tagliare.
5. **Note di segmento vuote** (seg 13: 0 token e 124 token generati in 3,8 s): il fallback ha funzionato (2.408
   token di note), ma il giornale non dice da dove venivano. Proposta: un evento `notes_fallback` con la causa.
6. **File di sessione che si accumulano**: la prova ha lasciato ~20 GB di `c5c7ee0681130-seg*-A.bin` e
   `-anchor*.bin` nella cartella slot. `autosave_max_gb` limita solo gli autosalvataggi; seg-A e ancore
   superate restano (sul disco ci sono 324 GB liberi, quindi nessun rischio immediato). Proposta: un tetto
   unico in GB per tutti i file del proxy, con pulizia dei più vecchi delle conversazioni non attive.
   I file **non** sono stati cancellati: si possono togliere a mano (`c5c7ee0681130-*` sono solo della prova;
   non toccare `caeaa81de2522-*`).
7. **Il ragionamento tolto costa esattezza sulle domande "perché/quanto avevi stimato"** (sezione 5). Proposte:
   tenere intero il ragionamento delle ultime K risposte, con K misurato e non 2; oppure mettere nel prompt di
   sistema del proxy un'avvertenza del tipo "il tuo ragionamento vecchio non è visibile: per le tue stime
   passate usa recall". Da misurare con il ciclo del proxy attivo.
8. Claude Code segnala `unrecognized_model local-model`. Funziona lo stesso. Il README potrebbe suggerire un nome
   riconosciuto (`ANTHROPIC_MODEL=claude-…`) per avere i limiti di output giusti.
9. `mnemonic-proxy check` manca nella 0.2.0: già previsto nella 0.2.1.

## 7. Stato finale e come tornare indietro
- Acceso: `mnemonic-proxy.service` (utente) su 127.0.0.1:8096 → Strata 8095, config di produzione
  `/data/mnemonic-proxy/cfg.json`, dati `/data/mnemonic-proxy/data`.
- Fermi: `ctx-proxy.service` (vecchio, disabilitato), `mnemonic-livetest` (prova). Dashboard 8097: invariata.
- Strata: PID 3672161 e 3672155 attivi da oltre un giorno e mai riavviati. `/health` ok. Il lock GPU resta al
  server Strata.
- Ritorno al vecchio proxy:
  ```
  systemctl --user disable --now mnemonic-proxy
  systemctl --user enable --now ctx-proxy
  ```
  Usa la stessa porta 8096 e i dati vecchi in `/data/ctx-proxy/data`, che non sono stati toccati
  (archivio e sessione `caeaa81de2522` intatti; backup nei `*.bak-mnemonic020-20261005`).

## File
- In questa cartella `live020/`: `cfg.json`, `cfg-livetest.json`, `cc_live.sh`, `prompts/`, `steps.log`,
  `cc-journal.jsonl` (giornale della sessione Claude Code), `journal-summary.txt`, `facts_bench.py`,
  `reasoning_bench.py`, `jsum.py`.
- Su PC4070TI `/data/mnemonic-proxy/`: `logs/cc1/step*.jsonl` (stream di Claude Code),
  `results/facts-think0/results.jsonl`, `results/reasoning-76/results.jsonl`, `work/cc1` (progetto),
  `data-livetest/` (archivio della prova), `data/` (produzione, con dentro la sessione Codex).
- Su MVLINNA `~/.hermes/cache/scratch/live020/`: `codex-logs/`, `codex-work/`, `codex_live.sh`.
