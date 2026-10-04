"""Costruisce un tokenizer.json HF (libreria `tokenizers`, veloce) equivalente al tokenizer del pack di Strata
(vocab.json + merges.txt + token_type.json, pre-tokenizer qwen35). Il tokenizer.json del pack NON è in formato HF
(è un descrittore): il proxy di produzione non lo caricava e contava caratteri/3.5.

    python3 make_hf_tokenizer.py <dir tokenizer del pack> <out tokenizer.json> [--check strata_tokenizer.py]
"""
from __future__ import annotations

import json
import sys

from tokenizers import AddedToken, Regex, Tokenizer, decoders, models, pre_tokenizers


def build(d: str) -> Tokenizer:
    desc = json.load(open(d + "/tokenizer.json", encoding="utf-8"))
    vocab = json.load(open(d + "/vocab.json", encoding="utf-8"))
    merges = [tuple(m.split(" ")) for m in open(d + "/merges.txt", encoding="utf-8").read().split("\n") if m]
    types = json.load(open(d + "/token_type.json"))
    inv = {i: t for t, i in vocab.items()}
    tk = Tokenizer(models.BPE(vocab=vocab, merges=merges, fuse_unk=False, byte_fallback=False))
    tk.pre_tokenizer = pre_tokenizers.Sequence([
        pre_tokenizers.Split(Regex(desc["pre_pattern"]), behavior="isolated", invert=False),
        pre_tokenizers.ByteLevel(add_prefix_space=False, trim_offsets=False, use_regex=False)])
    tk.decoder = decoders.ByteLevel()
    # type 3 (CONTROL) e 4 (USER_DEFINED): Strata codifica il prompt con parse_special=True -> entrambi letterali
    special = [AddedToken(inv[i], special=True, normalized=False) for i, ty in enumerate(types) if ty in (3, 4)]
    tk.add_special_tokens(special)
    return tk


def main():
    src, out = sys.argv[1], sys.argv[2]
    tk = build(src)
    tk.save(out)
    print("salvato", out, tk.get_vocab_size())
    if "--check" in sys.argv:
        sys.path.insert(0, sys.argv[sys.argv.index("--check") + 1])
        import strata_tokenizer as ST
        vocab = json.load(open(src + "/vocab.json", encoding="utf-8"))
        toks = [None] * len(vocab)
        for t, i in vocab.items():
            toks[i] = t
        st = ST.Tokenizer(toks, open(src + "/merges.txt", encoding="utf-8").read().split("\n"),
                          json.load(open(src + "/token_type.json")))
        samples = ["ciao mondo  \n\n  x", "<|im_start|>user\nperché? 🎮 è così<|im_end|>\n<think>\n\n</think>\n\n",
                   "def f(x):\n\treturn x**2  # ünïcødé\r\n", "<tool_call>\n<function=bash>\n<parameter=command>\nls -la"
                   "\n</parameter>\n</function>\n</tool_call>", "   \n\n\n   12345.678 'll 'S", "日本語のテキスト。中文"]
        for extra in sys.argv[sys.argv.index("--check") + 2:]:
            samples.append(open(extra, encoding="utf-8").read())
        bad = 0
        for s in samples:
            a = tk.encode(s, add_special_tokens=False).ids
            b = st.encode(s, parse_special=True)
            if a != b:
                bad += 1
                print("DIVERSO", len(a), len(b), repr(s[:80]))
        print("campioni", len(samples), "diversi", bad)


if __name__ == "__main__":
    main()
