"""Level 1 of the README tests: everything that can be checked without running anything.

- every fenced block that could be executed carries a `<!-- readme-test: … -->` label;
- every `mnemonic-proxy` / `mnemonic-mcp` command line in the README parses with the real
  parser of the program it calls (built from the code, not copied here);
- every proxy-configuration JSON example contains only real `Config` fields and is accepted
  by `Config.from_dict`;
- every internal `#anchor` link points at a heading that exists (GitHub's slug rule, `-1`
  included).

Standard library only, CPU, a couple of seconds.
"""
import importlib.util
import json
import os
import sys
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
README = os.path.join(REPO, "README.md")


def _tool(name):
    path = os.path.join(REPO, "tools", name + ".py")
    mod_name = "readme_tools_" + name
    spec = importlib.util.spec_from_file_location(mod_name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod          # dataclasses needs the module to be importable
    spec.loader.exec_module(mod)
    return mod


blocks_mod = _tool("readme_blocks")
cli = _tool("readme_cli")


def readme_text():
    with open(README, encoding="utf-8") as f:
        return f.read()


class LabelTests(unittest.TestCase):
    """No executable block may go unlabeled: a new example must say how it is tested."""

    def test_every_executable_block_is_labeled(self):
        missing = [(b.line, b.lang, b.section) for b in blocks_mod.load(README)
                   if b.executable and b.label is None]
        self.assertEqual([], missing,
                         "add '<!-- readme-test: ci|windows|real-engine|illustrative -->' "
                         "on the line above these blocks: %r" % (missing,))

    def test_labels_are_known(self):
        bad = [(b.line, b.label) for b in blocks_mod.load(README)
               if b.label is not None and b.label not in blocks_mod.LABELS]
        self.assertEqual([], bad, "unknown readme-test label(s): %r" % (bad,))

    def test_json_blocks_declare_which_configuration_they_are(self):
        bad = [(b.line, b.section) for b in blocks_mod.load(README)
               if b.lang == "json" and b.config_kind not in blocks_mod.CONFIG_KINDS]
        self.assertEqual([], bad,
                         "every JSON block needs '; config: proxy|partial|client' in its label: %r" % (bad,))

    def test_at_least_one_block_of_each_label_is_present(self):
        labels = {b.label for b in blocks_mod.load(README)}
        for label in ("ci", "real-engine", "illustrative"):
            self.assertIn(label, labels)


class CommandLineTests(unittest.TestCase):
    """Every documented command line must be accepted by the real parser."""

    def all_commands(self):
        out = []
        for b in blocks_mod.load(README):
            if b.lang in ("json", "yaml", "toml", "bibtex"):
                continue
            for argv in cli.commands(b.text):
                if cli.program(argv) is not None:
                    out.append((b.line, argv))
        return out

    def test_the_readme_documents_some_commands(self):
        self.assertGreaterEqual(len(self.all_commands()), 15)

    def test_every_command_line_parses(self):
        bad = []
        for line, argv in self.all_commands():
            problem = cli.check_argv(argv)
            if problem:
                bad.append("README.md:%d  %s  ->  %s" % (line, " ".join(argv), problem))
        self.assertEqual([], bad, "invalid command line(s) in the README:\n" + "\n".join(bad))

    def test_the_check_catches_an_unknown_subcommand(self):
        """The regression this suite was written for: `mnemonic-proxy check` had to exist."""
        self.assertIsNotNone(cli.check_argv(["mnemonic-proxy", "checkk"]))
        self.assertIsNotNone(cli.check_argv(["mnemonic-proxy", "verify"]))
        self.assertIsNone(cli.check_argv(["mnemonic-proxy", "check"]))

    def test_the_check_catches_an_unknown_option(self):
        self.assertIsNotNone(cli.check_argv(["mnemonic-proxy", "--upstrem", "http://x"]))
        self.assertIsNotNone(cli.check_argv(["mnemonic-proxy", "check", "--alive"]))
        self.assertIsNotNone(cli.check_argv(["mnemonic-mcp", "--database", "a.sqlite"]))
        self.assertIsNone(cli.check_argv(["mnemonic-mcp", "--db", "a.sqlite"]))

    def test_known_subcommands_come_from_the_code(self):
        self.assertEqual(["check", "demo", "forget"], cli.known_subcommands())
        self.assertEqual(sorted(cli.SUBCOMMAND_PARSERS), cli.known_subcommands())


class ConfigExampleTests(unittest.TestCase):
    """A configuration example with a key the proxy does not know would be silently ignored."""

    def config_blocks(self):
        return [b for b in blocks_mod.load(README)
                if b.lang == "json" and b.config_kind in ("proxy", "partial")]

    def test_there_is_at_least_one_proxy_config_example(self):
        self.assertTrue([b for b in self.config_blocks() if b.config_kind == "proxy"])

    def test_examples_are_valid_json(self):
        for b in self.config_blocks():
            with self.subTest(line=b.line):
                json.loads(b.text)

    def test_examples_use_only_real_config_fields(self):
        import dataclasses
        from ctxproxy.core import Config
        names = {f.name for f in dataclasses.fields(Config)}
        for b in self.config_blocks():
            with self.subTest(line=b.line):
                unknown = sorted(set(json.loads(b.text)) - names)
                self.assertEqual([], unknown,
                                 "README.md:%d: keys the proxy would ignore: %r" % (b.line, unknown))

    def test_examples_are_accepted_by_from_dict(self):
        from ctxproxy.core import Config
        for b in self.config_blocks():
            with self.subTest(line=b.line):
                Config.from_dict(json.loads(b.text))

    def test_shipped_examples_use_only_real_config_fields(self):
        """The files the README tells the user to copy must be valid too."""
        import dataclasses
        from ctxproxy.core import Config
        names = {f.name for f in dataclasses.fields(Config)}
        folder = os.path.join(REPO, "examples")
        found = 0
        for name in sorted(os.listdir(folder)):
            if not name.endswith(".json"):
                continue
            with open(os.path.join(folder, name), encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                continue
            found += 1
            with self.subTest(example=name):
                self.assertEqual([], sorted(set(data) - names))
                Config.from_dict(data)
        self.assertGreater(found, 0)


class AnchorTests(unittest.TestCase):
    """A broken `#anchor` is invisible until a reader clicks it."""

    def test_internal_links_resolve(self):
        text = readme_text()
        known = blocks_mod.anchors(text)
        broken = sorted({a for a in blocks_mod.internal_links(text) if a not in known})
        self.assertEqual([], broken, "internal link(s) with no matching heading: %r" % (broken,))

    def test_there_are_internal_links_to_check(self):
        self.assertGreaterEqual(len(blocks_mod.internal_links(readme_text())), 20)

    def test_duplicate_headings_get_github_suffixes(self):
        text = "## A b\n\ntext\n\n## A b\n\n## A b\n"
        self.assertEqual({"a-b", "a-b-1", "a-b-2"}, blocks_mod.anchors(text))

    def test_slug_rule(self):
        self.assertEqual("t0--see-it-work-1-minute-no-model",
                         blocks_mod.heading_slug("T0 — see it work (1 minute, no model)"))
        self.assertEqual("one-session-measured", blocks_mod.heading_slug("One session, measured"))
        self.assertEqual("tested-combinations", blocks_mod.heading_slug("`Tested` **combinations**"))

    def test_headings_inside_code_blocks_are_not_anchors(self):
        text = "## Real\n\n```sh\n# not a heading\n```\n"
        self.assertEqual({"real"}, blocks_mod.anchors(text))


if __name__ == "__main__":
    unittest.main()
