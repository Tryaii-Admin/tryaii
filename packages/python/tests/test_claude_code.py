"""`tryaii claude-code` (shared/monitor/SPEC.md §10): the log reader, pricing,
the report, the event mapping and connect / disconnect.

Every test runs on a small synthetic Claude Code projects directory written
into tmp_path; the real ~/.claude is never read. Message text, tool inputs
and a subagent description carry a CANARY string that must never appear in
any output, event or upload body.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

from tryaii.catalog.bundle import starter_bundle
from tryaii.claude_code import importer
from tryaii.claude_code.pricing import Pricer
from tryaii.claude_code.reader import project_label, read_calls
from tryaii.claude_code.report import build_report, render_report
from tryaii.cli import claude_code as cli
from tryaii.monitor import store
from tryaii.monitor.events import build_event

CANARY = "CANARY-7f3a-do-not-leak"
NOW = "2026-10-21T12:00:00Z"
NOW_DT = datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc)
KEY = "tryaii_k1a2b3c4d5e6_fixtureSecretfixtureSecretfixtureSecretfixt"
REAL_HOME = Path.home()


def _assistant(mid, model, ts, usage, *, cwd="C:\\Users\\alice\\code\\shop-app",
               sidechain=False, content=None, request_id=None):
    return {
        "type": "assistant", "timestamp": ts, "isSidechain": sidechain, "cwd": cwd,
        "requestId": request_id or f"req_{mid}", "sessionId": "s", "uuid": str(uuid.uuid4()),
        "message": {"id": mid, "model": model, "role": "assistant", "type": "message",
                    "content": content if content is not None
                    else [{"type": "text", "text": f"answer {CANARY}"}],
                    "usage": usage},
    }


def _usage(inp=0, out=0, cw=0, cr=0, w5=None, w1=None, thinking=None):
    u = {"input_tokens": inp, "output_tokens": out, "cache_creation_input_tokens": cw,
         "cache_read_input_tokens": cr, "service_tier": "standard"}
    if w5 is not None or w1 is not None:
        u["cache_creation"] = {"ephemeral_5m_input_tokens": w5 or 0,
                               "ephemeral_1h_input_tokens": w1 or 0}
    if thinking is not None:
        u["output_tokens_details"] = {"thinking_tokens": thinking}
    return u


def _write(path, lines):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(x) + "\n" for x in lines), encoding="utf-8")


@pytest.fixture
def projects(tmp_path):
    root = tmp_path / "projects"
    proj = root / "C--Users-alice-secret-proj"
    user = {"type": "user", "timestamp": "2026-10-20T09:59:59.000Z",
            "message": {"role": "user", "content": f"please {CANARY}"}, "cwd": "C:\\x"}
    a1 = _assistant("msg_A", "claude-sonnet-5-5", "2026-10-20T10:00:00.000Z",
                    _usage(inp=10, out=5, cw=1000, w5=600, w1=400, thinking=3))
    a2 = _assistant("msg_A", "claude-sonnet-5-5", "2026-10-20T10:00:01.000Z",
                    _usage(inp=10, out=50, cw=1000, w5=600, w1=400, thinking=3),
                    content=[{"type": "tool_use", "name": "Read",
                              "input": {"file_path": f"C:\\Users\\alice\\{CANARY}.py"}}])
    synthetic = _assistant("msg_S", "<synthetic>", "2026-10-20T10:00:02.000Z", _usage(out=1))
    side = _assistant("msg_B", "claude-sonnet-5-5", "2026-10-20T10:01:00.000Z",
                      _usage(inp=3, out=7, cr=2000), sidechain=True)
    tool_result = {"type": "user", "timestamp": "2026-10-20T10:01:01.000Z",
                   "message": {"role": "user", "content": [
                       {"type": "tool_result", "content": f"file body {CANARY}"}]}}
    old = _assistant("msg_OLD", "claude-sonnet-5-5", "2026-08-01T10:00:00.000Z", _usage(out=1))
    _write(proj / "s1.jsonl", [user, a1, a2, synthetic, side, tool_result, old])
    # A resumed session copies msg_A into a new file.
    c = _assistant("msg_C", "claude-opus-5-5", "2026-10-21T08:00:00.000Z",
                   _usage(inp=1, out=100, cr=5000, w5=0, w1=0), cwd="/home/alice/work/billing")
    _write(proj / "s2.jsonl", [a2, c])
    sub = proj / "s1" / "subagents"
    _write(sub / "agent-x1.jsonl", [
        _assistant(f"msg_D{i}", "claude-opus-5-5", f"2026-10-20T11:0{i}:00.000Z",
                   _usage(inp=2, out=20, cr=10000, cw=500), sidechain=True)
        for i in range(1, 4)])
    (sub / "agent-x1.meta.json").write_text(json.dumps(
        {"agentType": "Explore", "description": f"look for {CANARY}"}), encoding="utf-8")
    _write(sub / "agent-x2.jsonl", [
        _assistant("msg_E", "claude-mystery-9", "2026-10-20T12:00:00.000Z",
                   _usage(inp=5, out=5), sidechain=True)])
    (proj / "memory").mkdir()
    (proj / "memory" / "notes.md").write_text(CANARY, encoding="utf-8")
    return root


@pytest.fixture(autouse=True)
def claude_dir(tmp_path, monkeypatch):
    """Every test gets its own Claude Code config directory: connect's live part
    writes settings.json there, never into the real ~/.claude."""
    path = tmp_path / "claude-config"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(path))
    for name in ("OTEL_EXPORTER_OTLP_ENDPOINT", "OTEL_EXPORTER_OTLP_HEADERS",
                 "OTEL_EXPORTER_OTLP_LOGS_HEADERS", "OTEL_EXPORTER_OTLP_LOGS_ENDPOINT",
                 "OTEL_LOG_USER_PROMPTS", "OTEL_LOG_TOOL_DETAILS", "OTEL_LOG_RAW_API_BODIES",
                 "OTEL_LOG_ASSISTANT_RESPONSES", "OTEL_LOG_TOOL_CONTENT",
                 "OTEL_METRICS_INCLUDE_SESSION_ID"):
        monkeypatch.delenv(name, raising=False)
    before = _real_settings_stamp()
    yield path
    assert _real_settings_stamp() == before, "a test touched the real Claude Code settings"


def _real_settings_stamp():
    """Size and mtime of the real ~/.claude/settings.json (never its content)."""
    try:
        st = (REAL_HOME / ".claude" / "settings.json").stat()
    except OSError:
        return None
    return st.st_size, st.st_mtime_ns


@pytest.fixture
def pricer():
    return Pricer(starter_bundle())


def _calls(projects, since=datetime(2026, 9, 21, 12, tzinfo=timezone.utc)):
    return read_calls(projects, since=since, until=NOW_DT)


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    path = tmp_path / "data"
    monkeypatch.setenv("TRYAII_DRE_DATA_DIR", str(path))
    monkeypatch.delenv("TRYAII_API_KEY", raising=False)
    for name in ("TRYAII_MONITOR_UPLOAD", "TRYAII_TELEMETRY", "DO_NOT_TRACK"):
        monkeypatch.delenv(name, raising=False)
    return path


def _run(capsys, *argv):
    code = 0
    try:
        cli.cmd_claude_code(list(argv))
    except SystemExit as exc:
        code = exc.code
    out = capsys.readouterr()
    return code, out.out, out.err


# --- reader ---------------------------------------------------------------------------

def test_dedupe_by_message_id_across_lines_and_files(projects):
    result = _calls(projects)
    by_key = {c.key: c for c in result.calls}
    assert sorted(by_key) == ["msg_A", "msg_B", "msg_C", "msg_D1", "msg_D2", "msg_D3", "msg_E"]
    a = by_key["msg_A"]
    assert a.tokens("output_tokens") == 50  # max over the duplicate lines
    assert a.tokens("cache_write_5m_tokens") == 600 and a.tokens("cache_write_1h_tokens") == 400
    assert a.files == 2  # the resumed session copied it
    assert a.ts == datetime(2026, 10, 20, 10, 0, tzinfo=timezone.utc)  # earliest line
    assert result.synthetic == 1


def test_window_excludes_old_calls(projects):
    keys = {c.key for c in read_calls(projects).calls}
    assert "msg_OLD" in keys
    assert "msg_OLD" not in {c.key for c in _calls(projects).calls}


def test_processes_main_subagent_type_and_unknown(projects):
    by_key = {c.key: c for c in _calls(projects).calls}
    assert by_key["msg_A"].process == "main"
    assert by_key["msg_B"].process == "subagent"  # sidechain line in a main file
    assert by_key["msg_D1"].process == "Explore"  # from agent-x1.meta.json
    assert by_key["msg_E"].process == "subagent"  # no meta file


@pytest.mark.parametrize("cwd,label", [
    ("C:\\Users\\alice\\code\\shop-app", "shop-app"),
    ("/home/alice/work/billing/", "billing"),
    ("C:\\", "unknown"),
    (None, "unknown"),
])
def test_project_label_is_the_base_name(cwd, label):
    assert project_label(cwd) == label


def test_projects_are_base_names_never_paths_or_encoded_folder(projects):
    labels = {c.project for c in _calls(projects).calls}
    assert labels == {"shop-app", "billing"}


# --- pricing --------------------------------------------------------------------------

def test_cost_rule_cache_write_and_read_multipliers(projects, pricer):
    by_key = {c.key: c for c in _calls(projects).calls}
    price = pricer.price("claude-sonnet-5-5")
    assert price.catalog_id == "anthropic/claude-sonnet-5.5"
    assert price.cache_read_source == "0.1x input"
    i, o = price.input, price.output
    cost = pricer.cost(by_key["msg_A"])
    assert cost["input"] == pytest.approx(10 * i)
    assert cost["cache_write_5m"] == pytest.approx(600 * i * 1.25)
    assert cost["cache_write_1h"] == pytest.approx(400 * i * 2)
    assert cost["output"] == pytest.approx(50 * o)
    b = pricer.cost(by_key["msg_B"])
    assert b["cache_read"] == pytest.approx(2000 * i * 0.1)


def test_write_without_breakdown_is_priced_as_5m(projects, pricer):
    d1 = {c.key: c for c in _calls(projects).calls}["msg_D1"]
    price = pricer.price(d1.model)
    assert pricer.cost(d1)["cache_write_5m"] == pytest.approx(500 * price.input * 1.25)
    assert pricer.cost(d1)["cache_write_1h"] == 0


def test_catalog_cache_price_wins_over_the_default(pricer):
    pricer._raw["anthropic/claude-sonnet-5.5"] = {"cache_read_per_1k": 0.0001}
    price = pricer.price("claude-sonnet-5-5")
    assert price.cache_read_source == "catalog"
    assert price.cache_read == pytest.approx(0.0001 / 1000)


@pytest.mark.parametrize("model,factor", [
    ("claude-opus-5-5", 0.05),
    ("claude-fable-5-1", 0.025),
    ("claude-sonnet-5-5", 0.1),
])
def test_cache_read_multiplier_table(pricer, model, factor):
    price = pricer.price(model)
    assert price.source == "catalog"
    assert price.cache_read == pytest.approx(price.input * factor)
    assert price.cache_read_source == f"{factor:g}x input"


def test_mythos_multiplier_applies_to_its_id():
    from tryaii.claude_code.pricing import cache_read_factor

    assert cache_read_factor("claude-mythos-5-1") == 0.025
    assert cache_read_factor("anthropic/claude-mythos-5.1") == 0.025
    assert cache_read_factor("claude-mythos-5") == 0.1


def test_list_price_fallback_for_models_the_catalog_lacks(pricer):
    haiku = pricer.price("claude-haiku-5-5")
    assert haiku.source == "list price (fallback)" and haiku.catalog_id is None
    assert haiku.input == pytest.approx(0.10 / 1e6) and haiku.output == pytest.approx(0.50 / 1e6)
    assert haiku.cache_read == pytest.approx(0.01 / 1e6)


def test_opus_5_is_priced_from_the_starter_catalog(pricer):
    """claude-opus-5 is in the starter (and full) catalog, so it no longer
    needs the list-price fallback."""
    from tryaii.claude_code.pricing import FALLBACK_LIST_PRICES

    opus5 = pricer.price("claude-opus-5")
    assert opus5.source == "catalog" and opus5.catalog_id == "anthropic/claude-opus-5"
    assert opus5.input == pytest.approx(5 / 1e6) and opus5.output == pytest.approx(25 / 1e6)
    assert opus5.cache_read == pytest.approx(0.5 / 1e6)
    assert "claude-opus-5" not in FALLBACK_LIST_PRICES


def test_haiku_long_context_tier():
    from tryaii.claude_code.reader import Call

    pricer = Pricer(starter_bundle())
    when = datetime(2026, 10, 20, tzinfo=timezone.utc)

    def call(cr):
        return Call(key="k", model="claude-haiku-5-5", ts=when, project="p", subagent=False,
                    agent_type=None, usage={"input_tokens": 1000, "output_tokens": 1000,
                                            "cache_read_input_tokens": cr})

    short = pricer.cost(call(50_000))
    assert short["input"] == pytest.approx(1000 * 0.10 / 1e6)
    assert short["cache_read"] == pytest.approx(50_000 * 0.01 / 1e6)
    long = pricer.cost(call(150_000))  # prompt 151,000 > 100K
    assert long["input"] == pytest.approx(1000 * 0.50 / 1e6)
    assert long["output"] == pytest.approx(1000 * 2.50 / 1e6)
    assert long["cache_read"] == pytest.approx(150_000 * 0.05 / 1e6)


def test_report_marks_fallback_prices(pricer):
    from tryaii.claude_code.reader import Call

    when = datetime(2026, 10, 20, tzinfo=timezone.utc)
    calls = [Call(key="k", model="claude-haiku-5-5", ts=when, project="p", subagent=False,
                  agent_type=None, usage={"input_tokens": 10, "output_tokens": 10})]
    doc = build_report(calls, pricer, days=30, now=NOW_DT, since=when, files=1,
                       projects_dir="~")
    assert doc["models"][0]["price_source"] == "list price (fallback)"
    assert doc["unpriced_models"] == []
    assert "list price (fallback)" in render_report(doc)


def test_unknown_model_is_unpriced(projects, pricer):
    e = {c.key: c for c in _calls(projects).calls}["msg_E"]
    assert pricer.price("claude-mystery-9") is None
    assert pricer.cost(e) is None


# --- report ---------------------------------------------------------------------------

def _report(projects, pricer):
    result = _calls(projects)
    return build_report(result.calls, pricer, days=30, now=NOW_DT,
                        since=datetime(2026, 9, 21, 12, tzinfo=timezone.utc),
                        files=result.files, projects_dir="~/projects")


def test_report_totals_threads_and_unpriced(projects, pricer):
    doc = _report(projects, pricer)
    t = doc["totals"]
    assert t["calls"] == 7 and t["unpriced_calls"] == 1
    assert doc["cost_basis"] == "estimate at API list prices"
    assert {r["thread"]: r["calls"] for r in doc["threads"]} == {"main": 2, "subagents": 5}
    assert [r["model"] for r in doc["unpriced_models"]] == ["claude-mystery-9"]
    split = t["cost_split_usd"]
    assert t["est_cost_usd"] == pytest.approx(sum(split.values()), abs=1e-5)
    tok = t["tokens"]
    assert t["cache_hit_share"] == round(
        tok["cache_read"] / (tok["input"] + tok["cache_write"] + tok["cache_read"]), 4)


def test_report_by_day_rows_are_contiguous_utc_dates(projects, pricer):
    doc = _report(projects, pricer)
    days = doc["days"]
    # Window 2026-09-21T12:00Z .. 2026-10-21T12:00Z: 31 UTC dates, both edges partial.
    assert len(days) == 31
    assert (days[0]["date"], days[-1]["date"]) == ("2026-09-21", "2026-10-21")
    by = {r["date"]: r for r in days}
    assert (by["2026-10-20"]["calls"], by["2026-10-20"]["unpriced_calls"]) == (6, 1)
    assert (by["2026-10-20"]["main_calls"], by["2026-10-20"]["subagent_calls"]) == (1, 5)
    assert (by["2026-10-21"]["calls"], by["2026-10-21"]["main_calls"]) == (1, 1)
    empty = by["2026-10-01"]
    assert (empty["calls"], empty["est_cost_usd"], empty["cache_hit_share"]) == (0, 0, None)
    assert sum(r["calls"] for r in days) == doc["totals"]["calls"]
    assert sum(r["est_cost_usd"] for r in days) == pytest.approx(
        doc["totals"]["est_cost_usd"], abs=1e-5)
    text = render_report(doc)
    section = text.split("By day (UTC)\n", 1)[1].split("\n\n", 1)[0].splitlines()
    assert section[0].split() == ["date", "calls", "cache", "hit", "est", "cost"]
    assert len(section) == 1 + 31
    assert section[1].split() == ["2026-09-21", "0", "-", "$0.00"]
    assert section[-1].split()[:2] == ["2026-10-21", "1"]


def test_report_by_day_window_edges_bucket_by_utc(pricer):
    from datetime import timedelta as td

    from tryaii.claude_code.reader import Call

    since = datetime(2026, 10, 19, 23, 30, tzinfo=timezone.utc)
    now = datetime(2026, 10, 21, 0, 15, tzinfo=timezone.utc)
    plus2 = timezone(td(hours=2))
    usage = {"input_tokens": 10, "output_tokens": 10}
    calls = [Call(key=k, model="claude-sonnet-5-5", ts=ts, project="p", subagent=False,
                  agent_type=None, usage=usage)
             for k, ts in (("first", datetime(2026, 10, 19, 23, 45, tzinfo=timezone.utc)),
                           # 01:00 at +02:00 is 2026-10-20T23:00Z: counted on the 20th.
                           ("tz", datetime(2026, 10, 21, 1, 0, tzinfo=plus2)),
                           ("last", datetime(2026, 10, 21, 0, 10, tzinfo=timezone.utc)))]
    doc = build_report(calls, pricer, days=1, now=now, since=since, files=1, projects_dir="~")
    assert [(r["date"], r["calls"]) for r in doc["days"]] == [
        ("2026-10-19", 1), ("2026-10-20", 1), ("2026-10-21", 1)]
    empty = build_report([], pricer, days=1, now=now, since=since, files=0, projects_dir="~")
    assert [(r["date"], r["calls"], r["est_cost_usd"]) for r in empty["days"]] == [
        ("2026-10-19", 0, 0), ("2026-10-20", 0, 0), ("2026-10-21", 0, 0)]


def test_one_run_is_not_enough_for_a_hint(projects, pricer):
    doc = _report(projects, pricer)
    runs = {r["process"]: r for r in doc["subagent_runs"]}
    # agent-x1.jsonl is ONE Explore run of three calls (not three runs).
    assert (runs["Explore"]["runs"], runs["Explore"]["calls"]) == (1, 3)
    assert runs["Explore"]["median_run_output_tokens"] == 60
    assert doc["suggestions"] == []
    assert "nothing stands out" in render_report(doc)


@pytest.fixture
def runs_projects(tmp_path):
    """Four small Explore runs and three large general-purpose runs, plus two
    sidechain lines of one agentId in a main file (one run)."""
    root = tmp_path / "runs"
    sub = root / "C--Users-alice-p" / "s" / "subagents"
    outputs = [100, 300, 500, 6000]
    for n, out in enumerate(outputs):
        _write(sub / f"agent-e{n}.jsonl", [
            _assistant(f"msg_e{n}_{i}", "claude-opus-5-5", f"2026-10-20T1{n}:0{i}:00.000Z",
                       _usage(inp=2, out=out // 2, cr=20000, cw=1000)) for i in range(2)])
        (sub / f"agent-e{n}.meta.json").write_text(json.dumps({"agentType": "Explore"}),
                                                   encoding="utf-8")
    for n in range(3):
        _write(sub / f"agent-g{n}.jsonl", [
            _assistant(f"msg_g{n}", "claude-opus-5-5", f"2026-10-20T0{n}:00:00.000Z",
                       _usage(inp=2, out=20000, cr=20000))])
        (sub / f"agent-g{n}.meta.json").write_text(
            json.dumps({"agentType": "general-purpose"}), encoding="utf-8")
    side = [_assistant(f"msg_s{i}", "claude-opus-5-5", f"2026-10-20T09:0{i}:00.000Z",
                       _usage(inp=1, out=10), sidechain=True) for i in range(2)]
    for line in side:
        line["agentId"] = "a1"
    _write(root / "C--Users-alice-p" / "s.jsonl", side)
    return root


def test_hint_is_measured_per_run(runs_projects, pricer):
    result = read_calls(runs_projects)
    doc = build_report(result.calls, pricer, days=30, now=NOW_DT,
                       since=datetime(2026, 9, 21, 12, tzinfo=timezone.utc),
                       files=result.files, projects_dir="~/runs")
    runs = {r["process"]: r for r in doc["subagent_runs"]}
    explore = runs["Explore"]
    assert (explore["runs"], explore["calls"]) == (4, 8)
    assert explore["median_run_output_tokens"] == 400  # median of 100, 300, 500, 6000
    assert explore["p90_run_output_tokens"] == 6000
    assert runs["general-purpose"]["runs"] == 3
    assert (runs["subagent"]["runs"], runs["subagent"]["calls"]) == (1, 2)  # one agentId
    (s,) = doc["suggestions"]  # general-purpose runs are large: no hint
    assert s["process"] == "Explore" and s["runs"] == 4 and s["models"] == ["claude-opus-5-5"]
    assert s["suggested_model"] == "haiku" and s["priced_as"] == "claude-haiku-5-5"
    assert 0 < s["max_saving_usd"] < s["est_cost_usd"]
    assert s["median_run_cost_usd"] <= s["p90_run_cost_usd"]
    text = render_report(doc)
    assert "4 runs, output per run median 400 / p90 6,000 tokens" in text
    assert "model: haiku" in text and "upper bound" in text
    assert "estimate at API list prices" in text


def test_a_long_p90_run_blocks_the_hint(runs_projects, pricer):
    big = runs_projects / "C--Users-alice-p" / "s" / "subagents"
    for n in range(4, 8):
        _write(big / f"agent-e{n}.jsonl", [
            _assistant(f"msg_e{n}", "claude-opus-5-5", "2026-10-20T20:00:00.000Z",
                       _usage(inp=2, out=50000 if n > 4 else 100, cr=20000))])
        (big / f"agent-e{n}.meta.json").write_text(json.dumps({"agentType": "Explore"}),
                                                   encoding="utf-8")
    result = read_calls(runs_projects)
    doc = build_report(result.calls, pricer, days=30, now=NOW_DT,
                       since=datetime(2026, 9, 21, 12, tzinfo=timezone.utc),
                       files=result.files, projects_dir="~/runs")
    assert {r["process"]: r for r in doc["subagent_runs"]}["Explore"]["p90_run_output_tokens"] \
        == 50000
    assert doc["suggestions"] == []


def test_report_cli_text_and_json_never_leak(projects, data_dir, capsys):
    code, out, err = _run(capsys, "report", "--projects-dir", str(projects), "--now", NOW)
    assert code == 0, err
    assert "Claude Code usage -- last 30 days" in out and "unpriced" in out
    code, jout, err = _run(capsys, "report", "--json", "--projects-dir", str(projects),
                           "--now", NOW)
    assert code == 0, err
    doc = json.loads(jout)
    assert doc["totals"]["calls"] == 7
    assert "By day (UTC)" in out
    assert len(doc["days"]) == 31 and set(doc["days"][0]) == {
        "date", "calls", "unpriced_calls", "main_calls", "subagent_calls", "cache_hit_share",
        "est_cost_usd"}
    for text in (out, jout, err):
        assert CANARY not in text
        assert "alice" not in text  # no OS user name: no full path, no encoded folder


def test_usage_errors(projects, data_dir, capsys):
    assert _run(capsys)[0] == 2
    assert _run(capsys, "bogus")[0] == 2
    assert _run(capsys, "report", "--days", "0")[0] == 2
    assert _run(capsys, "report", "--dry-run")[0] == 2
    assert _run(capsys, "connect", "--days", "91", "--projects-dir", str(projects))[0] == 2
    code, _out, err = _run(capsys, "report", "--projects-dir", str(projects / "nope"))
    assert code == 1 and "no Claude Code logs" in err


# --- events -----------------------------------------------------------------------------

_REFERENCE_ARGS = dict(ts="2026-10-20T10:00:00.000Z", app="x", process="p", call_site=None,
                       provider="openai", model_requested=None, model_resolved=None,
                       model_used=None, mode="off", winner=None, winner_reachable=False,
                       route_status="ok", classification=None, usage=None, est_cost_usd=None,
                       est_cost_winner_usd=None, latency_ms=None, stream=False, error=None,
                       prompt_hash="h", vector=False)


def test_event_mapping_and_key_order(projects, pricer):
    a = {c.key: c for c in _calls(projects).calls}["msg_A"]
    event = importer.build_call_event(a, pricer)
    reference = build_event(ts="2026-10-20T10:00:00.000Z", app="x", process="p", call_site=None,
                            provider="openai", model_requested=None, model_resolved=None,
                            model_used=None, mode="off", winner=None, winner_reachable=False,
                            route_status="ok", classification=None, usage=None,
                            est_cost_usd=None, est_cost_winner_usd=None, latency_ms=None,
                            stream=False, error=None, prompt_hash="h", vector=False,
                            cache_cold=None)
    assert list(event) == list(reference)
    assert "cache_cold" not in build_event(**{**_REFERENCE_ARGS})  # other SDK events: no key
    assert event["cache_cold"] is False  # the first main call of its session
    assert event["event_id"] == str(uuid.uuid5(uuid.NAMESPACE_URL, "tryaii:claude-code:req_msg_A"))
    assert event["app"] == "claude-code" and event["provider"] == "anthropic"
    assert event["mode"] == "off" and event["process"] == "main"
    assert event["model_requested"] == event["model_used"] == "claude-sonnet-5-5"
    assert event["model_resolved"] == "anthropic/claude-sonnet-5.5"
    assert event["call_site"] == {"file": "shop-app", "line": None, "function": None}
    assert event["usage"] == {"prompt_tokens": 10, "completion_tokens": 50,
                              "total_tokens": 1060, "cached_tokens": 0,
                              "cache_write_tokens": 1000,
                              "cache_writes": [{"tokens": 600, "ttl": "5m"},
                                               {"tokens": 400, "ttl": "1h"}]}
    assert event["est_cost_usd"] == event["est_cost_anchor_usd"] == round(
        pricer.cost(a)["total"], 6)
    assert event["latency_ms"] is None and event["prompt_hash"] is None
    assert event["route_status"] == "skipped" and event["vector"] is False


def test_event_of_unpriced_call_has_null_cost(projects, pricer):
    e = {c.key: c for c in _calls(projects).calls}["msg_E"]
    event = importer.build_call_event(e, pricer)
    assert event["est_cost_usd"] is None and event["model_resolved"] is None
    assert event["usage"]["cache_writes"] is None  # no breakdown reported


# --- connect / disconnect ------------------------------------------------------------------

def _all_event_text(data_dir):
    directory = importer.app_directory(data_dir)
    return "".join(p.read_text(encoding="utf-8") for p in store.event_files(directory))


def test_dry_run_writes_nothing(projects, data_dir, capsys):
    code, out, err = _run(capsys, "connect", "--dry-run", "--projects-dir", str(projects),
                          "--now", NOW)
    assert code == 0, err
    assert "dry run: nothing is written or uploaded" in out
    assert "new: 7" in out and "example (redacted)" in out
    assert "no TRYAII_API_KEY" in out
    assert CANARY not in out and "alice" not in out
    assert not data_dir.exists() or not any(data_dir.rglob("*"))


def test_connect_without_terminal_needs_yes(projects, data_dir, capsys, monkeypatch):
    monkeypatch.setattr("sys.stdin", None)
    code, _out, err = _run(capsys, "connect", "--projects-dir", str(projects), "--now", NOW)
    assert code == 2 and "--yes" in err
    assert not importer.app_directory(data_dir).exists()


def test_connect_stores_once_and_rerun_adds_nothing(projects, data_dir, capsys):
    code, out, err = _run(capsys, "connect", "--yes", "--projects-dir", str(projects),
                          "--now", NOW)
    assert code == 0, err
    assert "stored 7 events" in out
    text = _all_event_text(data_dir)
    lines = [json.loads(x) for x in text.splitlines()]
    assert len(lines) == 7 and len({e["event_id"] for e in lines}) == 7
    assert CANARY not in text and "alice" not in text
    state = importer.read_state(data_dir)
    assert state["imported_events"] == 7
    code, out, err = _run(capsys, "connect", "--yes", "--projects-dir", str(projects),
                          "--now", NOW)
    assert code == 0, err
    assert "already stored: 7; new: 0" in out and "nothing new to import" in out
    assert len(_all_event_text(data_dir).splitlines()) == 7


def test_connect_uploads_with_the_monitor_uploader(projects, data_dir, capsys, monkeypatch):
    from tests.test_monitor_upload import VALID, Opener
    from tryaii.monitor import upload

    opener = Opener({"/v1/apps/keys/validate": [(200, VALID)],
                     "/v1/monitor/events": [(202, {"accepted": 7, "batch_id": "b"})]})

    class Clock:
        t = 1000.0

        def monotonic(self):
            return self.t

        def sleep(self, seconds):
            self.t += seconds

    clock = Clock()
    real = upload.Uploader
    monkeypatch.setattr(upload, "Uploader",
                        lambda key, base: real(key, base, opener=opener, clock=clock.monotonic))
    monkeypatch.setattr(cli, "time", clock)
    monkeypatch.setenv("TRYAII_API_KEY", KEY)
    monkeypatch.setenv("TRYAII_API_URL", "https://api.test")
    code, out, err = _run(capsys, "connect", "--yes", "--projects-dir", str(projects),
                          "--now", NOW)
    assert code == 0, err
    assert "uploaded 7 events" in out
    posts = [c for c in opener.calls if c["path"] == "/v1/monitor/events"]
    assert len(posts) == 2  # one batch per daily events file
    sent = []
    for post in posts:
        body = post["data"].decode("utf-8")
        assert CANARY not in body and "alice" not in body
        # API.md v1.6: the import labels its batches; the session never goes out.
        assert json.loads(body)["sdk"]["source"] == "claude-code-import"
        assert "session" not in body
        sent += json.loads(body)["events"]
    assert len(sent) == 7 and {e["provider"] for e in sent} == {"anthropic"}
    assert KEY not in out + err


def test_disconnect_removes_only_its_own_state(projects, data_dir, capsys):
    _run(capsys, "connect", "--yes", "--projects-dir", str(projects), "--now", NOW)
    other = store.app_dir(data_dir, "shop")
    other.mkdir(parents=True)
    code, out, err = _run(capsys, "disconnect", "--yes")
    assert code == 0, err
    assert not importer.app_directory(data_dir).exists()
    assert importer.read_state(data_dir) is None
    assert other.is_dir()
    assert (projects / "C--Users-alice-secret-proj" / "s1.jsonl").is_file()
    code, out, _err = _run(capsys, "disconnect", "--yes")
    assert code == 0 and "nothing to remove" in out


def test_help_routes_through_main(capsys, monkeypatch):
    from tryaii.cli.main import cli as main_cli

    for argv in (["--no-banner", "help", "claude-code"], ["--no-banner", "claude-code", "--help"]):
        monkeypatch.setattr("sys.argv", ["tryaii", *argv])
        try:
            main_cli()
        except SystemExit as exc:
            assert exc.code in (0, None)
        assert "tryaii claude-code report" in capsys.readouterr().out


# --- analyze (SPEC.md §10.7) --------------------------------------------------------------

REMINDER = "REMINDER-MARK"
SECRET = "sk-ant-api03-" + "a1B2c3D4e5F6g7H8i9J0" * 2
HAIKU, SONNET, OPUS = ("anthropic/claude-haiku-4.5", "anthropic/claude-sonnet-5.5",
                       "anthropic/claude-opus-5.5")
NANO = "openai/gpt-5-nano"


def _user(text, ts, **extra):
    line = {"type": "user", "timestamp": ts, "cwd": "C:\\Users\\alice\\code\\shop-app",
            "uuid": str(uuid.uuid4()), "message": {"role": "user", "content": text}}
    line.update(extra)
    return line


def _reminder(text):
    return f"<system-reminder>{REMINDER} {CANARY} {text}</system-reminder>"


@pytest.fixture
def analyze_projects(tmp_path):
    """A main session with two classifiable turns (one long, one short), a
    secret turn, tool-result-only and injected user lines; three Explore runs
    with a search instruction and one general-purpose run whose instruction
    holds a secret. CANARY is in every text field."""
    root = tmp_path / "projects"
    proj = root / "C--Users-alice-secret-proj"
    opus = "claude-opus-5-5"
    big = _usage(inp=50, out=4000, cr=200000, cw=20000)
    small = _usage(inp=5, out=100, cr=2000)
    main = [
        _user(_reminder("ctx") + f"\nimplement the billing module {CANARY}",
              "2026-10-20T09:00:00.000Z", origin={"kind": "human"}),
        _assistant("msg_m1", opus, "2026-10-20T09:00:01.000Z", big),
        _user([{"type": "tool_result", "tool_use_id": "t1", "content": f"file body {CANARY}"}],
              "2026-10-20T09:00:02.000Z", toolUseResult={"stdout": CANARY}),
        _assistant("msg_m2", opus, "2026-10-20T09:00:03.000Z", big),
        _user(_reminder("only a reminder"), "2026-10-20T09:00:04.000Z"),
        _user(f"<task-notification>{CANARY}</task-notification>", "2026-10-20T09:00:05.000Z",
              origin={"kind": "task-notification"}),
        _assistant("msg_m3", opus, "2026-10-20T09:00:06.000Z", big),
        _user(f"my key is {SECRET} {CANARY}", "2026-10-20T09:10:00.000Z"),
        _assistant("msg_m4", opus, "2026-10-20T09:10:01.000Z", small),
        _user([{"type": "text", "text": f"summarize the README {CANARY}"}],
              "2026-10-20T09:20:00.000Z"),
        _assistant("msg_m5", opus, "2026-10-20T09:20:01.000Z", small),
        _user(f"<command-name>/clear</command-name> {CANARY}", "2026-10-20T09:30:00.000Z",
              isMeta=True),
    ]
    _write(proj / "s1.jsonl", main)
    sub = proj / "s1" / "subagents"
    for n in range(3):
        _write(sub / f"agent-e{n}.jsonl", [
            _user(_reminder("sub") + f"\nfind the files that mention {CANARY} {n}",
                  f"2026-10-20T1{n}:00:00.000Z", isSidechain=True),
            *[_assistant(f"msg_e{n}_{i}", opus, f"2026-10-20T1{n}:0{i + 1}:00.000Z",
                         _usage(inp=2, out=300, cr=20000, cw=1000), sidechain=True)
              for i in range(2)],
            _user([{"type": "tool_result", "content": CANARY}], f"2026-10-20T1{n}:05:00.000Z",
                  isSidechain=True),
        ])
        (sub / f"agent-e{n}.meta.json").write_text(json.dumps(
            {"agentType": "Explore", "description": f"look for {CANARY}"}), encoding="utf-8")
    _write(sub / "agent-g0.jsonl", [
        _user(f"deploy with password={SECRET} {CANARY}", "2026-10-20T15:00:00.000Z",
              isSidechain=True),
        _assistant("msg_g0", opus, "2026-10-20T15:00:01.000Z", small, sidechain=True)])
    (sub / "agent-g0.meta.json").write_text(json.dumps({"agentType": "general-purpose"}),
                                            encoding="utf-8")
    return root


class FakeRouter:
    """Stands in for the local embedding model: search/summary texts are
    'easy' (Haiku within the band), everything else 'hard' (Sonnet and up)."""

    def __init__(self):
        self.texts = []

    def __call__(self, text):
        from tryaii.classifiers.base import ClassificationResult

        self.texts.append(text)
        easy = any(w in text for w in ("find", "summarize"))
        if easy:
            quality = {NANO: 0.8, HAIKU: 0.8, SONNET: 0.8, OPUS: 0.8}
            cls = ClassificationResult(broad_category="EDUCATIONAL",
                                       subcategory="RESEARCH_METHODOLOGY", confidence=0.7)
        else:
            quality = {NANO: 0.4, HAIKU: 0.5, SONNET: 0.78, OPUS: 0.8}
            cls = ClassificationResult(broad_category="TECHNICAL", subcategory="CODE_TECHNICAL",
                                       confidence=0.9)
        scores = [{"modelId": m, "qualityScore": q} for m, q in quality.items()]
        return scores, cls


def test_task_texts_main_turns_and_subagent_instructions(analyze_projects):
    from tryaii.claude_code.tasks import read_tasks

    read = read_tasks(analyze_projects)
    main = [t for t in read.tasks if t.kind == "main"]
    subs = [t for t in read.tasks if t.kind == "subagent"]
    assert [t.call_keys for t in main] == [["msg_m1", "msg_m2", "msg_m3"], ["msg_m5"]]
    assert main[0].text.startswith("implement the billing module")
    assert all(REMINDER not in t.text and "file body" not in t.text for t in read.tasks)
    assert sorted(t.text.split()[-1] for t in subs) == ["0", "1", "2"]
    assert all(t.text.startswith("find the files") for t in subs)
    assert all(SECRET not in t.text for t in read.tasks)
    c = read.counts
    assert c["skipped_secret"] == 2  # the main turn with a key and the general-purpose run
    assert c["skipped_tool_result_only"] == 1  # subagent lines after the first are not read
    assert c["skipped_injected"] == 3  # reminder only, task notification, isMeta command
    assert c["subagent_runs"] == 4 and c["main_turns"] == 3


@pytest.mark.parametrize("text", [
    f"use {SECRET}", "password: hunter2hunter2", "-----BEGIN OPENSSH PRIVATE KEY-----",
    "AKIAABCDEFGHIJKLMNOP", "token ghp_" + "a" * 36, "api_key=abcdefghijklmnop",
    "postgres://admin:s3cretpw@db.internal/x", KEY,
])
def test_secret_detector_flags(text):
    from tryaii.claude_code.secret_scan import looks_secret

    assert looks_secret(text)


def test_secret_detector_passes_ordinary_text():
    from tryaii.claude_code.secret_scan import looks_secret

    assert not looks_secret("find where the password reset email is sent and summarize it")


def _analysis(projects, pricer, route=None):
    from tryaii.claude_code.analysis import build_analysis
    from tryaii.claude_code.tasks import read_tasks

    since = datetime(2026, 9, 21, 12, tzinfo=timezone.utc)
    calls = read_calls(projects, since=since, until=NOW_DT).calls
    return build_analysis(calls, read_tasks(projects), pricer, route or FakeRouter(), days=30,
                          now=NOW_DT, since=since, consent={"method": "test"},
                          embedding_model="fake", taxonomy_version=None)


def test_analysis_answers_and_advice(analyze_projects, pricer):
    route = FakeRouter()
    doc = _analysis(analyze_projects, pricer, route)
    assert len(route.texts) == 5  # 2 main turns + 3 Explore runs; secrets never routed
    calls = doc["calls"]
    assert calls["msg_e0_0"]["recommended"] == HAIKU and calls["msg_e0_0"]["method"] == "band"
    assert calls["msg_e0_0"]["overall_winner"] == NANO
    assert calls["msg_m1"]["recommended"] == SONNET  # the long coding turn
    assert calls["msg_m1"] is calls["msg_m3"] and "msg_m4" not in calls  # secret turn
    assert "msg_g0" not in calls
    procs = {p["process"]: p for p in doc["processes"]}
    explore = procs["Explore"]
    assert explore["tasks"] == 3 and explore["calls"] == 6
    assert explore["classes"] == [{"class": "EDUCATIONAL/RESEARCH_METHODOLOGY", "share": 1.0}]
    rec = explore["recommendation"]
    assert rec["model"] == HAIKU and rec["alias"] == "haiku" and rec["good_enough_share"] == 1.0
    assert 0 < rec["saving_usd"] == rec["saving_usd_per_month"]  # 30-day window
    assert "model: haiku" in explore["advice"] and "Explore" in explore["advice"]
    assert explore["confidence"] == "low"  # 3 runs only
    main = procs["main"]
    assert main["confidence"] == "low" and main["recommendation"] is None  # 2 tasks < 3
    assert main["classes"][0]["class"] == "TECHNICAL/CODE_TECHNICAL"
    assert main["classes"][0]["share"] > 0.99  # weighted by cost, not by count (1 of 2)
    assert "low confidence" in doc["confidence_note"]


def _row(key, out, good):
    from tryaii.claude_code.reader import Call

    call = Call(key=key, model="claude-opus-5-5", ts=NOW_DT, project="p", subagent=True,
                agent_type="Explore", usage={"input_tokens": 10, "output_tokens": out}, run=key)
    result = {"task": "subagent", "category": "TECHNICAL", "subcategory": "X",
              "confidence": 0.5, "anchor": OPUS, "recommended": good[0], "method": "band",
              "good_enough": good, "overall_winner": NANO, "overall_method": "band"}
    return {"process": "Explore", "kind": "subagent", "calls": [call], "result": result}


@pytest.mark.parametrize("hard_out,expected", [(100_000, SONNET), (10, HAIKU)])
def test_advice_is_weighted_by_cost(pricer, hard_out, expected):
    from tryaii.claude_code.analysis import advise

    rows = [_row(f"e{i}", 100, [HAIKU, SONNET, OPUS]) for i in range(9)]
    rows.append(_row("hard", hard_out, [SONNET, OPUS]))
    calls = [c for r in rows for c in r["calls"]]
    (p,) = advise(rows, calls, pricer, days=30)
    # Haiku fits 9 of 10 tasks; whether that is 80% of the COST decides.
    assert p["recommendation"]["model"] == expected
    assert p["confidence"] == "medium"


class _Tty:
    def isatty(self):
        return True


def test_analyze_asks_for_consent_first(analyze_projects, data_dir, capsys, monkeypatch):
    from tryaii.claude_code import analysis

    route = FakeRouter()
    monkeypatch.setattr(cli, "_make_route_fn", lambda bundle, config: route)
    args = ("analyze", "--projects-dir", str(analyze_projects), "--now", NOW)
    monkeypatch.setattr("sys.stdin", None)
    code, out, err = _run(capsys, *args)
    assert code == 2 and "--yes" in err and "on this machine only" in out
    monkeypatch.setattr("sys.stdin", _Tty())
    monkeypatch.setattr("builtins.input", lambda prompt: "n")
    code, out, err = _run(capsys, *args)
    assert code == 2 and "no task text was read" in err
    assert route.texts == [] and analysis.read_consent(data_dir) is None
    assert analysis.read_analysis(data_dir) is None
    monkeypatch.setattr("builtins.input", lambda prompt: "y")
    code, out, err = _run(capsys, *args)
    assert code == 0, err
    consent = analysis.read_consent(data_dir)
    assert consent["method"] == "prompt" and consent["version"] == analysis.CONSENT_VERSION
    stored = analysis.read_analysis(data_dir)
    assert stored["consent"] == consent and stored["counts"]["tasks_classified"] == 5
    # The stored consent is used next time: no question.
    monkeypatch.setattr("builtins.input", lambda prompt: pytest.fail("asked again"))
    code, out, err = _run(capsys, *args)
    assert code == 0 and "consent given" in out


def test_analyze_never_leaks_text(analyze_projects, data_dir, capsys, monkeypatch):
    from tests.test_monitor_upload import VALID, Opener
    from tryaii.monitor import upload

    monkeypatch.setattr(cli, "_make_route_fn", lambda bundle, config: FakeRouter())
    outputs = []
    for extra in ((), ("--json",)):
        code, out, err = _run(capsys, "analyze", "--yes", *extra, "--projects-dir",
                              str(analyze_projects), "--now", NOW)
        assert code == 0, err
        outputs += [out, err]
    doc = json.loads(outputs[2])
    assert doc["schema"] == "tryaii.claude-code.analysis/1" and "calls" not in doc
    assert doc["consent"]["method"] == "--yes"
    assert "Explore" in outputs[0] and "model: haiku" in outputs[0]
    code, out, err = _run(capsys, "report", "--projects-dir", str(analyze_projects),
                          "--now", NOW)
    assert code == 0 and "Task analysis" in out
    outputs += [out, err]
    code, out, err = _run(capsys, "report", "--json", "--projects-dir", str(analyze_projects),
                          "--now", NOW)
    assert json.loads(out)["analysis"]["counts"]["tasks_classified"] == 5
    outputs += [out, err]

    opener = Opener({"/v1/apps/keys/validate": [(200, VALID)],
                     "/v1/monitor/events": [(202, {"accepted": 11, "batch_id": "b"})]})

    class Clock:
        t = 1000.0

        def monotonic(self):
            return self.t

        def sleep(self, seconds):
            self.t += seconds

    clock = Clock()
    real = upload.Uploader
    monkeypatch.setattr(upload, "Uploader",
                        lambda key, base: real(key, base, opener=opener, clock=clock.monotonic))
    monkeypatch.setattr(cli, "time", clock)
    monkeypatch.setenv("TRYAII_API_KEY", KEY)
    monkeypatch.setenv("TRYAII_API_URL", "https://api.test")
    code, out, err = _run(capsys, "connect", "--yes", "--projects-dir", str(analyze_projects),
                          "--now", NOW)
    assert code == 0, err
    assert "task profiles from analyze: 10 of the new events" in out
    outputs += [out, err]
    bodies = [c["data"].decode("utf-8") for c in opener.calls
              if c["path"] == "/v1/monitor/events"]
    sent = [e for b in bodies for e in json.loads(b)["events"]]
    assert len(sent) == 12
    assert {e["winner"] for e in sent if e["classification"]} == {HAIKU, SONNET}
    for e in sent:
        assert e["prompt_hash"] is None and e["vector"] is False
        if e["classification"]:
            assert set(e["classification"]) == {"category", "subcategory", "confidence"}
            assert e["route_status"] == "ok" and e["winner_reachable"] is True
            assert e["est_cost_winner_usd"] < e["est_cost_usd"]
        else:
            assert e["winner"] is None and e["route_status"] == "skipped"
    stored = "".join(p.read_text(encoding="utf-8", errors="replace")
                     for p in data_dir.rglob("*") if p.is_file())
    for text in (*outputs, *bodies, stored):
        assert CANARY not in text and REMINDER not in text and SECRET not in text
        assert "alice" not in text and "billing module" not in text


# --- live telemetry (SPEC.md §10.8) -------------------------------------------------------

API = "https://api.test"
ENDPOINT = API + "/v1/otlp/claude-code/v1/logs"
EXPECTED_BLOCK = {
    "env": {
        "CLAUDE_CODE_ENABLE_TELEMETRY": "1",
        "OTEL_LOGS_EXPORTER": "otlp",
        "OTEL_METRICS_EXPORTER": "none",
        "OTEL_EXPORTER_OTLP_PROTOCOL": "http/json",
        "OTEL_EXPORTER_OTLP_LOGS_ENDPOINT": ENDPOINT,
        "OTEL_LOG_USER_PROMPTS": "0",
        "OTEL_LOG_TOOL_DETAILS": "0",
    },
    "otelHeadersHelper": "tryaii claude-code otel-headers",
}
EXISTING = {
    "$schema": "https://json.schemastore.org/claude-code-settings.json",
    "model": "claude-opus-5-5",
    "env": {"FOO": "bar", "OTEL_LOG_USER_PROMPTS": "false"},
    "permissions": {"allow": ["Bash(npm test)"], "deny": []},
    "statusLine": {"type": "command", "command": "echo hi"},
}


@pytest.fixture
def keyed(data_dir, monkeypatch):
    monkeypatch.setenv("TRYAII_API_KEY", KEY)
    monkeypatch.setenv("TRYAII_API_URL", API)
    return data_dir


def _settings_file(claude_dir):
    return claude_dir / "settings.json"


def _write_settings(claude_dir, doc, indent=4):
    claude_dir.mkdir(parents=True, exist_ok=True)
    path = _settings_file(claude_dir)
    path.write_text(json.dumps(doc, indent=indent) + "\n", encoding="utf-8")
    return path.read_bytes()


def _backups(claude_dir):
    return sorted(claude_dir.glob("settings.json.tryaii-backup-*"))


def _has_files(directory):
    return directory.exists() and any(p.is_file() for p in directory.rglob("*"))


def test_event_id_uses_the_shared_request_id_rule():
    from tryaii.claude_code.reader import Call

    call = Call(key="msg_X", model="claude-opus-5-5", ts=NOW_DT, project="p", subagent=False,
                agent_type=None, request_id="req_011CTryaiiSample42")
    # uuid5(NAMESPACE_URL, "tryaii:claude-code:req_011CTryaiiSample42"): the server's rule
    assert importer.event_id(call) == "78514a44-63a2-5069-8804-9f7c31fe38f7"
    call.request_id = None
    assert importer.event_id(call) == str(uuid.uuid5(importer.EVENT_NAMESPACE, "msg_X"))


def test_event_id_falls_back_without_request_id(tmp_path):
    line = _assistant("msg_N", "claude-sonnet-5-5", "2026-10-20T10:00:00.000Z", _usage(out=1))
    del line["requestId"]
    _write(tmp_path / "p" / "s.jsonl", [line])
    call = read_calls(tmp_path).calls[0]
    assert call.request_id is None
    assert importer.event_id(call) == str(uuid.uuid5(importer.EVENT_NAMESPACE, "msg_N"))


def test_live_dry_run_shows_the_exact_block_and_writes_nothing(projects, keyed, claude_dir,
                                                               capsys):
    original = _write_settings(claude_dir, EXISTING)
    code, out, err = _run(capsys, "connect", "--dry-run", "--projects-dir", str(projects),
                          "--now", NOW)
    assert code == 0, err
    start = out.index("would add to")
    block = out[out.index("{", start):out.index("\n}\n", start) + 2]
    expected = json.loads(json.dumps(EXPECTED_BLOCK))
    del expected["env"]["OTEL_LOG_USER_PROMPTS"]  # already off ("false"): left as is
    assert json.loads(block) == expected
    assert "already set, left as is: OTEL_LOG_USER_PROMPTS" in out
    assert "cutoff" in out and "backup first" in out
    assert KEY not in out + err and CANARY not in out
    assert _settings_file(claude_dir).read_bytes() == original
    assert not _backups(claude_dir)
    assert not _has_files(keyed)


def test_live_into_an_empty_claude_dir_and_disconnect(keyed, claude_dir, capsys):
    from tryaii.claude_code import telemetry

    code, out, err = _run(capsys, "connect", "--live-only", "--yes")
    assert code == 0, err
    doc = json.loads(_settings_file(claude_dir).read_text(encoding="utf-8"))
    assert doc == EXPECTED_BLOCK
    assert not _backups(claude_dir)  # there was no file to back up
    state = telemetry.read_state(keyed)
    assert state["created_env"] is True and state["endpoint"] == ENDPOINT
    assert state["live_since"]
    text = _settings_file(claude_dir).read_text(encoding="utf-8")
    assert KEY not in text and KEY not in out + err
    assert importer.read_state(keyed) is None  # --live-only: no backfill
    code, out, err = _run(capsys, "disconnect", "--yes")
    assert code == 0, err
    assert json.loads(_settings_file(claude_dir).read_text(encoding="utf-8")) == {}
    assert telemetry.read_state(keyed) is None and telemetry.read_key(keyed) is None


def test_live_merge_keeps_every_other_setting_and_round_trips(keyed, claude_dir, capsys):
    from tryaii.claude_code import telemetry

    original = _write_settings(claude_dir, EXISTING, indent=4)
    code, out, err = _run(capsys, "connect", "--live-only", "--yes")
    assert code == 0, err
    text = _settings_file(claude_dir).read_text(encoding="utf-8")
    doc = json.loads(text)
    assert list(doc) == ["$schema", "model", "env", "permissions", "statusLine",
                         "otelHeadersHelper"]
    for key in ("$schema", "model", "permissions", "statusLine"):
        assert doc[key] == EXISTING[key]
    assert list(doc["env"])[:2] == ["FOO", "OTEL_LOG_USER_PROMPTS"]
    assert doc["env"]["OTEL_LOG_USER_PROMPTS"] == "false"  # the user's off value, untouched
    assert doc["env"]["OTEL_EXPORTER_OTLP_LOGS_ENDPOINT"] == ENDPOINT
    assert '\n    "model"' in text  # the file's 4-space indent is kept
    backups = _backups(claude_dir)
    assert len(backups) == 1 and backups[0].read_bytes() == original
    state = telemetry.read_state(keyed)
    assert "OTEL_LOG_USER_PROMPTS" not in state["env"] and state["created_env"] is False
    # A second connect changes nothing.
    code, out, err = _run(capsys, "connect", "--live-only", "--yes")
    assert code == 0 and "already connected" in out
    assert len(_backups(claude_dir)) == 1
    code, out, err = _run(capsys, "disconnect", "--yes")
    assert code == 0, err
    assert _settings_file(claude_dir).read_bytes() == original  # exact round trip
    assert len(_backups(claude_dir)) == 2


def test_disconnect_leaves_a_key_the_user_changed(keyed, claude_dir, capsys):
    _write_settings(claude_dir, {"model": "x"})
    _run(capsys, "connect", "--live-only", "--yes")
    path = _settings_file(claude_dir)
    doc = json.loads(path.read_text(encoding="utf-8"))
    doc["env"]["OTEL_METRICS_EXPORTER"] = "console"
    path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    code, out, err = _run(capsys, "disconnect", "--yes")
    assert code == 0, err
    assert "left OTEL_METRICS_EXPORTER" in out
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "model": "x", "env": {"OTEL_METRICS_EXPORTER": "console"}}


@pytest.mark.parametrize("doc, needle", [
    ({"env": {"OTEL_EXPORTER_OTLP_LOGS_ENDPOINT": "https://otel.example/v1/logs"}},
     "OTEL_EXPORTER_OTLP_LOGS_ENDPOINT"),
    ({"env": {"OTEL_EXPORTER_OTLP_ENDPOINT": "https://otel.example"}},
     "OTEL_EXPORTER_OTLP_ENDPOINT"),
    ({"env": {"OTEL_EXPORTER_OTLP_HEADERS": "x-key=secret"}}, "OTEL_EXPORTER_OTLP_HEADERS"),
    ({"otelHeadersHelper": "/usr/local/bin/other-helper"}, "otelHeadersHelper"),
    ({"env": {"OTEL_LOGS_EXPORTER": "console"}}, "OTEL_LOGS_EXPORTER"),
    ({"env": {"OTEL_LOG_USER_PROMPTS": "1"}}, "OTEL_LOG_USER_PROMPTS"),
    ({"env": {"OTEL_LOG_RAW_API_BODIES": "file:/tmp/bodies"}}, "OTEL_LOG_RAW_API_BODIES"),
])
def test_live_refuses_another_tools_telemetry(projects, keyed, claude_dir, capsys, doc, needle):
    from tryaii.claude_code import telemetry

    original = _write_settings(claude_dir, doc)
    code, out, err = _run(capsys, "connect", "--yes", "--projects-dir", str(projects),
                          "--now", NOW)
    assert code == 1
    assert needle in out and "never overwrites another tool's telemetry" in err
    assert _settings_file(claude_dir).read_bytes() == original
    assert not _backups(claude_dir)
    assert telemetry.read_state(keyed) is None
    assert importer.read_state(keyed) is None  # nothing at all was written


def test_live_refuses_static_headers_in_the_environment(keyed, claude_dir, capsys,
                                                       monkeypatch):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_HEADERS", "authorization=Bearer other")
    code, out, err = _run(capsys, "connect", "--live-only", "--yes")
    assert code == 1 and "your environment sets OTEL_EXPORTER_OTLP_HEADERS" in out
    assert "Bearer other" not in out + err
    assert not _settings_file(claude_dir).exists()


def test_live_refuses_invalid_settings_json(keyed, claude_dir, capsys):
    claude_dir.mkdir(parents=True)
    _settings_file(claude_dir).write_text('{"model": "x",}', encoding="utf-8")
    code, out, err = _run(capsys, "connect", "--live-only", "--yes")
    assert code == 1 and "not valid JSON" in out
    assert _settings_file(claude_dir).read_text(encoding="utf-8") == '{"model": "x",}'


def test_no_live_and_live_only(projects, keyed, claude_dir, capsys, monkeypatch):
    monkeypatch.setattr(cli, "_run_upload", lambda directory: "uploaded (stub)")
    code, _out, err = _run(capsys, "connect", "--no-live", "--live-only")
    assert code == 2 and "cannot be combined" in err
    code, out, err = _run(capsys, "connect", "--no-live", "--yes", "--projects-dir",
                          str(projects), "--now", NOW)
    assert code == 0, err
    assert "live telemetry" not in out and not _settings_file(claude_dir).exists()


def test_live_only_needs_an_app_key(data_dir, claude_dir, capsys):
    code, _out, err = _run(capsys, "connect", "--live-only", "--yes")
    assert code == 1 and "TRYAII_API_KEY" in err
    assert not _settings_file(claude_dir).exists()


def test_two_separate_consents(projects, keyed, claude_dir, capsys, monkeypatch):
    from tryaii.claude_code import telemetry

    monkeypatch.setattr(cli, "_run_upload", lambda directory: "uploaded (stub)")
    monkeypatch.setattr("sys.stdin", _Tty())
    asked = []

    def answer(prompt):
        asked.append(prompt)
        return "y" if prompt.startswith("Store") else "n"

    monkeypatch.setattr("builtins.input", answer)
    code, out, err = _run(capsys, "connect", "--projects-dir", str(projects), "--now", NOW)
    assert code == 0, err
    assert len(asked) == 2 and "live telemetry" in asked[1]
    assert "stored 7 events" in out and "live telemetry cancelled" in err
    assert not _settings_file(claude_dir).exists() and telemetry.read_state(keyed) is None
    asked.clear()
    monkeypatch.setattr("builtins.input", lambda prompt: asked.append(prompt) or "y")
    code, out, err = _run(capsys, "connect", "--projects-dir", str(projects), "--now", NOW)
    assert code == 0, err
    assert "live telemetry" in asked[-1] and _settings_file(claude_dir).is_file()


def test_backfill_stops_at_the_live_cutoff(projects, keyed, claude_dir, capsys, monkeypatch):
    from tryaii.claude_code import telemetry

    monkeypatch.setattr(cli, "_run_upload", lambda directory: "uploaded (stub)")
    code, out, err = _run(capsys, "connect", "--live-only", "--yes")
    assert code == 0, err
    # As if live telemetry had been turned on between the fixture's calls.
    state = telemetry.read_state(keyed)
    state["live_since"] = "2026-10-20T11:30:00.000Z"
    telemetry.state_path(keyed).write_text(json.dumps(state), encoding="utf-8")
    code, out, err = _run(capsys, "connect", "--dry-run", "--projects-dir", str(projects),
                          "--now", NOW)
    assert code == 0, err
    assert "live telemetry since 2026-10-20T11:30:00.000Z" in out
    assert "new: 5" in out and "2 later calls come by live telemetry" in out
    code, out, err = _run(capsys, "connect", "--no-live", "--yes", "--projects-dir",
                          str(projects), "--now", NOW)
    assert code == 0, err
    stamps = [json.loads(x)["ts"] for x in _all_event_text(keyed).splitlines()]
    assert len(stamps) == 5 and max(stamps) < "2026-10-20T11:30:00.000Z"
    code, out, err = _run(capsys, "status")
    assert "cutoff: calls from 2026-10-20T11:30:00.000Z on" in out
    # disconnect clears the cutoff: a later import covers the gap again.
    _run(capsys, "disconnect", "--yes")
    code, out, err = _run(capsys, "connect", "--no-live", "--dry-run", "--projects-dir",
                          str(projects), "--now", NOW)
    assert "new: 7" in out and "live telemetry since" not in out


def test_otel_headers_connected_and_not(keyed, claude_dir, capsys):
    from tryaii.monitor.privacy import NOTICE_VERSION

    code, out, err = _run(capsys, "otel-headers")
    assert code == 1 and out == "" and "not connected" in err
    _run(capsys, "connect", "--live-only", "--yes")
    code, out, err = _run(capsys, "otel-headers")
    assert code == 0 and err == ""
    assert json.loads(out) == {"Authorization": f"Bearer {KEY}",
                               "X-Tryaii-Notice-Version": NOTICE_VERSION}
    _run(capsys, "disconnect", "--yes")
    code, out, err = _run(capsys, "otel-headers")
    assert code == 1 and out == "" and KEY not in err


def test_status_warns_loudly(keyed, claude_dir, capsys, monkeypatch):
    code, out, err = _run(capsys, "status")
    assert code == 0, err
    assert "backfill: not connected" in out and "live telemetry: not connected" in out
    assert "WARNING" not in out
    _write_settings(claude_dir, {"env": {"OTEL_LOG_TOOL_DETAILS": "1"}})
    monkeypatch.setenv("OTEL_LOG_RAW_API_BODIES", "1")
    monkeypatch.setenv("OTEL_LOG_USER_PROMPTS", "true")
    code, out, err = _run(capsys, "status")
    assert code == 0, err
    for var in ("OTEL_LOG_TOOL_DETAILS", "OTEL_LOG_RAW_API_BODIES", "OTEL_LOG_USER_PROMPTS"):
        assert f"WARNING: {var} is ON" in out
    assert out.count("drops such data before storage") == 3
    code, out, err = _run(capsys, "status", "--json")
    doc = json.loads(out)
    assert {w["variable"] for w in doc["warnings"]} == {
        "OTEL_LOG_TOOL_DETAILS", "OTEL_LOG_RAW_API_BODIES", "OTEL_LOG_USER_PROMPTS"}
    assert "WARNING" in err


def test_status_when_connected(projects, keyed, claude_dir, capsys, monkeypatch):
    monkeypatch.setattr(cli, "_run_upload", lambda directory: "uploaded (stub)")
    code, out, err = _run(capsys, "connect", "--yes", "--projects-dir", str(projects),
                          "--now", NOW)
    assert code == 0, err
    code, out, err = _run(capsys, "status")
    assert code == 0, err
    assert "backfill: connected" in out and "(7 events imported)" in out
    assert "live telemetry: connected" in out and ENDPOINT in out
    assert "tryaii_k1a2b3c4d5e6_..." in out and KEY not in out + err
    assert CANARY not in out and "WARNING" not in out
    text = _settings_file(claude_dir).read_text(encoding="utf-8")
    assert CANARY not in text and KEY not in text


# --- sessions: cold cache restarts (S1) ------------------------------------------------

COLD_FIXTURE = (Path(__file__).resolve().parents[3] / "shared" / "monitor" / "fixtures"
                / "claude-code" / "cold-restarts.json")
SESSION_CANARY = "SESSIONCANARY-5e55-9d2e"


def _cold_fixture():
    return json.loads(COLD_FIXTURE.read_text(encoding="utf-8"))


def _split(usage):
    writes = usage["cache_writes"]
    if writes is None:
        return None, None
    return (sum(w["tokens"] for w in writes if w["ttl"] == "5m"),
            sum(w["tokens"] for w in writes if w["ttl"] == "1h"))


def _fixture_call(row):
    from tryaii.claude_code.reader import Call
    from tryaii.monitor.events import parse_ts

    usage = row["usage"]
    w5, w1 = _split(usage)
    sub = row["process"] != "main"
    return Call(key=row["id"], model=row["model"], ts=parse_ts(row["ts"]), project="p",
                subagent=sub, agent_type=row["process"] if sub else None,
                usage={"input_tokens": 0, "output_tokens": 0,
                       "cache_creation_input_tokens": usage["cache_write_tokens"],
                       "cache_read_input_tokens": usage["cached_tokens"],
                       "cache_write_5m_tokens": w5, "cache_write_1h_tokens": w1},
                has_breakdown=usage["cache_writes"] is not None, session=row["session"])


def _fixture_price(fx, model):
    from tryaii.claude_code.pricing import Price

    p = fx["prices"][model]
    return Price(catalog_id=None, input=p["input_per_mtok"] / 1e6, output=0.0,
                 cache_read_factor=None, cache_read_per_token=p["cache_read_per_mtok"] / 1e6,
                 source="fixture")


def test_cold_restart_rule_matches_the_shared_fixture():
    from tryaii.claude_code import sessions

    fx = _cold_fixture()
    rule = fx["rule"]
    assert sessions.GAP.total_seconds() == rule["gap_minutes"] * 60
    assert sessions.MIN_WRITE_TOKENS == rule["min_write_tokens"]
    assert sessions.DEFAULT_TTL == rule["default_ttl"]
    assert list(sessions.REASONS) == rule["reasons"]
    calls = [_fixture_call(row) for row in fx["calls"]]
    restarts = sessions.mark(calls)
    by_id = {c.key: c for c in calls}
    for exp in fx["expected"]:
        call = by_id[exp["id"]]
        assert call.cache_cold is exp["cache_cold"], exp
        if exp["cache_cold"]:
            assert (call.cold.reason, call.cold.ttl) == (exp["reason"], exp["ttl"]), exp
            cost = sessions.restart_cost(call, _fixture_price(fx, call.model))
            assert round(cost, 6) == exp["cost_usd"], exp
        else:
            assert call.cold is None
    totals = fx["expected_totals"]
    assert len(restarts) == totals["cold_restarts"]
    assert {r: sum(x.reason == r for x in restarts) for r in sessions.REASONS} == \
        totals["by_reason"]
    assert round(sum(e["cost_usd"] or 0 for e in fx["expected"]), 6) == totals["cost_usd"]
    assert sessions.session_labels(calls) == fx["expected_session_labels"]
    # Input order does not matter.
    again = [_fixture_call(row) for row in reversed(fx["calls"])]
    assert [r.key for r in sessions.mark(again)] == [r.key for r in restarts]


def _write_fixture_logs(root, fx, session_name=lambda s: s):
    """The fixture's calls as Claude Code log files: main calls in
    ``<session>.jsonl``, subagent calls in ``<session>/subagents/``."""
    proj = root / "C--Users-alice-proj"
    files: dict = {}
    for row in fx["calls"]:
        usage = row["usage"]
        w5, w1 = _split(usage)
        u = _usage(inp=1, out=10, cw=usage["cache_write_tokens"] or 0,
                   cr=usage["cached_tokens"], w5=w5, w1=w1)
        session = session_name(row["session"])
        sub = row["process"] != "main"
        line = _assistant("msg_" + row["id"], row["model"], row["ts"], u, sidechain=sub)
        line["sessionId"] = session
        path = (proj / session / "subagents" / f"agent-{row['process']}.jsonl" if sub
                else proj / f"{session}.jsonl")
        files.setdefault(path, []).append(line)
        if sub:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.with_name(path.name[:-len(".jsonl")] + ".meta.json").write_text(
                json.dumps({"agentType": row["process"]}), encoding="utf-8")
    for path, lines in files.items():
        _write(path, lines)
    return root


def test_cold_fixture_through_the_reader(tmp_path):
    fx = _cold_fixture()
    root = _write_fixture_logs(tmp_path / "projects", fx)
    calls = {c.key: c for c in read_calls(root).calls}
    for row in fx["calls"]:
        call = calls["msg_" + row["id"]]
        assert call.session == row["session"]  # subagent files: the parent folder's name
    for exp in fx["expected"]:
        assert calls["msg_" + exp["id"]].cache_cold is exp["cache_cold"], exp


def test_resumed_session_copies_continue_the_session(tmp_path):
    proj = tmp_path / "projects" / "p"
    a = _assistant("msg_A", "claude-opus-5-5", "2026-10-20T10:00:00.000Z",
                   _usage(cw=30000, w1=30000))
    b = _assistant("msg_B", "claude-opus-5-5", "2026-10-20T10:02:00.000Z",
                   _usage(cr=30000, cw=200, w1=200))
    c = _assistant("msg_C", "claude-opus-5-5", "2026-10-20T13:00:00.000Z",
                   _usage(cw=31000, w1=31000))
    _write(proj / "old-session.jsonl", [a, b])
    # Resumed 3 hours later: the earlier lines are copied (same times) into a new file.
    _write(proj / "new-session.jsonl", [a, b, c])
    calls = {x.key: x for x in read_calls(tmp_path / "projects").calls}
    assert len(calls) == 3 and calls["msg_A"].files == 2
    assert {x.session for x in calls.values()} == {"new-session"}
    assert calls["msg_A"].cache_cold is False and calls["msg_B"].cache_cold is False
    assert calls["msg_C"].cache_cold is True and calls["msg_C"].cold.reason == "expired"
    # The old file alone: nothing cold, its own session.
    (proj / "new-session.jsonl").unlink()
    calls = read_calls(tmp_path / "projects").calls
    assert {x.session for x in calls} == {"old-session"}
    assert not any(x.cache_cold for x in calls)


def test_window_start_keeps_the_previous_call(tmp_path):
    proj = tmp_path / "projects" / "p"
    _write(proj / "s.jsonl", [
        _assistant("msg_A", "claude-opus-5-5", "2026-10-20T10:00:00.000Z",
                   _usage(cw=30000, w1=30000)),
        _assistant("msg_C", "claude-opus-5-5", "2026-10-20T13:00:00.000Z",
                   _usage(cw=31000, w1=31000)),
    ])
    since = datetime(2026, 10, 20, 12, tzinfo=timezone.utc)
    calls = read_calls(tmp_path / "projects", since=since).calls
    assert [c.key for c in calls] == ["msg_C"] and calls[0].cache_cold is True


def test_cold_restarts_in_the_report(tmp_path, pricer):
    fx = _cold_fixture()
    root = _write_fixture_logs(tmp_path / "projects", fx)
    result = read_calls(root)
    doc = build_report(result.calls, pricer, days=30, now=NOW_DT,
                       since=datetime(2026, 9, 21, tzinfo=timezone.utc), files=result.files,
                       projects_dir="~/p")
    cold = doc["cold_restarts"]
    assert cold["count"] == fx["expected_totals"]["cold_restarts"]
    assert cold["main_sessions"] == 3 and cold["sessions_with_cold_restarts"] == 3
    assert {r["reason"]: r["count"] for r in cold["by_reason"]} == \
        fx["expected_totals"]["by_reason"]
    assert cold["unpriced"] == 0 and cold["est_cost_usd"] > 0
    assert sum(r["est_cost_usd"] for r in cold["by_reason"]) == pytest.approx(
        cold["est_cost_usd"])
    assert {r["session"] for r in cold["sessions"]} == {"Session 1", "Session 2", "Session 3"}
    assert len(cold["largest"]) == 5
    text = render_report(doc)
    assert "Cold cache restarts" in text and "8 cold restarts in 3 of 3 sessions" in text
    assert "Session 1" in text and "sess-" not in text and "sess-" not in json.dumps(doc)


def test_report_without_cold_restarts(projects, pricer):
    text = render_report(_report(projects, pricer))
    assert "Cold cache restarts" in text and "none in 2 sessions" in text


def test_session_id_never_leaves_the_machine(tmp_path, data_dir, capsys, monkeypatch):
    """Canary: the session (the log file's name) is in no event, output or upload."""
    from tests.test_monitor_upload import VALID, Opener
    from tryaii.monitor import upload

    fx = _cold_fixture()
    root = _write_fixture_logs(tmp_path / "projects", fx,
                               session_name=lambda s: f"{SESSION_CANARY}-{s}")
    opener = Opener({"/v1/apps/keys/validate": [(200, VALID)],
                     "/v1/monitor/events": [(202, {"accepted": 19, "batch_id": "b"})]})

    class Clock:
        t = 1000.0

        def monotonic(self):
            return self.t

        def sleep(self, seconds):
            self.t += seconds

    clock = Clock()
    real = upload.Uploader
    monkeypatch.setattr(upload, "Uploader",
                        lambda key, base: real(key, base, opener=opener, clock=clock.monotonic))
    monkeypatch.setattr(cli, "time", clock)
    monkeypatch.setenv("TRYAII_API_KEY", KEY)
    monkeypatch.setenv("TRYAII_API_URL", "https://api.test")
    outputs = []
    for argv in (("report", "--projects-dir", str(root), "--now", NOW),
                 ("report", "--json", "--projects-dir", str(root), "--now", NOW),
                 ("connect", "--dry-run", "--no-live", "--projects-dir", str(root),
                  "--now", NOW),
                 ("connect", "--yes", "--no-live", "--projects-dir", str(root), "--now", NOW),
                 ("status",), ("status", "--json")):
        code, out, err = _run(capsys, *argv)
        assert code == 0, err
        outputs.append(out + err)
    assert "Cold cache restarts" in outputs[0] and "cache_cold" in outputs[2]
    bodies = [c["data"].decode("utf-8") for c in opener.calls
              if c["path"] == "/v1/monitor/events"]
    assert bodies
    stored = _all_event_text(data_dir)
    for text in outputs + bodies + [stored]:
        assert SESSION_CANARY not in text
        assert "sess-a" not in text and "sess-b" not in text
    for path in data_dir.rglob("*"):
        assert SESSION_CANARY not in str(path)
        if path.is_file():
            assert SESSION_CANARY not in path.read_text(encoding="utf-8", errors="replace")
    events = list(map(json.loads, stored.splitlines()))
    sent = [e for b in bodies for e in json.loads(b)["events"]]
    assert len(sent) == len(events) == len(fx["calls"])
    by_event = {importer.request_event_id("req_msg_" + e["id"]): e["cache_cold"]
                for e in fx["expected"]}
    for e in sent:
        assert e["cache_cold"] is by_event[e["event_id"]]
        assert (e["cache_cold"] is None) == (e["process"] != "main")


# --- connect --no-session (S2) ------------------------------------------------------------

SESSION_VAR = "OTEL_METRICS_INCLUDE_SESSION_ID"


def _settings_doc(claude_dir):
    return json.loads(_settings_file(claude_dir).read_text(encoding="utf-8"))


def test_no_session_on_a_connected_setup_and_disconnect(keyed, claude_dir, capsys):
    from tryaii.claude_code import telemetry

    original = _write_settings(claude_dir, EXISTING)
    code, out, err = _run(capsys, "connect", "--live-only", "--yes")
    assert code == 0, err
    assert SESSION_VAR not in _settings_doc(claude_dir)["env"]
    code, out, err = _run(capsys, "status")
    assert "sessions: on: Claude Code sends its session id" in out
    assert json.loads(_run(capsys, "status", "--json")[1])["live"]["session_id_sent"] is True
    # Re-run on the connected setup with --no-session: only the one key is added.
    code, out, err = _run(capsys, "connect", "--live-only", "--no-session", "--dry-run")
    assert code == 0, err
    start = out.index("would add to")
    block = json.loads(out[out.index("{", start):out.index("\n}\n", start) + 2])
    assert block == {"env": {SESSION_VAR: "false"}}
    assert ("--no-session: OTEL_METRICS_INCLUDE_SESSION_ID=false, so Claude Code sends no "
            "session id") in out
    code, out, err = _run(capsys, "connect", "--live-only", "--no-session", "--yes")
    assert code == 0, err
    assert "added 1 settings" in out and "sends no session id" in out
    assert _settings_doc(claude_dir)["env"][SESSION_VAR] == "false"
    assert telemetry.read_state(keyed)["env"][SESSION_VAR] == "false"
    code, out, err = _run(capsys, "status")
    assert "live telemetry: connected" in out
    assert ("sessions: off (OTEL_METRICS_INCLUDE_SESSION_ID=false in settings.json): Claude "
            "Code sends no session id") in out
    live = json.loads(_run(capsys, "status", "--json")[1])["live"]
    assert live["session_id_sent"] is False and live["no_session_by_connect"] is True
    assert live["session_id_setting"] == {"variable": SESSION_VAR, "value": "false",
                                          "where": "settings.json"}
    # A plain connect afterwards leaves it in place.
    code, out, err = _run(capsys, "connect", "--live-only", "--yes")
    assert code == 0 and "already connected" in out
    assert _settings_doc(claude_dir)["env"][SESSION_VAR] == "false"
    code, out, err = _run(capsys, "disconnect", "--yes")
    assert code == 0, err
    assert SESSION_VAR in out
    assert _settings_file(claude_dir).read_bytes() == original  # exactly that key went too


def test_no_session_into_an_empty_claude_dir(keyed, claude_dir, capsys):
    code, out, err = _run(capsys, "connect", "--live-only", "--no-session", "--yes")
    assert code == 0, err
    doc = _settings_doc(claude_dir)
    assert doc["env"] == {**EXPECTED_BLOCK["env"], SESSION_VAR: "false"}
    assert list(doc["env"])[-1] == SESSION_VAR
    _run(capsys, "disconnect", "--yes")
    assert _settings_doc(claude_dir) == {}


def test_no_session_respects_the_users_own_value(keyed, claude_dir, capsys):
    from tryaii.claude_code import telemetry

    # Already off by the user: left alone, not recorded, so disconnect keeps it.
    _write_settings(claude_dir, {"env": {SESSION_VAR: "0"}})
    code, out, err = _run(capsys, "connect", "--live-only", "--no-session", "--yes")
    assert code == 0, err
    assert SESSION_VAR not in telemetry.read_state(keyed)["env"]
    assert "sessions: off" in _run(capsys, "status")[1]
    _run(capsys, "disconnect", "--yes")
    assert _settings_doc(claude_dir) == {"env": {SESSION_VAR: "0"}}
    # Turned on by the user: a conflict, nothing written.
    original = _write_settings(claude_dir, {"env": {SESSION_VAR: "true"}})
    code, out, err = _run(capsys, "connect", "--live-only", "--no-session", "--yes")
    assert code == 1 and f"sets {SESSION_VAR}=true" in out
    assert _settings_file(claude_dir).read_bytes() == original
    assert telemetry.read_state(keyed) is None
    # Without --no-session the user's value is left alone.
    code, out, err = _run(capsys, "connect", "--live-only", "--yes")
    assert code == 0, err
    assert ("sessions: on (OTEL_METRICS_INCLUDE_SESSION_ID=true in settings.json)"
            in _run(capsys, "status")[1])


def test_no_session_usage_and_environment(keyed, claude_dir, capsys, monkeypatch):
    from tryaii.claude_code import telemetry

    code, _out, err = _run(capsys, "connect", "--no-live", "--no-session")
    assert code == 2 and "--no-session" in err
    code, _out, err = _run(capsys, "status", "--no-session")
    assert code == 2 and "unknown option" in err
    assert "--no-session" in cli.HELP_CLAUDE_CODE
    assert telemetry.session_id_setting(None) == {"sent": True, "where": None, "value": None}
    monkeypatch.setenv(SESSION_VAR, "false")
    assert telemetry.session_id_setting(None) == {"sent": False, "where": "your environment",
                                                  "value": "false"}
    monkeypatch.setenv(SESSION_VAR, "")
    assert telemetry.session_id_setting(None)["sent"] is True


# --- sync: live advice (SPEC.md §10.9) ----------------------------------------------------

ANNOTATIONS = "/v1/monitor/claude-code/annotations"


def _sync_connected(keyed, capsys, monkeypatch, live_since):
    """Both parts of connect done; live telemetry 'turned on' at ``live_since``."""
    from tryaii.claude_code import telemetry

    code, out, err = _run(capsys, "connect", "--live-only", "--yes")
    assert code == 0, err
    assert cli.SYNC_HINT in out  # the one-line hint for users without the plugin
    state = telemetry.read_state(keyed)
    state["live_since"] = live_since
    telemetry.state_path(keyed).write_text(json.dumps(state), encoding="utf-8")
    importer.write_state(keyed, now=NOW, imported=0)


def _sync_opener(monkeypatch, answers):
    from types import SimpleNamespace

    from tests.test_monitor_upload import Opener
    from tryaii.monitor import api

    opener = Opener({ANNOTATIONS: answers})
    monkeypatch.setattr(api, "_OPENER", SimpleNamespace(open=opener))
    return opener


def _sent(opener):
    return [json.loads(c["data"].decode("utf-8")) for c in opener.posts()]


def _sync(capsys, projects, now, *extra):
    return _run(capsys, "sync", "--projects-dir", str(projects), "--now", now, *extra)


def _later(minutes):
    from datetime import timedelta

    from tryaii.monitor.events import format_ts

    return format_ts(NOW_DT + timedelta(minutes=minutes))


def test_sync_annotates_only_calls_after_live_since(projects, keyed, capsys, monkeypatch):
    from tryaii.claude_code import sync
    from tryaii.monitor.upload import find_forbidden

    _sync_connected(keyed, capsys, monkeypatch, "2026-10-20T11:30:00.000Z")
    opener = _sync_opener(monkeypatch, [(200, {"matched": 2, "unmatched": [],
                                               "advice_stored": 0})])
    code, out, err = _sync(capsys, projects, NOW)
    assert code == 0, err
    assert "2 calls annotated (2 matched" in out and "run tryaii claude-code analyze" in out
    (body,) = _sent(opener)
    assert body["schema"] == "tryaii.claude-code.annotations/1"
    assert body["sdk"]["lang"] == "python" and body["notice_version"] and body["advice"] == []
    calls = {c["event_id"]: c for c in body["calls"]}
    assert set(calls) == {importer.request_event_id("req_msg_E"),
                          importer.request_event_id("req_msg_C")}
    e = calls[importer.request_event_id("req_msg_E")]
    assert e == {"event_id": e["event_id"], "ts": "2026-10-20T12:00:00.000Z",
                 "project": "shop-app", "process": "subagent", "classification": None,
                 "winner": None, "est_cost_winner_usd": None, "taxonomy_version": None}
    assert calls[importer.request_event_id("req_msg_C")]["project"] == "billing"
    assert opener.posts()[0]["auth"] == f"Bearer {KEY}"
    raw = opener.posts()[0]["data"].decode("utf-8")
    assert CANARY not in raw and "alice" not in raw and find_forbidden(body) is None
    state = sync.read_state(keyed)
    assert len(state["applied"]) == 2 and state["last_ok_at"] == "2026-10-21T12:00:00.000Z"
    # Applied calls are not sent again.
    code, out, err = _sync(capsys, projects, _later(6))
    assert code == 0, err
    assert "0 calls annotated" in out and len(opener.posts()) == 1


def test_sync_leaves_unsettled_calls_for_the_next_run(projects, keyed, capsys, monkeypatch):
    _sync_connected(keyed, capsys, monkeypatch, "2026-10-20T11:30:00.000Z")
    opener = _sync_opener(monkeypatch, [(200, {"matched": 1, "unmatched": [],
                                               "advice_stored": 0})])
    code, out, err = _sync(capsys, projects, "2026-10-21T08:01:00.000Z")  # msg_C + 1 min
    assert code == 0, err
    assert [c["event_id"] for c in _sent(opener)[0]["calls"]] == [
        importer.request_event_id("req_msg_E")]


def test_sync_with_analysis_sends_profiles_and_advice(analyze_projects, keyed, pricer, capsys,
                                                      monkeypatch):
    from tryaii.claude_code import analysis

    _sync_connected(keyed, capsys, monkeypatch, "2026-10-20T09:15:00.000Z")
    consent = analysis.write_consent(keyed, now=NOW, method="--yes")
    doc = _analysis(analyze_projects, pricer)
    doc["consent"] = consent
    analysis.write_analysis(keyed, doc)
    opener = _sync_opener(monkeypatch, [(200, {"matched": 7, "unmatched": [],
                                               "advice_stored": 2})])
    code, out, err = _sync(capsys, analyze_projects, NOW)
    assert code == 0, err
    assert "task profiles from analyze" in out
    (body,) = _sent(opener)
    calls = {c["event_id"]: c for c in body["calls"]}
    e0 = calls[importer.request_event_id("req_msg_e0_0")]
    assert e0["process"] == "Explore" and e0["winner"] == HAIKU
    assert e0["classification"]["category"] == "EDUCATIONAL"
    assert e0["est_cost_winner_usd"] is not None
    assert importer.request_event_id("req_msg_m1") not in calls  # before live_since
    advice = {a["process"]: a for a in body["advice"]}
    explore = advice["Explore"]
    assert set(explore) == {"process", "thread", "window_days", "generated_at", "tasks",
                            "calls", "est_cost_usd", "classes", "good_enough_shares",
                            "recommendation", "confidence"}
    assert explore["recommendation"]["model"] == HAIKU and explore["window_days"] == 30
    assert set(explore["recommendation"]) == {"model", "alias", "good_enough_share",
                                              "saving_usd", "saving_usd_per_month"}
    raw = opener.posts()[0]["data"].decode("utf-8")
    assert CANARY not in raw and "frontmatter" not in raw and "alice" not in raw
    assert SECRET not in raw
    # The same analysis (same generated_at) is not sent twice.
    code, out, err = _sync(capsys, analyze_projects, _later(6))
    assert code == 0, err
    assert len(opener.posts()) == 1 and "0 process advice documents" in out


def test_sync_without_analyze_consent_sends_no_profile(analyze_projects, keyed, pricer, capsys,
                                                       monkeypatch):
    from tryaii.claude_code import analysis

    _sync_connected(keyed, capsys, monkeypatch, "2026-10-20T09:15:00.000Z")
    doc = _analysis(analyze_projects, pricer)
    analysis.write_analysis(keyed, doc)  # an analysis, but no (current) consent
    monkeypatch.setattr(cli, "_make_route_fn",
                        lambda bundle, config: pytest.fail("analyze ran without consent"))
    opener = _sync_opener(monkeypatch, [(200, {"matched": 7, "unmatched": [],
                                               "advice_stored": 0})])
    code, out, err = _sync(capsys, analyze_projects, NOW)
    assert code == 0, err
    (body,) = _sent(opener)
    assert body["advice"] == [] and body["calls"]
    assert all(c["classification"] is None and c["winner"] is None for c in body["calls"])


def test_sync_reruns_a_day_old_analysis_with_consent(analyze_projects, keyed, pricer, capsys,
                                                     monkeypatch):
    from tryaii.claude_code import analysis

    _sync_connected(keyed, capsys, monkeypatch, "2026-10-20T09:15:00.000Z")
    consent = analysis.write_consent(keyed, now=NOW, method="--yes")
    doc = _analysis(analyze_projects, pricer)
    doc["consent"], doc["created_at"] = consent, "2026-10-19T12:00:00.000Z"
    analysis.write_analysis(keyed, doc)
    route = FakeRouter()
    monkeypatch.setattr(cli, "_make_route_fn", lambda bundle, config: route)
    opener = _sync_opener(monkeypatch, [(200, {"matched": 7, "unmatched": [],
                                               "advice_stored": 2})])
    code, out, err = _sync(capsys, analyze_projects, NOW)
    assert code == 0, err
    assert route.texts and "a new analyze run" in out
    assert analysis.read_analysis(keyed)["created_at"] == "2026-10-21T12:00:00.000Z"
    assert _sent(opener)[0]["advice"]


def test_sync_retries_unmatched_for_a_day_then_drops(projects, keyed, capsys, monkeypatch):
    from tryaii.claude_code import sync

    e_id = importer.request_event_id("req_msg_E")
    _sync_connected(keyed, capsys, monkeypatch, "2026-10-20T11:30:00.000Z")
    opener = _sync_opener(monkeypatch, [(200, {"matched": 1, "unmatched": [e_id],
                                               "advice_stored": 0})])
    code, out, err = _sync(capsys, projects, NOW)
    assert code == 0, err
    assert "1 not found yet" in out and e_id in sync.read_state(keyed)["unmatched"]
    _sync(capsys, projects, _later(10))
    assert [c["event_id"] for c in _sent(opener)[1]["calls"]] == [e_id]  # retried alone
    _sync(capsys, projects, _later(25 * 60))
    state = sync.read_state(keyed)
    assert e_id in state["dropped"] and e_id not in state["unmatched"]
    count = len(opener.posts())
    _sync(capsys, projects, _later(26 * 60))
    assert len(opener.posts()) == count  # dropped: never sent again


def test_sync_batches_calls_and_advice():
    from tryaii.claude_code import sync

    parts = sync.batches(list(range(1201)), list(range(60)))
    assert [(len(c), len(a)) for c, a in parts] == [(500, 50), (500, 10), (201, 0)]
    assert sync.batches([], []) == []
    assert [(len(c), len(a)) for c, a in sync.batches([], [1])] == [(0, 1)]


def test_sync_sends_one_request_per_batch(tmp_path, keyed, capsys, monkeypatch):
    from tryaii.claude_code import sync

    root = tmp_path / "many"
    _write(root / "proj" / "s.jsonl", [
        _assistant(f"msg_{i}", "claude-sonnet-5-5",
                   f"2026-10-20T{10 + i // 3600:02d}:{i // 60 % 60:02d}:{i % 60:02d}.000Z",
                   _usage(inp=1, out=1)) for i in range(1100)])
    _sync_connected(keyed, capsys, monkeypatch, "2026-10-20T00:00:00.000Z")
    opener = _sync_opener(monkeypatch, [(200, {"matched": 500, "unmatched": [],
                                               "advice_stored": 0})])
    code, out, err = _sync(capsys, root, NOW)
    assert code == 0, err
    assert [len(b["calls"]) for b in _sent(opener)] == [500, 500, 100]
    assert len(sync.read_state(keyed)["applied"]) == 1100


def test_sync_lock_and_interval(projects, keyed, capsys, monkeypatch):
    from tryaii.claude_code import sync

    _sync_connected(keyed, capsys, monkeypatch, "2026-10-20T11:30:00.000Z")
    opener = _sync_opener(monkeypatch, [(200, {"matched": 2, "unmatched": [],
                                               "advice_stored": 0})])
    lock = sync.Lock(keyed)
    assert lock.acquire() and not sync.Lock(keyed).acquire()
    code, out, err = _sync(capsys, projects, NOW)
    assert code == 0 and "another sync is running" in out and not opener.posts()
    # A crashed run's lock goes stale.
    stale = sync.Lock(keyed, clock=lambda: 10 ** 12)
    assert stale.acquire()
    stale.release()
    code, out, err = _sync(capsys, projects, NOW)
    assert code == 0 and len(opener.posts()) == 1
    code, out, err = _sync(capsys, projects, _later(3))
    assert code == 0 and "at most once every 5 minutes" in out and len(opener.posts()) == 1
    code, out, err = _sync(capsys, projects, _later(3), "--quiet")
    assert (code, out, err) == (0, "", "")
    assert not sync.lock_path(keyed).exists()


def test_sync_background_detaches_and_returns(projects, keyed, capsys, monkeypatch):
    import os
    import subprocess

    from tryaii.claude_code import sync

    _sync_connected(keyed, capsys, monkeypatch, "2026-10-20T11:30:00.000Z")
    opener = _sync_opener(monkeypatch, [(200, {"matched": 0, "unmatched": [],
                                               "advice_stored": 0})])
    spawned = []
    monkeypatch.setattr(sync, "spawn_background", spawned.append)
    code, out, err = _sync(capsys, projects, NOW, "--background", "--quiet")
    assert (code, out, err) == (0, "", "") and not opener.posts()
    (args,) = spawned
    assert args[0] == "--quiet" and "--projects-dir" in args and "--background" not in args
    monkeypatch.undo()

    calls = []
    sync.spawn_background(["--quiet"], popen=lambda cmd, **kw: calls.append((cmd, kw)))
    ((cmd, kw),) = calls
    assert cmd[-3:] == ["claude-code", "sync", "--quiet"]
    assert kw["stdin"] is kw["stdout"] is kw["stderr"] is subprocess.DEVNULL
    if os.name == "nt":
        assert kw["creationflags"] == sync.DETACHED_PROCESS | sync.CREATE_NEW_PROCESS_GROUP
    else:
        assert kw["start_new_session"] is True


def test_sync_when_not_connected_exits_0(projects, data_dir, capsys, monkeypatch):
    from tryaii.claude_code import sync

    monkeypatch.setattr(sync, "spawn_background", lambda args: pytest.fail("spawned"))
    code, out, err = _sync(capsys, projects, NOW, "--background", "--quiet")
    assert (code, out, err) == (0, "", "")
    code, out, err = _sync(capsys, projects, NOW)
    assert code == 0 and "live telemetry is not connected" in out
    monkeypatch.setenv("TRYAII_API_KEY", KEY)
    monkeypatch.setenv("TRYAII_API_URL", API)
    _run(capsys, "connect", "--live-only", "--yes")
    code, out, err = _sync(capsys, projects, NOW)
    assert code == 0 and "log import is not connected" in out
    importer.write_state(data_dir, now=NOW, imported=0)
    monkeypatch.setenv("TRYAII_MONITOR_UPLOAD", "0")
    code, out, err = _sync(capsys, projects, NOW)
    assert code == 0 and "upload is off" in out


def test_sync_failure_is_quiet_with_quiet(projects, keyed, capsys, monkeypatch):
    from tryaii.claude_code import sync

    _sync_connected(keyed, capsys, monkeypatch, "2026-10-20T11:30:00.000Z")
    _sync_opener(monkeypatch, [(404, {"error": "not_found"})])
    code, out, err = _sync(capsys, projects, NOW, "--quiet")
    assert (code, out, err) == (0, "", "")
    state = sync.read_state(keyed)
    assert "404" in state["last_error"] and not state["applied"]
    code, out, err = _sync(capsys, projects, _later(6))
    assert code == 1 and "404" in err and KEY not in err


def test_sync_uses_the_connected_api_without_env(projects, keyed, capsys, monkeypatch):
    """A SessionEnd hook runs without the user's TRYAII_* variables: the key
    and the API come from what connect stored."""
    _sync_connected(keyed, capsys, monkeypatch, "2026-10-20T11:30:00.000Z")
    monkeypatch.delenv("TRYAII_API_KEY")
    monkeypatch.delenv("TRYAII_API_URL")
    opener = _sync_opener(monkeypatch, [(200, {"matched": 2, "unmatched": [],
                                               "advice_stored": 0})])
    code, out, err = _sync(capsys, projects, NOW)
    assert code == 0, err
    assert opener.posts()[0]["auth"] == f"Bearer {KEY}"


def test_sync_payload_canary():
    from tryaii.claude_code import sync

    assert sync.project_value("shop-app") == "shop-app"
    for bad in ("C:\\Users\\alice\\shop", "/home/alice/x", "a:b", "unknown", "", None,
                "x" * 300, "tab\there"):
        assert sync.project_value(bad) is None
    good = sync.body([{"event_id": "e", "ts": NOW, "project": "p", "process": "main",
                       "classification": None, "winner": None, "est_cost_winner_usd": None,
                       "taxonomy_version": None}], [], "0")
    sync.check_payload(good)
    for planted in ({"text": f"prompt {CANARY}"}, {"content": "def f(): pass"},
                    {"Prompt": CANARY},
                    {"project": "C:\\Users\\alice\\code"}):
        doc = json.loads(json.dumps(good))
        doc["calls"][0].update(planted)
        with pytest.raises(sync.SyncError):
            sync.check_payload(doc)


PLUGIN = Path(__file__).resolve().parents[3] / "integrations" / "claude-code-plugin"


def test_plugin_session_end_hook_always_exits_0(tmp_path):
    import os
    import shutil
    import subprocess

    doc = json.loads((PLUGIN / "hooks" / "hooks.json").read_text(encoding="utf-8"))
    (group,) = doc["hooks"]["SessionEnd"]
    (hook,) = group["hooks"]
    assert hook["type"] == "command" and "matcher" not in group
    assert hook["command"] == "tryaii claude-code sync --background --quiet; exit 0"
    manifest = json.loads((PLUGIN / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    assert manifest["version"] == "0.2.0"
    # Without tryaii on PATH the command still exits 0 (sh / Git Bash, and PowerShell).
    bash = shutil.which("bash")
    powershell = shutil.which("powershell") or shutil.which("pwsh")
    env = {k: v for k, v in os.environ.items() if k.upper() != "PATH"}
    env["PATH"] = str(tmp_path)
    if bash:
        done = subprocess.run([bash, "-c", hook["command"]], env=env, capture_output=True,
                              timeout=60)
        assert done.returncode == 0
    if powershell:
        done = subprocess.run([powershell, "-NoProfile", "-NonInteractive", "-Command",
                               hook["command"]], env=env, capture_output=True, timeout=60)
        assert done.returncode == 0
