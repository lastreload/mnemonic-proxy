# 0.3.4 — i pezzi che portano gli identificativi della domanda si rimuovono per ultimi

Risultato della card `t_b6643416` (gemelli/proxy-guard, ramo `fix/guard-question-idents`, base `fix/window-guard`
= PR #8: `6f0cf1c` + `9222794`). Solo CPU, nessuna GPU, nessun push.

## 1. Il difetto, misurato sul giornale vero

Caso `38_i13_j22_a_0031` (giro ALB finestra 16K, cartella
`runs/guard16k/w16384/G/kf-c/64k/38_i13_j22_a_0031`), domanda:

> Please determine if 0031 appears in the tool results of both round 13 and round 22.

Identificativi della domanda: `0031`. Il caso ha 15 risultati di recall, **6 portano `0031`**, e il giornale
registra 9 eventi di rimozione (`window_guard action=remove`).

La regola di 0,3,3 (`window.piece_value`) moltiplica per 0,25 il valore di un pezzo i cui identificativi sono già
tutti comparsi in un pezzo più vecchio: fra le ricevute che rispondono alla domanda questo correttivo scatta (si
assomigliano fra loro), quindi **la prova che la domanda cerca è la cosa più economica da buttare**. Con il
rigioco del giornale (`tools/replay_removal_034.py`, vedi §5):

| | 0.3.3 (regola in uso) | 0.3.4 |
|---|---|---|
| risultati di recall con `0031` | 6 | 6 |
| di quelli rimossi dal guardiano | **5** | **1** |
| eventi di rimozione in cui la prima scelta porta `0031` | 4 su 9 | 1 su 9 |

L'unico pezzo con `0031` che anche 0,3,4 rimuove (round 2, `where=round`, token 1027) è quello in cui **tutti** i
candidati rimasti portavano `0031`: l'invariante della finestra vince, e il giornale lo annota in `protected`.

Secondo caso, `2_i6_j13_a_0892` (domanda su `0892`): 4 risultati con `0892`, rimossi 3 con 0,3,3 e 2 con 0,3,4.
La ricostruzione qui è più debole (§5): i numeri valgono come indicazione, non come misura.

## 2. La regola

`window.piece_value`: il correttivo di duplicazione **non si applica** a un pezzo che porta almeno un
identificativo della domanda corrente (`ids & idents(question)`), e `window.removal_order` ordina con
`protected` come chiave primaria — prima i pezzi senza quegli identificativi (ragionamento, poi risultati), i
pezzi con essi per ultimi, in coda fra loro la stessa regola di valore di prima. La chiave vale per tutte e tre le
politiche (`age`, `reasoning`, `value`), non solo per `value`.

Non cambia l'invariante: il ciclo continua a rimuovere finché la richiesta entra nella finestra, quindi un pezzo
protetto **viene rimosso comunque** quando è l'unico rimasto; in quel caso il giornale lo registra (campo
`protected` negli eventi `window_guard`, oltre a `policy`).

## 3. Test

`tests/test_window_guard.py`, tre prove nuove:

- `TestRemoval.test_question_idents_are_removed_last_whatever_the_policy` — funzione pura, tutte e tre le
  politiche: i due pezzi con l'identificativo della domanda sono gli ultimi, gli altri prima.
- `TestQuestionIdents.test_receipts_the_question_is_about_are_removed_last` — integrazione con `_plan_round`: lo
  spazio manca di una sola rimozione, i pezzi protetti sono due (uno è il duplicato che 0,3,3 buttava via) e
  viene rimosso il pezzo **senza** l'identificativo; la prova controlla anche che per valore/token il duplicato
  sarebbe stato il primo, cioè che il test è sensibile alla correzione, e che la richiesta resta nella finestra.
- `TestQuestionIdents.test_a_piece_with_the_question_idents_is_removed_when_it_is_the_only_one` — resta solo un
  pezzo e porta l'identificativo: viene rimosso lo stesso e il giornale lo scrive in `protected`.

`test_value_keeps_relevant_result` (0.3.3) è aggiornato: pretendeva che il primo risultato con gli identificativi
fosse l'ultimo *grazie* al correttivo di duplicazione, ora lo pretende perché è protetto.

Suite: **312 test ok su 3.10, 338 ok su 3.14** (`python3.10 -m unittest discover -s tests -t .`).

## 4. Prova a tappeto e identità a 128K

`tests/sweep_window_guard.py --n 2000 --workers 12 --tokdir /mnt/gsq-ssd/strata-data/packs/iq3_xxs/tokenizer`:

| | richieste | sforamenti | rimozioni | giri finali |
|---|---|---|---|---|
| 0,3,3 | 27.411 | 0 | 2.145 | 216 |
| 0,3,4 | 27.411 | 0 | 2.135 | 183 |

Stesso numero di richieste (le stesse decisioni di recall), 0 sforamenti e 0 eccezioni ovunque, 8K→128K
(`tokenizer_loaded: 2000`). `tests/test_default_identity.py` con `CTX_TOKDIR` puntato al tokenizer del pack:
`test_guard_identity_at_128k` verde — a 128K le richieste restano identiche byte per byte con e senza guardiano.

**Attenzione all'interprete.** Con un Python senza il modulo `tokenizers` (qui: tutti i `python3.x` di sistema)
la prova a tappeto *non* fallisce ma misura male: il guardiano conta con la sua stima, mentre il motore finto
contava con `TokenCounter.raw` pezzo per pezzo, e le due cose differiscono — la prima esecuzione di questa card
ha riportato 4.934 "sforamenti" che erano solo questo disallineamento. Usare
`/mnt/mneme-nvme/g3/venv/bin/python` (tokenizers 0.23.2). Rimedio nel banco: se il tokenizer non si carica, il
motore finto usa il contatore del guardiano (`px.wguard.count_body`), come già fa `tests/test_window_guard.py`, e
`tokenizer_loaded` compare nel riepilogo.

## 5. Come è stata ricostruita la tabella, e cosa non regge

`tools/replay_removal_034.py --run <caso> --tokdir <pack>` legge `proxy/journal.jsonl` e una **copia**
dell'`archive.sqlite` del giro, e per ogni evento di rimozione ricostruisce i candidati (i risultati di recall
che il ciclo aveva in quel momento: il giornale dice quali record d'archivio ognuno ha restituito) e ricalcola
l'ordine con la regola 0,3,3 (copiata nel tool, solo per la colonna "prima") e con quella 0,3,4.

Limiti dichiarati:

- il testo di un pezzo non è nel giornale: il rigioco usa il contenuto dei record, ma il registratore mostra un
  **estratto** attorno alla corrispondenza e non il record intero (deviazione dai token del giornale: da −832 a
  +7.952; sul caso `0031` mediana −105). Gli identificativi quindi sono sovrastimati: la ricostruzione è una
  approssimazione, non un rigioco byte per byte;
- la prova che l'approssimazione regga è che l'ordine ricostruito di 0,3,3 **riproduce i pezzi che il giornale
  dice rimossi** in 8 eventi su 9 (caso `0031`) e 6 su 7 (`0892`). L'evento che non torna è quello in cui il
  giornale rimuove il risultato *pendente* del giro (registrato dopo la rimozione, quindi assente dai candidati);
- i due giri `GE` degli stessi casi non hanno recall nel giornale: lì non c'è niente da confrontare;
- il testo dei pezzi reali non è disponibile, quindi i valori assoluti di `piece_value` non sono confrontabili
  con quelli del 0,3,3 vero: il confronto è fra le due regole **sugli stessi candidati**.

## 6. Cosa non è provato

- che la nuova regola porti a risposte migliori: servirebbe un giro ALB con GPU (blocco GPU in corso). Qui è
  provato che non butta via la prova che la domanda cerca;
- il costo: la regola nuova può togliere prima un pezzo piccolo non protetto invece di uno grande protetto,
  quindi servono più rimozioni (nel banco: 2.135 contro 2.145, cioè praticamente identiche, ma su un caso reale
  sono 5 contro 1 pezzi con la prova);
- se convenga proteggere anche i pezzi che portano identificativi comparsi in una *risposta* precedente del
  modello (oggi solo il testo della domanda conta).

## 7. Versioni

`pyproject.toml` resta a 0,3,3 e la voce va sotto `## Unreleased` del CHANGELOG: 0,3,3 non è ancora released,
quindi una sezione 0,3,4 sarebbe fuori posto. Bump da fare al rilascio.
