# WINDOW-GUARD-RESULT — mnemonic-proxy 0.3.3, guardia di finestra

Data: 14/10/2026. Branch `fix/window-guard` da `main a9eba25`, in `/mnt/mneme-nvme/proxy-ideas/gemelli/proxy-guard`
(PC4070TI) e in `…/workspaces/t_b0eba5e1/proxy-guard` (MVLINNA). Commit come Maurizio Verde, **nessun push**.
Tutto su CPU, senza lock GPU; la produzione :8095/:8096 non è stata toccata. **Nota:** i due giri dello smoke G3
(sezione 3.5, motore finto, nice 19/ionice idle, ~18:05–18:21) sono girati su PC4070TI, cioè durante il blocco GPU
del coordinatore iniziato alle 18:10: possono aver disturbato le misure di velocità di `runs/par` in quella
finestra. Dopo le 18:21 tutto il lavoro CPU (suite, prova a tappeto finale) è girato su MVLINNA.

## 1. Invarianti (G1)

Un solo punto di passaggio: `WindowGuard.enforce`, chiamato da `Upstream.chat` e `Upstream.chat_stream`. Ogni
chiamata del proxy al motore passa da lì: risposta, giri di recall, note di passaggio, ritentativi di answerfix,
con e senza streaming. La guardia è accesa di serie.

- **INV-1** `prompt_esatto + max_tokens + window_margin (64) ≤ W`. Se non ci sta, i rimedi vanno in quest'ordine:
  1. si rimuove per valore ciò che il ciclo ha aggiunto (G2);
  2. si abbassa `max_tokens`, mai sotto il pavimento F;
  3. si rifiuta: HTTP 400 al client (`WindowExceeded`, sottoclasse di `UpstreamError`) ed evento
     `window_guard/refused`. La richiesta non parte mai.
- **INV-2** `reasoning_budget_tokens ≤ max_tokens − answer_min_tokens`, evento `budget_cap`. Su Strata il
  ragionamento conta dentro `max_tokens`: l'ho verificato nel sorgente 0.1.40, `serve/server.py`, dove il budget
  chiude il thinking e poi parte la risposta nello stesso `max_new`. Se il budget non lascia spazio, la risposta
  resta vuota.
- **INV-3** un giro di ricerca parte solo se ci sta il caso peggiore: `P + CALL_ALLOWANCE(256) + RESULT_MIN(512) +
  F + margine ≤ W`. Altrimenti il giro è **finale**: niente strumenti del proxy, e una nota nell'ultimo risultato
  dice al modello che non c'è più spazio per cercare. Dal rigioco G3 vale anche questo: il giro finale per limite
  di giri non offre più gli strumenti del proxy. Prima una recall in quel giro veniva scartata e la risposta
  poteva restare vuota (smoke, `2_i6_j13_a_0892`).

**Terminazione.** P0 è il prompt preparato: masking e segmenti, prima del ciclo. In preparazione vale già
`P0 + F + margine ≤ W`, e se non vale cambia segmento. Il ciclo aggiunge solo pezzi rimovibili: chiamate,
ragionamento, risultati. Il passo C2 di `_plan_round` toglie giri interi, dal più vecchio. Togliendo tutto si torna
a P0, quindi la chiamata finale ci sta sempre. Ogni giro aumenta `rnd`, e oltre `max_recall_rounds` il giro è
finale: il ciclo finisce.

**Conteggio.** Con `--tokenizer` il conteggio è esatto, quello del motore (`render_pieces` + tokenizer del pack):
gli eventi hanno `guarantee=exact`. Senza tokenizer si usa la stima calibrata chars/3.5, più `window_estimate_slack`
(di serie 0), e gli eventi hanno `guarantee=estimated`. **La stima non è una garanzia:** sul testo ALB (id e
numeri) il tokenizer vero conta 1,7 volte la stima. Nella prova a tappeto senza tokenizer, con un motore che conta
esatto, gli sforamenti sono stati 837 su 3.876 richieste con slack 0, 626 con 0,15 e 425 con 0,3. Il README lo
dice e consiglia `--tokenizer` a finestra piccola. Con il tokenizer gli sforamenti sono 0 (sezione 3).

Strata 0.1.40, verificato nel sorgente:
- rifiuta con 400 quando prompt + max_tokens > W, senza troncare;
- con `tool_choice: none` toglie gli strumenti dal prompt (`serve/server.py` ~4150).

Per il giro finale ho scelto di togliere gli strumenti interni dalla lista. Costa una rilettura del prefisso, ma il
giro finale accade una volta per risposta.

## 2. Rimozione per valore (G2)

Implementata in `window.removal_order` + `remove_piece`, come funzione pura e deterministica (lo verifica un test).
I pezzi rimovibili sono il ragionamento dei giri passati, le chiamate e i risultati di recall. Segnali, solo quelli
disponibili prima della decisione, come chiede il commento del coordinatore:
- tipo di pezzo;
- token liberati davvero (dimensione meno ricevuta o estratto);
- età;
- duplicazione;
- identificatori della richiesta corrente presenti nel pezzo.

`recall_use.overlap` non è usato. Un risultato rimosso diventa una ricevuta di una riga con l'id, recuperabile con
recall. Il ragionamento si riduce all'ultima riga di conclusione, **estratta**.

**0.3.4 (questo ramo).** Il correttivo di duplicazione non si applica ai pezzi che portano almeno un
identificativo della domanda corrente, e `removal_order` li mette in coda a tutto il resto (tutte le politiche):
sono la prova che la domanda cerca, e il giornale registra `protected` quando l'invariante della finestra
costringe a rimuoverli lo stesso. Vedi `docs/dev-notes/GUARD-PROTECT-RESULT.md`.

**Confronto delle 3 politiche.** Rigioco a motore finto severo di tutti i casi D/Dfix di `runs/w16k` e `runs/w32k`
che hanno recall: 18 casi su 26, gli altri 8 non hanno recall. Il copione è quello delle recall del modello vero,
ognuna ripetuta con un ragionamento lungo quanto il caso peggiore. Metrica: valori gold rimasti nel prompt finale
dopo la domanda.

| politica | gold nel ciclo / gold totali | sforamenti | risposte "unknown" |
|---|---|---|---|
| età | 9 / 26 | 0 | 6 |
| ragionamento-per-primo | **17 / 26** | 0 | 6 |
| valore-per-token | **17 / 26** | 0 | 6 |

Adottata: **valore** (`recall_removal: value` di serie). Pari al ragionamento-per-primo sui casi reali, quasi il
doppio dell'età. Sui casi in cui le due prime politiche divergono (scenari sintetici con risultati irrilevanti e
pertinenti mescolati, `TestRemoval.test_value_keeps_relevant_result`), valore tiene il pezzo con gli id della
domanda e ragionamento no. Il motore finto non "legge", quindi il punteggio è uguale nelle tre politiche (3/18);
conta la metrica delle prove rimaste. Le 6 "unknown" sono casi in cui il gold non è mai stato recuperato dal
copione stesso: con il motore finto il copione non cambia.

## 3. Numeri dei test

1. **Suite completa**: verde su Python 3.10 (309 test, 8 saltati) e 3.14 (335 test, 7 saltati: con 3.14 girano
   anche i test con tokenizer). MVLINNA, ultimo giro dopo tutte le modifiche.
2. **Prova a tappeto** (`tests/sweep_window_guard.py`, seed 1000–2999), PC4070TI, solo CPU, tokenizer esatto del
   pack, motore finto severo:
   - **2.000 scenari, 27.411 richieste, 0 sforamenti**, 0 eccezioni, 0 rifiuti, 0 risposte sotto il pavimento;
   - 2.158 riduzioni di max_tokens, 2.145 rimozioni, 216 giri finali, 627 cap del budget, 830 note accorciate;
   - per finestra, 0 sforamenti ovunque (8K 7.109 richieste, 16K 5.462, 24K 4.965, 32K 5.013, 128K 4.862);
   - controllo, stessi scenari con guardia spenta (300): **1.021 sforamenti su 3.867**;
   - ripetuta su MVLINNA con il codice finale (giro finale senza strumenti anche per limite di giri), stessi seed:
     **27.411 richieste, 0 sforamenti**, 0 eccezioni, 0 recall vuote, 181 giri finali (`sweep-final-exact.json`).
3. **Rigioco dei casi falliti**, motore finto severo con copione delle recall vere:

   | caso | W | 0.3.2 (D) | guardia (G) |
   |---|---|---|---|
   | 13_i3_b | 16K | EngineError (sforamento) | ok, 0 sforamenti |
   | 13_i3_b | 32K | EngineError | ok, 0 sforamenti |
   | 9_i2_b | 32K | EngineError | ok, 0 sforamenti |
   | 35_i9_b | 32K | EngineError | **ok, corretta** (0507, 0595) |

   Nessuna risposta vuota per spazio. Le "unknown" di 13_i3_b e 9_i2_b vengono dal copione: il modello vero non
   aveva trovato il dato.
4. **Identità** (sezione 5).
5. **Smoke del banco G3** a motore finto severo, 10 domande (le stesse di `runs/w16k/A`), W=16K, politiche A (W
   piena) / T / B corretta / G / GE (E corretta):
   - primo giro fermato dalla regola d'arresto su G `2_i6_j13_a_0892`, risposta vuota: era la recall nel giro
     finale per limite di giri, corretta (sezione 1);
   - secondo giro completo: **50 corse, 0 sforamenti sul motore a W, 0 risposte vuote**.

   | politica | ok | vuote | ufficiale | formato normalizzato | interamente corretta |
   |---|---|---|---|---|---|
   | A (262K) | 10 | 0 | 8 | 8 | 8 |
   | T | 10 | 0 | 3 | 3 | 3 |
   | B | 10 | 0 | 3 | 3 | 3 |
   | G | 10 | 0 | 2 | 2 | 2 |
   | GE | 10 | 0 | 2 | 2 | 2 |

   I punteggi a motore finto non sono qualità: l'oracolo risponde bene solo se trova il gold nel prompt. Contano
   gli zeri, cioè 0 sforamenti e 0 vuote.

## 4. Banco G3

Modifiche in `/mnt/mneme-nvme/proxy-ideas/gemelli/guard-out/scripts/`. Gli script condivisi del banco non sono
toccati: `run_alb_guard.py` è una copia di `run_alb.py`.

- **B**:
  - compattazione con un prompt di sola sintesi, con un suo system e non quello dell'agente; la conversazione va
    come testo;
  - `enable_thinking: false` già al primo tentativo, senza budget di ragionamento;
  - `max_tokens` entro INV-1; se la conversazione da riassumere non ci sta, si accorcia dai messaggi più vecchi.
- **E corretta (GE)**: proxy con guardia **senza** lo strumento recall offerto (`inject_recall: false`), non una
  recall che fallisce.
- **T/B**: il controllo `prompt + 4096 ≤ 0,92 W` è sostituito da INV-1, `prompt + max_tokens + 64 ≤ W`, uguale
  per tutti.
- **Telemetria** in `result.json`:
  - `answer_empty`;
  - `final_tool_calls` (nomi e argomenti);
  - nel riassunto del giornale del proxy, `internal_completion_tokens` e `internal_requests` (tutte le generazioni
    interne) e i contatori `guard_*`, con `guard_violations` che deve restare vuoto.
- **Scorer a tre misure**: `score_official`; `score_format_norm`, cioè l'ufficiale dopo la normalizzazione del solo
  formato (tolti `<think>` e recinti ```, JSON canonico dentro `<answer>`; il contenitore `<answer>` resta perché
  il parser ufficiale lo cerca); `fully_correct`, binaria. `rescore3.py` le ricalcola sui result esistenti.
- **Verifica round → messaggio pertinente → gold** su `13_i3_b` e `35_i9_b`: il motore finto registra l'evento
  `gold` (gold presenti nel prompt della risposta, `in_loop`/`in_prompt`). Su `35_i9_b` il gold arriva al prompt
  finale (2/2) e la risposta è corretta. Su `13_i3_b`, con valore o ragionamento, 2/2 gold sono nel ciclo, ma la
  risposta resta "unknown" perché il copione del modello vero chiede pagine sbagliate. È un limite del modello,
  non dello spazio.
- **Checkpoint del motore** (commento del coordinatore, punto 1a): i file del motore con `--prompt-cache-every`
  pari a W/8 e `--prompt-cache 12` li ha già creati il coordinatore (`logs/strata-16384_2048_12.json`, da
  `scripts/gpu_block_par.sh`), quindi non li ho duplicati. RAM: secondo `docs/DETAILS.md`, ~118 MB per checkpoint,
  quindi 12 × 118 ≈ 1,4 GB a 16K. Va verificata dal blocco GPU in corso, che misura la RSS.

## 5. Differenze di comportamento a 128K

Dove lo spazio abbonda la guardia non cambia nulla: le richieste che il motore riceve sono identiche byte per byte
con e senza guardia (`test_guard_identity_at_128k`). Nella prova a tappeto a 128K i giri finali sono stati 3 su
4.862 richieste e le rimozioni 46. Vengono da scenari costruiti per riempire anche 128K (storie casuali grandi):
non sono cambiamenti a vuoto.

Golden:
- `test_default_identity` (0.3.2) è invariato con guardia spenta. Con guardia accesa a 12K cambia una cosa: il
  risultato della recall non viene più tagliato dalla doppia riserva della 0.3.2 (lasciava ~0 token) e riceve ciò
  che ci sta col pavimento (`test_guard_on_small_window`).
- golden lingua 0.3.0 (`test_lang`): invariato con guardia spenta. Con guardia accesa differisce **una sola**
  richiesta: quella delle note di passaggio al cambio di segmento a 40K. Era oltre la finestra (40.092 > 40.000 −
  64) e ora la parte A è accorciata dalla fine, con lo stesso prefisso.
  `test_guard_changes_only_notes` controlla esattamente questo.

## 6. Misura per i checkpoint (commento del coordinatore, punto 1b)

Nuovo evento `mask_reuse`, solo misura: a ogni mascheramento registra il prefisso riusato atteso (`expected_reused`
= prompt precedente fino alla prima differenza) e quello reale (`cached_tokens` / `cache_n`), con `gap`. È testato
in `TestMasking`. La politica "mascherare dopo un checkpoint" resta per la card successiva.

## 7. Piano della corsa GPU a 16K (STOP: serve l'autorizzazione di Maurizio)

- Stesse 10 domande di `runs/w16k/A` (kf-c 32K + 64K) a W=16K, Strata 0.1.40, file motore del banco (`--kv int8`,
  `--kv-resident` 16384). Il checkpoint ogni W/8 va scelto dopo l'A/B del blocco in corso.
- Politiche:
  - A: riuso dei risultati esistenti a 81.920;
  - T e B corretta: `run_alb_guard.py`;
  - G: proxy 0.3.3 con guardia, config D, `--tokenizer` del pack;
  - GE: E corretta.
- Regola d'arresto, già nello script dello smoke e da riusare con il motore vero: **una violazione di finestra
  (400 dal motore) o una risposta vuota per spazio → stop immediato**.
- Stima: ~40 corse a motore vero, cioè 4 politiche × 10, perché A si riusa.

## 8. File

- Codice:
  - `ctxproxy/window.py` (nuovo);
  - `server.py` (ciclo recall con `_plan_round`/`_fit_result`, `mask_reuse`);
  - `upstream.py` (enforce);
  - `core.py` (Config `window_guard`, `window_margin`, `window_estimate_slack`, `answer_min_tokens`,
    `recall_removal`, `final_drop_internal_tools`; note entro finestra; `expected_reused`);
  - `answerfix.py` (strumenti del giro);
  - `fake_engine.py` (`strict_window`);
  - `__init__.py`/`pyproject.toml` 0.3.3.
- Test: `tests/test_window_guard.py` (nuovo), `tests/sweep_window_guard.py` (nuovo), più `test_default_identity`,
  `test_lang` e `test_proxy` aggiornati.
- Documenti: `CHANGELOG.md`, `README.md` (sezione Window guard), `PR-BODY.md`, questo file.
- Banco (`guard-out/scripts/`): `run_alb_guard.py`, `fake_engine_alb_script.py` (severo + copione + evento gold),
  `build_replay_script.py`, `replay_guard.sh`, `g2_policies.sh`, `smoke16k_guard.sh`, `smoke16k_summary.py`,
  `rescore3.py`.
- Risultati (`guard-out/`): `sweep-exact.json`, `sweep-guardoff.json`, `sweep-estimated*.json`, `replay/`, `g2/`,
  `g2-summary.txt`, `smoke16k/`, `smoke16k-summary.txt`, `smoke16k-run1-stop/`.
