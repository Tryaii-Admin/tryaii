"""In-memory fake of the tryaii auth API (docs/auth/CONTRACT-auth-v1.md section 4).

Stdlib only. Used by the Python auth CLI tests in-process (``start_server``)
and by the cross-SDK byte-parity harness as a subprocess:

    python fake_auth_server.py --port 0 --outcome approve

prints the bound port on its FIRST stdout line (flushed), then serves until
killed. Every value it hands out is deterministic (user code, tokens, session
ids, timestamps) so two CLIs driven against two fresh instances see identical
responses.

Endpoints: POST /v1/auth/device/code, POST /v1/auth/token (device + refresh
grants, rotation, reuse detection), POST /v1/auth/revoke, GET /v1/auth/me,
GET /v1/catalog/live (docs/catalog/CONTRACT-catalog-v1.md section 4: serves
the bundle directory given by ``--catalog-dir``, ETag / If-None-Match -> 304,
gzip when accepted, entitlement ``catalog:full``), GET /healthz. Test-only
endpoints: GET /_state (request log + counters), POST /_catalog
(``{"mode": ..., "dir": ...}`` switches the catalog behaviour at runtime).

Catalog modes (``--catalog-mode``): ``ok`` (serve the bundle; 404
no_live_release when there is none), ``401``, ``403``, ``404``, ``429``,
``500``, ``corrupt`` (one file's text no longer matches its manifest hash),
``schema`` (manifest ``schema`` bumped past what clients support),
``malformed`` (``benchmarks.json`` is ``{}`` WITH a matching manifest hash:
right hashes, wrong shape), ``badtype`` (``files["models.json"]`` is the
number 10**12 instead of a string), and the signing modes of catalog
contract section 6: ``unsigned`` (``signature``/``key_id`` null),
``badsig`` (one bit of the signature flipped), ``unknownkey`` (validly signed
by a key that is not in the trusted list) and ``tampered`` (a manifest field
changed after signing). ``schema`` and ``malformed`` re-sign the changed
manifest, so they test the schema / shape rules, not the signature.

Bundles are signed with the TEST key of ``_catalog_signing.py`` (catalog
contract section 6). Clients must trust it through
``TRYAII_CATALOG_TRUSTED_KEYS``:
``python fake_auth_server.py --write-trusted-keys FILE`` writes such a file
and exits.

``python fake_auth_server.py --make-full-bundle DIR [--bundle-version V] [--unsigned]``
writes a small but valid *full* bundle (the starter data plus one ``:free``
twin, so its routable count differs from ``counts.models``), signed with the
test key unless ``--unsigned``, and exits.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

try:  # imported as tests.fake_auth_server
    from tests import _catalog_signing as signing
except ImportError:  # run as a script from any directory
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import _catalog_signing as signing

DEVICE_GRANT_TYPE = "urn:ietf:params:oauth:grant-type:device_code"
FIXED_CREATED_AT = "2026-10-03T12:00:00Z"
OUTCOMES = ("approve", "deny", "expire", "invalid_grant")
# Refresh-grant results: "ok", or an error. "rate_limited" answers HTTP 429;
# every other value is sent as a 400 {"error": <value>} (v1.1 item 14 needs a
# non-invalid_grant code, e.g. "invalid_request").
REFRESH_OUTCOMES = ("ok", "invalid_grant", "invalid_request", "rate_limited")
# /me results: "ok", "401" (invalid_token), "429", or "400" (invalid_request).
ME_OUTCOMES = ("ok", "401", "400", "429")
# Which endpoint answers HTTP 429 (contract: 429 {"error": "slow_down"}).
RATE_LIMIT_AT = ("none", "device", "poll")
_RATE_LIMITED = (429, {"error": "slow_down"})
CATALOG_MODES = ("ok", "401", "403", "404", "429", "500", "corrupt", "schema",
                 "malformed", "badtype", "unsigned", "badsig", "unknownkey", "tampered")
BUNDLE_FILES = ("models.json", "benchmarks.json", "normalization_ranges.json",
                "centroids.json", "training_queries.json")
STARTER_DIR = Path(__file__).resolve().parents[1] / "tryaii" / "catalog" / "data" / "starter"


def _canonical(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def make_full_bundle(dest, version="2026.10.04.1", *, extra_free=True, signed=True) -> Path:
    """Write a valid full bundle to ``dest``: the starter's data (plus, by
    default, a ``:free`` twin of its first model) under a ``kind: full``
    manifest, signed with the test key (catalog contract section 6) unless
    ``signed=False``. Routable models = the starter's count."""
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    texts = {}
    for name in BUNDLE_FILES:
        texts[name] = (STARTER_DIR / name).read_text(encoding="utf-8")
    if extra_free:
        models = json.loads(texts["models.json"])
        twin = dict(models["models"][0])
        twin["model_id"] = twin["model_id"] + ":free"
        models["models"].append(twin)
        texts["models.json"] = _canonical(models)
    starter = json.loads((STARTER_DIR / "manifest.json").read_text(encoding="utf-8"))
    n_models = len(json.loads(texts["models.json"])["models"])
    manifest = {
        "schema": 1, "kind": "full", "version": version,
        "embedding_model": starter["embedding_model"],
        "created_at": "2026-10-04T00:00:00Z",
        "counts": {"models": n_models, "benchmarks": starter["counts"]["benchmarks"]},
        "files": {name: hashlib.sha256(texts[name].encode("utf-8")).hexdigest()
                  for name in BUNDLE_FILES},
        "signature": None, "key_id": None,
    }
    if signed:
        signing.sign_manifest(manifest)
    for name, text in texts.items():
        (dest / name).write_bytes(text.encode("utf-8"))
    (dest / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n",
                                        encoding="utf-8")
    return dest


class FakeAuthState:
    def __init__(self, *, outcome="approve", approve_after=2, interval=0,
                 expires_in=600, access_expires_in=3600,
                 refresh_expires_in=7776000, refresh_outcome="ok",
                 me_outcome="ok", slow_down_first=False, rate_limit_at="none",
                 email="dev@example.com", name="Dev User",
                 user_id="google-sub-1", entitlements=("catalog:full",),
                 web_url="https://tryaii.com", catalog_dir=None, catalog_mode="ok"):
        if outcome not in OUTCOMES:
            raise ValueError(f"unknown outcome {outcome!r}")
        if refresh_outcome not in REFRESH_OUTCOMES:
            raise ValueError(f"unknown refresh_outcome {refresh_outcome!r}")
        if str(me_outcome) not in ME_OUTCOMES:
            raise ValueError(f"unknown me_outcome {me_outcome!r}")
        if rate_limit_at not in RATE_LIMIT_AT:
            raise ValueError(f"unknown rate_limit_at {rate_limit_at!r}")
        self.outcome = outcome
        self.approve_after = max(1, int(approve_after))
        self.interval = int(interval)
        self.expires_in = int(expires_in)
        self.access_expires_in = int(access_expires_in)
        self.refresh_expires_in = int(refresh_expires_in)
        self.refresh_outcome = refresh_outcome
        self.me_outcome = str(me_outcome)
        self.rate_limit_at = rate_limit_at
        self.slow_down_first = slow_down_first
        self.user = {"id": user_id, "email": email, "name": name}
        self.entitlements = list(entitlements)
        self.web_url = web_url.rstrip("/")
        if catalog_mode not in CATALOG_MODES:
            raise ValueError(f"unknown catalog_mode {catalog_mode!r}")
        self.catalog_dir = catalog_dir
        self.catalog_mode = catalog_mode
        self.lock = threading.Lock()
        self.counter = 0
        self.grants = {}      # device_code -> grant dict
        self.sessions = {}    # refresh_token -> session dict
        self.access = {}      # access_token -> family
        self.revoked_families = set()
        self.requests = []    # (method, path, body) log

    def _next(self) -> int:
        self.counter += 1
        return self.counter

    # -- endpoints --------------------------------------------------------
    def device_code(self, body):
        if self.rate_limit_at == "device":
            return _RATE_LIMITED
        if not isinstance(body, dict):
            return 400, {"error": "invalid_request", "error_description": "bad body"}
        if body.get("client_id") != "tryaii-cli":
            return 400, {"error": "invalid_client", "error_description": "unknown client"}
        n = self._next()
        device_code = f"fake-device-code-{n:04d}"
        user_code = "BCDF-GHJK"
        self.grants[device_code] = {
            "status": "pending", "polls": 0, "interval": self.interval,
            "last_polled": None, "client": body.get("client"),
        }
        return 200, {
            "device_code": device_code,
            "user_code": user_code,
            "verification_uri": f"{self.web_url}/device",
            "verification_uri_complete": f"{self.web_url}/device?user_code={user_code}",
            "expires_in": self.expires_in,
            "interval": self.interval,
        }

    def _issue(self, family):
        n = self._next()
        refresh = f"tair_fake{n:04d}"
        access = f"fake-access-{n:04d}"
        self.sessions[refresh] = {"family": family, "rotated": False}
        self.access[access] = family
        return 200, {
            "access_token": access, "token_type": "Bearer",
            "expires_in": self.access_expires_in,
            "refresh_token": refresh,
            "refresh_expires_in": self.refresh_expires_in,
            "user": dict(self.user),
            "entitlements": list(self.entitlements),
        }

    def token(self, body):
        if not isinstance(body, dict):
            return 400, {"error": "invalid_request"}
        if body.get("client_id") != "tryaii-cli":
            return 400, {"error": "invalid_client"}
        grant_type = body.get("grant_type")
        if grant_type == DEVICE_GRANT_TYPE:
            return self._device_grant(body.get("device_code"))
        if grant_type == "refresh_token":
            return self._refresh_grant(body.get("refresh_token"))
        return 400, {"error": "unsupported_grant_type"}

    def _device_grant(self, device_code):
        if self.rate_limit_at == "poll":
            return _RATE_LIMITED
        grant = self.grants.get(device_code)
        if grant is None or grant["status"] == "consumed":
            return 400, {"error": "invalid_grant"}
        now = time.monotonic()
        grant["polls"] += 1
        too_fast = (grant["interval"] > 0 and grant["last_polled"] is not None
                    and now - grant["last_polled"] < grant["interval"])
        grant["last_polled"] = now
        if (self.slow_down_first and grant["polls"] == 1) or too_fast:
            grant["interval"] += 5
            return 400, {"error": "slow_down"}
        if grant["polls"] < self.approve_after:
            return 400, {"error": "authorization_pending"}
        if self.outcome == "deny":
            return 400, {"error": "access_denied"}
        if self.outcome == "expire":
            return 400, {"error": "expired_token"}
        if self.outcome == "invalid_grant":
            return 400, {"error": "invalid_grant"}
        grant["status"] = "consumed"
        family = f"{self._next():032x}"
        return self._issue(family)

    def _refresh_grant(self, refresh_token):
        if self.refresh_outcome == "rate_limited":
            return _RATE_LIMITED
        if self.refresh_outcome not in ("ok", "invalid_grant"):
            return 400, {"error": self.refresh_outcome}
        session = self.sessions.get(refresh_token)
        if self.refresh_outcome != "ok" or session is None:
            return 400, {"error": "invalid_grant"}
        family = session["family"]
        if family in self.revoked_families:
            return 400, {"error": "invalid_grant"}
        if session["rotated"]:
            # Reuse detection: revoke the whole family.
            self.revoked_families.add(family)
            return 400, {"error": "invalid_grant"}
        session["rotated"] = True
        return self._issue(family)

    def revoke(self, body):
        token = body.get("refresh_token") if isinstance(body, dict) else None
        session = self.sessions.get(token)
        if session is not None:
            self.revoked_families.add(session["family"])
        return 200, {}

    def catalog_live(self, authorization, if_none_match):
        """GET /v1/catalog/live -> (status, payload or None, headers)."""
        token = authorization[7:] if authorization and authorization.startswith("Bearer ") else None
        mode = self.catalog_mode
        family = self.access.get(token)
        if mode == "401" or family is None or family in self.revoked_families:
            return 401, {"error": "invalid_token"}, {}
        if mode == "403" or "catalog:full" not in self.entitlements:
            return 403, {"error": "insufficient_entitlement"}, {}
        if mode == "429":
            return 429, {"error": "slow_down"}, {"Retry-After": "120"}
        if mode == "500":
            return 500, {"error": "release_corrupt"}, {}
        if mode == "404" or not self.catalog_dir:
            return 404, {"error": "no_live_release"}, {}
        base = Path(self.catalog_dir)
        manifest = json.loads((base / "manifest.json").read_text(encoding="utf-8"))
        files = {name: (base / name).read_bytes().decode("utf-8") for name in BUNDLE_FILES}
        was_signed = manifest.get("signature") is not None
        if mode == "corrupt":
            files["models.json"] = files["models.json"].replace("{", "{ ", 1)
        if mode == "schema":
            manifest["schema"] = 99
        if mode == "malformed":
            files["benchmarks.json"] = "{}"
            manifest["files"]["benchmarks.json"] = hashlib.sha256(b"{}").hexdigest()
        if mode in ("schema", "malformed") and was_signed:
            signing.sign_manifest(manifest)  # a validly signed but unusable bundle
        if mode == "badtype":
            files["models.json"] = 10 ** 12
        # catalog contract section 6
        if mode == "unsigned":
            manifest["signature"] = None
            manifest["key_id"] = None
        if mode == "badsig":
            if not was_signed:
                signing.sign_manifest(manifest)
            raw = bytearray(signing.b64url_decode_any(manifest["signature"]))
            raw[40] ^= 0x01
            manifest["signature"] = signing.b64url(bytes(raw))
        if mode == "unknownkey":
            signing.sign_manifest(manifest, seed=signing.OTHER_SEED, key_id=signing.OTHER_KEY_ID)
        if mode == "tampered":
            if not was_signed:
                signing.sign_manifest(manifest)
            manifest["created_at"] = "2026-10-04T00:00:01Z"  # after signing
        etag = '"' + manifest["version"] + '"'
        if if_none_match == etag:
            return 304, None, {"ETag": etag}
        return 200, {"manifest": manifest, "files": files}, {
            "ETag": etag, "Cache-Control": "private, no-store"}

    def me(self, authorization):
        token = authorization[7:] if authorization and authorization.startswith("Bearer ") else None
        if self.me_outcome == "429":
            return _RATE_LIMITED
        if self.me_outcome == "400":
            return 400, {"error": "invalid_request"}
        family = self.access.get(token)
        if self.me_outcome != "ok" or family is None or family in self.revoked_families:
            return 401, {"error": "invalid_token"}
        return 200, {
            "user": dict(self.user),
            "entitlements": list(self.entitlements),
            "session": {"id": family, "created_at": FIXED_CREATED_AT},
        }


def _make_handler(state: FakeAuthState):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):  # keep stdout = port line only
            pass

        def _send(self, status, payload, headers=None):
            data = b"" if payload is None else json.dumps(payload).encode("utf-8")
            gz = bool(data) and "gzip" in (self.headers.get("Accept-Encoding") or "").lower()
            if gz:
                data = gzip.compress(data)
            self.send_response(status)
            if payload is not None:
                self.send_header("Content-Type", "application/json")
            if gz:
                self.send_header("Content-Encoding", "gzip")
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _body(self):
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            try:
                return json.loads(raw.decode("utf-8")) if raw else None
            except ValueError:
                return None

        def do_GET(self):
            with state.lock:
                headers = None
                if self.path == "/v1/catalog/live":
                    headers = {k: self.headers.get(k) for k in (
                        "Authorization", "If-None-Match", "Accept-Encoding", "User-Agent")}
                state.requests.append(("GET", self.path, headers))
                if self.path == "/healthz":
                    return self._send(200, {"ok": True})
                if self.path == "/v1/auth/me":
                    return self._send(*state.me(self.headers.get("Authorization")))
                if self.path == "/v1/catalog/live":
                    return self._send(*state.catalog_live(
                        self.headers.get("Authorization"),
                        self.headers.get("If-None-Match")))
                if self.path == "/_state":
                    return self._send(200, {
                        "requests": [[m, p] for m, p, _ in state.requests],
                        "revoked_families": sorted(state.revoked_families),
                    })
            self._send(404, {"error": "not_found"})

        def do_POST(self):
            body = self._body()
            routes = {
                "/v1/auth/device/code": state.device_code,
                "/v1/auth/token": state.token,
                "/v1/auth/revoke": state.revoke,
            }
            with state.lock:
                state.requests.append(("POST", self.path, body))
                if self.path == "/_catalog" and isinstance(body, dict):
                    if body.get("mode") in CATALOG_MODES:
                        state.catalog_mode = body["mode"]
                    if "dir" in body:
                        state.catalog_dir = body["dir"]
                    return self._send(200, {"mode": state.catalog_mode,
                                            "dir": state.catalog_dir})
                handler = routes.get(self.path)
                if handler is not None:
                    return self._send(*handler(body))
            self._send(404, {"error": "not_found"})

    return Handler


def start_server(host: str = "127.0.0.1", port: int = 0, **options):
    """Start in a daemon thread. Returns ``(server, state, base_url)``;
    call ``server.shutdown()`` when done."""
    state = FakeAuthState(**options)
    server = ThreadingHTTPServer((host, port), _make_handler(state))
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, state, f"http://{host}:{server.server_address[1]}"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--outcome", choices=OUTCOMES, default="approve",
                        help="device-grant result once --approve-after polls happened")
    parser.add_argument("--approve-after", type=int, default=2,
                        help="poll number that gets the outcome "
                             "(earlier polls: authorization_pending)")
    parser.add_argument("--interval", type=int, default=0,
                        help="polling interval handed out (contract value is 5)")
    parser.add_argument("--expires-in", type=int, default=600)
    parser.add_argument("--access-expires-in", type=int, default=3600,
                        help="0 forces a refresh on every whoami")
    parser.add_argument("--refresh-outcome", choices=REFRESH_OUTCOMES, default="ok")
    parser.add_argument("--me-outcome", choices=ME_OUTCOMES, default="ok")
    parser.add_argument("--rate-limit", choices=RATE_LIMIT_AT, default="none",
                        help="answer HTTP 429 on the device-code request or on every poll")
    parser.add_argument("--slow-down-first", action="store_true",
                        help="answer the first poll with slow_down (+5 s interval)")
    parser.add_argument("--email", default="dev@example.com")
    parser.add_argument("--name", default="Dev User")
    parser.add_argument("--entitlements", default="catalog:full",
                        help="comma-separated; empty for none")
    parser.add_argument("--catalog-dir", help="bundle directory GET /v1/catalog/live serves")
    parser.add_argument("--catalog-mode", choices=CATALOG_MODES, default="ok")
    parser.add_argument("--make-full-bundle", metavar="DIR",
                        help="write a small valid full bundle to DIR and exit")
    parser.add_argument("--bundle-version", default="2026.10.04.1")
    parser.add_argument("--unsigned", action="store_true",
                        help="with --make-full-bundle: leave the bundle unsigned")
    parser.add_argument("--write-trusted-keys", metavar="FILE",
                        help="write a trusted-keys file listing the test key and exit "
                             "(point TRYAII_CATALOG_TRUSTED_KEYS at it)")
    args = parser.parse_args(argv)
    if args.make_full_bundle:
        make_full_bundle(args.make_full_bundle, args.bundle_version, signed=not args.unsigned)
        return 0
    if args.write_trusted_keys:
        signing.write_trusted_keys(args.write_trusted_keys)
        return 0

    server, _state, _url = start_server(
        args.host, args.port,
        outcome=args.outcome, approve_after=args.approve_after,
        interval=args.interval, expires_in=args.expires_in,
        access_expires_in=args.access_expires_in,
        refresh_outcome=args.refresh_outcome, me_outcome=args.me_outcome,
        slow_down_first=args.slow_down_first, rate_limit_at=args.rate_limit,
        email=args.email, name=args.name,
        entitlements=[e for e in args.entitlements.split(",") if e],
        catalog_dir=args.catalog_dir, catalog_mode=args.catalog_mode,
    )
    print(server.server_address[1], flush=True)
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
