# mnemonic-proxy × ds4 — risultato (card t_f71f2e6b)

Maurizio Verde — LastReload. File non tracciato.

## In breve
- Parte A fatta: ramo `feature/ds4` (worktree `/home/mverde/src/vcp-wt/ds4`), 2 commit locali (334d7d9, cf91eb3), niente push. Suite completa: 181 test OK, 5 saltati.
- Parte B: Qwen3.8 su PC4070TI non fattibile (codice e doc ds4) e PC4070TI comunque escluso dall'orchestratore. Prova fatta su AI395 con GLM 5.3 Flash Q2 (ROCm, SSD streaming).
- Decodifica: 2,0 tok/s senza MTP, 2,4–4,3 tok/s con MTP; nella sessione reale con pi ~2,4–2,6 tok/s, cioè sotto la soglia di ~3 tok/s. Prefill 9–17 tok/s. Utilizzabile solo per la verifica funzionale, non per lavorare.
- La sessione pi → proxy → ds4 è partita. Verificati: ripresa per id dopo uno strumento e cache su disco dopo il riavvio. Da lì è uscito un bug reale del proxy, corretto. La sessione lunga (mascheramento + `recall`) non è finita: il server ds4 è stato sostituito due volte da altri (vedi "Interferenze").

## Parte A — cosa è cambiato nel proxy
Commit ds4 di riferimento: `0aaea5a238fb41a35106a551e73c8409dfb751ac` (2026-09-20).
1. Riconoscimento (`ctxproxy/engines.py`): ds4-server non ha /v1/status, /props, /slots o /tokenize. Si riconosce da `GET /v1/models` con `owned_by: "ds4.c"`; `context_length` (= `--ctx`) diventa la finestra. Tipo `ds4` più alias (`ds4-server`, `ds4.c`, `dwarfstar`); `--engine ds4` lo forza. Spenti slot_save/mask_anchor/autosave/autorestore/kv_archive, perché ds4 salva da solo con `--kv-disk-dir`.
   Verifica reale: su AI395 il proxy ha riconosciuto il vero ds4-server (kind ds4, n_ctx 4096 e poi 16384, modello "GLM 5.3…").
2. Id degli strumenti: prima `out_tool_id` aggiungeva `toolu_` agli id dei client Anthropic; ora un id valido passa intatto. Chat e Responses erano già intatti. Le `recall` interne tengono l'id del motore e la chiamata originale torna identica nella storia. Test con un ds4 finto (`tests/fake_engine.py`, flavor `ds4`) che come il vero risponde 400 «replay full history» a un risultato con id sconosciuto e conta le chiamate rimandate con id noto o sconosciuto.
3. `mask_tool_args` spento con ds4: ds4 rimette nel prompt il testo campionato preso per id («exact DSML tool replay»), quindi gli argomenti accorciati non arriverebbero al modello. Si riattiva solo con `ds4_exact_tool_replay: false` se ds4 gira con `--disable-exact-dsml-tool-replay`.
4. Ragionamento: l'ultimo turno con chiamate resta integro (test Responses senza stato e Chat).
5. `checkpoint_align_tokens` (spento di serie): fa partire il blocco di mascheramento subito dopo un multiplo dell'intervallo dei checkpoint di ds4. Testato solo con il finto, non misurato sul vero.
6. Correzione da prova reale (cf91eb3): un `response_floor` scritto in configurazione (16384) non veniva scalato. Con `--ctx 16384` ogni richiesta sembrava fuori finestra, quindi a ogni richiesta c'erano note di passaggio e cambio di segmento, con il prefisso sempre invalidato. Ora `response_floor` è tra le soglie scalate ed è comunque limitato a 1/4 della finestra; tolto da `examples/config.ds4.json`. Ha un test.
7. `examples/config.ds4.json` e una sezione README «ds4-server».
Test nuovi: `tests/test_ds4.py` (15). Aggiornati 3 test di `test_clients.py` che si aspettavano il prefisso `toolu_`.

## Parte B — fattibilità
- Qwen3.8 Flash Next Q2 su RTX 4070 Ti 12 GB: NO. `docs/QWEN38_FLASH_NEXT.md` alla commit fissata dice: «Tensor parallelism, pipeline execution and SSD expert streaming are not implemented for this model yet. ROCm is not supported.» Nel codice `ds4.c` (~71417) i percorsi di cache derivata CUDA escludono `ds4_model_is_qwen4()`. I pesi principali sono 41,73 GiB residenti e non c'è streaming, quindi non entrano in 12 GB. Su AI395 (ROCm) Qwen non è supportato.
- Un download `qwen38-q2` su PC4070TI era partito prima dell'indicazione dell'orchestratore; l'ho fermato. Restano 33 GB parziali in `/mnt/mneme-nvme/ds4/ds4/gguf/.cache/...incomplete`, da cancellare (rm bloccato in questa sessione). Anche la build CUDA lì (ds4, ds4-server) è rimasta, ma non è mai stata eseguita: GPU non usata, Strata non toccato.
- Variante su AI395: GLM 5.3 Flash Q2 (96,5 GB) con `--ssd-streaming`, supportata su ROCm (`docs/STRIX_HALO.md`, `docs/MODELS.md`).

## Parte B — setup su AI395
- Clone in `/home/mverde/ds4/ds4` a 0aaea5a. Build `make strix-halo` (ROCm 7.2.1, gfx1151). Mancava `libxml2.so.2` per `lld`; l'ho risolto con un link privato in `/home/mverde/ds4/compat-lib` via LD_LIBRARY_PATH solo per la build. Il sistema non è stato toccato.
- Modello: `download_model.sh glm53-q2` in `/home/mverde/ds4/ds4/gguf/` (96,5 GB). Dopo restano 167 GB liberi su `/`.
- Le mie corse: `systemd-run --user --scope -p MemoryMax=56G nice -n 19 ionice -c3`, `-t 12`, porta 8110, `--kv-disk-dir /home/mverde/ds4/kv`. Proxy su 8111, pi isolato (`PI_CODING_AGENT_DIR=/home/mverde/ds4/pi-agent`).

## Misure (GLM 5.3 Flash Q2, ds4 0aaea5a, ROCm, SSD streaming)
| corsa | cache esperti | prompt | TTFT | prefill | decodifica |
|---|---|---|---|---|---|
| ctx 4096, senza MTP, prima richiesta | auto ~37 GiB (5604 esp.) | 212 | 31,3 s | 6,8 t/s | 2,0 t/s |
| idem, cache calda | | 212 | 23,2 s | 9,1 t/s | ~2,8 t/s |
| idem | | 2174 | 135 s | 16,1 t/s | 2,5 t/s |
| ctx 4096, `--mtp` | auto | 212 | 22–32 s | 6,6–9,6 t/s | 3,1–4,3 t/s |
| ctx 16384, `--mtp`, sessione pi | auto ~42 GiB (6187 esp.) | 2183 / 2837 | 215 s / 202 s | 10–17 t/s | 2,4–2,6 t/s |
Memoria del processo (cgroup): picco ~1,2 GB RSS. Gli esperti stanno in memoria GPU (GTT): `mem_info_gtt_used` arrivava a ~55,6 GB con la cache automatica a ctx 4096, ed è conteggiata fuori dal MemoryMax dello scope. Questo va tenuto presente: il tetto di 56G non limita davvero la memoria GTT.

## Proxy davanti a ds4 — cosa si è visto davvero
- Ripresa per id dopo uno strumento: ds4 ha generato `call_1ca6ad3d…` (finish=tool_calls). La richiesta successiva di pi con il risultato è ripartita da ctx=3034, calcolando solo 232 token nuovi (`chat ctx=3034..3266:232`), senza rileggere il prefisso.
- Cache su disco: ds4 ha salvato i checkpoint (`kv cache stored … reason=cold/evict/shutdown`, ~190–320 MiB l'uno). Dopo il riavvio del server ha ripreso dal disco: `kv cache hit text tokens=1960 … load=203.5 ms` → `chat ctx=1960..2417:457`. Calcolati solo 457 token su 2417.
- Bug trovato: vedi Parte A punto 6. Con il primo proxy, ogni richiesta faceva note più cambio di segmento: la seconda richiesta ha avuto un `live kv cache miss common=1895`, cioè 2837 token ricalcolati invece di ~700.
- Non verificati sul vero: mascheramento, `recall` interna e ripresa per id dopo `recall`, effetto di `checkpoint_align_tokens`, confronto senza proxy. Alla velocità misurata, un compito abbastanza lungo da superare la soglia di mascheramento (10000 token con finestra 16K) richiede ore.

## Interferenze (da sapere)
- Alle 00:57 e alle 01:11 il mio ds4-server è stato fermato da altri e sostituito con `ds4-glm-61g.service` e poi `ds4-glm-76g.service` (script `/home/mverde/ds4/ds4-61g.sh`, commento «richiesta Maurizio», cache esperti 61 e poi 76 GB, senza MemoryMax). Queste corse mandano richieste di misura (999 e 7484 token) che si alternano alla mia sessione e si sfrattano la cache a vicenda (`live kv cache miss … common=0`).
- Alle 01:18 su AI395 la RAM disponibile era ~7 GB (114/122 usati), con un llama-server qwen2.5-coder, jest e altro attivi. Per non sovrapporre carico ho fermato il mio client pi e il mio proxy. Il server 76G non l'ho toccato perché non è mio.

## Stato finale
- AI395: nessun mio processo attivo (pi e proxy fermati). Restano il modello (96,5 GB), la build, i log e le tracce in `/home/mverde/ds4/`, e `ds4-glm-76g.service` di altri.
- PC4070TI: GPU mai usata, Strata e lock non toccati. Da pulire: 33 GB di download Qwen parziale in `/mnt/mneme-nvme/ds4/ds4/gguf/.cache/`.

## Cosa resta
1. Sessione lunga pi → proxy (cf91eb3) → ds4 con un server dedicato: mascheramento, `recall`, ripresa per id dopo `recall`, `checkpoint_align_tokens`. Alla velocità attuale conviene una finestra più piccola (es. `--ctx 8192`) per far scattare il mascheramento prima.
2. Decidere se GLM su AI395 vale la pena: con 2,5 tok/s reali è sotto la soglia di 3.
3. Claude Code e Codex davanti a ds4: non provati.
