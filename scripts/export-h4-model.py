#!/usr/bin/env python
"""Export the H4 router (Hebrew + English) into the data-package payload.

H4 is a Model2Vec static model (Unigram tokenizer, 40k tokens, 256-d int8
vectors) plus a one-hidden-layer classification head over the 33 benchmark
labels. It was trained in the routing POC (routing-poc, phase H4, setup
``H4-int8``, seed 0); this script only repackages it, it does not train.

Input: the POC model dir (``models/hebrew/H4-int8-s0``)::

    static/model.safetensors   "embeddings" int8 (vocab, 256)
    static/tokenizer.json      Unigram tokenizer (normalizer: Precompiled + Replace ...)
    static/config.json         max_length 700, normalize true
    head.npz                   W0 (512,256) b0 (512) W1 (33,512) b1 (33) classes (33,) int64
    meta.json                  "benches": the sorted benchmark names; classes[i] indexes it

Output (the payload; the same files go into both data packages)::

    vectors.safetensors   "embeddings" int8 (vocab, 256), the exact POC values. No scale
                          tensor: the vector of a text is the mean of its token rows,
                          L2-normalised, so any per-matrix scale cancels out.
    head.safetensors      W0, b0, W1, b1 (float32, exact)
    tokenizer.json        the POC tokenizer; with --normalizer nfkc (default) the inner
                          normalizer Sequence [Precompiled, Replace " {2,}" -> " "] is
                          replaced by NFKC_SEQUENCE (variant "A2": drop control chars,
                          map whitespace-like chars to " ", NFKC, collapse spaces);
                          everything after it is unchanged. A2 was checked to be
                          accuracy-neutral against the Precompiled charsmap, and it
                          needs no SentencePiece charsmap (easy to port to Node).
    classes.json          benchmark names in head-output order: benches[classes[i]]
    meta.json             router id, version, temperature, token/char caps, ...
    SHA256SUMS            "<sha256>  <file>" per payload file (sorted by name)

The payload hash pinned by the SDK (``tryaii.embeddings.model_store.ROUTERS``)
and by both packages (``SHA256``) is the sha256 of the SHA256SUMS text: one
hash over every file's hash. ``SIZE_BYTES`` is the sum of the payload file
sizes (SHA256SUMS itself excluded).

Inference contract (what the SDK providers implement; the POC's
model2vec 0.10 StaticModel.encode + phase_f_common.Setup / phase_sh4_common):

1. text = text[: max_tokens * median_token_length] (code points; 700 * 5 = 3500)
2. ids  = tokenizer.encode(text, add_special_tokens=False), truncated to 700 tokens,
          then every unk id (1) dropped
3. e    = mean of vectors[ids] (int8 -> float), then e / (||e|| + 1e-32); no ids -> zeros
4. h    = relu(W0 e + b0); logits = W1 h + b1
5. p    = softmax(logits / temperature), temperature 2 ("P-T2"); scores[classes[i]] = p[i]

Deterministic: the same input gives byte-identical output (hand-written
safetensors with sorted keys, sorted JSON). Requirements: numpy only.

Usage:
  python scripts/export-h4-model.py --src <POC>/models/hebrew/H4-int8-s0 \
      --out packages/python-router-h4-model/tryaii_router_h4 \
      --out packages/router-h4-model [--normalizer nfkc|precompiled]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import struct
import sys
from pathlib import Path

import numpy as np

ROUTER_ID = "h4-he-en"
VERSION = "1.0.0"
TEMPERATURE = 2.0
MAX_TOKENS = 700
DIM = 256
PAYLOAD = ("classes.json", "head.safetensors", "meta.json", "tokenizer.json",
           "vectors.safetensors")

_ST_DTYPES = {np.dtype("int8"): "I8", np.dtype("float32"): "F32", np.dtype("int64"): "I64"}
_ST_NP = {v: k for k, v in _ST_DTYPES.items()}


# -- safetensors (tiny, dependency-free) -------------------------------------

def safetensors_bytes(tensors: dict) -> bytes:
    """Byte-stable safetensors: keys sorted, compact sorted-key JSON header
    padded with spaces to 8 bytes, little-endian C-order data."""
    header, blobs, offset = {}, [], 0
    for name in sorted(tensors):
        arr = np.ascontiguousarray(tensors[name])
        arr = arr.astype(arr.dtype.newbyteorder("<"), copy=False)
        raw = arr.tobytes(order="C")
        header[name] = {"dtype": _ST_DTYPES[arr.dtype], "shape": list(arr.shape),
                        "data_offsets": [offset, offset + len(raw)]}
        blobs.append(raw)
        offset += len(raw)
    text = json.dumps(header, sort_keys=True, separators=(",", ":")).encode("utf-8")
    text += b" " * ((8 - len(text) % 8) % 8)
    return struct.pack("<Q", len(text)) + text + b"".join(blobs)


def parse_safetensors(data: bytes) -> dict:
    (n,) = struct.unpack("<Q", data[:8])
    header = json.loads(data[8:8 + n].decode("utf-8"))
    body = data[8 + n:]
    out = {}
    for name, info in header.items():
        if name == "__metadata__":
            continue
        s, e = info["data_offsets"]
        dt = _ST_NP[info["dtype"]].newbyteorder("<")
        out[name] = np.frombuffer(body[s:e], dtype=dt).reshape(info["shape"]).copy()
    return out


# -- tokenizer ---------------------------------------------------------------

#: The inner normalizer Sequence of the "nfkc" variant (A2), replacing
#: [Precompiled, Replace " {2,}" -> " "].
NFKC_SEQUENCE = {"type": "Sequence", "normalizers": [
    {"type": "Replace", "pattern": {"Regex": (
        "[\u0001\u0002\u0003\u0004\u0005\u0006\u0007\b\u000b\u000e\u000f\u0010"
        "\u0011\u0012\u0013\u0014\u0015\u0016\u0017\u0018\u0019\u001a\u001b"
        "\u001c\u001d\u001e\u001f\u007f\u008f\u009f]")}, "content": ""},
    {"type": "Replace", "pattern": {"Regex": (
        "[\t\n\f\r\u1680\u200b\u200c\u200d\u200e\u200f\u2028\u2029\u2581"
        "\ufeff\ufffd]")}, "content": " "},
    {"type": "NFKC"},
    {"type": "Replace", "pattern": {"Regex": " {2,}"}, "content": " "},
]}


def replace_precompiled(node, replaced: list):
    """Replace the Sequence that holds {"type": "Precompiled"} (expected:
    [Precompiled, Replace " {2,}" -> " "]) by NFKC_SEQUENCE."""
    if isinstance(node, dict):
        if node.get("type") == "Sequence" and any(
                isinstance(n, dict) and n.get("type") == "Precompiled"
                for n in node.get("normalizers", [])):
            inner = node["normalizers"]
            assert [n.get("type") for n in inner] == ["Precompiled", "Replace"], inner
            assert inner[1] == {"type": "Replace", "pattern": {"Regex": " {2,}"},
                                "content": " "}, inner[1]
            replaced.append(1)
            return json.loads(json.dumps(NFKC_SEQUENCE))
        return {k: replace_precompiled(v, replaced) for k, v in node.items()}
    if isinstance(node, list):
        return [replace_precompiled(v, replaced) for v in node]
    return node


def median_token_length(tok: dict) -> int:
    """model2vec's StaticModel.median_token_length: int(np.median(len(token)))
    over the whole vocabulary (added tokens included)."""
    vocab = {}
    for i, (token, _score) in enumerate(tok["model"]["vocab"]):
        vocab[token] = i
    for added in tok.get("added_tokens") or []:
        vocab[added["content"]] = added["id"]
    return int(np.median([len(t) for t in vocab]))


def dumps(obj) -> bytes:
    return (json.dumps(obj, ensure_ascii=False, indent=1, sort_keys=False) + "\n").encode("utf-8")


# -- export --------------------------------------------------------------------

def build(src: Path, normalizer: str) -> dict:
    """Payload file name -> bytes."""
    meta_in = json.loads((src / "meta.json").read_text(encoding="utf-8"))
    cfg = json.loads((src / "static" / "config.json").read_text(encoding="utf-8"))
    assert cfg.get("normalize") is True, "the router expects normalize=true"
    assert int(cfg.get("max_length")) == MAX_TOKENS, cfg
    benches = list(meta_in["benches"])
    assert benches == sorted(benches), "meta.json benches must be the sorted label list"

    st = parse_safetensors((src / "static" / "model.safetensors").read_bytes())
    assert set(st) == {"embeddings"}, f"unexpected tensors {sorted(st)} (weights/mapping unsupported)"
    vectors = st["embeddings"]
    assert vectors.dtype == np.int8 and vectors.shape[1] == DIM, (vectors.dtype, vectors.shape)

    h = np.load(src / "head.npz")
    assert sorted(h.files) == ["W0", "W1", "b0", "b1", "classes"], h.files
    head = {k: np.asarray(h[k], dtype=np.float32) for k in ("W0", "b0", "W1", "b1")}
    for k, value in head.items():
        assert np.array_equal(value, h[k]), f"{k} is not float32 in the source"
    classes = [int(c) for c in h["classes"]]
    assert sorted(classes) == list(range(len(benches))), "classes must index every benchmark once"
    assert head["W1"].shape[0] == len(classes) and head["W0"].shape[1] == DIM

    tok = json.loads((src / "static" / "tokenizer.json").read_text(encoding="utf-8"))
    assert tok["model"]["type"] == "Unigram"
    unk_id = tok["model"].get("unk_id")
    vocab_size = len(tok["model"]["vocab"])
    assert vocab_size == vectors.shape[0], (vocab_size, vectors.shape)
    if normalizer == "nfkc":
        replaced: list = []
        tok["normalizer"] = replace_precompiled(tok["normalizer"], replaced)
        assert len(replaced) == 1, f"expected one Precompiled Sequence, found {len(replaced)}"
        assert "Precompiled" not in json.dumps(tok["normalizer"])
    mtl = median_token_length(tok)

    files = {}
    for name, tensors in (("vectors.safetensors", {"embeddings": vectors}),
                          ("head.safetensors", head)):
        files[name] = safetensors_bytes(tensors)
        back = parse_safetensors(files[name])
        for k, v in tensors.items():
            assert back[k].dtype == v.dtype and np.array_equal(back[k], v), f"{name}:{k} round trip"
    files["tokenizer.json"] = dumps(tok)
    files["classes.json"] = dumps([benches[c] for c in classes])
    files["meta.json"] = dumps({
        "router_id": ROUTER_ID,
        "version": VERSION,
        "kind": "model2vec-static+mlp-head",
        "dim": DIM,
        "vocab_size": vocab_size,
        "max_tokens": MAX_TOKENS,
        "median_token_length": mtl,
        "char_cap": MAX_TOKENS * mtl,
        "unk_id": unk_id,
        "add_special_tokens": False,
        "normalize": True,
        "vectors": {"tensor": "embeddings", "dtype": "int8", "scale": None,
                    "note": "mean of int8 rows then L2-normalised; a scale would cancel"},
        "head": {"layers": [["W0", "b0", "relu"], ["W1", "b1", None]]},
        "temperature": TEMPERATURE,
        "num_classes": len(classes),
        "normalizer": normalizer,
        "source": {"setup": meta_in.get("setup"), "cfg": meta_in.get("cfg"),
                   "seed": meta_in.get("seed"), "epochs": meta_in.get("epochs")},
    })
    return files


def manifest(files: dict) -> str:
    return "".join(f"{hashlib.sha256(files[n]).hexdigest()}  {n}\n" for n in sorted(PAYLOAD))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--src", required=True, type=Path, help="POC model dir (H4-int8-s0)")
    ap.add_argument("--out", action="append", type=Path, default=None,
                    help="output dir (repeatable); default build/models/h4-he-en")
    ap.add_argument("--normalizer", choices=("nfkc", "precompiled"), default="nfkc")
    args = ap.parse_args(argv)

    files = build(args.src, args.normalizer)
    sums = manifest(files)
    payload_sha = hashlib.sha256(sums.encode("utf-8")).hexdigest()
    size = sum(len(files[n]) for n in PAYLOAD)
    outs = args.out or [Path("build") / "models" / ROUTER_ID]
    for out in outs:
        out.mkdir(parents=True, exist_ok=True)
        for name in PAYLOAD:
            (out / name).write_bytes(files[name])
        (out / "SHA256SUMS").write_bytes(sums.encode("utf-8"))
        print(f"wrote {out}")
    print(sums, end="")
    print(f"payload sha256 {payload_sha}")
    print(f"payload size   {size} bytes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
