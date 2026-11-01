"""Level 3 of the README tests: the documentation must be exhaustive.

Every subcommand, every command-line option and every `Config` field is read from the code.
Anything that neither appears in README.md nor is listed in `tools/readme_internal.txt`
(with a reason) fails this test: a new public option cannot be added without documenting it
or declaring it internal.

Standard library only, CPU, a fraction of a second.
"""
import dataclasses
import importlib.util
import os
import re
import sys
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
README = os.path.join(REPO, "README.md")
INTERNAL = os.path.join(REPO, "tools", "readme_internal.txt")


def _tool(name):
    path = os.path.join(REPO, "tools", name + ".py")
    mod_name = "readme_tools_" + name
    spec = importlib.util.spec_from_file_location(mod_name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


cli = _tool("readme_cli")


def readme_text():
    with open(README, encoding="utf-8") as f:
        return f.read()


def internal_entries():
    """{entry: reason} from tools/readme_internal.txt."""
    out = {}
    with open(INTERNAL, encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            entry, _, reason = line.partition("#")
            out[entry.strip()] = reason.strip()
    return out


def mentioned(text, name):
    """Is `name` named in the README, as a whole word?"""
    return re.search(r"(?<![\w./-])" + re.escape(name) + r"(?![\w-])", text) is not None


class CoverageTests(unittest.TestCase):

    def setUp(self):
        self.text = readme_text()
        self.internal = internal_entries()
        self.used = set()

    def tearDown(self):
        pass

    def assertDocumented(self, entries):
        """entries: list of (key, name). Each must be in the README or declared internal."""
        undocumented = []
        for key, name in entries:
            if key in self.internal:
                self.used.add(key)
                continue
            if not mentioned(self.text, name):
                undocumented.append(key)
        self.assertEqual([], sorted(undocumented),
                         "not in README.md and not in tools/readme_internal.txt:\n  "
                         + "\n  ".join(sorted(undocumented)))

    # ------------------------------------------------------------- subcommands

    def test_every_subcommand_is_documented(self):
        subs = cli.known_subcommands()
        self.assertTrue(subs)
        self.assertDocumented([("subcommand:" + s, "mnemonic-proxy " + s) for s in subs])

    # ------------------------------------------------------------- options

    def test_every_option_is_documented(self):
        entries = []
        for prog, parser in cli.all_parsers().items():
            for opt in cli.options(parser):
                entries.append(("option:%s:%s" % (prog, opt), opt))
        self.assertGreater(len(entries), 20)
        self.assertDocumented(entries)

    def test_the_options_come_from_the_real_parsers(self):
        parsers = cli.all_parsers()
        self.assertIn("--upstream", cli.options(parsers["mnemonic-proxy"]))
        self.assertIn("--live", cli.options(parsers["mnemonic-proxy check"]))
        self.assertIn("--upstream", cli.options(parsers["mnemonic-proxy check"]),
                      "check inherits the server's options")
        self.assertIn("--keep", cli.options(parsers["mnemonic-proxy demo"]))
        self.assertIn("--dry-run", cli.options(parsers["mnemonic-proxy forget"]))
        self.assertIn("--db", cli.options(parsers["mnemonic-mcp"]))

    # ------------------------------------------------------------- Config

    def config_fields(self):
        from ctxproxy.core import Config
        return [f.name for f in dataclasses.fields(Config)]

    def test_every_config_field_is_documented(self):
        fields = self.config_fields()
        self.assertGreater(len(fields), 50)
        self.assertDocumented([("config:" + f, f) for f in fields])

    def test_a_missing_field_would_be_caught(self):
        """The check must be able to fail: a name that is not in the README must not pass."""
        self.assertFalse(mentioned(self.text, "no_such_config_field"))
        self.assertTrue(mentioned(self.text, "mask_trigger"))

    def test_word_boundaries_are_respected(self):
        """`auto_recall` must not be considered documented by `auto_recall_hint` alone."""
        self.assertFalse(mentioned("see auto_recall_hint", "auto_recall"))
        self.assertTrue(mentioned("see `auto_recall` here", "auto_recall"))
        self.assertFalse(mentioned("--livewire", "--live"))
        self.assertTrue(mentioned("pass `--live` to check", "--live"))

    # ------------------------------------------------------------- the list itself

    def test_internal_entries_have_a_reason(self):
        for entry, reason in internal_entries().items():
            with self.subTest(entry=entry):
                self.assertTrue(reason, "tools/readme_internal.txt: %s has no reason" % entry)

    def test_internal_entries_still_exist_in_the_code(self):
        """A stale exemption would hide a real gap."""
        known = {"subcommand:" + s for s in cli.known_subcommands()}
        known |= {"config:" + f for f in self.config_fields()}
        for prog, parser in cli.all_parsers().items():
            known |= {"option:%s:%s" % (prog, o) for o in cli.options(parser)}
        stale = sorted(set(internal_entries()) - known)
        self.assertEqual([], stale,
                         "tools/readme_internal.txt lists entries that no longer exist: %r" % (stale,))


if __name__ == "__main__":
    unittest.main()
