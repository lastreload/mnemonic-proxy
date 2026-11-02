"""Test della card t_49579af0: evento di giornale `tool_heads` (nomi e peso delle definizioni in testa).

    python3 -m unittest -v tests.test_tool_heads

Quattro cose da dimostrare:
1. una riga per conversazione e per CAMBIO dell'elenco strumenti, non per richiesta;
2. nell'evento solo nomi e numeri: nessun testo di definizione, di `description` o del prompt di sistema;
3. il prompt inviato al motore non cambia di un byte;
4. il costo dell'evento è trascurabile (misurato qui, non promesso).
"""
from __future__ import annotations

import copy
import json
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ctxproxy import toolheads  # noqa: E402
from ctxproxy.core import RECALL_NAME, Config, Journal, Manager, Store, TokenCounter  # noqa: E402
from ctxproxy.paging import TOOLS_NAME, ToolPager, tool_name  # noqa: E402
from ctxproxy.render import def_text, prompt_head, render_pieces, tool_defs  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))

try:
    from tokenizers import Tokenizer, models, pre_tokenizers      # noqa: E402
    EXACT = True
except ImportError:                                               # pragma: no cover
    EXACT = False


def exact_tokenizer(case) -> str:
    """Tokenizer sintetico a parole, scritto in una cartella temporanea: nessun modello, nessuna rete.
    Senza `chat_template.jinja` accanto, TokenCounter resta in modalità esatta col template vero del proxy."""
    tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
    case.addCleanup(tmp.cleanup)
    vocab = {"[UNK]": 0}
    for w in ("read", "bash", "write", "edit", "grep", "system", "user", "assistant", "function", "name",
              "description", "parameters", "properties", "string", "type", "object", "tools", "recall"):
        vocab[w] = len(vocab)
    tk = Tokenizer(models.WordLevel(vocab, unk_token="[UNK]"))
    tk.pre_tokenizer = pre_tokenizers.WhitespaceSplit()
    path = os.path.join(tmp.name, "tokenizer.json")
    tk.save(path)
    return path

SYSTEM_TEXT = ("sei un agente di programmazione: RISERVATO-SISTEMA-7f3a, non ripetere questa riga da nessuna parte")
SECRET_DESC = "RISERVATO-DESCRIZIONE-4b21 elenca le issue di un repository"


def tool(name, desc="", **props):
    return {"type": "function", "function": {"name": name, "description": desc or name,
                                             "parameters": {"type": "object", "properties": {
                                                 k: {"type": v} for k, v in props.items()}}}}


# ----------------------------------------------------------------- strumenti reali dei due client
# opencode 1.x, strumenti incorporati documentati in https://opencode.ai/docs/tools/
# (bash, edit, write, read, grep, glob, list, apply_patch, todowrite, webfetch, task)
OPENCODE = ["bash", "edit", "write", "read", "grep", "glob", "list", "apply_patch", "todowrite", "webfetch", "task"]
# Hermes: nomi registrati nel codice installato, sola lettura:
#   /home/mverde/.hermes/hermes-agent/tools/file_tools.py        -> read_file, write_file, patch, search_files
#   /home/mverde/.hermes/hermes-agent/tools/terminal_tool.py:1631 -> terminal
#   /home/mverde/.hermes/hermes-agent/tools/{browser_tool,async_delegation,web_tools,...}.py -> gli altri
HERMES = ["read_file", "write_file", "patch", "search_files", "terminal", "process_manage", "execute_code",
          "browser_exec", "delegate_task", "web_search", "web_extract", "vision_analyze", "todo_list",
          "skill_view", "skills_list", "skill_manage", "memory", "clarify", "session_search", "image_generate",
          "video_analyze", "video_generate", "text_to_speech", "cronjob_manage", "manage_connections",
          "computer_use", "x_search"]


class TestFingerprint(unittest.TestCase):
    def test_same_list_same_fingerprint(self):
        a = [tool("read", path="string"), tool("bash", command="string")]
        b = json.loads(json.dumps(a))
        self.assertEqual(toolheads.fingerprint(a), toolheads.fingerprint(b))

    def test_changed_description_changes_fingerprint(self):
        a = [tool("read", "leggi un file")]
        b = [tool("read", "leggi un file, anche a pezzi")]
        self.assertNotEqual(toolheads.fingerprint(a), toolheads.fingerprint(b))

    def test_order_matters_and_empty_is_stable(self):
        a, b = [tool("read"), tool("bash")], [tool("bash"), tool("read")]
        self.assertNotEqual(toolheads.fingerprint(a), toolheads.fingerprint(b))
        self.assertEqual(toolheads.fingerprint(None), toolheads.fingerprint([]))

    def test_is_a_short_hex_digest(self):
        fp = toolheads.fingerprint([tool("read")])
        self.assertEqual(len(fp), 16)
        self.assertTrue(all(c in "0123456789abcdef" for c in fp))


class TestPayload(unittest.TestCase):
    """`payload()` riceve SOLO nomi e numeri: per costruzione non può portare testo nel giornale."""

    def rec(self, **over):
        kw = dict(fp="0123456789abcdef", names=["read", "github_list_issues"], def_tokens=[120, 340],
                  desc_tokens=[12, 31], head_tokens=700, system_tokens=90, exact=True)
        kw.update(over)
        return toolheads.payload(**kw)

    def test_shape(self):
        r = self.rec()
        self.assertEqual(r["fp"], "0123456789abcdef")
        self.assertEqual(r["tools"], 2)
        self.assertEqual(r["counting"], "exact")
        self.assertEqual(r["head_tokens"], 700)
        self.assertEqual(r["defs_tokens"], 460)
        self.assertEqual(r["system_tokens"], 90)
        self.assertEqual(r["frame_tokens"], 700 - 460 - 90)
        self.assertEqual(r["defs"], [{"name": "read", "tokens": 120, "desc": 12},
                                     {"name": "github_list_issues", "tokens": 340, "desc": 31}])
        self.assertNotIn("paging", r)

    def test_counting_declared(self):
        self.assertEqual(self.rec(exact=False)["counting"], "estimated")

    def test_paging_fields_only_when_active(self):
        r = self.rec(paging=True, base=["read"], catalog=["github_list_issues", "aaa_tool"])
        self.assertTrue(r["paging"])
        self.assertEqual(r["base"], ["read"])
        self.assertEqual(r["catalog"], ["aaa_tool", "github_list_issues"])    # ordinato: deterministico

    def test_refuses_mismatched_lists(self):
        with self.assertRaises(ValueError):
            self.rec(def_tokens=[1])

    def test_every_string_is_a_name_or_a_known_label(self):
        r = self.rec(paging=True, base=["read"], catalog=["github_list_issues"])
        self.assertEqual(toolheads.unknown_strings(r, ["read", "github_list_issues"]), [])
        # una stringa estranea va individuata dal predicato stesso, chiave compresa (prova del predicato)
        bad = dict(r)
        bad["note"] = "testo che non dovrebbe esserci"
        self.assertEqual(toolheads.unknown_strings(bad, ["read", "github_list_issues"]),
                         ["note", "testo che non dovrebbe esserci"])

    def test_leaks_finds_text(self):
        r = self.rec()
        self.assertEqual(toolheads.leaks(r, [SYSTEM_TEXT, SECRET_DESC]), [])
        self.assertEqual(toolheads.leaks({"x": "a " + SECRET_DESC}, [SECRET_DESC]), [SECRET_DESC])


# ----------------------------------------------------------------- testa del prompt
class TestPromptHead(unittest.TestCase):
    def msgs(self):
        return [{"role": "system", "content": SYSTEM_TEXT}, {"role": "user", "content": "ciao"}]

    def test_head_is_the_first_piece_of_render_pieces(self):
        tools = [tool("read", path="string"), tool("bash", command="string")]
        head, sys_text, tdefs = prompt_head(self.msgs(), tools, {})
        pieces = render_pieces(self.msgs(), tools, {})
        self.assertEqual(pieces[0][0], None)
        self.assertEqual(pieces[0][1], head)
        self.assertEqual(sys_text, SYSTEM_TEXT)
        self.assertEqual([t["name"] for t in tdefs], ["read", "bash"])
        for t in tdefs:
            self.assertIn(def_text(t), head)

    def test_head_without_tools_and_without_system(self):
        head, sys_text, tdefs = prompt_head([{"role": "user", "content": "ciao"}], None, {})
        self.assertEqual(tdefs, [])
        self.assertEqual(sys_text, "")
        self.assertEqual(render_pieces([{"role": "user", "content": "ciao"}], None, {})[0][1], head)

    def test_tool_defs_unwraps_the_function_envelope(self):
        self.assertEqual(tool_defs([tool("read")]), [tool("read")["function"]])
        self.assertIsNone(tool_defs([]))


# ----------------------------------------------------------------- evento nel giornale
class Base(unittest.TestCase):
    def mk(self, **over):
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        cfg = Config.from_dict({"mask_anchor": False, **over})
        mgr = Manager(cfg, Store(os.path.join(tmp.name, "a.sqlite")), TokenCounter(3.5),
                      Journal(os.path.join(tmp.name, "j.jsonl")))
        self.addCleanup(tmp.cleanup)
        return mgr

    def req(self, n=2, tools=None, system=SYSTEM_TEXT):
        msgs = [{"role": "system", "content": system}, {"role": "user", "content": "ciao"}]
        for k in range(n):
            msgs.append({"role": "assistant", "content": "passo %d" % k})
            msgs.append({"role": "user", "content": "continua %d" % k})
        return {"messages": msgs, "tools": tools if tools is not None else self.tools(), "max_tokens": 256}

    def tools(self):
        return [tool("read", "Legge un file", path="string"), tool("bash", "Esegue un comando", command="string"),
                tool("github_list_issues", SECRET_DESC, repo="string"),
                tool("todo", "Gestisce la lista dei compiti", action="string")]

    def heads(self, mgr):
        return [e for e in mgr.journal.mem if e.get("event") == toolheads.EVENT]


class TestJournalEvent(Base):
    def test_on_by_default_one_event_per_conversation(self):
        self.assertTrue(Config().journal_tool_heads)
        mgr = self.mk()
        for n in (2, 3, 4, 5):
            mgr.prepare(self.req(n))
        ev = self.heads(mgr)
        self.assertEqual(len(ev), 1, "una riga per conversazione, non per richiesta")
        self.assertEqual(ev[0]["tools"], 5)        # 4 del client + recall iniettato
        self.assertEqual([d["name"] for d in ev[0]["defs"]][:4],
                         ["read", "bash", "github_list_issues", "todo"])
        self.assertEqual([d["name"] for d in ev[0]["defs"]][4], RECALL_NAME)

    def test_a_new_event_only_when_the_list_changes(self):
        mgr = self.mk()
        mgr.prepare(self.req(2))
        mgr.prepare(self.req(3))
        self.assertEqual(len(self.heads(mgr)), 1)
        changed = self.tools()
        changed[0]["function"]["description"] = "Legge un file, anche a pezzi"
        mgr.prepare(self.req(4, tools=changed))
        ev = self.heads(mgr)
        self.assertEqual(len(ev), 2, "cambio dell'elenco: una riga nuova")
        self.assertNotEqual(ev[0]["fp"], ev[1]["fp"])
        mgr.prepare(self.req(5, tools=changed))
        self.assertEqual(len(self.heads(mgr)), 2)

    def test_one_event_per_conversation_not_shared(self):
        mgr = self.mk()
        mgr.prepare(self.req(2))
        # un'altra conversazione (prompt di sistema diverso: catena di hash diversa dal primo messaggio)
        mgr.prepare(self.req(2, system="sei un agente di ricerca"))
        ev = self.heads(mgr)
        self.assertEqual(len(ev), 2)
        self.assertEqual(ev[0]["fp"], ev[1]["fp"])     # stesso elenco, conversazioni diverse
        self.assertNotEqual(ev[0]["conv"], ev[1]["conv"])

    def test_a_modified_message_stays_the_same_conversation(self):
        """Una storia modificata è un RAMO della stessa conversazione: l'elenco non è cambiato, niente riga nuova."""
        mgr = self.mk()
        mgr.prepare(self.req(2))
        branch = self.req(2)
        branch["messages"][1]["content"] = "ciao, altra domanda"
        mgr.prepare(branch)
        self.assertEqual(len(self.heads(mgr)), 1)
        self.assertIn("branch", {e.get("event") for e in mgr.journal.mem})

    def test_numbers_add_up(self):
        mgr = self.mk()
        p = mgr.prepare(self.req(2))
        e = self.heads(mgr)[0]
        self.assertEqual(e["tools"], len(p.tools))
        self.assertEqual(e["defs_tokens"], sum(d["tokens"] for d in e["defs"]))
        self.assertEqual(e["head_tokens"], e["defs_tokens"] + e["system_tokens"] + e["frame_tokens"])
        self.assertEqual(e["fp"], toolheads.fingerprint(p.tools))
        self.assertEqual(e["counting"], "estimated")           # TokenCounter(3.5): nessun tokenizer
        for d in e["defs"]:
            self.assertGreater(d["tokens"], 0)
            self.assertGreaterEqual(d["tokens"], d["desc"])
        self.assertGreater(e["system_tokens"], 0)

    def test_head_tokens_is_the_counted_head(self):
        """Col tokenizer: `head_tokens` è esattamente il conto del primo pezzo del prompt reso, e `counting` lo dice.

        Tokenizer sintetico (nessun modello, nessuna rete), come in tests/test_pack_template.py: senza un
        `chat_template.jinja` accanto, `estimate` usa il template vero del proxy (`render_pieces`), cioè la stessa
        funzione da cui `prompt_head` ricava la testa.
        """
        if not EXACT:
            self.skipTest("serve mnemonic-proxy[exact] (tokenizers)")
        mgr = self.mk()
        mgr.tc = TokenCounter(tokenizer_path=exact_tokenizer(self))
        self.assertTrue(mgr.tc.exact)
        self.assertIsNone(mgr.tc.prompt_template)
        p = mgr.prepare(self.req(2))
        e = self.heads(mgr)[0]
        self.assertEqual(e["counting"], "exact")
        head, sys_text, tdefs = prompt_head(p.messages, p.tools, mgr.kw)
        self.assertEqual(e["head_tokens"], mgr.piece_tokens(head))
        self.assertEqual(e["system_tokens"], mgr.piece_tokens(sys_text))
        self.assertEqual([d["tokens"] for d in e["defs"]], [mgr.piece_tokens(def_text(t)) for t in tdefs])
        # la testa contata è una parte del prompt contato dalla stessa funzione
        self.assertLessEqual(e["head_tokens"], p.est_tokens)
        self.assertEqual(e["head_tokens"],
                         sum(mgr.piece_tokens(t) for i, t in render_pieces(p.messages, p.tools, mgr.kw)
                             if i is None) - mgr.piece_tokens(render_pieces(p.messages, p.tools, mgr.kw)[-1][1]))

    def test_off_by_switch(self):
        mgr = self.mk(journal_tool_heads=False)
        mgr.prepare(self.req(2))
        self.assertEqual(self.heads(mgr), [])

    def test_shortener_is_visible_in_the_event(self):
        """La leva `tooldefs_desc_max` diventa misurabile: è il motivo della card."""
        a = self.mk()
        b = self.mk(tooldefs_desc_max=40, tooldefs_param_max=20)
        a.prepare(self.req(2))
        b.prepare(self.req(2))
        ea, eb = self.heads(a)[0], self.heads(b)[0]
        self.assertNotEqual(ea["fp"], eb["fp"])
        self.assertLess(eb["defs_tokens"], ea["defs_tokens"])
        for x, y in zip(ea["defs"], eb["defs"]):
            self.assertEqual(x["name"], y["name"])
            self.assertLessEqual(y["desc"], x["desc"])


class TestNoText(Base):
    """Prova del punto 2 della card: nel giornale non entra testo."""

    def journal_lines(self, mgr):
        with open(mgr.journal.path, encoding="utf-8") as f:
            return [json.loads(x) for x in f if x.strip()]

    def test_no_definition_text_no_system_prompt_in_the_event(self):
        mgr = self.mk()
        mgr.prepare(self.req(2))
        e = self.heads(mgr)[0]
        secrets = [SYSTEM_TEXT, SECRET_DESC, "RISERVATO-SISTEMA-7f3a", "RISERVATO-DESCRIZIONE-4b21",
                   "Legge un file", "Esegue un comando", "Gestisce la lista dei compiti", "properties", "string"]
        self.assertEqual(toolheads.leaks(e, secrets), [])
        # e anche sulla riga vera scritta su disco
        on_disk = [r for r in self.journal_lines(mgr) if r.get("event") == toolheads.EVENT]
        self.assertEqual(len(on_disk), 1)
        self.assertEqual(toolheads.leaks(on_disk[0], secrets), [])

    def test_only_names_numbers_and_known_labels(self):
        mgr = self.mk(tools_paging=True)
        p = mgr.prepare(self.req(2))
        e = self.heads(mgr)[0]
        allowed = [tool_name(t) for t in p.tools] + [tool_name(t) for t in self.tools()]
        self.assertEqual(toolheads.unknown_strings(e, allowed), [])

    def test_a_long_description_does_not_enlarge_the_event(self):
        """Il peso dell'evento dipende dal NUMERO di strumenti, non dalla lunghezza delle definizioni."""
        short = [tool("read", "x" * 10, path="string")]
        long_ = [tool("read", "y" * 40000, path="string")]
        a, b = self.mk(), self.mk()
        a.prepare(self.req(2, tools=short))
        b.prepare(self.req(2, tools=long_))
        la = len(json.dumps(self.heads(a)[0], ensure_ascii=False))
        lb = len(json.dumps(self.heads(b)[0], ensure_ascii=False))
        self.assertLess(abs(la - lb), 20)
        self.assertLess(lb, 400)


class TestEnginePromptUnchanged(Base):
    """Punto 4: il prompt inviato al motore è identico byte per byte, con l'evento acceso o spento."""

    def rendered(self, mgr, req):
        p = mgr.prepare(copy.deepcopy(req))
        return "".join(t for _, t in render_pieces(p.messages, p.tools, mgr.kw)), p

    def test_byte_identical(self):
        on, off = self.mk(), self.mk(journal_tool_heads=False)
        for n in (2, 3, 4):
            req = self.req(n)
            a, pa = self.rendered(on, req)
            b, pb = self.rendered(off, req)
            self.assertEqual(a.encode("utf-8"), b.encode("utf-8"))
            self.assertEqual(json.dumps(pa.tools, ensure_ascii=False), json.dumps(pb.tools, ensure_ascii=False))
        self.assertEqual(len(self.heads(on)), 1)
        self.assertEqual(self.heads(off), [])

    def test_byte_identical_with_paging(self):
        on = self.mk(tools_paging=True)
        off = self.mk(tools_paging=True, journal_tool_heads=False)
        req = self.req(3)
        a, pa = self.rendered(on, req)
        b, pb = self.rendered(off, req)
        self.assertEqual(a.encode("utf-8"), b.encode("utf-8"))
        self.assertEqual(json.dumps(pa.tools), json.dumps(pb.tools))

    def test_client_tools_are_not_modified(self):
        mgr = self.mk()
        tools = self.tools()
        before = json.dumps(tools, ensure_ascii=False)
        mgr.prepare(self.req(2, tools=tools))
        self.assertEqual(json.dumps(tools, ensure_ascii=False), before)


class TestCost(Base):
    """Il costo si MISURA, ma non si trasforma in un cancello a tempo: su un esecutore condiviso l'orologio non è
    una prova (lo stesso codice ha fatto 2,2 s e 3,8 s sulla stessa macchina). I cancelli qui sono due fatti
    deterministici — quante volte la riga viene COSTRUITA e quanto è grande — e i tempi si stampano, nient'altro.
    """

    def test_the_line_is_built_once_not_per_request(self):
        reqs = [self.req(n) for n in range(2, 52)] * 4
        import ctxproxy.core as core_mod
        calls = []
        real = core_mod.prompt_head

        def counted(*a, **k):
            calls.append(1)
            return real(*a, **k)

        on, off = self.mk(), self.mk(journal_tool_heads=False)
        core_mod.prompt_head = counted
        try:
            t0 = time.perf_counter()
            for r in reqs:
                on.prepare(copy.deepcopy(r))
            with_ev = time.perf_counter() - t0
        finally:
            core_mod.prompt_head = real
        t0 = time.perf_counter()
        for r in reqs:
            off.prepare(copy.deepcopy(r))
        base = time.perf_counter() - t0
        n = len(self.heads(on))
        size = len(json.dumps(self.heads(on)[0], ensure_ascii=False))
        print("\n[tool_heads] %d richieste, %d eventi, %d costruzioni, %d byte/evento, "
              "%.3fs senza -> %.3fs con (solo informativo)" % (len(reqs), n, len(calls), size, base, with_ev))
        self.assertEqual(n, 1, "un evento su %d richieste" % len(reqs))
        self.assertEqual(len(calls), 1, "la testa viene letta e contata una volta sola, non a ogni richiesta")
        self.assertLess(size, 600)

    def test_the_fast_path_only_fingerprints(self):
        """Nel caso normale (elenco invariato) il metodo esce prima di leggere la testa: nessuna lettura, nessun
        conteggio di token, nessuna riga."""
        mgr = self.mk()
        p = mgr.prepare(self.req(2))
        before = len(mgr.journal.mem)
        import ctxproxy.core as core_mod
        real = core_mod.prompt_head

        def boom(*a, **k):
            raise AssertionError("la scorciatoia non deve leggere la testa")

        core_mod.prompt_head = boom
        try:
            for _ in range(50):
                self.assertIsNone(mgr.tool_heads(p.conv, p.messages, p.tools))
        finally:
            core_mod.prompt_head = real
        self.assertEqual(len(mgr.journal.mem), before)


# ----------------------------------------------------------------- perché in produzione il pager è muto
class TestPagerSilence(Base):
    """Punto 3 della card.

    Una riga: `ToolPager.split` (paging.py:79-87) e il suo punto d'uso (core.py:1448-1457) non scrivono NIENTE nel
    giornale — gli unici eventi del pager, `tools_search` e `tool_not_loaded`, nascono in server.py solo quando il
    MODELLO chiama `tools` o uno strumento non caricato — quindi un giornale senza quegli eventi non distingue
    «pager mai entrato in funzione» da «pager attivo e modello che non ha mai toccato il catalogo».
    """

    def test_split_journals_nothing(self):
        mgr = self.mk(tools_paging=True)
        before = len(mgr.journal.mem)
        base, catalog = mgr.pager.split(self.tools())
        self.assertEqual(len(mgr.journal.mem), before, "split() non scrive nel giornale")
        self.assertTrue(catalog)

    def test_paging_active_but_journal_silent_without_the_event(self):
        mgr = self.mk(tools_paging=True, journal_tool_heads=False)
        p = mgr.prepare(self.req(2))
        self.assertEqual([tool_name(t) for t in p.tools], ["read", "bash", RECALL_NAME, TOOLS_NAME])
        kinds = {e.get("event") for e in mgr.journal.mem}
        self.assertNotIn("tools_search", kinds)
        self.assertNotIn("tool_not_loaded", kinds)
        self.assertEqual(self.heads(mgr), [])      # prima della card: nessuna traccia del pager

    def test_the_event_shows_the_split(self):
        mgr = self.mk(tools_paging=True)
        mgr.prepare(self.req(2))
        e = self.heads(mgr)[0]
        self.assertTrue(e["paging"])
        self.assertEqual(e["base"], ["read", "bash"])
        self.assertEqual(e["catalog"], ["github_list_issues", "todo"])
        # in testa restano solo base + recall + tools: i nomi dell'evento lo dicono
        self.assertEqual([d["name"] for d in e["defs"]], ["read", "bash", RECALL_NAME, TOOLS_NAME])

    def test_real_clients_split(self):
        """Strumenti reali dei due client contro `tools_core` di serie."""
        core = Config().tools_core
        pager = ToolPager(core)
        base, catalog = pager.split([tool(n) for n in OPENCODE])
        self.assertEqual([tool_name(t) for t in base], ["bash", "edit", "write", "read", "grep"])
        self.assertEqual(sorted(catalog), ["apply_patch", "glob", "list", "task", "todowrite", "webfetch"])
        # Hermes: NESSUN nome coincide con tools_core, l'intero elenco finisce in catalogo
        base, catalog = pager.split([tool(n) for n in HERMES])
        self.assertEqual(base, [])
        self.assertEqual(len(catalog), len(HERMES))
        self.assertEqual(sorted(catalog), sorted(HERMES))

    def test_real_clients_in_the_event(self):
        for names, expect_base in ((OPENCODE, ["bash", "edit", "write", "read", "grep"]), (HERMES, [])):
            mgr = self.mk(tools_paging=True)
            p = mgr.prepare(self.req(2, tools=[tool(n, "descrizione di %s" % n, path="string") for n in names]))
            e = self.heads(mgr)[0]
            self.assertEqual(e["base"], expect_base)
            self.assertEqual(sorted(e["catalog"]), sorted(n for n in names if n not in expect_base))
            self.assertEqual([d["name"] for d in e["defs"]],
                             expect_base + [RECALL_NAME, TOOLS_NAME])
            self.assertEqual(e["tools"], len(p.tools))
            # la testa resta misurata anche quando il catalogo è tutto fuori
            self.assertGreater(e["head_tokens"], 0)
            self.assertEqual(e["head_tokens"], e["defs_tokens"] + e["system_tokens"] + e["frame_tokens"])

    def test_hermes_without_paging_pays_the_whole_catalog_in_the_head(self):
        """Col pager spento (di serie) l'evento misura quanto costa l'intero elenco di Hermes in testa."""
        tools = [tool(n, "descrizione di %s" % n, path="string") for n in HERMES]
        off, on = self.mk(), self.mk(tools_paging=True)
        off.prepare(self.req(2, tools=tools))
        on.prepare(self.req(2, tools=tools))
        a, b = self.heads(off)[0], self.heads(on)[0]
        self.assertEqual(a["tools"], len(HERMES) + 1)        # + recall
        self.assertEqual(b["tools"], 2)                      # solo recall + tools
        self.assertGreater(a["head_tokens"], b["head_tokens"])


if __name__ == "__main__":
    unittest.main()
