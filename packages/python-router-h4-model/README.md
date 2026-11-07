# tryaii-router-h4

The local prompt router of the [tryaii](https://pypi.org/project/tryaii/) SDK:
**H4** (`h4-he-en`), a Hebrew + English [Model2Vec](https://github.com/MinishLab/model2vec)
static embedding model (Unigram tokenizer, 40,000 tokens, 256-d int8 vectors)
and a small classification head over the 33 catalog benchmarks. It runs on
numpy and `tokenizers` only: no PyTorch, no onnxruntime.

You do not install this package yourself: `pip install tryaii` brings it in.
It holds five files (12.9 MB in total):

| file | content |
| --- | --- |
| `vectors.safetensors` | `embeddings`, int8 (40000, 256) |
| `head.safetensors` | `W0` (512, 256), `b0` (512), `W1` (33, 512), `b1` (33), float32 |
| `tokenizer.json` | the Unigram tokenizer (NFKC-based normalizer, no SentencePiece charsmap) |
| `classes.json` | benchmark name of each head output |
| `meta.json` | router id, version, temperature (2.0), token cap (700), ... |

`SHA256SUMS` lists the sha256 of each file. The package hash
(`b85b07c215479dce26cd283e3220e47924b08944f53b450e477e1d13a8cb9675`) is the
sha256 of `SHA256SUMS`; tryaii verifies it before it uses the files. Prompts
are classified on your machine; nothing is downloaded at run time.

On a machine that cannot install it, copy the directory anywhere and set
`TRYAII_ROUTER_MODEL_PATH` to it.

Built by `scripts/export-h4-model.py` in the tryaii repository.
