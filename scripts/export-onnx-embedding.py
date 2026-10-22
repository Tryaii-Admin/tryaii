#!/usr/bin/env python
"""Export all-MiniLM-L6-v2 to ONNX and quantise it to int8 (TS-252).

The SDK's default embedding engine is this file run on ``onnxruntime``
(packages/python/tryaii/embeddings/onnx_provider.py): no PyTorch at run time.
This script is the only place PyTorch is needed, and it is maintainer-only.

Steps:

1. Load ``sentence-transformers/all-MiniLM-L6-v2`` at a pinned revision with
   ``transformers`` (the BERT encoder only; pooling and normalisation are done
   by the SDK: mean over the attention mask, then L2).
2. ``torch.onnx.export`` (the torch.export exporter) the encoder: inputs
   ``input_ids``, ``attention_mask``, ``token_type_ids`` (int64, dynamic batch and sequence), output
   ``last_hidden_state``; opset 17.
3. Weight-only int8 (see ``quantize``): per-channel int8 weights, cast and
   scaled back to fp32 at load; fp32 arithmetic, the same results on every CPU.
4. Copy the model's ``tokenizer.json`` into the package
   (``tryaii/embeddings/data/<model id>/tokenizer.json``, small, Apache-2.0)
   so only the model file has to be downloaded.
5. Copy the model into the model package source
   (``packages/python-model/tryaii_minilm_int8/model.onnx``, published to PyPI
   as ``tryaii-minilm-int8``; tryaii depends on it, pinned exactly).
6. Check parity against sentence-transformers on the starter training queries
   (cosine between the two embeddings of the same text) and print the
   model file's sha256 and size.

When the sha256 changes: pin it in ``tryaii/embeddings/model_store.py``
(``MODELS``) and in the model package's ``__init__.py``, bump the model
package version (and tryaii's pin), and rebuild the int8 centroids
(scripts/gen-centroids.py, then scripts/build-catalog-bundles.py).

The output is byte-stable for the same torch / onnx / onnxruntime versions;
a different toolchain can produce a different (equally valid) file, whose
new sha256 must then be pinned and published together with fresh centroids.

Requirements (a maintainer venv, not the SDK's): torch, transformers,
sentence-transformers, onnx, onnxruntime, tokenizers.

Usage:
  python scripts/export-onnx-embedding.py              # writes build/models/<id>/
  python scripts/export-onnx-embedding.py --out DIR
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PKG = REPO / "packages" / "python"

HF_REPO = "sentence-transformers/all-MiniLM-L6-v2"
HF_REVISION = "1110a243fdf4706b3f48f1d95db1a4f5529b4d41"
MODEL_ID = "all-MiniLM-L6-v2-onnx-int8"
OPSET = 17
MAX_LENGTH = 256
MODEL_PACKAGE_FILE = REPO / "packages" / "python-model" / "tryaii_minilm_int8" / "model.onnx"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def export_fp32(out: Path) -> Path:
    import torch
    from transformers import AutoModel

    model = AutoModel.from_pretrained(HF_REPO, revision=HF_REVISION)
    model.eval()
    # Batch 2 x length 8: torch.export specialises any dimension of size 1.
    ids = [[101, 7592, 2088, 2003, 1037, 3231, 1012, 102],
           [101, 2129, 2024, 2017, 102, 0, 0, 0]]
    sample = {
        "input_ids": torch.tensor(ids, dtype=torch.long),
        "attention_mask": torch.tensor([[1] * 8, [1] * 5 + [0] * 3], dtype=torch.long),
        "token_type_ids": torch.zeros((2, 8), dtype=torch.long),
    }

    class Encoder(torch.nn.Module):
        def __init__(self, inner):
            super().__init__()
            self.inner = inner

        def forward(self, input_ids, attention_mask, token_type_ids):
            return self.inner(input_ids=input_ids, attention_mask=attention_mask,
                              token_type_ids=token_type_ids).last_hidden_state

    path = out / "model-fp32.onnx"
    batch = torch.export.Dim("batch", min=1, max=1024)
    seq = torch.export.Dim("sequence", min=1, max=MAX_LENGTH)
    shape = {0: batch, 1: seq}
    # The torch.export ("dynamo") exporter: transformers >= 5 builds its
    # attention masks with code the legacy TorchScript tracer cannot follow.
    with torch.no_grad():
        program = torch.onnx.export(
            Encoder(model),
            (sample["input_ids"], sample["attention_mask"], sample["token_type_ids"]),
            input_names=["input_ids", "attention_mask", "token_type_ids"],
            output_names=["last_hidden_state"],
            dynamic_shapes=(shape, shape, shape),
            opset_version=OPSET,
            dynamo=True,
            optimize=True,
            external_data=False,
        )
    program.save(str(path), external_data=False)
    return path


#: Weight matrices smaller than this stay fp32 (biases, LayerNorm, positions).
MIN_QUANT_ELEMENTS = 4096


def quantize(fp32: Path, out: Path) -> Path:
    """Weight-only int8: every MatMul weight and the embedding tables are
    stored as symmetric per-channel int8 plus an fp32 scale, turned back into
    fp32 by ``Cast(int8 -> float) * scale``; all arithmetic stays fp32.
    onnxruntime folds the Cast*Mul into fp32 weights when the session loads.

    Two things this deliberately avoids, both of which make the vectors
    depend on the CPU and the onnxruntime version (so the same text could be
    classified differently on two machines):

    * onnxruntime's dynamic quantisation, which also quantises the
      activations at run time and runs integer kernels (VNNI or not, u8s8
      saturation);
    * ``DequantizeLinear``: with full graph optimisation onnxruntime fuses
      DequantizeLinear -> MatMul into an integer MatMul kernel, which brings
      the same CPU dependence back (measured: up to 3e-3 per component).
    """
    import numpy as np
    import onnx
    from onnx import helper, numpy_helper

    model = onnx.load(str(fp32))
    graph = model.graph
    consumers: dict[str, list] = {}
    for node in graph.node:
        for name in node.input:
            consumers.setdefault(name, []).append(node)
    new_inits, dq_nodes = [], []
    for init in list(graph.initializer):
        weights = numpy_helper.to_array(init)
        if (weights.dtype != np.float32 or weights.ndim != 2
                or weights.size < MIN_QUANT_ELEMENTS):
            continue
        uses = consumers.get(init.name, [])
        if not uses or not all(n.op_type in ("MatMul", "Gather") for n in uses):
            continue
        # MatMul weights [K, N]: one scale per output column (axis 1).
        # Gather tables [V, D]: one scale per row (axis 0).
        axis = 0 if uses[0].op_type == "Gather" else 1
        scale = np.max(np.abs(weights), axis=1 - axis) / 127.0
        scale[scale == 0] = 1.0
        shape = (-1, 1) if axis == 0 else (1, -1)
        q = np.clip(np.round(weights / scale.reshape(shape)), -127, 127).astype(np.int8)
        graph.initializer.remove(init)
        name = init.name
        new_inits += [numpy_helper.from_array(q, f"{name}_q"),
                      numpy_helper.from_array(scale.reshape(shape).astype(np.float32),
                                              f"{name}_scale")]
        dq_nodes += [
            helper.make_node("Cast", [f"{name}_q"], [f"{name}_f"], to=onnx.TensorProto.FLOAT,
                             name=f"{name}_cast"),
            helper.make_node("Mul", [f"{name}_f", f"{name}_scale"], [name], name=f"{name}_mul"),
        ]
    graph.initializer.extend(new_inits)
    nodes = list(graph.node)
    del graph.node[:]
    graph.node.extend(dq_nodes + nodes)
    onnx.checker.check_model(model)
    path = out / "model.onnx"
    onnx.save(model, str(path))
    return path


def copy_tokenizer() -> Path:
    from huggingface_hub import hf_hub_download

    src = Path(hf_hub_download(HF_REPO, "tokenizer.json", revision=HF_REVISION))
    dst = PKG / "tryaii" / "embeddings" / "data" / MODEL_ID / "tokenizer.json"
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dst)
    return dst


def parity(model_path: Path) -> dict:
    """Cosine between the sentence-transformers (fp32, PyTorch) and the int8
    ONNX embedding of every starter training query."""
    import numpy as np
    from sentence_transformers import SentenceTransformer

    sys.path.insert(0, str(PKG))
    from tryaii.catalog.bundle import starter_bundle
    from tryaii.embeddings.onnx_provider import OnnxEmbeddingProvider

    texts = [q for qs in starter_bundle().training_query_map().values() for q in qs]
    ref = SentenceTransformer(HF_REPO, revision=HF_REVISION, device="cpu").encode(
        texts, convert_to_numpy=True, normalize_embeddings=True, batch_size=32)
    onnx = np.stack(OnnxEmbeddingProvider(model_path=model_path).embed_batch(texts))
    cos = (ref * onnx).sum(axis=1)
    return {"texts": len(texts), "cos_min": float(cos.min()),
            "cos_mean": float(cos.mean()), "cos_p01": float(np.percentile(cos, 1))}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=REPO / "build" / "models" / MODEL_ID)
    parser.add_argument("--skip-parity", action="store_true")
    args = parser.parse_args(argv)
    args.out.mkdir(parents=True, exist_ok=True)

    fp32 = export_fp32(args.out)
    model = quantize(fp32, args.out)
    tokenizer = copy_tokenizer()
    shutil.copyfile(model, MODEL_PACKAGE_FILE)
    info = {
        "model_id": MODEL_ID,
        "source": f"{HF_REPO}@{HF_REVISION}",
        "file": str(model),
        "sha256": sha256_file(model),
        "size_bytes": model.stat().st_size,
        "fp32_size_bytes": fp32.stat().st_size,
        "model_package_file": str(MODEL_PACKAGE_FILE.relative_to(REPO)),
        "tokenizer": str(tokenizer.relative_to(REPO)),
        "tokenizer_sha256": sha256_file(tokenizer),
    }
    if not args.skip_parity:
        info["parity_vs_sentence_transformers"] = parity(model)
    print(json.dumps(info, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
