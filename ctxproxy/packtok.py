"""Tokenizer del pack di Strata -> tokenizer HF (`tokenizers`), identico a serve/strata_tokenizer.py.

Il tokenizer.json del pack è un descrittore (model gpt2, pre qwen35, pre_pattern), non un file HF: qui si costruisce
BPE byte-level con lo split regex del pack e i token speciali (CONTROL e USER_DEFINED, che Strata legge con
parse_special=True). Verificato: 109.490 token = prompt_tokens di Strata su una sessione reale.
"""
from __future__ import annotations

import json
import os


def build(d: str):
    from tokenizers import AddedToken, Regex, Tokenizer, decoders, models, pre_tokenizers  # type: ignore
    desc = json.load(open(os.path.join(d, "tokenizer.json"), encoding="utf-8"))
    vocab = json.load(open(os.path.join(d, "vocab.json"), encoding="utf-8"))
    merges = [tuple(m.split(" ")) for m in open(os.path.join(d, "merges.txt"), encoding="utf-8").read().split("\n")
              if m]
    types = json.load(open(os.path.join(d, "token_type.json")))
    inv = {i: t for t, i in vocab.items()}
    tk = Tokenizer(models.BPE(vocab=vocab, merges=merges, fuse_unk=False, byte_fallback=False))
    tk.pre_tokenizer = pre_tokenizers.Sequence([
        pre_tokenizers.Split(Regex(desc["pre_pattern"]), behavior="isolated", invert=False),
        pre_tokenizers.ByteLevel(add_prefix_space=False, trim_offsets=False, use_regex=False)])
    tk.decoder = decoders.ByteLevel()
    tk.add_special_tokens([AddedToken(inv[i], special=True, normalized=False)
                           for i, ty in enumerate(types) if ty in (3, 4)])
    return tk
