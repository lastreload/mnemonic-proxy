# Author: Maurizio Verde — LastReload
"""Example configurations, shipped inside the wheel as `ctxproxy.examples` (0.3.23).

This folder keeps its place in the repository — the README links and CI point at it — and is also installed
with the package, so `mnemonic-proxy init-config --engine strata|llama-server|ds4|openai-32k` writes a real
`config.json` after a plain `pip install`, with no clone and no network. The mapping lives in `pyproject.toml`
(`[tool.setuptools.package-dir]`); the code that reads them is `ctxproxy/initconfig.py`.

No code here: only data files.
"""
