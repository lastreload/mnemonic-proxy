"""Level 1 of the README tests: everything that can be checked without running anything.

Since 0.3.25 "the README" is the whole tested documentation: `README.md` (the landing page)
plus `docs/*.md` (`docs/dev-notes/` excluded — raw experiment logs, partly in Italian).
`tools/readme_blocks.doc_files()` is the single definition of that set, so a block that moved
from the README into `docs/install.md` is still tested, in the same way, with its page named
in the failure message.

- every fenced block that could be executed carries a `<!-- readme-test: … -->` label;
- every `mnemonic-proxy` / `mnemonic-mcp` command line in the documentation parses with the
  real parser of the program it calls (built from the code, not copied here);
- every proxy-configuration JSON example contains only real `Config` fields and is accepted
  by `Config.from_dict`;
- every internal link resolves: a same-page `#anchor`, and also a `page.md#anchor` from one
  page to another, in both directions.

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


def doc_files():
    return blocks_mod.doc_files(REPO)


def all_blocks():
    """Every block of every documentation page, each one knowing which page it is in."""
    return blocks_mod.load_all(REPO)


def readme_text():
    with open(README, encoding="utf-8") as f:
        return f.read()


def doc_texts():
    """{"README.md": text, "docs/install.md": text, …}"""
    out = {}
    for path in doc_files():
        with open(path, encoding="utf-8") as f:
            out[blocks_mod.rel(path, REPO)] = f.read()
    return out


class DocumentSetTests(unittest.TestCase):
    """The set of tested pages must be the real one: a new page is tested by existing."""

    def test_the_landing_page_and_the_docs_pages_are_all_in(self):
        names = [blocks_mod.rel(p, REPO) for p in doc_files()]
        self.assertEqual("README.md", names[0])
        for expected in ("docs/install.md", "docs/agents.md", "docs/engines.md",
                         "docs/configuration.md", "docs/how-it-works.md", "docs/benchmarks.md",
                         "docs/limits.md", "docs/security.md", "docs/troubleshooting.md",
                         "docs/tools.md", "docs/development.md"):
            self.assertIn(expected, names)

    def test_every_markdown_page_in_docs_is_tested(self):
        """Adding docs/new-page.md must not create an untested page."""
        on_disk = {"docs/" + n for n in os.listdir(os.path.join(REPO, "docs"))
                   if n.endswith(".md")}
        self.assertEqual(on_disk, {blocks_mod.rel(p, REPO) for p in doc_files()} - {"README.md"})

    def test_dev_notes_are_left_out(self):
        names = [blocks_mod.rel(p, REPO) for p in doc_files()]
        self.assertEqual([], [n for n in names if "dev-notes" in n])

    def test_the_landing_page_stays_short(self):
        """The point of the split: the first page must stay readable in one sitting."""
        self.assertLess(len(readme_text().splitlines()), 200)


class LabelTests(unittest.TestCase):
    """No executable block may go unlabeled: a new example must say how it is tested."""

    def test_every_executable_block_is_labeled(self):
        missing = [(b.path, b.line, b.lang, b.section) for b in all_blocks()
                   if b.executable and b.label is None]
        self.assertEqual([], missing,
                         "add '<!-- readme-test: ci|windows|real-engine|illustrative -->' "
                         "on the line above these blocks: %r" % (missing,))

    def test_labels_are_known(self):
        bad = [(b.path, b.line, b.label) for b in all_blocks()
               if b.label is not None and b.label not in blocks_mod.LABELS]
        self.assertEqual([], bad, "unknown readme-test label(s): %r" % (bad,))

    def test_json_blocks_declare_which_configuration_they_are(self):
        bad = [(b.path, b.line, b.section) for b in all_blocks()
               if b.lang == "json" and b.config_kind not in blocks_mod.CONFIG_KINDS]
        self.assertEqual([], bad,
                         "every JSON block needs '; config: proxy|partial|client' in its label: %r" % (bad,))

    def test_at_least_one_block_of_each_label_is_present(self):
        labels = {b.label for b in all_blocks()}
        for label in ("ci", "real-engine", "illustrative"):
            self.assertIn(label, labels)

    def test_the_landing_page_has_a_ci_block(self):
        """The three lines of the first page are the ones a new user types first."""
        self.assertIn("ci", {b.label for b in blocks_mod.load(README)})


class CommandLineTests(unittest.TestCase):
    """Every documented command line must be accepted by the real parser."""

    def all_commands(self):
        out = []
        for b in all_blocks():
            if b.lang in ("json", "yaml", "toml", "bibtex"):
                continue
            for argv in cli.commands(b.text):
                if cli.program(argv) is not None:
                    out.append((b.path, b.line, argv))
        return out

    def test_the_documentation_documents_some_commands(self):
        self.assertGreaterEqual(len(self.all_commands()), 15)

    def test_every_command_line_parses(self):
        bad = []
        for path, line, argv in self.all_commands():
            problem = cli.check_argv(argv)
            if problem:
                bad.append("%s:%d  %s  ->  %s" % (path, line, " ".join(argv), problem))
        self.assertEqual([], bad, "invalid command line(s) in the documentation:\n" + "\n".join(bad))

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
        self.assertEqual(["check", "demo", "forget", "init-config"], cli.known_subcommands())
        self.assertEqual(sorted(cli.SUBCOMMAND_PARSERS), cli.known_subcommands())


class ConfigExampleTests(unittest.TestCase):
    """A configuration example with a key the proxy does not know would be silently ignored."""

    def config_blocks(self):
        return [b for b in all_blocks()
                if b.lang == "json" and b.config_kind in ("proxy", "partial")]

    def test_there_is_at_least_one_proxy_config_example(self):
        self.assertTrue([b for b in self.config_blocks() if b.config_kind == "proxy"])

    def test_examples_are_valid_json(self):
        for b in self.config_blocks():
            with self.subTest(page=b.path, line=b.line):
                json.loads(b.text)

    def test_examples_use_only_real_config_fields(self):
        import dataclasses
        from ctxproxy.core import Config
        names = {f.name for f in dataclasses.fields(Config)}
        for b in self.config_blocks():
            with self.subTest(page=b.path, line=b.line):
                unknown = sorted(set(json.loads(b.text)) - names)
                self.assertEqual([], unknown,
                                 "%s:%d: keys the proxy would ignore: %r" % (b.path, b.line, unknown))

    def test_examples_are_accepted_by_from_dict(self):
        from ctxproxy.core import Config
        for b in self.config_blocks():
            with self.subTest(page=b.path, line=b.line):
                Config.from_dict(json.loads(b.text))

    def test_shipped_examples_use_only_real_config_fields(self):
        """The files the documentation tells the user to copy must be valid too."""
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
    """A broken link is invisible until a reader clicks it — and the split made most of
    them cross-page, so checking one file at a time is no longer enough."""

    def test_internal_links_resolve(self):
        broken = blocks_mod.broken_links(REPO)
        self.assertEqual([], broken,
                         "link(s) that do not resolve:\n  " + "\n  ".join(broken))

    def test_there_are_internal_links_to_check(self):
        """Same threshold as before the split, counted over the whole documentation."""
        total = sum(len(blocks_mod.links(t)) for t in doc_texts().values())
        self.assertGreaterEqual(total, 20)

    def test_the_landing_page_links_into_docs(self):
        """A short first page is only useful if it sends the reader to the right page."""
        targets = {t for _, t, _ in blocks_mod.links(readme_text()) if t.startswith("docs/")}
        self.assertGreaterEqual(len(targets), 8, targets)

    def test_every_docs_page_is_reachable_from_the_landing_page(self):
        """A page nobody links to is a page nobody reads. Reachable means reachable by
        following links, not necessarily linked from the first page itself:
        `docs/manual-forget.md` hangs off `docs/tools.md`."""
        texts = doc_texts()
        seen = {"README.md"}
        queue = ["README.md"]
        while queue:
            name = queue.pop()
            here = os.path.dirname(name)
            for _, target, _ in blocks_mod.links(texts[name]):
                if not target:
                    continue
                nxt = os.path.normpath(os.path.join(here, target)).replace(os.sep, "/")
                if nxt in texts and nxt not in seen:
                    seen.add(nxt)
                    queue.append(nxt)
        unreachable = sorted(set(texts) - seen)
        self.assertEqual([], unreachable,
                         "no path of links from README.md reaches: %r" % (unreachable,))

    def test_a_broken_cross_page_link_would_be_caught(self):
        """The check must be able to fail."""
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "docs"))
            with open(os.path.join(d, "README.md"), "w", encoding="utf-8") as f:
                f.write("# A\n\n[good](docs/p.md#real) [bad anchor](docs/p.md#nope) "
                        "[bad file](docs/gone.md)\n")
            with open(os.path.join(d, "docs", "p.md"), "w", encoding="utf-8") as f:
                f.write("# Real\n")
            problems = blocks_mod.broken_links(d)
            self.assertEqual(2, len(problems), problems)
            self.assertTrue(any("docs/p.md#nope" in p for p in problems), problems)
            self.assertTrue(any("docs/gone.md" in p for p in problems), problems)

    def test_links_inside_code_blocks_are_not_links(self):
        text = "# A\n\n```sh\ncurl ](nowhere.md)\n```\n"
        self.assertEqual([], blocks_mod.links(text))

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
