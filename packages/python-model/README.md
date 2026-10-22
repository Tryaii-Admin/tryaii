# tryaii-minilm-int8

The local embedding model of the [tryaii](https://pypi.org/project/tryaii/) SDK:
[all-MiniLM-L6-v2](https://huggingface.co/sentence-transformers/all-MiniLM-L6-v2)
(Apache-2.0) exported to ONNX with int8 weights and run on `onnxruntime`, so
tryaii needs no PyTorch.

You do not install this package yourself: `pip install tryaii` brings it in.
It contains one 23.0 MB file, `model.onnx`
(sha256 `8199be6f699a395f99f89df973f9dc5285fbcfcdb2ae1a4e1201dab014f3d2a6`),
which tryaii verifies before it uses it. Prompts are embedded on your machine;
nothing is downloaded at run time.

On a machine that cannot install it, place the file anywhere and set
`TRYAII_EMBEDDING_MODEL_PATH` to it.

Built by `scripts/export-onnx-embedding.py` in the tryaii repository.
