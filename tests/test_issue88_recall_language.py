# Author: Maurizio Verde — LastReload
"""#88 (residuo): la riga dei RISULTATI DI RECALL emessa nel flusso deve seguire la lingua della conversazione.

    python3 -m unittest -v tests.test_issue88_recall_language

Prima della correzione `server.py` scriveva `[<nome> <args>: %d risultati]` in modo incondizionato (stringa
presente dalla 0.1.0, anteriore alla selezione di lingua della 0.3.1), mentre i suoi fratelli dello stesso
giro — «definizioni caricate», «chiamata bloccata», «ricerca chiusa» — erano già localizzati (0.3.28).
In una sessione inglese il client mostrava quella riga nel riquadro `thinking`: è il testo del proxy, non
una scelta del modello.

Integrazione HTTP vera (ThreadingHTTPServer + make_handler) con motore finto in-process e archivio reale
temporaneo: le uscite grandi vengono mascherate e archiviate dal proxy, poi il modello chiama il recall e
i suoi risultati tornano dall'archivio. Nessuna GPU, nessun modello, nessuna rete esterna: le stringhe
controllate sono deterministiche.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ctxproxy.core import RECALL_NAME, Config, Journal, Store, TokenCounter  # noqa: E402
from ctxproxy.server import Proxy, make_handler  # noqa: E402
from ctxproxy.upstream import Upstream  # noqa: E402
from tests.fake_engine import FakeEngine  # noqa: E402

SYSTEM = {"role": "system", "content": "You are a coding agent. " * 10}
# una domanda inglese e una italiana: la lingua della conversazione la decide la configurazione, non il testo
EN_QUESTION = "QUESTION: does db-s3-007 appear in the service logs?"
IT_QUESTION = "DOMANDA: db-s3-007 compare nei log dei servizi?"
NOTHING = "zzz-absent-token-9f4c"

# contenuto italiano LEGITTIMO dell'utente/archivio: deve tornare intatto anche in conversazione inglese
IT_PAYLOAD = "ATTENZIONE: il servizio db-s3-007 non risponde; nessun risultato dalla sonda italiana."

# parole dei testi italiani del proxy cercate SOLO nei segnaposti strutturali del flusso (mai nel testo libero)
IT_MARKER_WORDS = ("risultat", "nessun", "definizioni caricate", "gestore del contesto", "ricerca chiusa",
                   "eseguita", "bloccata", "ricevuta", "messaggio", "uscita", "strumento")

TOOLS = [{"type": "function", "function": {"name": "bash", "description": "run a command", "parameters": {
    "type": "object", "properties": {"command": {"type": "string"}}}}}]


def record(tag: str, n: int) -> str:
    return "".join("%s line %d: host db-%s-%03d port %d status ok\n" % (tag, i, tag, i, 30000 + i)
                   for i in range(n))


class RecallStreamBase(unittest.TestCase):
    """Sessione lunga via HTTP: uscite grandi archiviate, poi una domanda che fa chiamare il recall."""

    cfg_over: dict = {}
    question = EN_QUESTION
    query = "db-s3-007"
    steps = 10
    size = 300

    @property
    def rn(self) -> str:
        """Nome di chiamata del recall in questa conversazione (`recall_tool_name` se configurato)."""
        return self.cfg_over.get("recall_tool_name") or RECALL_NAME

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.recalls = 0
        self.eng = FakeEngine(self.policy)
        url = self.eng.start()
        cfg = Config.from_dict({"window": 131072, "mask_trigger": 20000, "mask_target": 10000,
                                "keep_recent_tokens": 4000, "min_mask_tokens": 200, **self.cfg_over})
        self.jpath = os.path.join(self.tmp.name, "journal.jsonl")
        self.proxy = Proxy(cfg, Upstream(url), Store(os.path.join(self.tmp.name, "a.sqlite")),
                           Journal(self.jpath), TokenCounter(3.5))
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.proxy))
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.base = "http://127.0.0.1:%d" % self.srv.server_address[1]

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        self.eng.stop()
        self.proxy.store.db.close()
        self.tmp.cleanup()

    # ---------------------------------------------------------------- motore finto
    def recall_name(self, body):
        names = {((t or {}).get("function") or {}).get("name") for t in body.get("tools") or []}
        return self.rn if self.rn in names else None

    def policy(self, body):
        last = body["messages"][-1]
        rn = self.recall_name(body)
        if rn and last.get("role") == "user" and ("QUESTION" in str(last.get("content"))
                                                  or "DOMANDA" in str(last.get("content"))):
            self.recalls += 1
            return {"role": "assistant", "content": "", "reasoning_content": "Let me look in the archive.",
                    "tool_calls": [{"id": "rc1", "type": "function",
                                    "function": {"name": rn, "arguments": json.dumps({"query": self.query})}}]}
        return {"role": "assistant", "content": "answer: yes"}

    # ---------------------------------------------------------------- client HTTP
    def post(self, msgs, stream: bool):
        body = json.dumps({"model": "m", "messages": msgs, "tools": TOOLS, "max_tokens": 1024,
                           **({"stream": True} if stream else {})}).encode()
        req = urllib.request.Request(self.base + "/v1/chat/completions", data=body,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req) as r:
            raw = r.read().decode()
        if not stream:
            return json.loads(raw)
        return [json.loads(l[6:]) for l in raw.split("\n")
                if l.startswith("data: ") and l[6:].strip() != "[DONE]"]

    @staticmethod
    def assembled(chunks):
        content, reasoning, finish = [], [], None
        for c in chunks:
            for ch in c.get("choices") or []:
                d = ch.get("delta") or {}
                if d.get("content"):
                    content.append(d["content"])
                if d.get("reasoning_content"):
                    reasoning.append(d["reasoning_content"])
                if ch.get("finish_reason"):
                    finish = ch["finish_reason"]
        return {"content": "".join(content), "reasoning": "".join(reasoning), "finish_reason": finish}

    # ---------------------------------------------------------------- archivio reale
    def history(self):
        h = [SYSTEM, {"role": "user", "content": "inspect the services"}]
        for k in range(1, self.steps + 1):
            cid = "call_%d" % k
            out = record("s%d" % k, self.size)
            if k == 3:
                out += IT_PAYLOAD + "\n"
            h += [{"role": "assistant", "content": "", "tool_calls": [{"id": cid, "type": "function", "function": {
                "name": "bash", "arguments": json.dumps({"command": "cat svc-%d.log" % k})}}]},
                {"role": "tool", "tool_call_id": cid, "content": out},
                {"role": "assistant", "content": "read svc-%d" % k},
                {"role": "user", "content": "next"}]
        return h

    def build_archive(self):
        """Manda la sessione a pezzi (come un client reale) finché il proxy maschera e archivia."""
        h = self.history()
        for k in range(4, len(h) + 1, 8):
            self.post(h[:k], stream=False)
        self.post(h, stream=False)
        self.assertTrue(self.proxy.store.db.execute("SELECT count(*) FROM archive WHERE role='tool'").fetchone()[0],
                        "l'archivio deve contenere uscite di strumento mascherate")
        return h

    def ask(self, stream=True, question=None):
        h = self.build_archive() + [{"role": "user", "content": question or self.question}]
        if stream:
            return self.assembled(self.post(h, stream=True))
        return self.post(h, stream=False)

    def events(self, name):
        if not os.path.exists(self.jpath):
            return []
        with open(self.jpath) as f:
            return [e for e in (json.loads(l) for l in f) if e["event"] == name]

    def one_recall(self):
        ev = self.events("recall")
        self.assertEqual(len(ev), 1, "esattamente un giro di recall servito dal proxy")
        return ev[0]


# ------------------------------------------------------------------ conversazione inglese (il difetto)
class TestEnglishConversation(RecallStreamBase):
    def test_recall_results_marker_is_english(self):
        asm = self.ask()
        rsn = asm["reasoning"]
        self.assertEqual(self.proxy.mgr.lang, "en")
        n = len(self.one_recall()["results"])
        self.assertGreater(n, 0, "la ricerca deve trovare pezzi archiviati")
        self.assertNotIn("risultati", rsn)
        self.assertIn("[%s %r: %d results]" % (self.rn, self.query, n), rsn)
        self.assertEqual(asm["content"], "answer: yes")

    def test_empty_search_marker_is_english(self):
        self.query = NOTHING
        asm = self.ask()
        self.assertEqual(len(self.one_recall()["results"]), 0)
        self.assertNotIn("risultati", asm["reasoning"])
        self.assertIn("[%s %r: 0 results]" % (self.rn, NOTHING), asm["reasoning"])

    def test_recalled_italian_content_is_delivered_verbatim(self):
        """Il contenuto italiano legittimo dell'archivio NON si tocca: si controlla la cornice del proxy."""
        self.query = "sonda italiana"
        asm = self.ask()
        self.assertEqual(len(self.one_recall()["results"]), 1)
        self.assertIn("[%s %r: 1 results]" % (self.rn, self.query), asm["reasoning"])
        sent = [m for b in self.eng.requests for m in b["messages"] if m.get("role") == "tool"]
        joined = "\n".join(str(m.get("content")) for m in sent)
        self.assertIn(IT_PAYLOAD, joined, "il testo italiano richiamato deve arrivare al modello intatto")

    def test_non_streaming_answer_has_no_marker(self):
        """Percorso non streaming: la riga non esiste affatto (già sano, da preservare)."""
        r = self.ask(stream=False)
        blob = json.dumps(r, ensure_ascii=False)
        self.assertNotIn("risultati", blob)
        self.assertNotIn("results]", blob)
        self.assertEqual(len(self.one_recall()["results"]) > 0, True)
        self.assertEqual(r["choices"][0]["message"]["content"], "answer: yes")

    def test_resumed_conversation_keeps_english(self):
        """Ripresa: seconda domanda nella stessa conversazione, stessa lingua memorizzata."""
        h = self.build_archive() + [{"role": "user", "content": self.question}]
        first = self.assembled(self.post(h, stream=True))
        self.assertIn(" results]", first["reasoning"])
        h2 = h + [{"role": "assistant", "content": "answer: yes"},
                  {"role": "user", "content": "QUESTION: and db-s3-007 again?"}]
        second = self.assembled(self.post(h2, stream=True))
        self.assertIn(" results]", second["reasoning"])
        self.assertNotIn("risultati", second["reasoning"])
        self.assertEqual(len(self.events("recall")), 2)

    def test_no_italian_word_in_any_structural_marker_of_the_stream(self):
        """Guardia: SOLO le righe-segnaposto del proxy (fra parentesi quadre) del flusso, non il testo libero.

        Non vieta l'italiano nel ragionamento del modello né nel contenuto dell'archivio: controlla la
        cornice che il proxy scrive da sé.
        """
        asm = self.ask()
        markers = [l for l in asm["reasoning"].splitlines() if l.startswith("[") and l.endswith("]")]
        self.assertTrue(markers, "il giro di recall deve aver emesso almeno un segnaposto")
        for m in markers:
            found = [w for w in IT_MARKER_WORDS if w in m.lower()]
            self.assertEqual(found, [], "parole italiane nel segnaposto %r" % m)


class TestEnglishRenamedTool(TestEnglishConversation):
    """Il nome di chiamata del recall è quello della conversazione (lo scenario della issue: mem0_search)."""
    cfg_over = {"recall_tool_name": "mem0_search"}

    def test_marker_carries_the_conversation_tool_name(self):
        asm = self.ask()
        n = len(self.one_recall()["results"])
        self.assertNotIn("risultati", asm["reasoning"])
        self.assertIn("[mem0_search %r: %d results]" % (self.query, n), asm["reasoning"])


# ------------------------------------------------------------------ italiano preservato
class TestItalianConversation(RecallStreamBase):
    cfg_over = {"prompt_language": "it"}
    question = IT_QUESTION

    def test_marker_stays_italian(self):
        asm = self.ask()
        self.assertEqual(self.proxy.mgr.lang, "it")
        n = len(self.one_recall()["results"])
        self.assertGreater(n, 0)
        self.assertIn("[%s %r: %d risultati]" % (self.rn, self.query, n), asm["reasoning"])
        self.assertNotIn("results]", asm["reasoning"])

    def test_empty_search_stays_italian(self):
        self.query = NOTHING
        asm = self.ask()
        self.assertEqual(len(self.one_recall()["results"]), 0)
        self.assertIn("[%s %r: 0 risultati]" % (self.rn, NOTHING), asm["reasoning"])
        self.assertNotIn("results]", asm["reasoning"])


if __name__ == "__main__":
    unittest.main()
