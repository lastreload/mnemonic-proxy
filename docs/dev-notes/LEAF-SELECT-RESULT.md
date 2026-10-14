# LEAF-SELECT — recall a sotto-blocco foglia e stop prematuro dopo il recall (P0–P6)
Maurizio Verde — LastReload. 12–13/10/2026, PC4070TI (RTX 4070 Ti 12 GB, 64 GB RAM).
Opzioni: `recall_leaf_select` (`ctxproxy/leafsel.py`), `answer_thinking_after_recall` e `truncation_guard`
(`ctxproxy/answerfix.py`). Tutte spente di serie: con la config di serie il comportamento è identico byte per byte
alla 0.3.2 (`tests/test_default_identity.py`).

## Problema: i gemelli
Banco a trappola (insieme A): l'agente legge un output lungo, il proxy lo nasconde e molti turni dopo l'utente
chiede un valore preciso (*"qual è la porta della replica del database del servizio pagamenti?"*). Ogni output
contiene molti **gemelli**, cioè valori della stessa forma che appartengono a record diversi.
Granite 4.2 3B (LoRA r16kl/r16v3b), finestre 4K e 8K, proxy 0.3.1 small-window. Su 1.651 risposte con recall: 877
giuste, 762 sbagliate. Di queste, **574 sono un gemello**.

## P0 — diagnosi (CPU): due problemi diversi
- **R (recupero)**, 55% dei gemelli: il primo recall non contiene il record giusto. È l'inizio del testo, oppure una
  pagina che si interrompe subito prima del record. Il modello non chiede il resto e risponde con il gemello che vede.
  È il caso tipico di json, yaml, csv, docker e kube.
- **L (lettura)**, 45%: la riga giusta c'è, ma il modello copia quella del record accanto. È il caso tipico di py,
  xml e log.
- Confermati in cieco con due annotatori: pilota 16/16 R e 16/16 L; conferma 26/26 R e 24/26 L. Il replay del prompt
  è esatto byte per byte in 1.524 turni su 1.651; il resto differisce solo per l'ordine di una riga d'intestazione,
  corretta.

## P1/P1b — oracolo (GPU, 40 turni pilota)
- Bracci: A = recall attuale; B = A con la cornice; C = estratto oracolo verbatim con la prova completa; D = C con
  la cornice.
- Flash Next 32K (Qwen3.8, IQ3_XXS, Strata 0.1.40): R da 0/10 a **9/10** con C e D. La sola cornice (B) non serve.
- Granite (modello e finestra originali): D corregge 17 su 20 casi R+L.
- L'estratto stretto C2 tiene solo le righe-chiave degli antenati e la foglia, verbatim, con `⋮` sui salti. Dà lo
  stesso effetto ed entra nel budget.

## P2/P3 — selettore reale (senza oracolo)
- `leafsel.py`: parser conservativi, BM25 su foglia e antenati, guidato dalla query e/o dall'ultima domanda
  dell'utente. Il budget arriva dal server, prima della selezione. Se il parse è incerto si usa il recall di serie.
- Granite a un passo: 16/20 R+L corretti, 0 regressioni.
- Flash Next multi-passo: R da 6/10 a 9/10; recall extra da 19 a 2; tempo sugli R da 72 s a 31 s.
- Latenza del selettore: p95 35 ms.

## P4/P5 — metrica CPU su tutto l'insieme e conferma sugli 80 sigillati
- Metrica: la prova completa è nel PRIMO recall consegnato al modello? Si misura con il replay dei turni congelati
  attraverso il proxy e un motore finto che ripete il copione.
- Aggiunti formati (diff, git log, pytest, log con marca temporale, Python) e l'identità del record json. Altre
  regole: contraddizioni (replica/primario), due letture se il punteggio è quasi pari, più uscite candidate per la
  query, intervalli omessi con `offset`, controllo di fedeltà riga per riga. P5 aggiunge la cornice corta, provata
  prima di togliere blocchi.
- Risultato: dev **98,9%** (1.495/1.512, legacy 66,4%), sigillati **97,5%** (78/80, legacy 65%), 0 regressioni,
  fedeltà 1.555/1.555, git log 28/28.
- GPU P4 (Flash Next, 80 sigillati): selettore e recall di serie pari, 69/80 entrambi, con 5 regressioni. Il costo
  invece scende: recall extra da 47 a 23, token di prompt −25%, tempo −23%.

## P5/P6 — lo stop prematuro di Flash Next dopo un recall
- Le regressioni di P4 sono quasi tutte risposte **troncate a metà del valore**: "…is WX" per `WX-5130-TEAL`,
  "71." per `71.3`, "tests" per `tests/test_ledger.py::…`. Su 576 risposte testuali finali dei test, 34 (5,9%) sono
  troncate senza l'ago: 18 numeri tagliati al punto decimale, gli altri identificatori tagliati prima di `-` o `/`.
- I due rimedi (P6), entrambi opt-in:
  - **T**: thinking acceso solo nel giro che risponde dopo un recall, con tetto per richiesta
    (`reasoning_budget_tokens`, Strata #123).
  - **K**: una risposta di ≤16 token che finisce con un prefisso proprio di un token del recall viene rigiocata una
    volta con il thinking.
  - In entrambi i casi il ragionamento non torna al client e il prefisso della storia resta stabile.
- GPU, harness multi-passo, T=0, seed 1701, Flash Next 32K, **80 sigillati**:

| braccio | giuste | contro L | recall extra | tempo/turno mediano | TTFT risposta mediano |
|---|---|---|---|---|---|
| L (recall di serie) | 70 | – | 40 | 2,94 s | 2,09 s |
| S (selettore) | 70 | +3 / −3 | 18 | 2,40 s | 1,85 s |
| S+K | 75 | +7 / −2 | 18 | 2,43 s | 1,86 s |
| **S+T512** | **80** | **+10 / −0** | 3 | 3,70 s | 2,87 s |
| L+K | 68 | +1 / −3 | 43 | 3,02 s | 2,12 s |

- Rumore fra due corse uguali di L: 3 turni su 80 cambiano esito. Il +10 di S+T è netto, il +5 di S+K è al limite.
- K sugli 80 sigillati: 4 interventi, tutti utili, 0 falsi positivi.
- Pilota (40, quasi tutti a un passo): S+T 40/40, ma il tempo mediano per turno raddoppia (5,02 s contro 2,55 s).

## Prima della PR (branch feat/leaf-select, solo CPU)
- Il budget del recall ora conta anche la cornice del proxy, ma solo con l'opzione accesa. Prima un tetto di 300
  token dava 351 token: intestazione e riga di continuazione di `recall(id)` stavano fuori dal conto, e l'avviso di
  `fit_tokens` vale circa 40 token contro i 30 riservati.
- Il selettore vale anche nel percorso `recall2` (`recall_struct`/`flex`/`multi`) e nel server MCP.
- `recall2` non inventa più `offset=None`.
- `recall(id)` senza query e senza corrispondenza forte con la domanda: inizio del file, come senza l'opzione.
- Metrica CPU rifatta sul branch: identica a e976b5a turno per turno (dev 1.495/1.512, sigillati 78/80, 0 differenze
  di token nel primo recall, fedeltà 0 righe non verbatim, selettore p95 14,6 ms).

## Limiti
- Una sola macchina e un solo modello per la prova end-to-end; il banco è sintetico.
- Il costo di T in sessioni agentiche reali non è misurato. Il prossimo passo è una prova d'uso con Claude Code o pi
  tramite il proxy, misurando TTFT e tempo per turno.
