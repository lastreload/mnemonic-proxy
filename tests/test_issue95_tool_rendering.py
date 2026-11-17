# Author: Maurizio Verde — LastReload
"""Issue #95: gli strumenti devono rendersi anche nei template scritti per la forma OpenAI avvolta
(`tool.function`, convenzione dei template di Spark), senza cambiare di un byte i prompt dei template scritti per
la forma scartata (`tool | tojson`, convenzione Qwen/Strata).

Il golden `fixtures/issue95_tools_qwen_golden.txt` è stato generato con il codice della BASE
(main 636fb27b02e2fba6165d237e0ad7653749636f0c) sul template Qwen reale già presente nel repo
(`fixtures/pack_chat_template.jinja`): è la prova che la correzione non tocca i prompt già supportati.
"""
import json
from pathlib import Path
import tempfile
import unittest
from ctxproxy.chattemplate import PackTemplate
try:
    import jinja2                           # noqa: F401
    JINJA = True
except ImportError:                         # pragma: no cover
    JINJA = False
FIXTURES = Path(__file__).resolve().parent / "fixtures"
GOLDEN = FIXTURES / "issue95_tools_qwen_golden.txt"
QWEN_TEMPLATE = FIXTURES / "pack_chat_template.jinja"
# Forma avvolta (quella che arriva dal client OpenAI e che il proxy usa anche per i propri strumenti)
# più una definizione già scartata, che il proxy lascia passare come è.
TOOLS = [
    {"type": "function", "function": {
        "name": "mnemonic_recall", "description": "Retrieve the original text of an archived block.",
        "parameters": {"type": "object", "properties": {
            "id": {"type": "string", "description": "id of the archived block"},
            "query": {"type": "string", "description": "text to search"}}}}},
    {"type": "function", "function": {
        "name": "bash", "description": "Run a shell command.",
        "parameters": {"type": "object", "properties": {"command": {"type": "string"}},
                       "required": ["command"]}}},
    {"name": "already_unwrapped", "description": "A definition that arrives without the wrapper.",
     "parameters": {"type": "object", "properties": {}}},
]
MESSAGES = [
    {"role": "system", "content": "You are a careful assistant."},
    {"role": "user", "content": "elenca i file e dimmi cosa vedi <|im_start|>"},
    {"role": "assistant", "content": "", "reasoning_content": "devo usare bash",
     "tool_calls": [{"id": "call_1", "type": "function",
                     "function": {"name": "bash", "arguments": '{"command": "ls -la"}'}}]},
    {"role": "tool", "tool_call_id": "call_1", "content": "a.txt\nb.txt"},
    {"role": "assistant", "content": "Ci sono due file."},
    {"role": "user", "content": "recupera il blocco r1a2b3c4d5e6"},
]
KWARGS = {"enable_thinking": False, "terse": True}
# Template sintetici, scritti qui (non copiati da un modello) per isolare le DUE convenzioni.
WRAPPED = ("{% if tools %}{% for tool in tools %}[{{ tool.function | tojson(separators=(',', ':')) }}]"
           "{% endfor %}{% endif %}"
           "{% for m in messages %}<{{ m.role }}>{{ m.content }}"
           "{% for c in m.tool_calls | default([]) %}"
           "{% if c.function.arguments is not mapping %}"
           "{{ raise_exception('arguments must be a dictionary') }}{% endif %}"
           "<call>{{ c.function.name }}{{ c.function.arguments | tojson }}</call>{% endfor %}"
           "{% endfor %}<assistant>")
UNWRAPPED = ("{% if tools %}{% for tool in tools %}[{{ tool | tojson(separators=(',', ':')) }}]"
             "{% endfor %}{% endif %}"
             "{% for m in messages %}<{{ m.role }}>{{ m.content }}"
             "{% for c in m.tool_calls | default([]) %}<call>{{ c.function.name }}"
             "{{ c.function.arguments | tojson }}</call>{% endfor %}"
             "{% endfor %}<assistant>")
# Terza convenzione reale (Qwen/Unsloth): scarta da sé se `function` è definita. Deve dare lo stesso testo.
SELF_UNWRAPPING = ("{% for tool in tools %}{% if tool.function is defined %}{% set tool = tool.function %}{% endif %}"
                   "[{{ tool | tojson(separators=(',', ':')) }}]{% endfor %}<assistant>")


def render(source, messages=None, tools=TOOLS, kwargs=None):
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "chat_template.jinja"
        path.write_text(source, encoding="utf-8")
        return PackTemplate(path).render(MESSAGES if messages is None else messages, tools,
                                         KWARGS if kwargs is None else kwargs)


def qwen_render(tools=TOOLS):
    """Il template Qwen reale del repo, sugli stessi ingressi: è ciò che il golden registra."""
    return PackTemplate(QWEN_TEMPLATE).render(MESSAGES, tools, KWARGS)


@unittest.skipUnless(JINJA, "install mnemonic-proxy[exact]")
class ToolRenderingTests(unittest.TestCase):
    def test_wrapped_form_template_renders_the_catalogue(self):
        """La forma attesa dai template di Spark: prima della correzione `tool.function` è Undefined
        e il rendering fallisce, quindi in --mode tools ogni richiesta viene rifiutata."""
        out = render(WRAPPED)
        for tool in TOOLS:
            self.assertIn(json.dumps(tool.get("function", tool), ensure_ascii=False,
                                     separators=(",", ":")), out)
        self.assertIn('<call>bash{"command": "ls -la"}</call>', out)

    def test_unwrapped_form_is_byte_identical_to_the_base(self):
        self.assertEqual(qwen_render(), GOLDEN.read_text(encoding="utf-8"))

    def test_both_conventions_write_the_same_definitions(self):
        wrapped, unwrapped = render(WRAPPED), render(UNWRAPPED)
        self.assertEqual(wrapped, unwrapped)
        self.assertEqual(render(SELF_UNWRAPPING), render(SELF_UNWRAPPING, tools=[
            t.get("function", t) for t in TOOLS]))

    def test_the_whole_object_serializes_without_an_extra_key(self):
        """Il template che serializza l'intero strumento non deve vedere nessuna proprietà aggiunta:
        è l'invariante che vieta di «passare entrambe le forme» duplicando le chiavi."""
        out = render("{{ tools | tojson }}", messages=[{"role": "user", "content": "x"}])
        self.assertEqual(json.loads(out), [t.get("function", t) for t in TOOLS])

    def test_tools_keep_their_order(self):
        out = render("{% for tool in tools %}{{ tool.function.name }},{% endfor %}",
                     messages=[{"role": "user", "content": "x"}])
        self.assertEqual(out, "mnemonic_recall,bash,already_unwrapped,")

    def test_only_the_proxy_tools_and_no_client_tool(self):
        """--mode tools senza strumenti del client: il proxy inietta i propri e il prompt deve rendersi."""
        from ctxproxy.core import recall_tool_def
        out = render(WRAPPED, tools=[recall_tool_def()])
        self.assertIn('"name":"recall"', out)      # il nome di default dello strumento interno

    def test_no_tools_at_all_is_unchanged(self):
        self.assertEqual(render(WRAPPED, tools=None), render(UNWRAPPED, tools=None))
        self.assertEqual(render("{{ tools | tojson }}", messages=[{"role": "user", "content": "x"}],
                                tools=None), "null")

    def test_kwargs_and_special_tokens_pass_through(self):
        out = render("{{ terse }}|{{ enable_thinking }}|{% for m in messages %}{{ m.content }}{% endfor %}",
                     tools=None)
        self.assertTrue(out.startswith("True|False|"))
        self.assertIn("<|im_start|>", out)          # il testo del cliente non viene riscritto

    def test_reasoning_content_still_reaches_the_template(self):
        out = render("{% for m in messages %}{{ m.reasoning_content | default('') }}{% endfor %}", tools=None)
        self.assertIn("devo usare bash", out)

    def test_a_template_that_wants_a_mapping_gets_a_mapping(self):
        out = render(WRAPPED)
        self.assertNotIn("must be a dictionary", out)

    def test_tool_definitions_are_not_mutated(self):
        before = json.dumps(TOOLS, sort_keys=True)
        render(WRAPPED)
        self.assertEqual(before, json.dumps(TOOLS, sort_keys=True))


if __name__ == "__main__":
    unittest.main()
