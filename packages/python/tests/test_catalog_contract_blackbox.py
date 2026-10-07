"""Black-box tests for the catalog-behind-login behaviour of the Python CLI.

Derived ONLY from docs/catalog/CONTRACT-catalog-v1.md (section 1: bundle
format / canonical text / hashes / user-facing counts, section 4: the
GET /v1/catalog/live wire format, section 5: cache layout, which catalog is
used, nudge, CLI changes) and docs/auth/CONTRACT-auth-v1.md (sections 4, 6, 7)
for the login mechanics and the credentials file.

Nothing here imports the implementation. The CLI runs as a subprocess
(`python -m tryaii.cli.main ...`) with a temp TRYAII_DRE_DATA_DIR, a temp
HOME/USERPROFILE, inherited TRYAII_* variables stripped, and TRYAII_API_URL /
the credentials' api_url pointing at a programmable fake server defined in
this file (or at a closed local port). The packaged starter bundle is read
only as DATA (its manifest gives the nudge counts, its models.json the starter
model ids, its benchmarks.json the benchmark names used by the synthetic full
bundle built here).
"""

from __future__ import annotations

import copy
import datetime as _dt
import gzip
import hashlib
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

# Catalog contract section 6: a full catalog is accepted only when signed by a
# trusted key. The synthetic bundles below are signed with a TEST key this
# suite controls (tests/_catalog_signing.py: an independent signer, not the
# implementation) and the CLI trusts exactly that key via
# TRYAII_CATALOG_TRUSTED_KEYS (see Sandbox.env).
from tests._catalog_signing import sign_manifest, write_trusted_keys

PKG_DIR = Path(__file__).resolve().parents[1]
STARTER_DIR = PKG_DIR / "tryaii" / "catalog" / "data" / "starter"
REAL_HOME = Path(os.path.expanduser("~"))

BUNDLE_FILES = (
    "models.json",
    "benchmarks.json",
    "normalization_ranges.json",
    "centroids.json",
    "training_queries.json",
)
ALL_BUNDLE_FILES = ("manifest.json",) + BUNDLE_FILES

EMAIL = "bb.tester@example.test"
NAME = "Bee Tester"

# --- exact user-facing lines (catalog contract section 5) -------------------
SESSION_ENDED = "Your session has ended. Run: tryaii login"
NO_ACCESS = "This account does not have access to the full catalog; using the starter catalog."
DOWNLOAD_FAILED = "Could not download the full catalog; using the starter catalog for now."
SCHEMA_TOO_NEW = "The full catalog needs a newer tryaii version; using the starter catalog."
LOGIN_DOWNLOAD_FAILED = "Could not download the full catalog now; it will be fetched on next use."
GLOBAL_HELP_LOGIN_LINE = (
    "  login                 Sign in with your tryaii.com account (free; unlocks the full model catalog)"
)
HELP_LOGIN_CRED_AND_CATALOG = (
    "Credentials are stored in ~/.tryaii/credentials.json (or under\n"
    "TRYAII_DRE_DATA_DIR).\n"
    "\n"
    "After signing in, the full model catalog is downloaded and kept up to date\n"
    "automatically (checked at most once a day).\n"
)


# ---------------------------------------------------------------------------
# Starter data (read as data only)
# ---------------------------------------------------------------------------


def _load(name: str):
    return json.loads((STARTER_DIR / name).read_text(encoding="utf-8"))


STARTER_MANIFEST = _load("manifest.json")
STARTER_MODEL_IDS = {m["model_id"] for m in _load("models.json")["models"]}
NUDGE = (
    f"Routing on the starter catalog ({STARTER_MANIFEST['counts']['models']} models). "
    f"Log in for free to use the full catalog ({STARTER_MANIFEST['full_counts']['models']} models): "
    "tryaii login"
)


# ---------------------------------------------------------------------------
# Synthetic full bundle (section 1 + Appendix A)
# ---------------------------------------------------------------------------


def canon(obj) -> str:
    """Canonical file text (section 1)."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def make_bundle(version: str, *, extra_models: int = 0, schema: int = 1) -> dict:
    """Small valid full bundle: 3 starter benchmarks, 5 routable models + 2 `:free`.

    Returns {"manifest", "files" (name -> canonical text), "routable" (ids), "all" (ids)}.
    """
    sb = _load("benchmarks.json")
    sr = _load("normalization_ranges.json")
    sc = _load("centroids.json")
    st = _load("training_queries.json")
    by_name = {b["name"]: b for b in sb["benchmarks"]}
    wanted = [n for n in ("GPQA", "MMLU-Pro", "LiveCodeBench") if n in by_name]
    if len(wanted) < 3:
        wanted = [b["name"] for b in sb["benchmarks"][:3]]
    benches = [copy.deepcopy(by_name[n]) for n in wanted]
    fam_ids = {b["family"] for b in benches}
    benchmarks = {
        "families": [f for f in sb["families"] if f["id"] in fam_ids],
        "benchmarks": benches,
    }
    ranges = {
        "benchmarks": {n: sr["benchmarks"][n] for n in wanted},
        "generated_from": "black-box synthetic full bundle",
        "version": 1,
    }
    centroids = {
        "centroids": {n: sc["centroids"][n] for n in wanted},
        "metadata": {
            "benchmark_count": len(wanted),
            "benchmark_fingerprint": "|".join(sorted(wanted)),
            "dimension": len(sc["centroids"][wanted[0]]),
            "model": "all-MiniLM-L6-v2",
        },
    }
    queries = {
        "benchmarks": {
            n: {
                "description": st["benchmarks"][n].get("description", n),
                "queries": list(st["benchmarks"][n]["queries"][:3]),
            }
            for n in wanted
        },
        "description": "black-box synthetic training queries",
        "version": version,
    }

    names = ["alpha-1", "beta-2", "gamma-3", "delta-4", "epsilon-5"]
    names += [f"extra-{i}" for i in range(extra_models)]
    models = []
    for i, nm in enumerate(names):
        scores = {}
        for n in wanted:
            lo, hi = sr["benchmarks"][n]["lo"], sr["benchmarks"][n]["hi"]
            scores[n] = round(lo + (hi - lo) * (0.3 + 0.1 * (i % 6)), 3)
        models.append(
            {
                "benchmark_scores": scores,
                "capabilities": [],
                # non-ASCII on purpose: canonical text is ensure_ascii=False, hashed as UTF-8
                "description": f"Synthetic black-box model {nm} — café über",
                "latency": "fast",
                "model_id": f"bbtest/{nm}",
                "pricing": {"input_per_1k": 0.001 * (i + 1), "output_per_1k": 0.004 * (i + 1)},
                "provider": "bbtest",
                "tokens_per_second": 50.0 + 10 * i,
                "ttft_ms": 500 + 50 * i,
            }
        )
    for base in models[:2]:
        free = copy.deepcopy(base)
        free["model_id"] = base["model_id"] + ":free"
        free["pricing"] = {"input_per_1k": 0.0, "output_per_1k": 0.0}
        models.append(free)
    models_doc = {
        "generated_from": "black-box synthetic full bundle",
        "models": models,
        "updated": "2026-10",
        "version": version,
    }

    files = {
        "models.json": canon(models_doc),
        "benchmarks.json": canon(benchmarks),
        "normalization_ranges.json": canon(ranges),
        "centroids.json": canon(centroids),
        "training_queries.json": canon(queries),
    }
    manifest = {
        "schema": schema,
        "kind": "full",
        "version": version,
        "embedding_model": "all-MiniLM-L6-v2",
        "created_at": "2026-10-03T12:00:00Z",
        "counts": {"models": len(models), "benchmarks": len(wanted)},
        "files": {n: sha(t) for n, t in files.items()},
        "signature": None,
        "key_id": None,
    }
    sign_manifest(manifest)  # catalog contract section 6
    all_ids = [m["model_id"] for m in models]
    return {
        "manifest": manifest,
        "files": files,
        "all": all_ids,
        "routable": [i for i in all_ids if not i.endswith(":free")],
    }


TRUSTED_KEYS_FILE = write_trusted_keys(
    Path(tempfile.mkdtemp(prefix="bb-trusted-keys-")) / "trusted_keys.json")

V1 = "2026.10.03.1"
V2 = "2026.10.04.1"
BUNDLE_V1 = make_bundle(V1)
BUNDLE_V2 = make_bundle(V2, extra_models=1)


def corrupted(bundle: dict) -> dict:
    """Same manifest, but models.json no longer matches its sha256."""
    b = copy.deepcopy(bundle)
    b["files"]["models.json"] = b["files"]["models.json"].replace('"latency":"fast"', '"latency":"slow"', 1)
    assert sha(b["files"]["models.json"]) != b["manifest"]["files"]["models.json"]
    return b


def wire(bundle: dict) -> dict:
    return {"manifest": bundle["manifest"], "files": bundle["files"]}


# ---------------------------------------------------------------------------
# Programmable fake API (auth endpoints + GET /v1/catalog/live)
# ---------------------------------------------------------------------------


class FakeServer:
    """Stdlib threaded HTTP server. Routes map (method, path) to a list of
    responses; each response is (status, body[, headers]) or a callable
    taking the request record and returning such a tuple. The last response
    repeats. body: dict/list -> JSON; bytes -> raw; None -> empty body.
    Unrouted requests get 404 {"error": "not_found"}. Responses are gzipped
    when the client sends Accept-Encoding: gzip (section 4)."""

    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.routes: dict[tuple[str, str], list] = {}
        self.lock = threading.Lock()
        srv = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def _do(self):
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n) if n else b""
                try:
                    js = json.loads(raw.decode("utf-8")) if raw else None
                except ValueError:
                    js = None
                rec = {
                    "method": self.command,
                    "path": self.path,
                    "headers": {k.lower(): v for k, v in self.headers.items()},
                    "json": js,
                }
                with srv.lock:
                    srv.requests.append(rec)
                    q = srv.routes.get((self.command, self.path.split("?")[0]))
                    resp = (q.pop(0) if len(q) > 1 else q[0]) if q else (404, {"error": "not_found"})
                if callable(resp):
                    resp = resp(rec)
                status, body, *rest = resp
                headers = dict(rest[0]) if rest else {}
                if body is None:
                    payload = b""
                elif isinstance(body, (bytes, bytearray)):
                    payload = bytes(body)
                    headers.setdefault("Content-Type", "text/plain")
                else:
                    payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
                    headers.setdefault("Content-Type", "application/json")
                if payload and "gzip" in rec["headers"].get("accept-encoding", "").lower():
                    payload = gzip.compress(payload)
                    headers["Content-Encoding"] = "gzip"
                self.send_response(status)
                for k, v in headers.items():
                    self.send_header(k, v)
                if status != 304:
                    self.send_header("Content-Length", str(len(payload)))
                self.send_header("Connection", "close")
                self.end_headers()
                if status != 304 and payload:
                    self.wfile.write(payload)
                self.close_connection = True

            do_GET = _do  # noqa: N815 (BaseHTTPRequestHandler API)
            do_POST = _do  # noqa: N815 (BaseHTTPRequestHandler API)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.httpd.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def on(self, method: str, path: str, *responses) -> None:
        with self.lock:
            self.routes[(method, path)] = list(responses)

    def hits(self, method: str | None = None, path: str | None = None) -> list[dict]:
        with self.lock:
            return [
                r
                for r in self.requests
                if (method is None or r["method"] == method)
                and (path is None or r["path"].split("?")[0] == path)
            ]

    def catalog_hits(self) -> list[dict]:
        return self.hits("GET", "/v1/catalog/live")

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


CATALOG = "/v1/catalog/live"


def serve_live(bundle: dict):
    """A faithful live endpoint: 304 on a matching If-None-Match, else 200."""
    version = bundle["manifest"]["version"]

    def handler(rec):
        if rec["headers"].get("if-none-match") == f'"{version}"':
            return (304, None, {"ETag": f'"{version}"', "Cache-Control": "private, no-store"})
        return (200, wire(bundle), {"ETag": f'"{version}"', "Cache-Control": "private, no-store"})

    return handler


def serve_200(bundle: dict):
    version = bundle["manifest"]["version"]
    return (200, wire(bundle), {"ETag": f'"{version}"', "Cache-Control": "private, no-store"})


def closed_port_url() -> str:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return f"http://127.0.0.1:{port}"


def iso(dt: _dt.datetime) -> str:
    return dt.astimezone(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(s: str) -> _dt.datetime:
    return _dt.datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=_dt.timezone.utc)


def utcnow() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def token_response(access: str = "at_fresh_access", refresh: str = "tair_fresh_refresh") -> dict:
    return {
        "access_token": access,
        "token_type": "Bearer",
        "expires_in": 3600,
        "refresh_token": refresh,
        "refresh_expires_in": 7776000,
        "user": {"id": "u-bb-1", "email": EMAIL, "name": NAME},
        "entitlements": ["catalog:full"],
    }


ME = {
    "user": {"id": "u-bb-1", "email": EMAIL, "name": NAME},
    "entitlements": ["catalog:full"],
    "session": {"id": "fam0123", "created_at": "2026-10-03T12:00:00Z"},
}


# ---------------------------------------------------------------------------
# Sandbox: temp data dir + home, subprocess runner, seeding helpers
# ---------------------------------------------------------------------------


class Sandbox:
    def __init__(self, root: Path, api_url: str):
        self.root = root
        self.data = root / "data"
        self.home = root / "home"
        self.data.mkdir()
        self.home.mkdir()
        self.api_url = api_url
        self.extra_env: dict[str, str] = {}

    # paths (section 5 "Cache", auth section 6)
    @property
    def creds(self) -> Path:
        return self.data / "credentials.json"

    @property
    def catalog_dir(self) -> Path:
        return self.data / "catalog"

    @property
    def full_dir(self) -> Path:
        return self.catalog_dir / "full"

    @property
    def state_file(self) -> Path:
        return self.catalog_dir / "state.json"

    @property
    def nudge_file(self) -> Path:
        return self.catalog_dir / "nudge.json"

    def env(self, *, no_banner: bool = False, embedding: bool = False) -> dict:
        env = {
            k: v
            for k, v in os.environ.items()
            if not k.upper().startswith("TRYAII_")
            and k.upper() not in {"HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY"}
        }
        env["TRYAII_DRE_DATA_DIR"] = str(self.data)
        env["TRYAII_API_URL"] = self.api_url
        env["TRYAII_NO_DAEMON"] = "1"
        # catalog contract section 6: trust the suite's test signing key only
        env["TRYAII_CATALOG_TRUSTED_KEYS"] = str(TRUSTED_KEYS_FILE)
        env["HOME"] = str(self.home)
        env["USERPROFILE"] = str(self.home)
        env["NO_PROXY"] = "127.0.0.1,localhost"
        env["no_proxy"] = "127.0.0.1,localhost"
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        if no_banner:
            env["TRYAII_NO_BANNER"] = "1"
        if embedding:
            # The temp HOME hides the real Hugging Face cache; point at it
            # explicitly and forbid downloads (never contact a real server).
            env.setdefault("HF_HOME", str(REAL_HOME / ".cache" / "huggingface"))
            env["HF_HUB_OFFLINE"] = "1"
            env["TRANSFORMERS_OFFLINE"] = "1"
        env.update(self.extra_env)
        return env

    def run(self, *args: str, no_banner: bool = False, embedding: bool = False, timeout: float = 180):
        p = subprocess.run(
            [sys.executable, "-m", "tryaii.cli.main", *args],
            cwd=str(PKG_DIR),
            env=self.env(no_banner=no_banner, embedding=embedding),
            capture_output=True,
            timeout=timeout,
        )
        out = p.stdout.decode("utf-8", "replace").replace("\r\n", "\n")
        err = p.stderr.decode("utf-8", "replace").replace("\r\n", "\n")
        return p.returncode, out, err

    def models(self, **kw) -> tuple[int, list[str], str]:
        rc, out, err = self.run("models", "--json", **kw)
        ids: list[str] = []
        if rc == 0:
            data = json.loads(out)
            if isinstance(data, dict):
                data = data.get("models", [])
            ids = [m.get("model_id") or m.get("id") for m in data]
        return rc, ids, err

    def seed_creds(self, *, access_in: float = 3600, api_url: str | None = None) -> dict:
        t = utcnow()
        doc = {
            "version": 1,
            "api_url": api_url or self.api_url,
            "user": {"id": "u-bb-1", "email": EMAIL, "name": NAME},
            "entitlements": ["catalog:full"],
            "refresh_token": "tair_seeded_refresh",
            "refresh_expires_at": iso(t + _dt.timedelta(days=80)),
            "access_token": "at_seeded_access",
            "access_expires_at": iso(t + _dt.timedelta(seconds=access_in)),
            "created_at": iso(t - _dt.timedelta(days=1)),
        }
        self.creds.write_text(json.dumps(doc, indent=2), encoding="utf-8")
        return doc

    def seed_cache(self, bundle: dict, *, checked_ago: _dt.timedelta | None = _dt.timedelta(hours=25)) -> Path:
        """Write a full cache exactly per section 5 'Cache'."""
        v = bundle["manifest"]["version"]
        d = self.full_dir / v
        d.mkdir(parents=True, exist_ok=True)
        (d / "manifest.json").write_text(json.dumps(bundle["manifest"]), encoding="utf-8")
        for name, text in bundle["files"].items():
            (d / name).write_bytes(text.encode("utf-8"))
        if checked_ago is not None:
            self.write_state(v, utcnow() - checked_ago)
        return d

    def write_state(self, version, checked_at: _dt.datetime) -> None:
        self.catalog_dir.mkdir(parents=True, exist_ok=True)
        self.state_file.write_text(
            json.dumps({"version": version, "checked_at": iso(checked_at)}), encoding="utf-8"
        )

    def read_state(self) -> dict:
        return json.loads(self.state_file.read_text(encoding="utf-8"))

    def snapshot_full(self) -> dict:
        if not self.full_dir.exists():
            return {}
        return {
            str(p.relative_to(self.full_dir)).replace("\\", "/"): p.read_bytes()
            for p in sorted(self.full_dir.rglob("*"))
            if p.is_file()
        }


@pytest.fixture
def server():
    s = FakeServer()
    yield s
    s.stop()


@pytest.fixture
def sb(tmp_path, server):
    return Sandbox(tmp_path, server.url)


def stderr_lines(err: str) -> list[str]:
    return err.split("\n")


def assert_full(ids: list[str], bundle: dict) -> None:
    assert set(bundle["routable"]) <= set(ids), f"full models missing from `models`: {ids}"
    assert not (set(ids) & STARTER_MODEL_IDS), f"starter models listed on the full catalog: {ids}"


def assert_starter(ids: list[str]) -> None:
    assert set(ids) == STARTER_MODEL_IDS, f"`models` did not list the starter catalog: {ids}"


def run_login(sb: Sandbox, server: FakeServer):
    server.on(
        "POST",
        "/v1/auth/device/code",
        (
            200,
            {
                "device_code": "dc_" + "B" * 40,
                "user_code": "BCDF-GHJK",
                "verification_uri": "https://web.example.test/device",
                "verification_uri_complete": "https://web.example.test/device?user_code=BCDF-GHJK",
                "expires_in": 600,
                "interval": 1,
            },
        ),
    )
    server.on("POST", "/v1/auth/token", (200, token_response()))
    server.on("GET", "/v1/auth/me", (200, ME))
    server.on("POST", "/v1/auth/revoke", (200, {}))
    return sb.run("login")


# ---------------------------------------------------------------------------
# Help text (section 5 "CLI changes")
# ---------------------------------------------------------------------------


def test_global_help_login_line(sb):
    rc, out, _ = sb.run("--help")
    assert rc == 0
    assert GLOBAL_HELP_LOGIN_LINE in out.split("\n")


def test_help_login_has_catalog_paragraph_after_credentials(sb):
    rc, out, _ = sb.run("login", "--help")
    assert rc == 0
    assert HELP_LOGIN_CRED_AND_CATALOG in out


# ---------------------------------------------------------------------------
# Logged out -> starter, nudge, no network (section 5 rule 1 + "Nudge")
# ---------------------------------------------------------------------------


def test_logged_out_models_uses_starter_prints_nudge_no_network(sb, server):
    rc, ids, err = sb.models()
    assert rc == 0
    assert_starter(ids)
    assert stderr_lines(err).count(NUDGE) == 1
    assert server.requests == []
    # nudge.json records today's local date
    assert json.loads(sb.nudge_file.read_text(encoding="utf-8")) == {
        "last_shown": _dt.date.today().isoformat()
    }


def test_nudge_counts_come_from_starter_manifest():
    # counts.models and full_counts.models of the packaged starter manifest
    assert STARTER_MANIFEST["kind"] == "starter"
    assert isinstance(STARTER_MANIFEST["counts"]["models"], int)
    assert isinstance(STARTER_MANIFEST["full_counts"]["models"], int)


def test_nudge_at_most_once_per_local_day(sb, server):
    rc, _, err1 = sb.run("models")
    assert rc == 0 and stderr_lines(err1).count(NUDGE) == 1
    rc, _, err2 = sb.run("models")
    assert rc == 0
    assert NUDGE not in err2
    assert server.requests == []


def test_nudge_shown_again_on_a_new_local_day(sb):
    sb.catalog_dir.mkdir(parents=True)
    yesterday = (_dt.date.today() - _dt.timedelta(days=1)).isoformat()
    sb.nudge_file.write_text(json.dumps({"last_shown": yesterday}), encoding="utf-8")
    rc, _, err = sb.run("models")
    assert rc == 0
    assert stderr_lines(err).count(NUDGE) == 1
    assert json.loads(sb.nudge_file.read_text(encoding="utf-8"))["last_shown"] == _dt.date.today().isoformat()


def test_nudge_not_shown_when_already_shown_today(sb):
    sb.catalog_dir.mkdir(parents=True)
    sb.nudge_file.write_text(json.dumps({"last_shown": _dt.date.today().isoformat()}), encoding="utf-8")
    rc, _, err = sb.run("models")
    assert rc == 0
    assert NUDGE not in err


def test_no_banner_env_suppresses_nudge(sb, server):
    rc, ids, err = sb.models(no_banner=True)
    assert rc == 0
    assert_starter(ids)
    assert NUDGE not in err
    assert server.requests == []


def test_logged_out_ignores_cached_full_catalog(sb, server):
    # Rule 1: no credentials file -> starter, even with a leftover full cache.
    sb.seed_cache(BUNDLE_V1)
    server.on("GET", CATALOG, serve_live(BUNDLE_V1))
    rc, ids, err = sb.models()
    assert rc == 0
    assert_starter(ids)
    assert stderr_lines(err).count(NUDGE) == 1
    assert server.requests == []


def _skip_if_no_embedding(rc: int, out: str, err: str) -> None:
    if rc != 0:
        text = (out + err).lower()
        if any(k in text for k in ("embedding", "sentence", "huggingface", "offline", "setup")):
            pytest.skip(f"embedding model unavailable offline: {err.strip()[-300:]}")


def test_route_on_starter_prints_nudge(sb, server):
    rc, out, err = sb.run("route", "Write a haiku about autumn", "--top-k", "1", embedding=True)
    _skip_if_no_embedding(rc, out, err)
    assert rc == 0, err
    assert stderr_lines(err).count(NUDGE) == 1
    assert server.requests == []


def test_eval_on_starter_prints_nudge(sb, server, tmp_path):
    inp = tmp_path / "in.json"
    inp.write_text(json.dumps([{"prompt": "Summarize this paragraph."}]), encoding="utf-8")
    rc, out, err = sb.run("eval", str(inp), "-o", str(tmp_path / "out"), embedding=True)
    _skip_if_no_embedding(rc, out, err)
    assert rc == 0, err
    assert stderr_lines(err).count(NUDGE) == 1
    assert server.requests == []


# ---------------------------------------------------------------------------
# login downloads the full catalog (section 5 "CLI changes" + "Cache")
# ---------------------------------------------------------------------------


def test_login_downloads_full_catalog_and_writes_cache(sb, server):
    server.on("GET", CATALOG, serve_live(BUNDLE_V1))
    before = utcnow() - _dt.timedelta(seconds=2)
    rc, out, err = run_login(sb, server)
    after = utcnow() + _dt.timedelta(seconds=2)
    assert rc == 0, err
    n = len(BUNDLE_V1["routable"])
    assert n == 5 and len(BUNDLE_V1["all"]) == 7  # :free entries are not counted
    assert out.endswith(f"Logged in as {EMAIL}.\nDownloaded the full catalog ({n} models).\n"), out

    hits = server.catalog_hits()
    assert len(hits) == 1
    assert hits[0]["headers"].get("authorization") == "Bearer at_fresh_access"

    # cache layout: catalog/full/<version>/ with exactly the six bundle files
    assert sorted(p.name for p in sb.full_dir.iterdir()) == [V1]
    vdir = sb.full_dir / V1
    assert sorted(p.name for p in vdir.iterdir()) == sorted(ALL_BUNDLE_FILES)
    for name in BUNDLE_FILES:
        assert (vdir / name).read_bytes() == BUNDLE_V1["files"][name].encode("utf-8"), name
        assert hashlib.sha256((vdir / name).read_bytes()).hexdigest() == BUNDLE_V1["manifest"]["files"][name]
    assert json.loads((vdir / "manifest.json").read_text(encoding="utf-8")) == BUNDLE_V1["manifest"]

    state = sb.read_state()
    assert set(state) == {"version", "checked_at"}
    assert state["version"] == V1
    checked = parse_iso(state["checked_at"])
    assert before <= checked <= after


@pytest.mark.parametrize(
    "response",
    [
        (500, {"error": "release_corrupt"}),
        (404, {"error": "no_live_release"}),
        (403, {"error": "insufficient_entitlement"}),
        (429, {"error": "slow_down"}, {"Retry-After": "120"}),
        "corrupt",
        "schema2",
    ],
    ids=["500", "404", "403", "429", "hash-mismatch", "schema-2"],
)
def test_login_catalog_failure_prints_failure_line_exit_0(sb, server, response):
    if response == "corrupt":
        response = serve_200(corrupted(BUNDLE_V1))
    elif response == "schema2":
        response = serve_200(make_bundle(V1, schema=2))
    server.on("GET", CATALOG, response)
    rc, out, err = run_login(sb, server)
    assert rc == 0, err
    assert out.endswith(f"Logged in as {EMAIL}.\n{LOGIN_DOWNLOAD_FAILED}\n"), out
    assert sb.creds.exists()
    assert not any(sb.full_dir.glob("*/manifest.json")) if sb.full_dir.exists() else True


def test_second_run_after_login_makes_no_catalog_request(sb, server):
    server.on("GET", CATALOG, serve_live(BUNDLE_V1))
    rc, _, err = run_login(sb, server)
    assert rc == 0, err
    assert len(server.catalog_hits()) == 1
    rc, ids, err = sb.models()
    assert rc == 0, err
    assert_full(ids, BUNDLE_V1)
    assert len(server.catalog_hits()) == 1, "checked again within 24 h"
    assert NUDGE not in err


# ---------------------------------------------------------------------------
# whoami third line (section 5 "CLI changes")
# ---------------------------------------------------------------------------


def test_whoami_with_full_cache(sb, server):
    sb.seed_creds()
    sb.seed_cache(BUNDLE_V1, checked_ago=_dt.timedelta(hours=1))
    server.on("GET", "/v1/auth/me", (200, ME))
    server.on("GET", CATALOG, serve_live(BUNDLE_V1))
    rc, out, err = sb.run("whoami")
    assert rc == 0, err
    assert out == (
        f"Logged in as {EMAIL} ({NAME})\n"
        "Entitlements: catalog:full\n"
        f"Catalog: full, release {V1} (5 models)\n"
    )


def test_whoami_without_full_cache(sb, server):
    sb.seed_creds()
    server.on("GET", "/v1/auth/me", (200, ME))
    server.on("GET", CATALOG, (404, {"error": "no_live_release"}))
    rc, out, err = sb.run("whoami")
    assert rc == 0, err
    assert out == (
        f"Logged in as {EMAIL} ({NAME})\n"
        "Entitlements: catalog:full\n"
        "Catalog: not downloaded yet\n"
    )


def test_whoami_json_unchanged(sb, server):
    sb.seed_creds()
    sb.seed_cache(BUNDLE_V1, checked_ago=_dt.timedelta(hours=1))
    server.on("GET", "/v1/auth/me", (200, ME))
    rc, out, err = sb.run("whoami", "--json")
    assert rc == 0, err
    assert out == json.dumps(ME, indent=2) + "\n"


# ---------------------------------------------------------------------------
# Which catalog when logged in (section 5 rule 2)
# ---------------------------------------------------------------------------


def test_models_lists_full_catalog_when_active(sb, server):
    sb.seed_creds()
    sb.seed_cache(BUNDLE_V1, checked_ago=_dt.timedelta(hours=1))
    rc, ids, err = sb.models()
    assert rc == 0, err
    assert_full(ids, BUNDLE_V1)
    assert server.requests == []  # fresh check -> no network at all
    assert NUDGE not in err


def test_check_within_24h_makes_no_request(sb, server):
    sb.seed_creds()
    sb.seed_cache(BUNDLE_V1, checked_ago=_dt.timedelta(hours=23))
    server.on("GET", CATALOG, serve_live(BUNDLE_V2))
    rc, ids, err = sb.models()
    assert rc == 0, err
    assert server.catalog_hits() == []
    assert_full(ids, BUNDLE_V1)


def test_first_use_without_state_downloads(sb, server):
    # credentials present, no state.json -> check now, 200 -> write + use
    sb.seed_creds()
    server.on("GET", CATALOG, serve_live(BUNDLE_V1))
    rc, ids, err = sb.models()
    assert rc == 0, err
    assert len(server.catalog_hits()) == 1
    assert server.catalog_hits()[0]["headers"].get("authorization") == "Bearer at_seeded_access"
    assert_full(ids, BUNDLE_V1)
    assert sb.read_state()["version"] == V1
    assert NUDGE not in err and DOWNLOAD_FAILED not in err


def test_after_24h_sends_if_none_match_and_304_keeps_cache(sb, server):
    sb.seed_creds()
    sb.seed_cache(BUNDLE_V1, checked_ago=_dt.timedelta(hours=25))
    before_files = sb.snapshot_full()
    old_checked = parse_iso(sb.read_state()["checked_at"])
    server.on("GET", CATALOG, serve_live(BUNDLE_V1))
    rc, ids, err = sb.models()
    assert rc == 0, err
    hits = server.catalog_hits()
    assert len(hits) == 1
    assert hits[0]["headers"].get("if-none-match") == f'"{V1}"'
    assert_full(ids, BUNDLE_V1)
    assert sb.snapshot_full() == before_files
    state = sb.read_state()
    assert state["version"] == V1
    assert parse_iso(state["checked_at"]) > old_checked + _dt.timedelta(hours=24)
    assert DOWNLOAD_FAILED not in err


def test_after_24h_new_version_replaces_cache_keeps_only_newest(sb, server):
    sb.seed_creds()
    sb.seed_cache(BUNDLE_V1, checked_ago=_dt.timedelta(hours=25))
    server.on("GET", CATALOG, serve_live(BUNDLE_V2))
    rc, ids, err = sb.models()
    assert rc == 0, err
    assert server.catalog_hits()[0]["headers"].get("if-none-match") == f'"{V1}"'
    assert_full(ids, BUNDLE_V2)
    assert "bbtest/extra-0" in ids
    assert sorted(p.name for p in sb.full_dir.iterdir()) == [V2]
    for name in BUNDLE_FILES:
        assert (sb.full_dir / V2 / name).read_bytes() == BUNDLE_V2["files"][name].encode("utf-8")
    assert sb.read_state()["version"] == V2


def test_expired_access_token_is_refreshed_before_catalog_request(sb, server):
    sb.seed_creds(access_in=-60)
    server.on("POST", "/v1/auth/token", (200, token_response(access="at_refreshed")))
    server.on("GET", CATALOG, serve_live(BUNDLE_V1))
    rc, ids, err = sb.models()
    assert rc == 0, err
    tok = server.hits("POST", "/v1/auth/token")
    assert len(tok) == 1 and tok[0]["json"]["grant_type"] == "refresh_token"
    assert tok[0]["json"]["refresh_token"] == "tair_seeded_refresh"
    assert server.catalog_hits()[0]["headers"].get("authorization") == "Bearer at_refreshed"
    assert_full(ids, BUNDLE_V1)


def test_corrupted_hash_rejected_with_cache(sb, server):
    sb.seed_creds()
    sb.seed_cache(BUNDLE_V1, checked_ago=_dt.timedelta(hours=25))
    before = sb.snapshot_full()
    server.on("GET", CATALOG, serve_200(corrupted(BUNDLE_V2)))
    rc, ids, err = sb.models()
    assert rc == 0, err
    assert len(server.catalog_hits()) == 1
    assert sb.snapshot_full() == before, "cache changed after a hash mismatch"
    assert_full(ids, BUNDLE_V1)
    # cache exists -> it is used; the "starter for now" line would be false
    assert DOWNLOAD_FAILED not in err


def test_corrupted_hash_rejected_without_cache(sb, server):
    sb.seed_creds()
    server.on("GET", CATALOG, serve_200(corrupted(BUNDLE_V1)))
    rc, ids, err = sb.models()
    assert rc == 0, err
    assert len(server.catalog_hits()) == 1
    assert not any(sb.full_dir.rglob("*")) if sb.full_dir.exists() else True
    assert_starter(ids)
    assert DOWNLOAD_FAILED in stderr_lines(err)
    assert NUDGE not in err  # nudge is only for the logged-out case


def test_schema_too_new_without_cache(sb, server):
    sb.seed_creds()
    server.on("GET", CATALOG, serve_200(make_bundle(V2, schema=2)))
    rc, ids, err = sb.models()
    assert rc == 0, err
    assert_starter(ids)
    assert SCHEMA_TOO_NEW in stderr_lines(err)
    assert DOWNLOAD_FAILED not in err
    assert not any(sb.full_dir.rglob("*")) if sb.full_dir.exists() else True


def test_schema_too_new_with_cache_uses_cache(sb, server):
    sb.seed_creds()
    sb.seed_cache(BUNDLE_V1, checked_ago=_dt.timedelta(hours=25))
    before = sb.snapshot_full()
    server.on("GET", CATALOG, serve_200(make_bundle(V2, schema=2)))
    rc, ids, err = sb.models()
    assert rc == 0, err
    assert_full(ids, BUNDLE_V1)
    assert sb.snapshot_full() == before
    assert SCHEMA_TOO_NEW not in err and DOWNLOAD_FAILED not in err


def test_403_without_cache_uses_starter_with_no_access_line(sb, server):
    sb.seed_creds()
    server.on("GET", CATALOG, (403, {"error": "insufficient_entitlement"}))
    rc, ids, err = sb.models()
    assert rc == 0, err
    assert_starter(ids)
    assert NO_ACCESS in stderr_lines(err)
    assert NUDGE not in err and DOWNLOAD_FAILED not in err
    assert sb.creds.exists()


def test_403_with_cache_still_uses_starter(sb, server):
    # Rule 2: "403 insufficient_entitlement -> starter" (no cache clause).
    sb.seed_creds()
    sb.seed_cache(BUNDLE_V1, checked_ago=_dt.timedelta(hours=25))
    server.on("GET", CATALOG, (403, {"error": "insufficient_entitlement"}))
    rc, ids, err = sb.models()
    assert rc == 0, err
    assert_starter(ids)
    assert NO_ACCESS in stderr_lines(err)


FAILURES = {
    "404": (404, {"error": "no_live_release"}),
    "429": (429, {"error": "slow_down"}, {"Retry-After": "120"}),
    "500": (500, {"error": "release_corrupt"}),
    "503": (503, {"error": "temporarily_unavailable"}),
    "unreachable": None,
}


@pytest.mark.parametrize("kind", list(FAILURES))
def test_failure_without_cache_starter_and_download_failed_line(sb, server, kind):
    if kind == "unreachable":
        sb.seed_creds(api_url=closed_port_url())
    else:
        sb.seed_creds()
        server.on("GET", CATALOG, FAILURES[kind])
    rc, ids, err = sb.models()
    assert rc == 0, err
    if kind != "unreachable":
        assert len(server.catalog_hits()) == 1
    assert_starter(ids)
    assert DOWNLOAD_FAILED in stderr_lines(err)
    assert NUDGE not in err
    assert sb.creds.exists()


@pytest.mark.parametrize("kind", list(FAILURES))
def test_failure_with_cache_uses_cache(sb, server, kind):
    if kind == "unreachable":
        sb.seed_creds(api_url=closed_port_url())
    else:
        sb.seed_creds()
        server.on("GET", CATALOG, FAILURES[kind])
    sb.seed_cache(BUNDLE_V1, checked_ago=_dt.timedelta(hours=25))
    before = sb.snapshot_full()
    rc, ids, err = sb.models()
    assert rc == 0, err
    if kind != "unreachable":
        assert len(server.catalog_hits()) == 1
    assert_full(ids, BUNDLE_V1)
    assert sb.snapshot_full() == before
    assert DOWNLOAD_FAILED not in err
    assert sb.creds.exists()


def test_slow_catalog_endpoint_times_out_and_falls_back(sb, server):
    # "The check never blocks longer than one request (10 s timeout)".
    import time

    def slow(rec):
        time.sleep(14)
        return serve_200(BUNDLE_V1)

    sb.seed_creds()
    server.on("GET", CATALOG, slow)
    t0 = time.monotonic()
    rc, ids, err = sb.models()
    elapsed = time.monotonic() - t0
    assert rc == 0, err
    assert elapsed < 13.5, f"check blocked {elapsed:.1f}s (> 10 s timeout + startup)"
    assert_starter(ids)
    assert DOWNLOAD_FAILED in stderr_lines(err)


# ---------------------------------------------------------------------------
# Session ended (section 5 rule 2)
# ---------------------------------------------------------------------------


def _assert_session_ended(sb: Sandbox, rc: int, out: str, err: str) -> None:
    assert rc == 1, (rc, out, err)
    assert SESSION_ENDED in stderr_lines(err), err
    assert "bbtest/" not in out and not any(m in out for m in STARTER_MODEL_IDS), "silent fallback"
    assert not sb.creds.exists(), "credentials file not deleted"
    assert not (sb.full_dir.exists() and any(sb.full_dir.rglob("*"))), "full cache not deleted"


def test_refresh_invalid_grant_ends_session(sb, server):
    sb.seed_creds(access_in=-60)
    sb.seed_cache(BUNDLE_V1, checked_ago=_dt.timedelta(hours=25))
    server.on("POST", "/v1/auth/token", (400, {"error": "invalid_grant", "error_description": "revoked"}))
    server.on("GET", CATALOG, serve_live(BUNDLE_V1))
    rc, out, err = sb.run("models")
    _assert_session_ended(sb, rc, out, err)
    assert server.catalog_hits() == []


def test_catalog_401_ends_session(sb, server):
    sb.seed_creds()
    sb.seed_cache(BUNDLE_V1, checked_ago=_dt.timedelta(hours=25))
    server.on("GET", CATALOG, (401, {"error": "invalid_token"}))
    rc, out, err = sb.run("models")
    _assert_session_ended(sb, rc, out, err)
    assert len(server.catalog_hits()) == 1


def test_catalog_401_without_cache_ends_session(sb, server):
    sb.seed_creds()
    server.on("GET", CATALOG, (401, {"error": "invalid_token"}))
    rc, out, err = sb.run("models")
    _assert_session_ended(sb, rc, out, err)


# ---------------------------------------------------------------------------
# logout (section 5 "Cache")
# ---------------------------------------------------------------------------


def test_logout_deletes_full_cache_and_state(sb, server):
    sb.seed_creds()
    sb.seed_cache(BUNDLE_V1, checked_ago=_dt.timedelta(hours=1))
    server.on("POST", "/v1/auth/revoke", (200, {}))
    rc, out, err = sb.run("logout")
    assert rc == 0, err
    assert out == "Logged out.\n"
    assert not sb.creds.exists()
    assert not sb.full_dir.exists()
    assert not sb.state_file.exists()


def test_logout_deletes_cache_even_when_revoke_fails(sb):
    sb.seed_creds(api_url=closed_port_url())
    sb.seed_cache(BUNDLE_V1, checked_ago=_dt.timedelta(hours=1))
    rc, out, err = sb.run("logout")
    assert rc == 0, err
    assert out == "Logged out.\n"
    assert not sb.creds.exists()
    assert not sb.full_dir.exists()
    assert not sb.state_file.exists()


def test_after_logout_models_back_on_starter_with_nudge(sb, server):
    sb.seed_creds()
    sb.seed_cache(BUNDLE_V1, checked_ago=_dt.timedelta(hours=1))
    server.on("POST", "/v1/auth/revoke", (200, {}))
    assert sb.run("logout")[0] == 0
    n_before = len(server.requests)
    rc, ids, err = sb.models()
    assert rc == 0, err
    assert_starter(ids)
    assert stderr_lines(err).count(NUDGE) == 1
    assert len(server.requests) == n_before


def test_whoami_after_login_reports_downloaded_release(sb, server):
    server.on("GET", CATALOG, serve_live(BUNDLE_V1))
    rc, _, err = run_login(sb, server)
    assert rc == 0, err
    rc, out, err = sb.run("whoami")
    assert rc == 0, err
    assert out.split("\n")[2] == f"Catalog: full, release {V1} (5 models)"
