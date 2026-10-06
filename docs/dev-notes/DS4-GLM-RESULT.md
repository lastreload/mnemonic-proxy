# DS4-GLM — pi → mnemonic-proxy → ds4-server, GLM 5.3 Flash Q2 su Strix Halo, finestra 8K

Maurizio Verde — LastReload. Corsa del 06/10/2026, 02:05–02:16, su AI395 (Ryzen AI Max+ 395 "Strix Halo", 128 GB
di memoria unificata, ROCm gfx1151). Numeri presi dai log: `~/ds4/server-full.log` (ds4-server), giornale del proxy
(riassunto in `glm-journal-summary.txt` della card t_5de89914), `~/ds4/pi2-session.log` (risposte di pi).

## Montaggio
- Motore: ds4-server ([antirez/ds4](https://github.com/antirez/ds4)) commit `0aaea5a`, build `make strix-halo`,
  GLM 5.3 Flash Q2 (96,5 GB) con `--ssd-streaming` e cache degli esperti, `--mtp`, `--ctx 8192`.
- Proxy: mnemonic-proxy ramo `feature/ds4` @ `cf91eb3`, `examples/config.ds4.json` (riconoscimento ds4, finestra da
  `context_length` = 8192, soglie scalate: mascheramento a 5.000, obiettivo 2.500, coda protetta 1.000).
- Client: pi, 4 compiti di fila nella stessa sessione (`pi2.sh`) su un `server.log` generato (140 righe, 9.282
  caratteri, 4 righe ERROR). Il quarto compito è la trappola: "senza rieseguire comandi, request id e shard del TERZO
  errore". La risposta giusta è `rq-2290`, shard 3, ore 12:51:33.

## Risultato
| voce | valore |
|---|---|
| durata dei 4 compiti | 02:05:51 → 02:16:25, **10 min 34 s** |
| compiti 1–3 | giusti (4 ERROR; conteggio per servizio 28×5; `--level ERROR` upload 2, search 1, billing 1) |
| mascheramento | `freeze` del segmento 0 a 5.747 token stimati, note (474 token), cambio di segmento: l'uscita di `cat server.log` (2.504 token) esce dal prompt |
| trappola | **giusta: `rq-2290`, servizio upload, shard 3**. Difetto: aggiunge un orario inventato (12:52:16 invece di 12:51:33) |
| `recall` | 5, tutte per id: offset 0 / 1335 / 2670 / 4005 (408–409 token l'una), la quinta a offset 5340 senza risultato |
| token nuovi letti dal motore per richiesta, dopo ogni `recall` | 614, 629, 634, 633, 43 (ripresa per id: il prefisso non viene riletto). Dopo il cambio di segmento (prima richiesta: 850 token, il prefisso nuovo) ogni richiesta legge da 16 a 634 token nuovi; nessuna rilettura completa |
| prefill (lettura del prompt) | 28,8 t/s sul primo prompt (1.917 token), 25–27 t/s su 3.673 token, 23,7 t/s su 850 token |
| decode (generazione) | 5,9–9,4 t/s a pezzi di 50 token, ~6 t/s di media sulle risposte lunghe |

## Che cosa prova e che cosa no
- Prova: con ds4 il proxy maschera, cambia segmento e risponde alle `recall` senza far rileggere al motore il
  prefisso (ds4 riprende per id della chiamata); il modello ritrova un dato tolto dal prompt.
- Non prova: velocità utile per lavorare (decode ~6 t/s), sessioni lunghe, un modello diverso (vedi Qwen3.8 in
  `DS4-QWEN-COMPARE-RESULT.md`), né che l'orario sia stato letto: è stato inventato e va dichiarato.
- Il proxy è uno strumento separato da ds4: qui è provato compatibile, non è un contributo al progetto ds4.
