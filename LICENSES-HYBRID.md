# Optional hybrid retrieval license inventory

`sqlite_hybrid` does not download or bundle model weights.

| Part | Exact component | License / redistribution terms |
| --- | --- | --- |
| Embedding model | None. Signed feature hashing over word and character n-grams in `above_all.hybrid` | Repository code only; no third-party weights or model redistribution |
| Tokenizer | Python `re` plus in-project Unicode word pattern | Python Software Foundation License v2; redistribution allowed with its notice |
| Runtime | Python 3.10+ standard library (`hashlib`, `json`, `math`, `re`) and SQLite bundled with Python | PSF License v2 for Python; SQLite is public domain |
| Transitive packages | None | None |

There are no API calls, hidden network dependencies, runtime downloads, or AGPL components. The tradeoff is quality: this 256-float hashing representation catches lexical and spelling similarity, not general sentence meaning. It is an opt-in measurement candidate, not a claim of parity with a trained embedding model.
