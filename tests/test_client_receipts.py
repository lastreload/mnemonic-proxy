"""Typed receipt aliases for documented clients; no engines or external services."""
import unittest

from ctxproxy.core import call_info
from ctxproxy.paging import typed_receipt


class TestClientReceipts(unittest.TestCase):
    def receipt(self, name, args, text="alpha\nbeta\n", failed=False, lang="en"):
        return typed_receipt({"name": name, "args": args}, text, failed, "a0123456789ab", lang=lang)

    def test_claude_read(self):
        for lang in ("it", "en"):
            for failed in (False, True):
                with self.subTest(lang=lang, failed=failed):
                    kind, receipt = self.receipt("Read", {"file_path": "a.py", "offset": 2, "limit": 2},
                                                 failed=failed, lang=lang)
                    self.assertEqual(kind, "lettura")
                    self.assertIn("2–3", receipt)
                    if failed:
                        self.assertIn("FALLITA" if lang == "it" else "FAILED", receipt)
                    else:
                        self.assertIn("2 righe" if lang == "it" else "2 lines", receipt)
                        self.assertIn("sha256 e49c81e2d2f8", receipt)
        _, whole = self.receipt("Read", {"file_path": "a.py"})
        self.assertIn("whole file", whole)

    def test_claude_writes_and_edits(self):
        cases = [
            ("Write", {"file_path": "a.py", "content": "è\n"}, "scrittura", "3 byte", "3 bytes"),
            ("Edit", {"file_path": "a.py", "old_string": "old", "new_string": "new", "replace_all": True},
             "modifica", "1 blocchi", "1 blocks"),
            ("MultiEdit", {"file_path": "a.py", "edits": [
                {"old_string": "a", "new_string": "b"}, {"old_string": "c", "new_string": "d"}]},
             "modifica", "2 blocchi", "2 blocks"),
        ]
        for name, args, expected, it, en in cases:
            for lang in ("it", "en"):
                for failed in (False, True):
                    with self.subTest(name=name, lang=lang, failed=failed):
                        kind, receipt = self.receipt(name, args, "Error: rejected" if failed else "OK", failed, lang)
                        self.assertEqual(kind, expected)
                        self.assertIn(it if lang == "it" else en, receipt)
                        self.assertIn("FALLITO" if lang == "it" else "FAILED", receipt) if failed else self.assertIn(
                            "riuscito" if lang == "it" else "succeeded", receipt)
                        self.assertIn("recall id=a0123456789ab", receipt)
                        if failed:
                            self.assertIn("Error: rejected", receipt)

    def test_codex_apply_patch(self):
        patch = "*** Begin Patch\n*** Update File: a.py\n@@\n-old\n+new\n*** Add File: b.py\n+hi\n*** Delete File: c.py\n*** End Patch"
        for key in ("input", "patch", "command"):
            for lang in ("it", "en"):
                for failed in (False, True):
                    with self.subTest(key=key, lang=lang, failed=failed):
                        kind, receipt = self.receipt("apply_patch", {key: patch}, "Error: rejected" if failed else "OK",
                                                     failed, lang)
                        self.assertEqual(kind, "modifica")
                        self.assertIn("3 blocchi" if lang == "it" else "3 blocks", receipt)
                        self.assertIn("FALLITO" if lang == "it" else "FAILED", receipt) if failed else self.assertIn(
                            "riuscito" if lang == "it" else "succeeded", receipt)

    def test_shell_aliases(self):
        for name, args in [("Bash", {"command": "echo hi"}), ("shell", {"command": "echo hi"}),
                           ("shell", {"command": ["sh", "-lc", "echo hi"]}),
                           ("exec_command", {"cmd": "echo hi"})]:
            for lang in ("it", "en"):
                for failed in (False, True):
                    with self.subTest(name=name, args=args, lang=lang, failed=failed):
                        text = "denied\nCommand exited with code 2" if failed else "hi"
                        kind, receipt = self.receipt(name, args, text, failed, lang)
                        self.assertEqual(kind, "shell_errore" if failed else "shell_ok")
                        self.assertIn("codice d'uscita" if lang == "it" else "exit code", receipt)
                        self.assertIn("denied" if failed else "hi", receipt)
                        self.assertIn("2" if failed else "0", receipt)
                        # Some clients signal failure without an exit-code line.
                        if failed:
                            kind, receipt = self.receipt(name, args, "permission denied", True, lang)
                            self.assertEqual(kind, "shell_errore")
                            self.assertIn("FALLITO" if lang == "it" else "FAILED", receipt)

    def test_test_commands_use_client_argument_names(self):
        for name, args in [("Bash", {"command": "pytest"}), ("shell", {"command": "pytest"}),
                           ("shell", {"command": ["sh", "-lc", "pytest"]}),
                           ("exec_command", {"cmd": "pytest"})]:
            for lang in ("it", "en"):
                for failed in (False, True):
                    with self.subTest(name=name, args=args, lang=lang, failed=failed):
                        text = "1 failed, 3 passed\nCommand exited with code 1" if failed else "4 passed"
                        kind, receipt = self.receipt(name, args, text, failed, lang)
                        self.assertEqual(kind, "test")
                        self.assertIn("passati" if lang == "it" else "passed", receipt)
                        if failed:
                            self.assertIn("falliti 1" if lang == "it" else "failed 1", receipt)

    def test_call_info_preserves_client_paths_and_commands(self):
        for name, args in [("Read", {"file_path": "src/a.py", "offset": 10, "limit": 5}),
                           ("shell", {"command": ["sh", "-lc", "pytest"]})]:
            with self.subTest(name=name):
                info = call_info({"tool_calls": [{"id": "x", "function": {"name": name, "arguments": args}}]})["x"]
                if name == "Read":
                    self.assertEqual(info["path"], "src/a.py")
                    self.assertIn("10–14", typed_receipt(info, "one", False, lang="en")[1])
                else:
                    self.assertEqual(typed_receipt(info, "4 passed", False, lang="en")[0], "test")

    def test_unknown_names_keep_fallback(self):
        for name in ("Grep", "Glob", "WebSearch", "read_file", "execute_command", "READ", "unknown"):
            for lang in ("it", "en"):
                for failed in (False, True):
                    with self.subTest(name=name, lang=lang, failed=failed):
                        self.assertEqual(self.receipt(name, {"command": "pytest"}, "4 passed", failed, lang),
                                         ("altro", ""))

    def test_pi_receipt_is_byte_identical_to_0311(self):
        info = {"name": "read", "args": {"path": "a.py", "offset": 2, "limit": 2}}
        expected = {
            "it": "lettura: righe 2–3, 2 righe restituite, sha256 e49c81e2d2f8 del testo letto",
            "en": "read: lines 2–3, 2 lines returned, sha256 e49c81e2d2f8 of the text read",
        }
        for lang, baseline in expected.items():
            self.assertEqual(typed_receipt(info, "alpha\nbeta\n", False, lang=lang)[1].encode("utf-8"),
                             baseline.encode("utf-8"))


if __name__ == "__main__":
    unittest.main()
