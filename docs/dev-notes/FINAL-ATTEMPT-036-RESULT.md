# 0.3.6 — il tentativo finale non resta vuoto se il modello richiama `recall`

Risultato della card `t_5ac9f0b4` (brief `BRIEF-FINAL-ATTEMPT-036.md`). Ramo `fix/final-attempt-never-empty` da
`b8d4920` (0.3.5, PR #10). Solo CPU: prove unitarie su MVLINNA, sweep su AI395 (`~/mp035-verify/`, unità
`systemd-run --user` con `MemoryMax=40G`, `Nice=19`, `IOSchedulingClass=idle`, 12 processi, 92 GB disponibili alla
partenza, picco 23 GB). PC4070TI solo in lettura (giornali dei due casi copiati); `alb-bench-v2` e il suo venv non
toccati. Nessuna GPU.

## 1. Il difetto

Corsa di conferma AgentLongBench (W = 32K, F = 4096, budget di ragionamento 3072), D a 64K: `14_r11_a_0175_neg` e
`18_r7_a_0639`, entrambe `round_limit`. Dai giornali copiati (`out/correzione3/casi/`):

| caso | chiamata finale | tentativo 0.3.5 | esito |
|---|---|---|---|
| 14_r11 | prompt 24.873, 3.140 token generati (ragionamento al budget), solo `recall` | `final_recall_retry used=true`, prompt 24.987; di nuovo 3.155 token e solo `recall` | `recall_dropped retry=true` → vuota |
| 18_r7 | prompt 22.484, 3.156 token, solo `recall` | `used=true`, prompt 22.614; 3.152 token e solo `recall` | vuota |

In tutte e quattro le chiamate senza `recall` offerto il modello ragiona fino al tetto e poi chiama lo strumento.
Lo spazio non era il problema: dopo il tentativo restavano 7.717 e 10.090 token (W − margine − P), oltre F.

## 2. La correzione (`server.py`, `answerfix.py`)

- **Ultimo tentativo, ragionamento spento.** Se anche il tentativo 0.3.5 restituisce solo chiamate interne (nessun
  testo, nessuna chiamata del client), il proxy fa **un ultimo** tentativo (`FINAL_ATTEMPTS = 2`: mai più di due
  tentativi nuovi in tutto). Corpo: `chat_template_kwargs.enable_thinking=false` (lo stesso interruttore che usano
  `answerfix` per accenderlo e `core` per le note con `notes_thinking=off`; nel template, `render.template_kwargs`
  come il frontend di Strata, vince su `reasoning_effort`), senza `reasoning_budget_tokens`. Il rimedio T
  (`answer_thinking_after_recall`) non lo riaccende (`no_think` passato a `answerfix.chat/stream_body`).
- **Istruzione esplicita.** Le chiamate del tentativo precedente entrano nel ciclo (ragionamento ridotto alla riga di
  conclusione) e ognuna riceve `ANSWER_NOW`: «recall: the search is closed and recall is not available. Do not call
  any tool: answer the question now, in the requested format, with the information already retrieved (or say that
  you do not have it).» (versione italiana per `lang=it`).
- **Stesso controllo.** `P + need + margine ≤ W`, con P contato col template a ragionamento spento e
  `need = min(F, spazio della chiamata finale precedente)` contato col template di quella chiamata: la risposta non
  ha mai meno spazio di prima. Per pagare si tolgono solo le fasce 0–1 (copie verificate, ragionamento finito), mai
  prove.
- **Variante nuda.** Se chiamate + istruzione non entrano, l'ultimo tentativo parte nudo: le chiamate escono (come un
  gruppo annullato), il prompt è quello della chiamata precedente col ragionamento spento, e `ANSWER_NOW` si aggiunge
  in coda all'ultimo risultato solo se entra. Se il tentativo 1 non entra, l'ultimo si prova subito.
- **Se anche l'ultimo chiama solo strumenti interni**: risposta vuota come oggi (niente testo inventato),
  `recall_dropped` nel giornale.
- **Giornale.** `final_recall_retry`: `attempt` (1|2), `thinking_off`, `bare`, più i campi 0.3.5. `recall_dropped`:
  `attempts`, `thinking_off`. In streaming una riga di ragionamento «last answer attempt, reasoning off» /
  «ultimo tentativo di risposta, senza ragionamento»; le chiamate interne non arrivano mai al client.
- **Costo da sapere.** Col ragionamento spento il template toglie dalla testa di sistema l'istruzione di ragionamento
  (`render.py`, riga `instr`): il prompt è 36 token più corto (tokenizer esatto, prova unitaria) ma la cache dei
  prefissi del motore si perde da lì, quindi quella sola chiamata rilegge tutto il prompt (a 64K ~25K token, ~28 s a
  ~900 tok/s su PC4070TI). Succede solo all'ultimo tentativo.

## 3. Sweep a tappeto (AI395, 2.000 conversazioni, semi 1000–2999, tokenizer esatto in tutte, 12 processi)

Stesso script (`tests/sweep_window_guard.py` 0.3.6) su `b8d4920` e su 0.3.6. `SWEEP_MODE` dice cosa fa il modello
finto quando `recall` NON è offerto: `nothink` = chiama sempre `recall` col ragionamento acceso, risponde col
ragionamento spento (il modello della corsa di conferma); `always` = chiama sempre `recall`; `residual` = come 0.3.5
(p = 0,3 finché ha giri da chiedere). Col ragionamento spento il modello finto non scrive ragionamento.

| | nothink b8d4920 | nothink 0.3.6 | always b8d4920 | always 0.3.6 | residual b8d4920 | residual 0.3.6 |
|---|---|---|---|---|---|---|
| richieste al motore | 26.803 | 27.681 | 26.803 | 27.681 | 26.089 | 26.147 |
| sforamenti (P + max_tokens + margine > W) | 0 | 0 | 0 | 0 | 0 | 0 |
| prove viste e poi perse / false copie | 0 / 0 | 0 / 0 | 0 / 0 | 0 / 0 | 0 / 0 | 0 / 0 |
| risposte sotto F con F raggiungibile | 0 | 0 | 0 | 0 | 0 | 0 |
| tentativi 1 fatti / senza spazio | 967 / 1 | 967 / 1 | 967 / 1 | 967 / 1 | 253 / 1 | 253 / 1 |
| ultimi tentativi (ragionamento spento) | — | 878 | — | 878 | — | 58 |
| tentativi nuovi per risposta, massimo | 1 | 2 | 1 | 2 | 1 | 2 |
| chiamate senza `recall` offerto per risposta, massimo (lato motore) | 2 | 3 | 2 | 3 | 2 | 3 |
| **risposte vuote** | **878** | **0** | **878** | **806** | **58** | **10** |
| vuote evitabili | 0 | 0 | 0 | 0 | 0 | 0 |
| semi vuoti in 0.3.6 ma non in b8d4920 | | 0 | | 0 | | 0 |

Criteri del brief: 0 sforamenti ✔, 0 prove perse ✔, vuote col motore che richiama sempre 806 ≤ 878 ✔ (la differenza
viene dal 10% di giri in cui il modello finto aggiunge una chiamata del client: al client va quella), vuote col
motore che risponde al secondo tentativo (ragionamento spento) 0 ✔. Il tentativo 1 senza spazio (1 caso) in 0.3.6 è
seguito dall'ultimo tentativo nudo, che entra. Gli 878 «ultimi tentativi» sono tutti con `thinking_off=true`.
Dati: `workspaces/proxy-ideas/out/correzione3/sweep/` (JSON completi, log, `sum036.py`, `sum036.out`).

## 4. Test

- `tests/test_final_recall_retry.py` (22 test): richiama al primo tentativo e risponde al secondo (spazio e limite di
  giri, non-stream e streaming); richiama sempre → vuota dopo esattamente due tentativi (4 combinazioni); modello che
  risponde solo col ragionamento spento con T acceso (T non lo riaccende); corpo a ragionamento spento
  (`enable_thinking=false` vince su `reasoning_effort`, niente budget); spazio al limite per l'ultimo tentativo
  (entra, paga con una copia mai con una prova, variante nuda senza istruzione, nuda con istruzione in coda,
  `floor_short`; «nemmeno nudo» saltato col tokenizer esatto perché il template spento è più corto, delta −36).
  Controprova sul codice `b8d4920` con la prima versione del file: falliscono tutti i casi «risponde all'ultimo
  tentativo» (6 sottocasi) e lo streaming «richiama sempre» (3 chiamate finali attese, 2 fatte); i casi «richiama
  sempre» e i test unitari al limite vanno in errore (manca `attempt`/`thinking_off` nel giornale e in
  `_final_retry`). Restano verdi i test di comportamento invariato (chiamate miste, testo visibile, finale normale,
  tentativo 1).
- `tests/test_recall_small_window.py`: la sequenza viva a 8K ora arriva all'ultimo tentativo (il modello a copione
  chiama `recall` anche nel tentativo 1): aggiornata l'attesa.
- Suite completa: Python 3.14 con tokenizer esatto 390 OK (5 saltati); Python 3.10 (stima) 364 OK (11 saltati).

## 5. Cosa non è provato

- Che Flash Next, col ragionamento spento e l'istruzione, risponda davvero invece di chiamare `recall`: lo sweep lo
  assume (`nothink`). Va visto su GPU rigiocando i due casi D a 64K.
- La qualità della risposta senza ragionamento (può essere peggiore di una con ragionamento; resta meglio di vuota).
- Il costo reale della rilettura del prompt intero in quell'ultima chiamata (stimato sopra, non misurato).
