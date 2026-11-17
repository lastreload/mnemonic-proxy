# Author: Maurizio Verde — LastReload
"""Issue #95: il formato del tokenizer si riconosce dalla STRUTTURA, non dal fatto che il percorso sia un file o
una cartella. La cartella HF di un modello (il caso della issue) deve caricarsi; un pack di Strata deve caricarsi
da cartella e da descrittore; tutto il resto deve fallire con un errore fedele, senza mai essere reinterpretato
come l'altro formato e senza mai declassare il conteggio in silenzio.
"""
import json
from pathlib import Path
import tempfile
import unittest
from ctxproxy.core import TokenCounter, load_tokenizer
try:
    from tokenizers import Tokenizer, models, pre_tokenizers
    EXACT = True
except ImportError:                                     # pragma: no cover
    EXACT = False
PRE_PATTERN = r"\p{L}+|\p{N}+|[^\s\p{L}\p{N}]+|\s+"


def write_hf(path: Path) -> None:
    """Un vero file HF, scritto dalla libreria stessa (3 token di vocabolario)."""
    tk = Tokenizer(models.WordLevel({"[UNK]": 0, "hello": 1, "world": 2}, unk_token="[UNK]"))
    tk.pre_tokenizer = pre_tokenizers.WhitespaceSplit()
    tk.save(str(path))


def write_pack(d: Path, token_type: bool = True) -> None:
    """Un pack di Strata minimo: descrittore (model stringa + pre_pattern) + vocab + merges + token_type."""
    (d / "vocab.json").write_text(json.dumps({"<|s|>": 0, "a": 1, "b": 2, "ab": 3}), encoding="utf-8")
    (d / "merges.txt").write_text("a b\n", encoding="utf-8")
    if token_type:
        (d / "token_type.json").write_text(json.dumps([3, 1, 1, 1]), encoding="utf-8")
    (d / "tokenizer.json").write_text(json.dumps({
        "model": "gpt2", "pre": "qwen35", "vocab_size": 4, "n_merges": 1,
        "add_bos_token": False, "pre_pattern": PRE_PATTERN}), encoding="utf-8")


@unittest.skipUnless(EXACT, "install mnemonic-proxy[exact]")
class LoaderFormatTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.d = Path(self.tmp.name)

    # ---------- casi che devono caricarsi ----------
    def test_hugging_face_directory_loads(self):
        """Il caso della issue #95: --tokenizer punta alla cartella del modello HF."""
        write_hf(self.d / "tokenizer.json")
        tk = load_tokenizer(str(self.d))
        self.assertEqual(tk.get_vocab_size(), 3)

    def test_hugging_face_file_loads(self):
        write_hf(self.d / "tokenizer.json")
        self.assertEqual(load_tokenizer(str(self.d / "tokenizer.json")).get_vocab_size(), 3)

    def test_strata_pack_directory_loads(self):
        write_pack(self.d)
        self.assertEqual(load_tokenizer(str(self.d)).get_vocab_size(), 4)

    def test_strata_pack_descriptor_file_loads(self):
        write_pack(self.d)
        self.assertEqual(load_tokenizer(str(self.d / "tokenizer.json")).get_vocab_size(), 4)

    def test_mixed_directory_is_decided_by_the_tokenizer_json_content(self):
        """Cartella ambigua: file HF valido accanto a vocab.json/merges.txt del pack.
        Decide la struttura del tokenizer.json, non la presenza di vocab.json."""
        write_pack(self.d)
        write_hf(self.d / "tokenizer.json")     # sovrascrive il descrittore con un vero file HF
        self.assertEqual(load_tokenizer(str(self.d)).get_vocab_size(), 3)

    # ---------- casi che devono fallire, con l'errore giusto ----------
    def test_corrupt_json_is_reported_as_json_not_as_a_missing_pack_file(self):
        (self.d / "tokenizer.json").write_text('{"model": {', encoding="utf-8")
        with self.assertRaises(ValueError) as caught:
            load_tokenizer(str(self.d / "tokenizer.json"))
        self.assertIn("JSON", str(caught.exception))
        self.assertNotIn("token_type", str(caught.exception))

    def test_unsupported_hugging_face_json_is_not_reinterpreted_as_a_pack(self):
        """JSON di forma HF che la libreria non sa aprire: resta un errore HF anche se accanto c'è un vocab.json."""
        write_pack(self.d)
        (self.d / "tokenizer.json").write_text(json.dumps({
            "version": "1.0", "added_tokens": [], "model": {"type": "NoSuchModel", "vocab": {}}}), encoding="utf-8")
        for path in (self.d, self.d / "tokenizer.json"):
            with self.subTest(path=str(path)), self.assertRaises(ValueError) as caught:
                load_tokenizer(str(path))
            self.assertIn("Hugging Face", str(caught.exception))
            self.assertNotIn("token_type", str(caught.exception))

    def test_incomplete_pack_names_the_missing_file(self):
        write_pack(self.d, token_type=False)
        for path in (self.d, self.d / "tokenizer.json"):
            with self.subTest(path=str(path)), self.assertRaises(ValueError) as caught:
                load_tokenizer(str(path))
            self.assertIn("token_type.json", str(caught.exception))
            self.assertIn("pack", str(caught.exception))

    def test_directory_without_a_tokenizer_json_is_unrecognized(self):
        (self.d / "vocab.json").write_text("{}", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "unrecognized tokenizer"):
            load_tokenizer(str(self.d))

    def test_sentencepiece_and_empty_and_missing_are_unrecognized(self):
        (self.d / "tokenizer.model").write_bytes(b"\x0a\x09sentencepiece\x00binary")
        empty = self.d / "empty"
        empty.mkdir()
        for path in (self.d / "tokenizer.model", empty, self.d / "nope", self.d / "nope" / "tokenizer.json"):
            with self.subTest(path=str(path)), self.assertRaisesRegex(ValueError, "unrecognized tokenizer"):
                load_tokenizer(str(path))

    # ---------- invarianti del contatore ----------
    def test_counter_is_exact_on_the_issue_directory_and_finds_the_template(self):
        write_hf(self.d / "tokenizer.json")
        (self.d / "chat_template.jinja").write_text("{{ messages[0].content }}", encoding="utf-8")
        tc = TokenCounter(tokenizer_path=str(self.d))
        self.assertTrue(tc.exact)
        self.assertIsNotNone(tc.prompt_template)

    def test_counter_stays_inexact_and_reports_the_reason(self):
        tc = TokenCounter(tokenizer_path=str(self.d / "nope"))
        self.assertFalse(tc.exact)
        self.assertIn("tokenizer not loaded", tc.kind)
        self.assertIn("unrecognized tokenizer", tc.kind)


if __name__ == "__main__":
    unittest.main()
