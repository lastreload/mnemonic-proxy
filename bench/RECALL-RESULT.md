# strata_recall: recupero affidabile (task t_3b7d7714)

Tutto è stato fatto offline e solo su CPU, senza modelli, solo con SQLite/FTS5 e Python stdlib. Le opzioni nuove sono tutte **spente di serie**: con le opzioni spente il comportamento è identico a prima (`recall()` → `recall_legacy()`, verificato da test). Il proxy installato non è stato toccato.

## Banco di prova (`proxy/bench/`)
- Archivi: copie in `bench/data/{prova-paging,nibble2,g3}.sqlite`, fatte con la API backup di SQLite da PC4070TI e da `g3/results/paging/archive-auto.sqlite`. Le copie sono anche in `pc4070ti:/mnt/mneme-nvme/recall-bench/`.
- Domande: `questions_spec.py` → `build_questions.py` → `questions.json`. In tutto 71: 37 per lo sviluppo (dev) e 34 per la verifica (test). Comprendono le 6 di oggi, le domande di G3, domande nuove da prova-paging e nibble2, 8 controlli negativi e 4 parafrasi. Per ogni domanda l'evidenza è una regola (regex + ruolo + intervallo di messaggi) che viene risolta negli id dell'archivio. Se un gruppo non trova evidenza, il build fallisce, così le etichette non possono restare vuote senza che ce ne accorgiamo.
- Separazione: la verifica è stata tenuta da parte. La taratura è avvenuta solo su dev; con test ho solo misurato.
- Comandi: `python3 bench/recall_bench.py --label X --split dev|test [--set opzione=valore …] [--list-miss] [--show id]` e `python3 bench/auto_bench.py --split all`.
- Metriche:
  - e@5: tutte le evidenze compaiono nei primi 5 risultati della prima chiamata;
  - e@budget: tutte le evidenze sono coperte entro 5 chiamate simulate, rispettando il tetto di token;
  - MRR;
  - negativi corretti: nessun risultato "forte";
  - token restituiti per domanda.

## Risultati: dalla versione di serie a ogni passo
| config | split | e@5 | e@budget | MRR | tok/dom | chiamate | negativi |
|---|---|---|---|---|---|---|---|
| di serie (baseline) | dev | 0.485 | 0.788 | 0.380 | 7131 | 1.91 | 0/4 |
| + struttura (`recall_struct`) | dev | 0.455 | **0.970** | 0.414 | 3413 | 1.58 | 4/4 |
| + parole flessibili (`recall_flex`) | dev | 0.424 | 0.939 | 0.411 | 3811 | 1.70 | 4/4 |
| + `queries` (`recall_multi`) | dev | 0.424 | 0.970 | 0.411 | 3752 | 1.61 | 4/4 |
| di serie (baseline) | test | 0.500 | 0.900 | 0.463 | 5940 | 1.53 | 1/4 |
| + struttura | test | 0.467 | 0.900 | 0.539 | 3468 | 1.53 | 4/4 |
| + parole flessibili | test | 0.533 | **1.000** | 0.531 | 3233 | 1.43 | 4/4 |
| + `queries` | test | 0.533 | **1.000** | 0.531 | **2737** | **1.20** | 4/4 |

Sulla verifica la configurazione completa trova tutta l'evidenza: 1.000 contro 0.900. Usa il 54% di token in meno (2737 contro 5940), fa meno chiamate e riconosce come vuoti tutti i controlli negativi (4/4 contro 1/4).

e@5 non migliora in modo netto. Il guadagno viene dalle modalità per file (first e timeline), dai collegamenti nelle intestazioni e dall'estratto centrato sul passaggio, non dal ranking della prima pagina.

Limiti da tenere presenti:
- la verifica è piccola (30 domande positive + 4 negative);
- `recall_flex` costa CPU: costruisce un indice normalizzato per conversazione e raddoppia circa il tempo del banco;
- su dev resta una domanda mancata, pp-n11. È una parafrasi italiana ("denaro iniziale") di `startGold`: servirebbe un sinonimo, non una radice. È l'unico indizio, debole, a favore dei vettori. Una sola domanda non giustifica un modello.

## Esempi prima/dopo
**q4** (oro iniziale nella prima scrittura e valore considerato nel ragionamento):
- Prima: `query=startGold` restituisce 17 risultati partendo dai più recenti (msg 847, «ripristino di startGold: 250…»). La scrittura del messaggio 21 e il ragionamento non compaiono entro il budget; l'evidenza coperta è 1/2.
- Dopo: `path=src/config.js mode=first query=startGold` dà in una sola chiamata un'intestazione «PRIMA scrittura … messaggio 21 … argomenti id=a4875f109c44e, uscita id=r813502f7a6da, ragionamento id=t42d3ee9ac871». Seguono l'estratto degli argomenti `startGold: 265` e l'estratto del ragionamento «Economy: startGold 240, lives 20…». L'evidenza coperta è 2/2. L'intestazione ricorda che ciò che è SCRITTO vale più di ciò che è CONSIDERATO.

**q5** (valori del boss, dalla prima versione alle correzioni):
- Prima: le ricerche per `boss hp` restituiscono pezzi sparsi; 4/5 gruppi entro il budget.
- Dopo: `path=src/config.js mode=timeline query="boss hp"` produce la cronologia ordinata delle 29 operazioni sul file. La prima scrittura è il messaggio 23 (indice registro, = uscita della scrittura del msg 21). Per ogni modifica c'è la riga corrispondente:
  - msg 153: `hp: 1600 → 1450`, armor 16, leak 10
  - msg 157: `1450 → 1250`, armor 14
  - msg 167: `1250 → 1150`, speed 28
  - msg 221/225: modifiche successive (1300/1200/1100, leak 8)

  L'evidenza coperta è 5/5. Ogni riga riporta gli id degli argomenti e dell'uscita, legati da tool_call_id (collegamento registrato, non semplice adiacenza).

## Richiamo automatico: indizio invece dei pezzi (`auto_recall_hint`)
Sono 56 domande con evidenza nella parte nascosta, più 8 negative:
| modo | scatta | almeno un'evidenza | token medi | negativi con inserimento |
|---|---|---|---|---|
| pezzi (di serie) | 33/56 | 13/56 | 650 | 1/8 |
| indizio | 33/56 | 13/56 | **73** | 1/8 |

L'indizio usa gli stessi candidati e raggiunge la stessa copertura con circa 9× meno token. Cita id e messaggi e lascia al modello `strata_recall id=…`. Il limite è la selezione dei candidati (13/56), non il formato.

Ho provato anche candidati presi dalla ricerca a passaggi in AND: non scattava mai su domande intere, quindi l'ho scartata.

Il verso di bm25 è stato controllato: `search_fts_scored` restituisce `-bm25`, dove più alto è meglio, e la soglia `score < auto_recall_min_score` scarta correttamente i punteggi bassi. Non c'era nessun errore di verso.

## Cosa è cambiato
- `ctxproxy/recall2.py` (nuovo):
  - indice a sottopassaggi con rid, offset e lunghezza; l'estratto mostra il passaggio che corrisponde;
  - intestazioni con id, numero di messaggio, segmento, ruolo, strumento, file e collegamenti argomenti↔uscita (registrati con tool_call_id oppure segnati "per posizione");
  - `mode`:
    - con `path`: `first` e `timeline`;
    - con `id`: `neighbors`, `output` e `reasoning`;
    - `tree`: albero segmento → blocco → turni;
  - filtri `role`, `seg`, `from` e `to`; `order=tempo`;
  - `queries=[…]` con fusione per rango;
  - parole flessibili: identificatori spezzati (camelCase, snake_case, kebab) e radice leggera italiano/inglese uguale per indice e domanda, senza Porter. La ricerca prova prima in modo stretto (AND) e poi largo (OR);
  - risposta vuota con suggerimenti: parole con o senza risultati, file modificati, mode=tree;
  - marcatura forte o parziale.
- `ctxproxy/core.py`:
  - opzioni di Config `recall_struct`, `recall_multi`, `recall_flex`, `recall_struct_max_tokens`, `auto_recall_hint`, `auto_recall_hint_k`;
  - `recall()` smista verso recall2 oppure verso `recall_legacy()`;
  - la definizione dello strumento viene estesa (con la riga sulle istruzioni per la prima versione) solo se le opzioni sono attive;
  - `auto_candidates()` e `auto_hint()` sono stati estratti da `_auto_recall`; il comportamento di serie è invariato.
- `tests/test_recall2.py`: 8 test nuovi.
- `bench/`: banco, domande e strumenti di etichettatura (`grep_archive.py`, `grep_batch.py`, `dump_range.py`, `fops.py`, `one.py`).

## Test
- Suite completa su PC4070TI, in una copia (`/mnt/mneme-nvme/recall-bench/repo-t3b`, con `/mnt/mneme-nvme/g3/venv/bin/python -m unittest discover -s tests -t .`): **109 test, OK** (5 saltati).
- Su MVLINNA due test di kvarchive falliscono anche nella copia non modificata, e `test_postprova::TestExact` fallisce perché manca `tokenizers`. Sono problemi dell'ambiente, non del codice.

## Commit
Commit locale sul ramo `feature/paging` di `~/src/virtual-context-proxy` (non pubblicato).

## Installazione (dopo la sessione dell'utente; da fare a cura dell'orchestratore)
1. Copiare `ctxproxy/recall2.py` e `ctxproxy/core.py` dal commit in `/mnt/mneme-nvme/ctx-proxy/ctxproxy/` su PC4070TI.
2. Aggiungere a `cfg.json`: `"recall_struct": true, "recall_flex": true, "recall_multi": true, "auto_recall_hint": true`.
3. `systemctl --user restart ctx-proxy`.

Proposta per l'impostazione di serie: tutte e quattro attive. Va dichiarato che questo cambia l'output di `strata_recall` e la forma del richiamo automatico. Prima di attivarle di serie, prova dal vivo su una sessione tipo prova-paging.

## Cosa resta
- La selezione dei candidati del richiamo automatico (13/56) è il collo di bottiglia.
- Sinonimi italiano/inglese per le parafrasi (pp-n11). I vettori non sono giustificati dal banco attuale.
- Prova dal vivo con le opzioni attive.
