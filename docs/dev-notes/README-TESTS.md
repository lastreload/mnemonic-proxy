# README-TESTS — il README sotto test

Nota di sviluppo in italiano, come le altre in `docs/dev-notes/`. Il contenuto pubblico
(README, messaggi, commenti dei test) è in inglese.

## Perché

Un comando documentato che non esiste è peggio di una funzione mancante: il README aveva
`mnemonic-proxy check` prima che il sottocomando esistesse, e nessun test se ne accorgeva.
Da questa versione il README è sotto test su tre livelli, tutti con la sola libreria standard.

## Etichette dei blocchi

Ogni blocco recintato eseguibile (`sh`, `bash`, `powershell`, `json`, senza linguaggio) porta
un'etichetta in un commento HTML sulla riga sopra la recinzione, quindi invisibile su GitHub:

    <!-- readme-test: ci -->            eseguito davvero dal livello 2
    <!-- readme-test: windows -->       PowerShell, solo Windows
    <!-- readme-test: real-engine -->   serve un modello, un motore o un programma di terzi
    <!-- readme-test: illustrative -->  niente da eseguire: output, dati di configurazione,
                                        pulizia distruttiva, o un comando che la CI esegue
                                        già come se stessa (la suite unitaria)

I blocchi JSON portano anche un attributo nello stesso commento, così un frammento parziale o
la configurazione di un altro programma non finiscono mai in `Config.from_dict`:

    <!-- readme-test: illustrative; config: proxy -->     configurazione completa del proxy
    <!-- readme-test: illustrative; config: partial -->   poche chiavi, non un file intero
    <!-- readme-test: illustrative; config: client -->    configurazione di un altro programma

`python3 tools/readme_blocks.py` elenca i blocchi; `--unlabeled` mostra solo quelli senza
etichetta (deve stampare zero), `--json` dà l'inventario leggibile da programma.

## I tre livelli

| livello | file | che cosa verifica | durata |
|---|---|---|---|
| 1 statico | `tests/test_readme_static.py` | etichette presenti e note; ogni riga di comando `mnemonic-proxy` / `mnemonic-mcp` del README passa il parser **vero** del programma; ogni esempio JSON di configurazione usa solo campi reali di `Config` ed è accettato da `from_dict`; ogni link `#ancora` risolve | < 0,1 s |
| 2 eseguito | `tests/test_readme_run.py` | i blocchi `ci` vengono eseguiti davvero contro `ctxproxy.fake_engine` (sapori `llama` e `strata`) | ~7 s |
| 3 esaustivo | `tests/test_readme_coverage.py` | nessun sottocomando, opzione o campo di `Config` esiste nel codice senza essere documentato nel README o dichiarato interno in `tools/readme_internal.txt` con un motivo | ~0,2 s |

Livelli 1 e 3 fanno parte della suite normale (`python3 -m unittest discover -s tests -t .`).
Il livello 2 è `@skipUnless(README_RUN)`: lo accende `tools/readme_run.sh` e il job CI
`readme-run`.

## Niente duplicazione del codice

I parser non sono ricopiati nei test. `check`, `demo`, `forget` e `mnemonic-mcp` espongono il
loro costruttore (`build_check_parser`, `build_demo_parser`, `build_forget_parser`,
`build_mcp_parser`) e `main()` lo chiama; `tools/readme_cli.py` importa gli stessi
costruttori. L'elenco dei sottocomandi si ricava dal sorgente di `server.main` con
`inspect.getsource`, quindi un sottocomando nuovo compare da solo nel livello 3.

## Livello 2: che cosa viene sostituito

I comandi arrivano dal README, non da una copia. Si sostituisce solo ciò che un documento non
può sapere: `--upstream` (il motore finto avviato dal test), `--port` (una porta libera),
`--data` (una cartella temporanea), `--config` (un file scritto dal blocco `config: proxy` del
README stesso) e `--db` di `mnemonic-mcp` (l'archivio che `demo --keep` lascia). Tutto il resto
della riga è quello pubblicato.

Asserzioni: `demo` stampa PASS ed esce 0; `check` contro il motore finto dice READY (uscita 0
o 2); `check` senza motore esce 1, dice NOT READY e stampa *start the engine first*; il proxy
davanti al motore finto risponde 200 a una richiesta Chat Completions e `/v1/engine` riconosce
il motore; `forget --list` esce 0 sull'archivio del demo; `mnemonic-mcp` su stdio risponde a
`initialize` ed elenca `recall`, `conversations`, `journal`.

## Ambiente pulito

`tools/readme_run.sh` costruisce la ruota, la installa in un venv nuovo e lancia il livello 2
da una cartella di staging che contiene solo `README.md`, `tools/readme_blocks.py`,
`tools/readme_cli.py` e `tests/test_readme_run.py`: senza il sorgente, `import ctxproxy` può
risolvere solo sul pacchetto installato. `tools/readme_run.sh --source` salta la costruzione e
usa l'albero di lavoro. Il job CI `readme-run` fa la stessa cosa con la ruota di `package`, su
Python 3.10 e 3.14.

`tools/readme_newuser.sh <uvx|pip> <pypi|wheel> [WHEEL_DIR]` è la prova "utente nuovo": non
usa nulla del repository, installa il pacchetto come dice il README e percorre la prima
schermata e T0. Va lanciato in un contenitore pulito.

## Verifica fatta su AI395

Matrice in contenitori `python:*-slim` rootless (podman, 6 GB, 8 CPU, nessuna GPU, nessun
modello): `python:3.10-slim` e `python:3.14-slim` × `pip` e `uvx` × pacchetto PyPI 0.3.20 e
ruota del ramo. Otto configurazioni su otto a buon fine, 4–8 s ciascuna; in `uvx` + PyPI si
esegue anche `uvx mnemonic-proxy demo` alla lettera, il primo comando del README. Le due
immagini scaricate per l'occasione sono state rimosse dopo la prova.

## Quando si aggiunge qualcosa

- un blocco nuovo nel README: mettergli l'etichetta, altrimenti il livello 1 fallisce;
- un'opzione nuova: documentarla nella tabella «Every command-line option», altrimenti il
  livello 3 fallisce;
- un campo nuovo di `Config`: documentarlo nella tabella dei campi o in «Every configuration
  field», altrimenti il livello 3 fallisce;
- qualcosa di davvero interno: aggiungerlo a `tools/readme_internal.txt` con il motivo. Le
  voci stantie (che non esistono più nel codice) fanno fallire il livello 3, così l'elenco non
  nasconde buchi veri.
