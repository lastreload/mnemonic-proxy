# RECALL-8K-030 — prova dal vivo della correzione `recall` a finestra piccola (0.3.0)

Maurizio Verde — LastReload. 06/10/2026, 11:49–12:08, AI395 (Ryzen AI Max+ 395, 128 GB, ROCm gfx1151).
Card t_0d1a5081. Log su AI395 in `~/ds4/qcompare/pi030-ctx8k.run1`, `pi030-ctx8k.run2`, `pi030cat-ctx8k`.

## Montaggio
Uguale alla corsa fallita della card t_5de89914 (`DS4-QWEN-COMPARE-RESULT.md`), cambia solo il proxy:
pi → mnemonic-proxy **release/0.3 @ 0b59de2** (`~/ds4/proxy-030`, `examples/config.ds4.json`, porta 8121) →
ds4-server port ROCm locale (`~/ds4/ds4-qwen-rocm`), Qwen3.8 Flash Next Q2, `--ctx 8192`, porta 8120. Stesso
`server.log` (seed 7, identico byte per byte a `task2/server.log`), stessi 4 compiti di `pi2.sh`.

## Risultati
| corsa | primo compito: che cosa ha eseguito il modello | `cat` mascherato? | `recall` | trappola (rq-2290, shard 3) | durata |
|---|---|---|---|---|---|
| 0.2 (t_5de89914) | `cat server.log` | sì | 5; la 2ª–4ª tagliate a ~0 (`room` −137, −771) | non trovata, dichiarato | 6 min 05 s |
| 0.3.0 corsa 1 | `cat server.log \| grep -c ERROR` | — (mai letto) | 1 | non trovata, dichiarato: "il file non è mai stato nel contesto" | 148 s |
| 0.3.0 corsa 2 | `cat server.log \| grep -c ERROR`, poi `head -20` | — (mai letto) | 2 | come sopra | 175 s |
| 0.3.0 corsa 3, prompt "esegui esattamente `cat server.log` (senza pipe, grep o head)" | `cat server.log` | sì (2 cambi di segmento) | **1: `{"id": …, "query": "ERROR"}` → 360 token** | **giusta**: `rq-2290`, shard 3, riga 94, testo esatto `2026-10-06 12:51:33 ERROR upload request rq-2290 failed: upstream timeout after 1479 ms (shard 3)` | 427 s |

- Corsa 3: dopo la `recall` ds4 ha letto 712 token nuovi (ctx 4214..4926), non tutto il prompt.
- Nessuna corsa ha fatto scattare la guardia (`receipt_guard`: 0 eventi), quindi nessun falso positivo sulle 7 write/edit (2 + 2 + 3).
- Le corse 1 e 2 non provano niente sulla correzione: il modello ha scelto di non leggere il file (con il prompt
  originale "Esegui: cat server.log . Dimmi solo quante righe ERROR ci sono."), come a 10K e 12K nella card
  t_5de89914. La corsa 3 cambia il primo prompt per obbligare la lettura: **non è la stessa prova** della 0.2.
- Una corsa della 0.3.0 alle 11:49 è stata interrotta da uno shutdown esterno del ds4-server (altra card in pulizia),
  scartata (`pi030-ctx8k.aborted`, `.killed`).

## Che cosa prova
Con la stessa finestra di 8K e lo stesso dato da ritrovare, la `recall` per id + query della 0.3.0 porta la riga
giusta al modello in una chiamata (360 token), dove la 0.2 con 5 chiamate non ci arrivava. Una sola corsa riuscita:
è un esempio dal vivo, non una misura di frequenza.
