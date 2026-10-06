# Confronto: Strata IQ3_XXS (PC4070TI) e ds4-ROCm Q2 (AI395) su Qwen3.8-Flash-Next

Card `t_5de89914`, 06/10/2026, dalle 09:57 alle 11:00. La colonna Strata riprende `STRATA-QUALITY-RESULT.md` (card `t_75571a85`). La colonna ds4 è stata misurata in questa card.

## Tabella unica

| Metrica | Strata IQ3_XXS (PC4070TI) | ds4-ROCm Q2 (AI395) |
|---|---|---|
| Esatte su 28 (20 + 8) | 28/28 | 27/28 con la regola rigida (19/20 senza contesto, 8/8 sul brano). La risposta attesa c'è in 28 casi su 28 |
| Parziali / sbagliate / vuote | 0 / 0 / 0 | 1 / 0 / 0. È P1.7: Manzoni è giusto, ma la data e il titolo aggiunti sono sbagliati (testo più sotto) |
| A. Codice C `fib` | OK | OK, codice identico parola per parola. Compila con `gcc -std=c11 -Wall -Wextra` senza warning. fib(0..12) = 0 1 1 2 3 5 8 13 21 34 **55** 89 144; fib(50) = 12586269025 |
| B. Italiano in 3 frasi | OK | OK: 3 frasi corrette (Rayleigh; lunghezze d'onda corte; perché azzurro e non violetto) |
| C. 5 capitali in ordine alfabetico | OK (…Bruxelles, Copenaghen) | OK: Amsterdam, Atene, Berlino, Bruxelles, Budapest. 5 righe, ordine giusto, nessun commento |
| finish_reason | 31/31 `stop` | 31/31 `stop` |
| PPL, protocollo KLD (8×2048, posizioni 1024–2046 di ogni finestra) | 2,0059 ± 0,0316 | **2,1213 ± 0,0342** |
| PPL BF16, stesse posizioni | 1,9596 | 1,9596 (ricalcolata da `bf16.kld`, coincide) |
| PPL nativa `ds4 --perplexity-file` | — | 2,0502 su 16.352 token in un unico flusso fino a 16K di contesto. Non confrontabile, vedi metodo |
| KLD media vs BF16 | 0,1364 ± 0,0050 | **0,1988 ± 0,0063** (mediana 0,0030; 99° percentile 2,95; massimo 10,03) |
| RMS Δp vs BF16 | 12,867 ± 0,330 % | **15,779 ± 0,345 %** (Δp medio −3,09 %) |
| Same top p vs BF16 | 85,203 ± 0,393 % | **82,551 ± 0,420 %** |
| Per confronto: UD-Q2_K_XL (llama.cpp CPU) | PPL 2,0595, KLD 0,1528, RMS Δp 13,451 %, same top 84,030 % | |
| Tempi sul set comune (31 richieste) | 98 s in totale, mediana 2,7 s a richiesta, circa 37 token generati al secondo | 275 s in totale, mediana 8,7 s a richiesta, circa 13,5 token generati al secondo (compreso il prefill) |
| Domande sul brano (411 token di prompt) | mediana 2,7 s | mediana 9,8 s |
| Prefill / TTFT / decode a 1,6K e 7,8K token | **non misurato in modo confrontabile** in questa card: PC4070TI era in sola lettura e non si poteva usare la GPU | ctx 32K senza MTP: 1.571 token → TTFT 19,0 s, prefill 82,8 tok/s, decode 15,7 tok/s. 7.849 token → TTFT 97,0 s, prefill 80,9 tok/s, decode 19,3 tok/s. Con MTP il decode sale a 19,7–20,8 tok/s (dati di `t_d18d838f`, stesso binario) |
| Memoria | VRAM 11.520 MiB (RTX 4070 Ti 12 GB) più RAM host per gli esperti; GGUF IQ3_XXS | 50,5 GiB pianificati in memoria unificata (modello residente 41,72 + buffer 7,74 + KV 1,04 a ctx 32K). Le n-gram BF16 (95,37 GiB) sono lette dal disco. GGUF da 147 GB. Il processo resta a circa 0,6–0,8 GiB di RSS perché i pesi vivono nella memoria GPU (GTT) |
| Proxy (mnemonic-proxy, 4 compiti con pi) | 0.2.0 dal vivo con Claude Code/Codex: 22 `recall` e richiamo esatto. Non è lo stesso test di pi | 8K: 3 compiti su 4 giusti. Trappola **non** risolta: il modello risponde "non lo so", **non inventa**. 5 `recall`. La ripresa per id funziona (415 e poi 126 token), poi il proxy azzera il budget (22 token). 32K: 4/4, **rq-2290 e shard 3 giusti** senza `recall`, perché il file è ancora nel contesto. Dettagli più sotto |
| Motore e commit | Strata 0.1.38 + session files (`8d8f664`), ctx 128K, KV int8 | ds4 `~/ds4/ds4-qwen-rocm` (commit `74f435c` e `f7445c6`, gli stessi binari di `~/ds4/ds4`), ROCm gfx1151, ctx 32768, greedy, ragionamento attivo di default |

**Differenze di cui tenere conto**
- Quantizzazione diversa: IQ3_XXS contro Q2 di ds4. Il Q2 perde di più: KLD +46 %, RMS Δp +2,9 punti, same top −2,7 punti. Fa peggio anche dell'UD-Q2_K_XL di llama.cpp.
- Motore diverso: per Strata PPL e KLD vengono da llama.cpp CPU e il motore conta solo per le 28 domande. Per ds4 vengono dal grafo ROCm di produzione.
- Il GGUF ds4 contiene le n-gram BF16 originali: 95 GiB su disco, lette in streaming.
- Macchine diverse (RTX 4070 Ti 12 GB contro Strix Halo 8060S). Il motore Strata usa la speculazione MTP (`--spec 4`). ds4 è stato acceso senza `--mtp`.

## Metodo

### Set comune
- `ds4-server --rocm -m gguf/Qwen3.8-Flash-Next-Q2.gguf --ctx 32768 --port 8120 -t 12`, avviato con `nice 19 ionice -c3` (`srv.sh`). Pronto in 9 s.
- Client: lo stesso `run_quality.py` usato per Strata, cambiando solo URL e nome del modello. Le 31 richieste sono quelle di Strata, nello stesso ordine, una per messaggio utente, con `temperature 0` e `max_tokens 4000`. Risposte e ragionamento completi sono in `ds4-answers.json`, nello stesso formato di `strata-answers.json`.

### KLD vs BF16
1. **Tokenizzazione verificata prima di tutto.** `ds4 --raw --prompt-file corpus.txt --dump-tokens` produce 23.538 id. I primi 16.384 coincidono **tutti** con gli id salvati in `bf16.kld` (8×2048, 0 differenze). Il GGUF ha `add_bos_token=false`, quindi niente BOS, come nel riferimento.
2. Logit di ds4 con `DS4_QWEN4_FT_LIST`, il percorso usato da `test_qwen4_logit_dump.py`: prefill a tutte le righe sul grafo ROCm di produzione. Per ogni chunk un contesto nuovo con i token 0..2046 e un vettore di logit fp32 completo (248.320) per ogni posizione.
3. Calcolo in Python (`kld_ds4.py`), riproducendo `llama-perplexity --kl-divergence`: righe 1024–2046 (1023 per chunk, 8.184 in tutto). La base si dequantizza dai uint16 (`scale·q + min_log_prob`). Nella somma KL entrano solo i token con log p_base > −16, come in llama.cpp. Δp = p_Q(token) − p_base(token). Same top = argmax uguale.
4. **Controlli fatti.**
   - La PPL della base calcolata da `bf16.kld` dà 1,9596, identica al valore di llama.cpp: le posizioni sono allineate.
   - Sul chunk 0 la PPL dei logit FT_LIST (token 32..2047) vale 3,5722. `ds4 --perplexity-file` a ctx 2048, cioè il percorso di sessione con un token alla volta, dà 3,5709. Scarto dello 0,04 %.
5. `bf16.kld` è stato copiato da PC4070TI ad AI395 sul link diretto, senza passare da MVLINNA. I logit (16 GB) sono stati cancellati dopo il calcolo.

### PPL nativa
`ds4 --perplexity-file` non usa finestre: fa un prefisso di 32 token e poi conta tutti i token in un unico flusso. A ctx 2048 (2.016 token) dà 3,5709. A ctx 16416 (16.352 token, cioè tutto lo span 8×2048 di fila, in 899 s) dà 2,0502. È più bassa perché il modello vede fino a 16K di contesto, contro un massimo di 2047 per finestra: per questo non è confrontabile con 2,0059. Il valore confrontabile è 2,1213, nella riga del protocollo KLD.

## Risposte parziali (testo integrale)

**P1.7 — PARZIALE.** Domanda: Chi ha scritto "I Promessi Sposi"?
> "I Promessi Sposi" sono stati scritti da **Alessandro Manzoni**. Il romanzo, considerato uno dei capolavori della letteratura italiana, fu pubblicato nella sua prima versione nel 1827 (con il titolo "Fermo e Lucia") e nella versione definitiva nel 1840-1842.

Errore: *Fermo e Lucia* è la stesura del 1821–23 e non fu pubblicata. L'edizione del 1827 (la Ventisettana) uscì già con il titolo *I promessi sposi*. Strata aveva risposto soltanto "Alessandro Manzoni".

Tutte le altre 27 risposte sono ESATTE e non contengono altri errori. "Romolo Augusto" in P1.3 è il nome reale di Romolo Augustolo. Le 3 generazioni per intero sono in `ds4-answers.json`. A coincide con Strata parola per parola. B: "Il cielo appare azzurro a causa della diffusione di Rayleigh, un fenomeno per cui le molecole di gas nell'atmosfera disperdono la luce solare in tutte le direzioni. Le lunghezze d'onda più corte, come il blu e il violetto, vengono diffuse molto più efficacemente rispetto a quelle più lunghe, come il rosso e l'arancione. Tuttavia, percepiamo il cielo come azzurro anziché violetto perché i nostri occhi sono più sensibili al blu e perché parte della luce violetta viene assorbita dall'alta atmosfera." C: Amsterdam / Atene / Berlino / Bruxelles / Budapest.

## mnemonic-proxy con pi (4 compiti)

Schema: pi → mnemonic-proxy `feature/ds4` (`~/ds4/proxy-cf91`, `examples/config.ds4.json`, porta 8121) → ds4-server Qwen (8120). Il `server.log` è identico byte per byte a `task2/server.log` (verificato con `cmp`). I 4 prompt sono quelli di `pi2.sh`. Script: `pi-qwen.sh <tag> <ctx>`.

| Corsa | Compiti 1–3 | Trappola (3° errore) | `recall` | Mascheramento | Durata |
|---|---|---|---|---|---|
| GLM 5.3 Q2, 8K (riferimento) | OK | **rq-2290, shard 3** (orario inventato) | 5, ripresa per id a offset 0/1335/2670/4005/5340, 408–409 token ciascuno | sì | 10 min 30 s |
| **Qwen Q2, 8K** | OK (4 ERROR; conteggio per servizio; `--level ERROR` 2/1/1) | **non risolta**: recupera solo il 1° errore (rq-4471, shard 8, testo esatto) e dichiara di non avere il terzo. **Nessuna invenzione** | 5. Round 0: id `r2c925c09b046` → 415 token (char 0–1198). Round 1: offset 1198 → 126 token (`recall_trimmed`, spazio libero 134). Round 2 e 3: offset 1198 e 3300 → 22 token (spazio libero −137 e −771, contenuto azzerato). Round 4: query → nessun risultato | sì: freeze del segmento 0 a 6.051 token stimati, switch a 140 s, `cat server.log` (9.282 caratteri) archiviato | 6 min 05 s |
| Qwen Q2, 10K | OK | non risolta: il modello non aveva mai letto il file (`cat server.log \| grep -c ERROR`, poi `head -20`) e lo dice correttamente | 3, senza mascheramento | no | 3 min 25 s |
| Qwen Q2, 12K | OK | come 10K, risposta onesta | 3 | no | 3 min 50 s |
| **Qwen Q2, 32K** | OK | **rq-2290, shard 3 giusti**, con orario esatto 12:51:33 ed elenco corretto di tutti e 4 gli errori | 0: il file era ancora nel contesto | no | 3 min 30 s |

**Lettura.** A 8K Qwen usa `recall` nel modo giusto, con ripresa per id e paginazione a offset, come GLM. Fallisce perché arriva alla domanda con un contesto più pieno: 4.823 token di prompt contro 3.319 di GLM (contesto virtuale 6.844 contro 4.908), perché nella seconda parte aveva riletto 15 righe con `read` e aveva fatto più giri di strumenti. Così dal secondo `recall` in poi il proxy taglia il risultato a zero (`room` negativo), e il terzo errore, che sta al carattere 6.183 di 9.282 (i frammenti arrivati coprono soltanto i caratteri 0–2.396), non arriva mai. Il limite è nel budget del proxy a 8K, non nel modello: Qwen non inventa e lo dichiara. Da 10K in su il proxy non maschera nulla. A 10K e 12K il modello aveva scelto `grep -c` e quindi non aveva mai visto le righe. A 32K le aveva lette con `read` e risponde giusto.

## File
- MVLINNA (questa cartella): `ds4-answers.json`, `COMPARE-RESULT.md` e `ds4-logs/`. Quest'ultima contiene `kld_ds4.py`, `kld-eval.log`, `kld-ds4.json`, `xcheck.*`, `ppl-*`, `run-quality.log`, le sessioni e i riassunti del journal per 8K, 10K, 12K e 32K, `pi-calls.txt`, `glm-journal-summary.txt`, `pi-qwen.sh` e `srv.sh`.
- AI395 `~/ds4/qcompare/`: tutto il materiale sopra più `server-quality.log`, `trace-quality.json`, `corpus.txt`, `bf16.kld` (copia da 4 GB), `ds4-tokens.txt`, e per ogni corsa pi i dati del proxy, le sessioni, `trace.json` e `server.log`.
- Stato finale: nessun ds4-server e nessun proxy acceso (porte 8120 e 8121 libere). Non ho toccato i servizi di Maurizio né GLM, non ho modificato `/home/mverde/src/ds4`, e su PC4070TI ho fatto solo letture.
