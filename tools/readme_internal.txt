# Entries that exist in the code and are deliberately NOT documented in README.md.
# One per line: <kind>:<name>  # why
# Kinds: subcommand, option:<program>, config
#
# tests/test_readme_coverage.py fails on anything in the code that is neither mentioned in
# the README nor listed here, so adding a public option without documenting it breaks CI.

option:mnemonic-proxy:--help          # argparse's own, present on every command
option:mnemonic-proxy check:--help    # idem
option:mnemonic-proxy demo:--help     # idem
option:mnemonic-proxy forget:--help   # idem
option:mnemonic-mcp:--help            # idem
