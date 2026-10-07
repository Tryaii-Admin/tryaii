"""Catalog signing: docs/catalog/CONTRACT-catalog-v1.md section 6.

* the vendored verify-only Ed25519 (``tryaii/catalog/ed25519.py``) against the
  RFC 8032 section 7.1 test vectors, plus the negative cases of the RFC 8032
  verification procedure (flipped bits, ``S >= L``, wrong lengths,
  non-canonical / small-order points) and its speed;
* the section 6 rules of ``tryaii/catalog/signing.py``: the signed message
  (manifest minus signature/key_id, canonical text, ASCII only), strict
  base64url, the key_id format, the trusted-keys file and its
  ``TRYAII_CATALOG_TRUSTED_KEYS`` override, the packaged list;
* the release guard (``scripts/check-release-keys.py``) and the signing
  scripts (``scripts/sign-catalog-bundle.py``, ``gen-catalog-signing-key.py``;
  those need the dev-only ``cryptography`` package and skip without it).

The client behaviour (download / cache / fallbacks) is in test_catalog_client.py.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tests import _catalog_signing as test_keys
from tests.fake_auth_server import make_full_bundle
from tryaii.catalog import ed25519, signing
from tryaii.catalog.bundle import (
    BundleIntegrityError,
    bundle_from_texts,
    load_bundle,
    starter_bundle,
)
from tryaii.catalog.signing import BundleSignatureError

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPTS = REPO_ROOT / "scripts"
FULL_BUNDLE_DIR = REPO_ROOT / "build" / "catalog" / "full"

# RFC 8032 section 7.1 (Ed25519), verbatim.
RFC8032_VECTORS = [
    {
        "name": "TEST 1",
        "secret": "9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60",
        "public": "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a",
        "message": "",
        "signature": (
            "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e06522490155"
            "5fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b"
        ),
    },
    {
        "name": "TEST 2",
        "secret": "4ccd089b28ff96da9db6c346ec114e0f5b8a319f35aba624da8cf6ed4fb8a6fb",
        "public": "3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c",
        "message": "72",
        "signature": (
            "92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da"
            "085ac1e43e15996e458f3613d0f11d8c387b2eaeb4302aeeb00d291612bb0c00"
        ),
    },
    {
        "name": "TEST 3",
        "secret": "c5aa8df43f9f837bedb7442f31dcb7b166d38535076f094b85ce3a2e0b4458f7",
        "public": "fc51cd8e6218a1a38da47ed00230f0580816ed13ba3303ac5deb911548908025",
        "message": "af82",
        "signature": (
            "6291d657deec24024827e69c3abe01a30ce548a284743a445e3680d7db5ac3ac"
            "18ff9b538d16f290ae67f760984dc6594a7c15e9716ed28dc027beceea1ec40a"
        ),
    },
    {
        "name": "TEST 1024",
        "secret": "f5e5767cf153319517630f226876b86c8160cc583bc013744c6bf255f5cc0ee5",
        "public": "278117fc144c72340f67d0f2316e8386ceffbf2b2428c9c51fef7c597f1d426e",
        "message": (
            "08b8b2b733424243760fe426a4b54908632110a66c2f6591eabd3345e3e4eb98"
            "fa6e264bf09efe12ee50f8f54e9f77b1e355f6c50544e23fb1433ddf73be84d8"
            "79de7c0046dc4996d9e773f4bc9efe5738829adb26c81b37c93a1b270b20329d"
            "658675fc6ea534e0810a4432826bf58c941efb65d57a338bbd2e26640f89ffbc"
            "1a858efcb8550ee3a5e1998bd177e93a7363c344fe6b199ee5d02e82d522c4fe"
            "ba15452f80288a821a579116ec6dad2b3b310da903401aa62100ab5d1a36553e"
            "06203b33890cc9b832f79ef80560ccb9a39ce767967ed628c6ad573cb116dbef"
            "efd75499da96bd68a8a97b928a8bbc103b6621fcde2beca1231d206be6cd9ec7"
            "aff6f6c94fcd7204ed3455c68c83f4a41da4af2b74ef5c53f1d8ac70bdcb7ed1"
            "85ce81bd84359d44254d95629e9855a94a7c1958d1f8ada5d0532ed8a5aa3fb2"
            "d17ba70eb6248e594e1a2297acbbb39d502f1a8c6eb6f1ce22b3de1a1f40cc24"
            "554119a831a9aad6079cad88425de6bde1a9187ebb6092cf67bf2b13fd65f270"
            "88d78b7e883c8759d2c4f5c65adb7553878ad575f9fad878e80a0c9ba63bcbcc"
            "2732e69485bbc9c90bfbd62481d9089beccf80cfe2df16a2cf65bd92dd597b07"
            "07e0917af48bbb75fed413d238f5555a7a569d80c3414a8d0859dc65a46128ba"
            "b27af87a71314f318c782b23ebfe808b82b0ce26401d2e22f04d83d1255dc51a"
            "ddd3b75a2b1ae0784504df543af8969be3ea7082ff7fc9888c144da2af58429e"
            "c96031dbcad3dad9af0dcbaaaf268cb8fcffead94f3c7ca495e056a9b47acdb7"
            "51fb73e666c6c655ade8297297d07ad1ba5e43f1bca32301651339e22904cc8c"
            "42f58c30c04aafdb038dda0847dd988dcda6f3bfd15c4b4c4525004aa06eeff8"
            "ca61783aacec57fb3d1f92b0fe2fd1a85f6724517b65e614ad6808d6f6ee34df"
            "f7310fdc82aebfd904b01e1dc54b2927094b2db68d6f903b68401adebf5a7e08"
            "d78ff4ef5d63653a65040cf9bfd4aca7984a74d37145986780fc0b16ac451649"
            "de6188a7dbdf191f64b5fc5e2ab47b57f7f7276cd419c17a3ca8e1b939ae49e4"
            "88acba6b965610b5480109c8b17b80e1b7b750dfc7598d5d5011fd2dcc5600a3"
            "2ef5b52a1ecc820e308aa342721aac0943bf6686b64b2579376504ccc493d97e"
            "6aed3fb0f9cd71a43dd497f01f17c0e2cb3797aa2a2f256656168e6c496afc5f"
            "b93246f6b1116398a346f1a641f3b041e989f7914f90cc2c7fff357876e506b5"
            "0d334ba77c225bc307ba537152f3f1610e4eafe595f6d9d90d11faa933a15ef1"
            "369546868a7f3a45a96768d40fd9d03412c091c6315cf4fde7cb68606937380d"
            "b2eaaa707b4c4185c32eddcdd306705e4dc1ffc872eeee475a64dfac86aba41c"
            "0618983f8741c5ef68d3a101e8a3b8cac60c905c15fc910840b94c00a0b9d0"
        ),
        "signature": (
            "0aab4c900501b3e24d7cdf4663326a3a87df5e4843b2cbdb67cbf6e460fec350"
            "aa5371b1508f9f4528ecea23c436d94b5e8fcd4f681e30a6ac00a9704a188a03"
        ),
    },
    {
        "name": "TEST SHA(abc)",
        "secret": "833fe62409237b9d62ec77587520911e9a759cec1d19755b7da901b96dca3d42",
        "public": "ec172b93ad5e563bf4932c70e1245034c35467ef2efd4d64ebf819683467e2bf",
        "message": (
            "ddaf35a193617abacc417349ae20413112e6fa4e89a97ea20a9eeee64b55d39a"
            "2192992a274fc1a836ba3c23a3feebbd454d4423643ce80e2a9ac94fa54ca49f"
        ),
        "signature": (
            "dc2a4459e7369633a52b1bf277839a00201009a3efbf3ecb69bea2186c26b589"
            "09351fc9ac90b3ecfdfbc7c66431e0303dca179c138ac17ad9bef1177331a704"
        ),
    },
]

L = 2**252 + 27742317777372353535851937790883648493
P = 2**255 - 19


def _vec(name):
    t = next(v for v in RFC8032_VECTORS if v["name"] == name)
    return (bytes.fromhex(t["public"]), bytes.fromhex(t["message"]),
            bytes.fromhex(t["signature"]))


def _flip(data: bytes, index: int, bit: int = 0) -> bytes:
    out = bytearray(data)
    out[index] ^= 1 << bit
    return bytes(out)


# ===================================================================== Ed25519
@pytest.mark.parametrize("vector", RFC8032_VECTORS, ids=lambda v: v["name"])
def test_rfc8032_vectors_verify(vector):
    pub, msg, sig = (bytes.fromhex(vector[k]) for k in ("public", "message", "signature"))
    assert ed25519.verify(pub, msg, sig) is True


def test_the_vectors_match_the_shared_fixture_the_node_tests_use():
    shared = REPO_ROOT / "shared" / "catalog" / "parity" / "rfc8032_ed25519_vectors.json"
    assert json.loads(shared.read_text(encoding="utf-8"))["vectors"] == RFC8032_VECTORS


def test_rfc8032_vector_lengths():
    assert [len(_vec(n)[1]) for n in ("TEST 1", "TEST 2", "TEST 3", "TEST 1024")] == [0, 1, 2, 1023]


@pytest.mark.parametrize("name", ["TEST 1", "TEST 2", "TEST 3", "TEST 1024"])
@pytest.mark.parametrize("where", ["sig_r", "sig_s", "msg", "key"])
def test_a_flipped_bit_is_rejected(name, where):
    pub, msg, sig = _vec(name)
    if where == "sig_r":
        sig = _flip(sig, 3)
    elif where == "sig_s":
        sig = _flip(sig, 40)
    elif where == "msg":
        msg = _flip(msg, len(msg) // 2) if msg else b"\x00"
    else:
        pub = _flip(pub, 7)
    assert ed25519.verify(pub, msg, sig) is False


def test_s_not_below_l_is_rejected():
    pub, msg, sig = _vec("TEST 2")
    s = int.from_bytes(sig[32:], "little")
    malleable = sig[:32] + (s + L).to_bytes(32, "little")  # same point, non-canonical S
    assert ed25519.verify(pub, msg, malleable) is False
    max_s = sig[:32] + b"\xff" * 32
    assert ed25519.verify(pub, msg, max_s) is False


@pytest.mark.parametrize("key_len,sig_len",
                         [(31, 64), (33, 64), (0, 64), (32, 63), (32, 65), (32, 0)])
def test_wrong_lengths_are_rejected(key_len, sig_len):
    pub, msg, sig = _vec("TEST 3")
    pub = (pub + b"\x00")[:key_len] if key_len <= 32 else pub + b"\x00" * (key_len - 32)
    sig = (sig + b"\x00")[:sig_len] if sig_len <= 64 else sig + b"\x00" * (sig_len - 64)
    assert ed25519.verify(pub, msg, sig) is False


def test_wrong_types_are_rejected():
    pub, msg, sig = _vec("TEST 3")
    assert ed25519.verify(pub.hex(), msg, sig) is False
    assert ed25519.verify(pub, msg.hex(), sig) is False
    assert ed25519.verify(pub, msg, None) is False
    assert ed25519.verify(bytearray(pub), bytearray(msg), bytearray(sig)) is True


def test_non_canonical_point_encodings_are_rejected():
    _, msg, sig = _vec("TEST 1")
    # y = p (>= p): non-canonical encoding of y = 0
    assert ed25519.verify(P.to_bytes(32, "little"), msg, sig) is False
    # y = 1 is the identity with x = 0; the sign bit set means "x = -0": invalid
    neg_zero = (1 | (1 << 255)).to_bytes(32, "little")
    assert ed25519.verify(neg_zero, msg, sig) is False
    # non-canonical R (y >= p) with an otherwise valid key
    pub, msg2, sig2 = _vec("TEST 2")
    assert ed25519.verify(pub, msg2, (P + 1).to_bytes(32, "little") + sig2[32:]) is False
    # a y with no x on the curve (y = 2 is not on edwards25519)
    assert ed25519.verify((2).to_bytes(32, "little"), msg, sig) is False


def test_small_order_points_are_rejected():
    identity = (1).to_bytes(32, "little")
    # The classic forgery: A = R = identity and S = 0 satisfy [S]B == R + [k]A
    # for EVERY message under the cofactorless equation. Rejected because
    # small-order points are refused.
    assert ed25519.verify(identity, b"any message", identity + bytes(32)) is False
    # a small-order R with a real key
    pub, msg, sig = _vec("TEST 2")
    assert ed25519.verify(pub, msg, identity + sig[32:]) is False
    # the order-2 point (0, -1): y = p - 1
    order2 = (P - 1).to_bytes(32, "little")
    assert ed25519.verify(order2, b"", order2 + bytes(32)) is False


def test_the_independent_test_signer_agrees_with_the_vectors():
    for vector in RFC8032_VECTORS:
        seed = bytes.fromhex(vector["secret"])
        msg = bytes.fromhex(vector["message"])
        assert test_keys.public_key(seed).hex() == vector["public"]
        assert test_keys.sign(msg, seed).hex() == vector["signature"]


def test_verify_speed():
    """One verification must stay well under ~25 ms (contract work item: if
    it did not, the client would verify once per process). Generous bound for
    slow CI machines; typical is ~6-10 ms."""
    pub, msg, sig = _vec("TEST 1024")
    ed25519.verify(pub, msg, sig)  # warm up
    runs = 5
    start = time.perf_counter()
    for _ in range(runs):
        assert ed25519.verify(pub, msg, sig)
    per_call = (time.perf_counter() - start) / runs
    assert per_call < 0.1, f"{per_call * 1000:.1f} ms per verification"


# ===================================================================== section 6 rules
def _manifest(**extra):
    m = {
        "schema": 1, "kind": "full", "version": "2026.10.04.1",
        "embedding_model": "all-MiniLM-L6-v2", "created_at": "2026-10-04T00:00:00Z",
        "counts": {"models": 3, "benchmarks": 2},
        "files": {"models.json": "ab" * 32},
        "signature": None, "key_id": None,
    }
    m.update(extra)
    return m


def test_signed_message_is_the_canonical_manifest_without_signature_and_key_id():
    m = _manifest()
    expected = json.dumps({k: v for k, v in m.items() if k not in ("signature", "key_id")},
                          sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    assert signing.signed_message(m) == expected.encode("utf-8")
    signed = test_keys.sign_manifest(copy.deepcopy(m))
    # signature / key_id never change the message; key order never matters
    assert signing.signed_message(signed) == signing.signed_message(m)
    reordered = dict(reversed(list(signed.items())))
    assert signing.signed_message(reordered) == signing.signed_message(m)
    assert signing.signed_message(m) == test_keys.signed_message(m)


@pytest.mark.parametrize("bad", [
    {"created_at": "2026-10-04T00:00:00Zé"},          # non-ASCII string value
    {"noteé": "x"},                                   # non-ASCII key
    {"counts": {"models": 3, "benchmarks": 2, "x": ["☃"]}},  # nested
])
def test_non_ascii_manifests_are_rejected(bad):
    m = test_keys.sign_manifest(_manifest(**bad))  # validly signed, still refused
    with pytest.raises(BundleSignatureError, match="ASCII"):
        signing.signed_message(m)
    with pytest.raises(BundleSignatureError, match="ASCII"):
        signing.verify_manifest_signature(m)


@pytest.mark.parametrize("value", [1.5, 1.0, 2**53])
def test_non_integer_or_unsafe_numbers_are_rejected(value):
    with pytest.raises(BundleSignatureError):
        signing.signed_message(_manifest(counts={"models": value, "benchmarks": 2}))


def test_signature_is_strict_base64url_without_padding():
    raw = bytes(range(64))
    good = signing.b64url_encode(raw)
    assert "=" not in good and len(good) == 86
    assert signing.b64url_decode(good, 64) == raw
    assert signing.b64url_decode(good + "==", 64) is None          # padding
    assert signing.b64url_decode(base64.b64encode(raw).decode(), 64) is None  # + and /
    assert signing.b64url_decode(good[:-1], 64) is None             # too short
    assert signing.b64url_decode(good, 63) is None                  # wrong length
    assert signing.b64url_decode(" " + good, 64) is None            # whitespace
    # non-zero unused trailing bits: same bytes, non-canonical text
    last = good[-1]
    alt = good[:-1] + "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"[
        ("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_".index(last) | 1)]
    if alt != good:
        assert signing.b64url_decode(alt, 64) is None
    assert signing.b64url_decode(None, 64) is None


@pytest.mark.parametrize("key_id,ok", [
    ("tryaii-catalog-2026-1", True), ("dev-catalog-2026-10", True), ("abc", True),
    ("a" * 64, True), ("ab", False), ("a" * 65, False), ("Dev-key", False),
    ("dev_key", False), ("dev key", False), ("dev-key\n", False), ("", False), (None, False),
])
def test_key_id_format(key_id, ok):
    assert signing.is_valid_key_id(key_id) is ok


def _signed(**kw):
    return test_keys.sign_manifest(_manifest(), **kw)


def test_valid_signature_from_a_trusted_key_is_accepted():
    # conftest points TRYAII_CATALOG_TRUSTED_KEYS at a file listing the test key
    assert signing.verify_manifest_signature(_signed()) == test_keys.TEST_KEY_ID


@pytest.mark.parametrize("case", ["unsigned", "no_key_id", "unknown_key", "bad_signature",
                                  "tampered_field", "bad_key_id", "bad_encoding",
                                  "wrong_key_same_id"])
def test_untrusted_signatures_are_rejected(case):
    m = _signed()
    if case == "unsigned":
        m["signature"] = None
        m["key_id"] = None
    elif case == "no_key_id":
        del m["key_id"]
    elif case == "unknown_key":
        m = _signed(seed=test_keys.OTHER_SEED, key_id=test_keys.OTHER_KEY_ID)
    elif case == "bad_signature":
        raw = bytearray(test_keys.b64url_decode_any(m["signature"]))
        raw[10] ^= 0x80
        m["signature"] = test_keys.b64url(bytes(raw))
    elif case == "tampered_field":
        m["counts"]["models"] += 1
    elif case == "bad_key_id":
        m["key_id"] = "Test Catalog"
    elif case == "bad_encoding":
        m["signature"] = m["signature"] + "=="
    elif case == "wrong_key_same_id":
        m = _signed(seed=test_keys.OTHER_SEED)  # claims the trusted key_id
    with pytest.raises(BundleSignatureError):
        signing.verify_manifest_signature(m)


def test_signature_error_is_an_integrity_error():
    # "handled exactly like a hash mismatch" (section 6 -> section 5)
    assert issubclass(BundleSignatureError, BundleIntegrityError)


def test_env_override_replaces_the_packaged_list(tmp_path, monkeypatch):
    m = _signed()
    only_other = test_keys.write_trusted_keys(
        tmp_path / "other.json",
        [test_keys.key_entry(test_keys.OTHER_KEY_ID, test_keys.OTHER_SEED)])
    monkeypatch.setenv(signing.TRUSTED_KEYS_ENV, str(only_other))
    with pytest.raises(BundleSignatureError, match="unknown key"):
        signing.verify_manifest_signature(m)
    both = test_keys.write_trusted_keys(tmp_path / "both.json", [
        test_keys.key_entry(test_keys.OTHER_KEY_ID, test_keys.OTHER_SEED), test_keys.key_entry()])
    monkeypatch.setenv(signing.TRUSTED_KEYS_ENV, str(both))
    assert signing.verify_manifest_signature(m) == test_keys.TEST_KEY_ID
    # the override REPLACES the built-in list: the packaged dev key is not added
    packaged = json.loads(signing.PACKAGED_TRUSTED_KEYS.read_text(encoding="utf-8"))
    assert all(e["key_id"] not in signing.load_trusted_keys() for e in packaged["keys"])
    # without the override the packaged list applies (it does not hold the test key)
    monkeypatch.delenv(signing.TRUSTED_KEYS_ENV)
    assert signing.trusted_keys_path() == signing.PACKAGED_TRUSTED_KEYS
    with pytest.raises(BundleSignatureError, match="unknown key"):
        signing.verify_manifest_signature(m)


def test_an_explicit_env_mapping_is_honoured(tmp_path):
    keys = test_keys.write_trusted_keys(tmp_path / "k.json")
    assert signing.verify_manifest_signature(
        _signed(), {signing.TRUSTED_KEYS_ENV: str(keys)}) == test_keys.TEST_KEY_ID
    with pytest.raises(BundleSignatureError):
        signing.verify_manifest_signature(_signed(), {})  # packaged list only


_GOOD_KEY = test_keys.b64url(test_keys.public_key())
_IDENTITY_KEY = test_keys.b64url((1).to_bytes(32, "little"))  # small order: weak
_ORDER4_KEY = test_keys.b64url(bytes(32))  # y = 0: small order
_NONCANONICAL_KEY = test_keys.b64url((2**255 - 19).to_bytes(32, "little"))  # y = p


@pytest.mark.parametrize("content", [
    "not json", "[]", '{"keys": {}}', '{"keys": [1]}',
    '{"keys": [{"key_id": "BAD", "public_key": "x", "env": "development"}]}',
    '{"keys": [{"key_id": "abc", "public_key": "AAAA", "env": "development"}]}',
    f'{{"keys": [{{"key_id": "abc", "public_key": "{_GOOD_KEY}", "env": "staging"}}]}}',
    f'{{"keys": [{{"key_id": "abc", "public_key": "{_IDENTITY_KEY}", "env": "development"}}]}}',
    f'{{"keys": [{{"key_id": "abc", "public_key": "{_ORDER4_KEY}", "env": "development"}}]}}',
    f'{{"keys": [{{"key_id": "abc", "public_key": "{_NONCANONICAL_KEY}", "env": "development"}}]}}',
    f'{{"keys": [{{"key_id": "abc", "public_key": "{_GOOD_KEY}", "env": "development"}},'
    f' {{"key_id": "abc", "public_key": "{_GOOD_KEY}", "env": "development"}}]}}',
])
def test_a_malformed_trusted_keys_file_trusts_nothing(tmp_path, monkeypatch, content):
    path = tmp_path / "keys.json"
    path.write_text(content, encoding="utf-8")
    monkeypatch.setenv(signing.TRUSTED_KEYS_ENV, str(path))
    with pytest.raises(BundleSignatureError, match="trusted catalog keys"):
        signing.verify_manifest_signature(_signed())


def test_a_missing_trusted_keys_file_trusts_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv(signing.TRUSTED_KEYS_ENV, str(tmp_path / "missing.json"))
    with pytest.raises(BundleSignatureError):
        signing.verify_manifest_signature(_signed())


def test_packaged_trusted_keys_match_the_shared_master():
    master = REPO_ROOT / "shared" / "catalog" / "trusted_keys.json"
    assert signing.PACKAGED_TRUSTED_KEYS.read_bytes() == master.read_bytes()
    keys = signing.parse_trusted_keys(json.loads(master.read_text(encoding="utf-8")))
    assert keys, "the packaged list must hold at least one key"
    # the test key is never shipped
    assert test_keys.TEST_KEY_ID not in keys
    assert all(k["public_key"] != test_keys.public_key() for k in keys.values())


def test_the_starter_bundle_needs_no_signature(tmp_path, monkeypatch):
    starter = starter_bundle()
    assert starter.manifest.get("signature") is None
    # even with an empty trusted list the starter loads
    monkeypatch.setenv(signing.TRUSTED_KEYS_ENV,
                       str(test_keys.write_trusted_keys(tmp_path / "none.json", [])))
    assert load_bundle(starter.directory).kind == "starter"


# --------------------------------------------------------------- bundle loader hooks
def _bundle_texts(directory: Path):
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    files = {name: (directory / name).read_text(encoding="utf-8") for name in (
        "models.json", "benchmarks.json", "normalization_ranges.json", "centroids.json",
        "training_queries.json")}
    return manifest, files


def test_bundle_loaders_verify_only_when_asked(tmp_path):
    signed_dir = make_full_bundle(tmp_path / "signed", "2026.10.04.1")
    unsigned_dir = make_full_bundle(tmp_path / "unsigned", "2026.10.04.1", signed=False)
    loaded = load_bundle(signed_dir, verify_signature=True)
    assert loaded.manifest["key_id"] == test_keys.TEST_KEY_ID
    assert load_bundle(unsigned_dir).kind == "full"  # an explicit path: no signature needed
    with pytest.raises(BundleSignatureError):
        load_bundle(unsigned_dir, verify_signature=True)
    manifest, files = _bundle_texts(signed_dir)
    assert bundle_from_texts(manifest, files, verify_signature=True).version == "2026.10.04.1"
    manifest["created_at"] = "2026-10-04T00:00:09Z"
    with pytest.raises(BundleSignatureError):
        bundle_from_texts(manifest, files, verify_signature=True)


def test_signature_is_checked_before_the_data_files(tmp_path):
    """Section 6 + 1: an untrusted manifest is refused before any file is
    hashed or parsed (a missing file would otherwise be the error)."""
    unsigned_dir = make_full_bundle(tmp_path / "u", "2026.10.04.1", signed=False)
    (unsigned_dir / "models.json").unlink()
    with pytest.raises(BundleSignatureError):
        load_bundle(unsigned_dir, verify_signature=True)


def test_schema_is_checked_before_the_signature(tmp_path):
    """A too-new schema keeps its own message (section 5) even when unsigned."""
    from tryaii.catalog.bundle import BundleSchemaError

    d = make_full_bundle(tmp_path / "s", "2026.10.04.1", signed=False)
    manifest = json.loads((d / "manifest.json").read_text(encoding="utf-8"))
    manifest["schema"] = 99
    (d / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(BundleSchemaError):
        load_bundle(d, verify_signature=True)


def test_the_built_full_bundle_is_signed_with_a_packaged_key(monkeypatch):
    """build/catalog/full (when built) is signed with a key of the packaged
    list -- ready for publishing."""
    if not (FULL_BUNDLE_DIR / "manifest.json").is_file():
        pytest.skip("build/catalog/full not built")
    manifest = json.loads((FULL_BUNDLE_DIR / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("signature") is None:
        pytest.skip("build/catalog/full is not signed yet (scripts/sign-catalog-bundle.py)")
    monkeypatch.delenv(signing.TRUSTED_KEYS_ENV, raising=False)
    assert signing.verify_manifest_signature(manifest) == manifest["key_id"]


# ===================================================================== release guard
def _load_script(name: str):
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _entry(key_id, env, seed_text):
    seed = hashlib.sha256(seed_text.encode()).digest()
    public = test_keys.b64url(test_keys.public_key(seed))
    return {"key_id": key_id, "public_key": public, "env": env}


def test_release_guard_accepts_production_only(tmp_path):
    guard = _load_script("check-release-keys")
    good = test_keys.write_trusted_keys(tmp_path / "prod.json", [
        _entry("tryaii-catalog-2026-1", "production", "p1"),
        _entry("tryaii-catalog-2027-1", "production", "p2")])
    assert guard.check_file(good) == []
    assert guard.main([str(good)]) == 0


@pytest.mark.parametrize("entries,needle", [
    ([("dev-catalog-x", "development")], "DEVELOPMENT"),
    ([("tryaii-catalog-2026-1", "production"), ("dev-catalog-x", "development")], "DEVELOPMENT"),
    ([], "no production key"),
    ([("tryaii-catalog-2026-1", "staging")], "env must be"),
])
def test_release_guard_fails_on_development_or_missing_production_keys(tmp_path, entries, needle):
    guard = _load_script("check-release-keys")
    path = test_keys.write_trusted_keys(
        tmp_path / "keys.json", [_entry(k, e, k) for k, e in entries])
    errors = guard.check_file(path)
    assert any(needle in line for line in errors), errors
    assert guard.main([str(path)]) == 1


def test_release_guard_flags_out_of_sync_package_copies(tmp_path, monkeypatch):
    guard = _load_script("check-release-keys")
    master = test_keys.write_trusted_keys(
        tmp_path / "master.json", [_entry("tryaii-catalog-2026-1", "production", "p")])
    same = tmp_path / "py.json"
    same.write_bytes(master.read_bytes())
    other = test_keys.write_trusted_keys(
        tmp_path / "node.json", [_entry("tryaii-catalog-2026-2", "production", "q")])
    monkeypatch.setattr(guard, "MASTER", master)
    monkeypatch.setattr(guard, "PACKAGED", (same, other))
    errors = guard.check()
    assert len(errors) == 1 and "differs" in errors[0]


@pytest.mark.skipif(os.environ.get("TRYAII_RELEASE_CHECK") != "1",
                    reason="release-only guard: run with TRYAII_RELEASE_CHECK=1 (fails if "
                           "the committed trusted_keys.json holds a development key)")
def test_release_ships_production_keys_only():
    """The release guard as a test (catalog contract section 6). Skipped in the
    normal suite; the release workflow runs scripts/check-release-keys.py."""
    guard = _load_script("check-release-keys")
    assert guard.check() == []


# ===================================================================== scripts
def _run(script, *args, cwd=REPO_ROOT):
    return subprocess.run([sys.executable, str(SCRIPTS / script), *args], cwd=str(cwd),
                          capture_output=True, text=True, timeout=300)


@pytest.fixture
def crypto_available():
    pytest.importorskip("cryptography", reason="dev-only dependency: pip install cryptography")


def test_gen_and_sign_scripts_round_trip(tmp_path, crypto_available, monkeypatch):
    keydir = tmp_path / "keys"  # outside the repository
    gen = _run("gen-catalog-signing-key.py", "--out", str(keydir), "--key-id", "dev-test-roundtrip")
    assert gen.returncode == 0, gen.stderr
    entry = json.loads(gen.stdout)  # stdout is ONLY the public entry
    assert set(entry) == {"key_id", "public_key", "env"} and entry["env"] == "development"
    assert "PRIVATE" not in gen.stdout
    pem = keydir / "dev-test-roundtrip.pem"
    assert pem.is_file()
    if os.name == "posix":
        assert pem.stat().st_mode & 0o777 == 0o600
    again = _run("gen-catalog-signing-key.py", "--out", str(keydir),
                 "--key-id", "dev-test-roundtrip")
    assert again.returncode != 0  # never overwrites

    bundle = make_full_bundle(tmp_path / "bundle", "2026.10.04.7", signed=False)
    before = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
    signed = _run("sign-catalog-bundle.py", str(bundle), "--key", str(pem),
                  "--key-id", "dev-test-roundtrip")
    assert signed.returncode == 0, signed.stderr
    assert "self-check OK" in signed.stdout
    assert "PRIVATE" not in signed.stdout + signed.stderr
    after = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
    assert list(after) == list(before)  # key order preserved
    assert {k: v for k, v in after.items() if k not in ("signature", "key_id")} == \
        {k: v for k, v in before.items() if k not in ("signature", "key_id")}
    keys = test_keys.write_trusted_keys(tmp_path / "trusted.json", [entry])
    monkeypatch.setenv(signing.TRUSTED_KEYS_ENV, str(keys))
    assert load_bundle(bundle, verify_signature=True).manifest["key_id"] == "dev-test-roundtrip"


def test_sign_script_refuses_non_ascii_manifests(tmp_path, crypto_available):
    keydir = tmp_path / "keys"
    assert _run("gen-catalog-signing-key.py", "--out", str(keydir),
                "--key-id", "dev-test-ascii").returncode == 0
    bundle = make_full_bundle(tmp_path / "bundle", "2026.10.04.8", signed=False)
    manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
    manifest["created_at"] = "2026-10-04T00:00:00Z été"
    (bundle / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False),
                                          encoding="utf-8")
    result = _run("sign-catalog-bundle.py", str(bundle), "--key",
                  str(keydir / "dev-test-ascii.pem"), "--key-id", "dev-test-ascii")
    assert result.returncode != 0 and "ASCII" in (result.stderr + result.stdout)


def test_gen_script_refuses_a_tracked_directory_in_the_repo(crypto_available):
    result = _run("gen-catalog-signing-key.py", "--out", str(REPO_ROOT / "docs"),
                  "--key-id", "dev-never-written")
    assert result.returncode != 0
    assert not (REPO_ROOT / "docs" / "dev-never-written.pem").exists()
