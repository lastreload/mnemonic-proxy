# 0.3.5 — la ricerca non consuma le prove già acquisite (correzione 2)

Risultato della card `t_b408e1d2`. Ramo `fix/search-keeps-evidence` da `main` `9a33f6c` (0.3.4). Solo CPU: prove
unitarie su MVLINNA, banco a tappeto e rigiochi su AI395 (`~/mp035-verify/`, `nice 19`, `ionice -c3`, scope con
`MemoryMax=40G`, RAM disponibile 90 GB alla partenza; per circa 3 minuti i rigiochi sono girati insieme allo sweep
0.3.4, quindi ~15 processi invece di 12). PC4070TI solo in lettura (tokenizer del pack, giornali e
archivi dei casi, script del banco). Nessuna GPU, nessun modello caricato. Progetto: `SINTESI-CORREZIONE2.md`
(vincolante), pareri Sol e Argon.

## 1. Cosa è cambiato

| # | Punto della card | Dove | Cosa fa ora |
|---|---|---|---|
| 1 | 4 fasce, niente sconto ×0,25 | `window.band`, `removal_order`, `piece_value` | 0 copie verificate → 1 ragionamento → 2 risultati non-copia → 3 risultati non-copia con identificativi della domanda (`protected`; mai il ragionamento, mai una copia). Lo sconto ×0,25 non c'è più (`seen_before` ignorato). Durante la ricerca (`_plan_round` prima della finale, `_fit_result`, `_fit_other`) solo le fasce 0–1 (`SEARCH_BANDS`). |
| 2 | Copia verificata | `window.copy_of`, `sources_from_meta`, `_covered` | Copia = testo consegnato identico a un risultato **precedente ancora presente**, oppure tutte le sorgenti certificate (`span` di caratteri, insiemi di righe `lines`/`leaf` dello stesso record) dentro l'unione delle sorgenti dei risultati precedenti presenti. `meta` vuoto, offset assente, `grouped` ("stesso testo anche nei messaggi…"), `cut` = opaco = mai copia; le ricevute non sono mai testimoni. `meta` esteso: `offset` per pagine per id, estratti per query (legacy e Recall2), `line_set` per id+query, `leaf_lines` (tutte) per il selettore, `cut` quando il blocco o il risultato è tagliato dopo `meta` (anche in `_fit_result`). Nella timeline di Recall2 `meta` si scrive solo dopo il controllo di spazio. `_src` sta in `PRIVATE`: `clean()` lo toglie da ciò che va al motore e da ciò che si memorizza. |
| 3 | Ammissione del giro | `Proxy._plan_round` | `P + max(S, CALL_ALLOWANCE + R + F) + H + M ≤ W` con `S = max(2048, massima generazione completa precedente + 256)` (`SearchState.saw_generation`, da `usage.completion_tokens`, mai calante; le generazioni finite per `length` alzano solo il limite inferiore), `R = 1024` per giro (i risultati del giro lo prendono in sequenza: il `cap` di ogni chiamata conta quelle già consegnate), `H` = cornice di un risultato + nota di chiusura, contati col contatore della guardia. Se togliendo solo le fasce 0–1 non basta → finale (`reason=space`). |
| 4 | Ricerca → finale | `SearchState.force_final`, `_shrink_newest`, `_c2` | Stato persistente. Nella finale: fasce 0–1; se non basta, il **gruppo più nuovo** (chiamata + risultati che il modello non ha ancora visto) diventa ricevute e poi viene annullato intero (protocollo coerente); solo dopo l'emergenza: risultati non-copia (fascia 2 prima della 3), poi C2 a tornate intere preferendo quelle senza pezzi protetti. Ogni perdita va in `evidence_lost` con causa e sorgenti. |
| 5 | `_fit_result` | `Proxy._fit_result`, `_free_search`, `_fit_other` | Libera fasce 0–1 finché `cap < need` (non `want`); il `cap` riserva anche F e la nota di chiusura. Selettore foglia; con `cap ≥ 256` taglio rigoroso (sorgenti `cut`); sotto 256 e risultato che non entra → avviso "no room" che entra nel `cap` (`meta.clear()`, `force_final`), e se nemmeno l'avviso entra la chiamata esce dal gruppo. Le chiamate successive dello stesso giro non cercano (`recall_skipped`) ed escono dal messaggio dell'assistente; un gruppo senza risultati è annullato (`recall_group_cancelled`). Definizioni caricate da `_tools_call` e avvisi della `receipt_guard` passano dallo stesso controllo (tutto o niente). |
| 6 | `max_recall_rounds` | `_plan_round` | `search_ok = rnd < max_recall_rounds`: alla soglia finale diretta, `recall` non offerto, nota "limite di ricerche" sull'ultimo risultato. `max_recall_rounds = 0` = nessuna ricerca. Il ramo `LIMIT_MSG` resta solo per la guardia spenta. |
| 7 | `FINAL_NOTE` | `_plan_round`, `remove_piece` | Niente `_keep`; `_final_note` porta il testo della nota, che resta sulla ricevuta se quel risultato viene tolto. |
| 8 | answerfix K | `answerfix.guard(..., tools=)` | Il retry usa gli strumenti del giro finale effettivo (prima riapriva la ricerca con `p.tools`). |
| 9 | F esplicito | `Proxy._check_floor` | Prima del ciclo `P0 + F + M ≤ W`; se non vale: evento `floor_short` con `room`, nessuna ricerca, la risposta prende ciò che resta (sotto 1 token la richiesta è rifiutata). In modalità esatta un errore del tokenizer/template non ripiega in silenzio: `WindowGuard.count_errors` + evento `count_error`. |
| 10 | Giornale | eventi `window_guard` | `removed_search`, `removed_final`, `newest_group`, `c2` (per ogni voce: tipo, giro, token, token liberati, fascia, `copy`, `protected`, `cause`, `sources`, `tool_call_id`), `noncopy_search`, `evidence_lost`, `s_est`, `extra`, `reason`. Restano `removed`/`protected` nel formato 0.3.4. |

Versione 0.3.5 (`pyproject.toml`, `ctxproxy/__init__.py`), CHANGELOG sotto 0.3.5.

## 2. Banco a tappeto: prima/dopo (AI395, 2.000 conversazioni, tokenizer esatto in tutte)

Stesso banco 0.3.5 (`tests/sweep_window_guard.py`, semi 1000–2999, 12 processi) su `9a33f6c` e sul ramo. Novità del
banco: ~30% delle chiamate chiede un id vero visto nel prompt (intero, una pagina, id+query) o ripete una chiamata
precedente; il motore rifiuta `prompt + max_tokens + 64 > W`; controllo lato motore, indipendente dal proxy, delle
prove perse (un risultato che il modello ha visto e che una richiesta successiva della stessa risposta non porta più,
mentre le sue righe non sono tutte in un altro risultato presente); falsa deduplicazione = una tale perdita su un pezzo
che il proxy ha tolto come copia.

| misura | 0.3.4 | 0.3.5 |
|---|---:|---:|
| richieste al motore | 27.377 | 25.836 |
| sforamenti (`P + mt + 64 > W`) | 0 | **0** |
| eccezioni | 0 | 0 |
| tokenizer caricato | 2.000/2.000 | 2.000/2.000 |
| errori di conteggio esatto | n/d | 0 |
| risposte sotto F con F raggiungibile | 0 | **0** (`floor_short` 0) |
| **prove viste dal modello e poi perse** | **669** | **0** |
| false deduplicazioni | 0 (nessuna copia) | **0** su 392 copie tolte |
| rimozioni di non-copie durante la ricerca | n/d | 0 |
| recall eseguite | 9.547 | 6.450 |
| giri chiusi in finale (`final`) | 185 | 1.037 |
| scenari con ≥ 2 risultati distinti consegnati insieme | 1.408 | 1.281 |
| esito del banco | **FALLISCE** (prove perse) | **passa** |

Perdite 0.3.4 per finestra: 8K 332, 16K 194, 24K 107, 32K 35, 128K 1. Il costo della 0.3.5 è dichiarato: meno
ricerche (−32%) e più finali per spazio, perché una ricerca non può più pagarsi con risultati già visti. Il banco non
esercita mai il gruppo più nuovo né C2 (0 eventi): quei percorsi sono provati dai test unitari (§4).

## 3. Rigioco CPU dei 5 casi 16K

Banco originale del fumo `smoke16k` (G = D con guardia), rieseguito su AI395 con il motore finto severo
`fake_engine_alb_script.py` e le recall registrate del modello vero (`replay-script-all.json`), stessa storia ALB,
stessa config `proxy-cfg-D.json` (`response_floor` 4096, `max_tokens` 4096, budget di ragionamento 3072), W = 16.384;
un passaggio con `venv-034` (main `9a33f6c`) e uno con `venv-035` (ramo). Due modifiche dichiarate al motore finto
(copia di verifica): `completion_tokens` contati col tokenizer del pack come fa Strata (prima chars/3 sul JSON:
raddoppiava il ragionamento e `S` della 0.3.5 si misura da `usage`), e l'evento `gold` elenca i risultati presenti
nella richiesta di risposta. Zero sforamenti sul motore in tutti i passaggi.

| caso | 0.3.4: chiusura / prove nella finale | 0.3.5: chiusura / prove nella finale |
|---|---|---|
| `38_i13_j22_a_0031` | 4 ricerche + 1 giro sprecato ("limite"), finale al giro 5; 4 risultati tutti su `re5eb79812bb5` (round 22), quello con `0031` presente | finale diretta al giro 4 (limite); stessi 4 risultati, nessuno tolto |
| `2_i6_j13_a_0892` | 4 ricerche + giro "limite"; **finale con 4 ricevute: tutte le prove tolte** | **chiusa per spazio dopo 1 ricerca** (giro 1); il risultato resta: i soli due record con `0892` (`r62b445b96bea` msg 37 = round 9, `re2cf81bd29a0` msg 41 = round 10) |
| `13_i3_b` | 4 ricerche + giro "limite"; 4 risultati, gold 2/2 nel ciclo | finale diretta al giro 4; 4 risultati, gold 2/2 |
| `42_i3_j13_a_0706` | 4 ricerche + giro "limite"; 4 risultati | finale diretta al giro 4; 4 risultati |
| `10_r21_a_0957_neg` | nessuna recall (dati visibili nel prompt) | uguale |

Variante a due prove (copione sintetico sugli stessi dati, `replay-script-two.json`: prima il round 22 poi il round 13
per `0031`; prima la timeline di `0892` poi l'uscita del round 6):

- `0031`: 0.3.4 e 0.3.5 consegnano entrambe le prove (`re5eb79812bb5` round 22 e `rd2b97e19767e` round 13, entrambe
  con `0031`) e le tengono fino alla finale (P = 9.318).
- `0892`: 0.3.4 fa la seconda ricerca togliendo la prima (finale: 1 ricevuta + "no line contains '0892'"); 0.3.5
  chiude dopo la prima, che resta.

Perché `0892` chiude dopo una ricerca: P0 = 10.684 su 16.384; dopo il primo risultato (1.040 token) il prompt è
11.069 e un altro giro richiede `P ≤ 16.384 − 64 − (256 + 1.024 + 4.096) − H ≈ 10.894`: mancano ~175 token. Con le
costanti decise nella SINTESI (R = 1.024 per giro, F = 4.096) è il comportamento atteso; non l'ho forzato.

Limiti del rigioco: il modello è un copione (recall registrate dal modello vero su altri prompt, ragionamento fittizio
" think" lungo quanto il budget); il rigioco si ferma quando la 0.3.5 chiude, quindi nessuna recall successiva della
traccia è attribuita alla nuova politica; l'oracolo del motore finto non è un modello e il punteggio non è una misura;
per `0031` le recall registrate non chiedono mai il round 13 (il copione registrato chiude per limite di giri in
entrambe le versioni, non per spazio). Copioni, script di analisi e uscite: `out/correzione2/replay/`.

## 4. Test

Nuovi (`tests/test_search_keeps_evidence.py`, 27 test): estratti disgiunti dello stesso `rid` (righe, foglia),
estratti per query con `span` coperti da una pagina solo se tutte le sorgenti lo sono, taglio dopo `meta`, record
diversi con testo identico, provenienza raggruppata opaca, `meta` vuoto e identità del testo, testimone diventato
ricevuta, stesso identificativo in due record (entrambi fascia 3), ragionamento mai protetto e niente sconto, `_src`
privato; ciclo completo a 16K con F = 4.096: due prove consegnate e presenti alla finale (anche in streaming),
generazione molto oltre S (argomenti enormi: costa solo il gruppo nuovo), tre chiamate nello stesso giro senza spazio
(la prima entra tagliata, la seconda non entra nemmeno come avviso ed esce, la terza non cerca), `max_recall_rounds`
0/1/2 (esattamente N ricerche e poi la finale diretta), K dopo la chiusura (nessun `recall` riofferto), chiamate miste
in streaming; unitari: confine esatto dell'ammissione (uguaglianza → ricerca, +1 token → finale), S mai calante e
censura, avviso che non entra → chiamata annullata, risultato corto che entra resta intero, avviso entro il `cap`,
controllo F prima del ciclo, prova lunga contro testo inutile corto, definizioni caricate allo stesso controllo,
errore di conteggio esatto registrato. Controllo indipendente lato motore (`seen_then_lost`) su ogni ciclo.
Sul codice 0.3.4 (`9a33f6c`, stesso file di test) 25 dei 27 falliscono (9 fallimenti contando i sottocasi, 18 errori
per API mancanti);
nei 7 test del ciclo completo, che usano solo l'interfaccia del proxy, 6 falliscono per comportamento (resta verde
solo quello delle chiamate miste, comportamento conservato). Sulla 0.3.5 tutti verdi.

Aggiornati (perché codificavano il vecchio comportamento):

- `test_window_guard.test_long_loop_results_never_empty`: chiedeva ≥ 4 risultati da 4K token in 16K; la 0.3.3/0.3.4
  ci arrivava solo togliendo i risultati dei giri precedenti (Argon §5.1.3). Ora: ≥ 2 risultati non vuoti, nessuna
  non-copia tolta in ricerca, nessuna emergenza, finale per spazio.
- `TestQuestionIdents` (Argon §5.1.3): `test_receipts_the_question_is_about_are_removed_last` diventa
  `test_search_round_never_removes_a_result` (al giro di ricerca si toglie solo ragionamento, poi finale con tutti i
  risultati); l'ordine "senza identificativi prima" si prova nella finale d'emergenza
  (`test_final_emergency_removes_the_unrelated_result_before_the_question_s`, dopo il gruppo più nuovo);
  `test_a_piece_with_the_question_idents_is_removed_when_it_is_the_only_one` passa alla finale (il pezzo protetto si
  toglie solo lì, registrato come `protected` ed `evidence_lost`).
- `TestRemoval` (3 test): il risultato del giro 2 identico a quello del giro 0 è ora una copia verificata (fascia 0)
  e va per primo; le politiche ordinano dentro la fascia.
- `test_proxy.test_recall_rounds_end_with_answer`: N ricerche e poi la finale diretta (N + 1 richieste, non N + 2);
  il motore finto risponde quando `recall` non è offerto e c'è la nota di limite.
- `test_default_identity`: il test a 128K con conteggio **esatto** (tokenizer passato al `TokenCounter`) e **tre recall
  vere** (query, id+query, id): richieste identiche byte per byte con guardia accesa e spenta. Il vecchio test a 128K
  resta (stima, nessuna recall).
- `test_recall_small_window` (caso dal vivo ds4 8K, t_5de89914): la sequenza registrata (pagina, pagina,
  id+offset+query, pagina) arrivava alla 3a riga ERROR solo togliendo le pagine già lette. Con la 0.3.5, dopo due
  pagine da ~1K P ≈ 5,0K e un altro giro richiede P ≤ ~4,75K: la ricerca chiude, le due pagine restano e la risposta ha
  F = 2.048. Ora due test: con `id + query ERROR` la riga arriva (8K); la sequenza registrata tiene le pagine e chiude
  per spazio. **È un costo reale della regola a finestre piccole** (vedi §5).

Suite completa verde: Python 3.14 con tokenizer esatto (`CTX_TOKDIR`) 368 test OK (4 saltati: semantica Windows, fallback
senza tomllib, Strata non importabile, file pi mancante); Python 3.10 (senza `tokenizers`) 342 test OK (10 saltati).

## 5. Cosa non è provato

- Qualità con un modello vero: nessuna GPU. Il rigioco usa recall registrate e un motore a copione.
- Il gruppo più nuovo ridotto/annullato e C2 non compaiono mai nel banco a tappeto (0 eventi): provati solo dai test.
- A finestre piccole la ricerca chiude prima: 8K con pagine da 1K si ferma dopo 2 pagine (il caso ds4 dal vivo non
  arriva più alla 3a riga ERROR con la sua sequenza); 16K con P0 ≈ 10,7K (`0892`) dopo 1 ricerca. Le costanti
  R = 1.024 per giro e F = 4.096 sono quelle decise: abbassare R (o farlo dipendere dallo spazio) è una decisione da
  prendere, non l'ho cambiata.
- La copertura distribuita su più risultati è riconosciuta; la copertura fra tipi diversi (pagina contro righe) no:
  copie non riconosciute = ottimizzazione mancata, mai perdita.
- La normalizzazione del banco ALB (`normalize_format`) e i difetti di B/T e dei costi: fuori da questa card (banco,
  non proxy).

## 6. Elenco "non partire con la GPU" (SINTESI)

| voce | esito |
|---|---|
| uno sforamento (`P + max_tokens + margine > W`) | **superato**: 0 su 25.836 richieste (banco, motore con margine 64), 0 nei test, 0 nei rigiochi |
| una finale ammissibile sotto F | **superato**: 0 risposte sotto F con F raggiungibile (banco; `floor_short` 0); finali dei rigiochi a 4.096 |
| una falsa deduplicazione | **superato**: 0 su 392 copie tolte (controllo indipendente sulle righe) |
| una prova vecchia non-copia persa per un giro di ricerca (finale e C2 compresi) | **superato**: 0 (banco, controllo lato motore; 0.3.4: 669); rigiochi: 0 |
| sweep con tokenizer non caricato | **superato**: 2.000/2.000, 0 errori di conteggio |
| nessun caso con ≥ 2 recuperi distinti consegnati | **superato**: 1.281 scenari |
| rigioco: `0031`/`0892` chiusi prima di avere entrambe le prove o con una tolta prima della finale | **`0031` superato** nella variante a due prove (entrambe presenti alla finale); col copione registrato il round 13 non è mai chiesto (chiusura per limite di giri, non per spazio, uguale alla 0.3.4). **`0892` non superato alla lettera**: nessuna prova tolta (la 0.3.4 le toglieva tutte), ma la ricerca chiude per spazio dopo 1 giro, prima della seconda chiamata del copione; mancano ~175 token con R = 1.024 e F = 4.096 (§3). Da decidere |
| una risposta sbagliata o un rifiuto che guadagna punti con la normalizzazione | **non verificato qui**: è il normalizzatore del banco ALB, fuori dal proxy |
| differenze a 128K fra guardia accesa e spenta (conteggio esatto, recall reali) | **superato**: richieste identiche byte per byte, 3 recall vere, tokenizer esatto |
