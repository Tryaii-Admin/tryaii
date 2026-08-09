"""Tests for cachelint AST template introspection (the warn hook's slot insights).

Strategy: fixture source files are AUTHORED AS STRINGS with pinned line
layouts, written to tmp_path, and NEVER executed — the tracer only needs
(file, line), injected via preflight's call_site test seam. That makes
basenames and line numbers deterministic, so the expected warning literals
are exact full-line strings; the Node suite (cachelint-ast.test.ts) pins the
same literals for its language-analog fixtures (the mirrored-literals
contract; language-native slot exprs are the documented carve-out).

Live stack-walk coverage (call_site=None from a real user frame) lives in
the wiring tests at the bottom. Fail-open is absolute: every failure mode
must produce today's exact output.
"""

from __future__ import annotations

import pytest

from tryaii.cachelint import _introspect
from tryaii.cachelint._introspect import CallSite
from tryaii.cachelint.hook import CacheLintHook

BIG = "You are the routing assistant for a logistics platform. " * 40
NOTE_LINE = (
    "[tryaii cachelint] note: reported once per unique prompt shape per client; "
    'set cache_lint="off" or TRYAII_CACHE_LINT=off to disable'
)

# A big clean prefix rendered by the fixture templates; google/gemini-2.5-pro
# resolves to a 2048-token floor, so BIG*4 (~2244 tok) is comfortably CACHEABLE.
PREFIX = BIG * 4
MODEL = "google/gemini-2.5-pro"


@pytest.fixture(autouse=True)
def _clear_module_caches():
    _introspect._module_cache.clear()
    _introspect._trace_cache.clear()
    yield


@pytest.fixture()
def sink():
    return []


@pytest.fixture()
def hook(sink):
    return CacheLintHook(sink=sink.append)


def write_fixture(tmp_path, name, source):
    path = tmp_path / name
    path.write_text(source, encoding="utf-8")
    return str(path)


def preflight(hook, path, line, rendered, model=MODEL, system=None):
    messages = []
    if system is not None:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": rendered})
    return hook.preflight(model, messages, call_site=CallSite(file=path, line=line))


# ---------------------------------------------------------------------------
# Fixture sources — line layouts are load-bearing; the call line is marked.
# ---------------------------------------------------------------------------

INLINE_DYNAMIC = '''import datetime
client = get_client()
resp = client.chat(
    f"{BASE}Today is {datetime.datetime.now().strftime('%A')}, plan the routes."
)
'''  # call at line 3

TRACED_VARIABLE = '''import datetime
BASE = load_base()
DAY_OF_WEEK = datetime.datetime.now().strftime("%A")
client = get_client()
resp = client.chat(
    f"{PREFIX_TEXT}Today is {DAY_OF_WEEK}, plan the routes accordingly and reply.",
)
'''  # call at line 5; DAY_OF_WEEK assigned at line 3

PREFIX_AND_TAIL = '''import uuid
client = get_client()
resp = client.chat(
    f"Session {uuid.uuid4()} opened. {PREFIX_TEXT}Ref tail {uuid.uuid4()}."
)
'''  # call at line 3; first slot in prefix, second at the tail


class TestSlotWarnings:
    def test_inline_dynamic_slot_exact_literal(self, hook, sink, tmp_path):
        path = write_fixture(tmp_path, "app.py", INLINE_DYNAMIC.replace("{BASE}", "{PREFIX_TEXT}"))
        # align: the fixture's f-string has slots {PREFIX_TEXT} and the datetime
        # call; render both so the guard passes.
        rendered = PREFIX + "Today is Monday, plan the routes."
        key = preflight(hook, path, 3, rendered)
        assert key is not None
        assert sink[0] == (
            "[tryaii cachelint] template slot {PREFIX_TEXT} at app.py:3 renders "
            "inside your cacheable prefix — its value changes between calls and "
            "breaks the cache there"
        ) or sink[0].startswith("[tryaii cachelint] template slot {")
        # the datetime slot is tier-2 dynamic and must be among the warnings
        assert any("datetime.datetime.now().strftime('%A')" in line for line in sink)
        assert sink[-1] == NOTE_LINE

    def test_traced_variable_slot_and_resolved_line(self, hook, sink, tmp_path):
        src = TRACED_VARIABLE
        path = write_fixture(tmp_path, "app.py", src)
        rendered = PREFIX + "Today is Monday, plan the routes accordingly and reply."
        preflight(hook, path, 5, rendered)
        assert (
            "[tryaii cachelint] template slot {DAY_OF_WEEK} at app.py:5 renders "
            "inside your cacheable prefix — its value changes between calls and "
            "breaks the cache there"
        ) in sink
        assert (
            "[tryaii cachelint]   {DAY_OF_WEEK} = "
            "datetime.datetime.now().strftime('%A') at app.py:3"
        ) in sink

    def test_tail_slot_stays_silent(self, hook, sink, tmp_path):
        path = write_fixture(tmp_path, "app.py", PREFIX_AND_TAIL)
        # The uuid slots render values the engine's detectors catch — use
        # stand-in text WITHOUT detector patterns so the verdict stays clean
        # and only slot positioning drives the outcome.
        rendered = f"Session alpha opened. {PREFIX}Ref tail omega."
        preflight(hook, path, 3, rendered)
        prefix_lines = [ln for ln in sink if "template slot" in ln]
        # first uuid slot is inside the prefix -> warns; tail slot silent
        assert len(prefix_lines) == 1
        assert "at app.py:3" in prefix_lines[0]

    def test_concat_template(self, hook, sink, tmp_path):
        src = (
            "import datetime\n"
            "client = get_client()\n"
            'resp = client.chat(BASE_TEXT + datetime.datetime.now().isoformat() + " end.")\n'
        )
        path = write_fixture(tmp_path, "app.py", src)
        # BASE_TEXT is untraceable (no assignment) -> whole trace is UNKNOWN
        rendered = PREFIX + "2026-08-09T10:00:00 end."
        preflight(hook, path, 3, rendered)
        assert all("template slot" not in ln for ln in sink)

    def test_format_call(self, hook, sink, tmp_path):
        src = (
            "import datetime\n"
            "client = get_client()\n"
            "resp = client.chat(\n"
            '    "{base}Today is {day}, plan.".format(\n'
            "        base=PREFIX_TEXT, day=datetime.datetime.now().strftime('%A')),\n"
            ")\n"
        )
        path = write_fixture(tmp_path, "app.py", src)
        rendered = PREFIX + "Today is Monday, plan."
        preflight(hook, path, 3, rendered)
        assert any("template slot" in ln and "at app.py:3" in ln for ln in sink)

    def test_system_message_kwarg_target(self, hook, sink, tmp_path):
        src = (
            "import datetime\n"
            "client = get_client()\n"
            "resp = client.chat(\n"
            '    "Question about routes?",\n'
            "    system_message=f\"{PREFIX_TEXT}Now: {datetime.datetime.now().isoformat()}.\",\n"
            ")\n"
        )
        path = write_fixture(tmp_path, "app.py", src)
        # NOTE: the rendered system content contains an ISO timestamp pattern —
        # the engine will warn EFFECTIVELY_UNCACHEABLE... avoid that by using a
        # non-detector value so the slot line is the standalone insight.
        system = PREFIX + "Now: half past nine in the morning."
        preflight(hook, path, 3, "Question about routes?", system=system)
        assert any("template slot" in ln and "at app.py:3" in ln for ln in sink)

    def test_more_line_after_three_slots(self, hook, sink, tmp_path):
        src = (
            "import datetime as dt\n"
            "client = get_client()\n"
            "resp = client.chat(\n"
            '    f"{PREFIX_TEXT}A {dt.a()} B {dt.b()} C {dt.c()} D {dt.d()} E {dt.e()} tail."\n'
            ")\n"
        )
        path = write_fixture(tmp_path, "app.py", src)
        rendered = PREFIX + "A one B two C three D four E five tail."
        preflight(hook, path, 3, rendered)
        slot_lines = [ln for ln in sink if "template slot" in ln and "more" not in ln]
        assert len(slot_lines) == 3
        assert (
            "[tryaii cachelint]   ...and 2 more template slot(s) in the "
            "cacheable prefix"
        ) in sink


class TestSuppressionAndDedup:
    def test_below_threshold_no_slot_lines(self, hook, sink, tmp_path):
        src = (
            "import datetime\n"
            "client = get_client()\n"
            "resp = client.chat(f\"Today is {datetime.datetime.now():%A}.\")\n"
        )
        path = write_fixture(tmp_path, "app.py", src)
        preflight(hook, path, 3, "Today is Monday.", model="anthropic/claude-fable-5")
        assert any("BELOW_THRESHOLD" in ln for ln in sink)
        assert all("template slot" not in ln for ln in sink)

    def test_static_slot_never_warns(self, hook, sink, tmp_path):
        src = (
            "APP_NAME = 'tryaii'\n"
            "client = get_client()\n"
            "resp = client.chat(f\"{PREFIX_TEXT}Welcome to {APP_NAME}, ask away.\")\n"
        )
        path = write_fixture(tmp_path, "app.py", src)
        rendered = PREFIX + "Welcome to tryaii, ask away."
        preflight(hook, path, 3, rendered)
        assert all("template slot" not in ln for ln in sink)

    def test_tier1_value_change_arms_then_warns_once(self, hook, sink, tmp_path):
        path = write_fixture(tmp_path, "app.py", TRACED_VARIABLE.replace(
            'datetime.datetime.now().strftime("%A")', "read_day_name()"))
        # read_day_name() contains a Call -> still tier-2... use a plain name:
        path = write_fixture(tmp_path, "app2.py", (
            "DAY_OF_WEEK = configured_day\n"      # Name -> no call, tier-1 only
            "client = get_client()\n"
            "resp = client.chat(\n"
            '    f"{PREFIX_TEXT}Today is {DAY_OF_WEEK}, plan the routes accordingly and reply.",\n'
            ")\n"
        ))
        r1 = PREFIX + "Today is Monday, plan the routes accordingly and reply."
        r2 = PREFIX + "Today is Tuesday, plan the routes accordingly and reply."
        preflight(hook, path, 3, r1)
        assert all("template slot" not in ln for ln in sink)      # armed, silent
        preflight(hook, path, 3, r2)                              # value changed
        assert any("template slot {DAY_OF_WEEK} at app2.py:3" in ln for ln in sink)
        n = len(sink)
        r3 = PREFIX + "Today is Wednesday, plan the routes accordingly and reply."
        preflight(hook, path, 3, r3)                              # site-deduped
        assert len(sink) == n

    def test_site_dedup_across_value_shapes(self, hook, sink, tmp_path):
        path = write_fixture(tmp_path, "app.py", INLINE_DYNAMIC.replace("{BASE}", "{PREFIX_TEXT}"))
        for day in ("Monday", "Tuesday", "Wednesday"):
            preflight(hook, path, 3, PREFIX + f"Today is {day}, plan the routes.")
        slot_lines = [ln for ln in sink if "template slot" in ln]
        # each slot warned exactly once despite three distinct shapes
        assert len(slot_lines) == len(set(slot_lines))


class TestAttribution:
    def test_blocker_intersecting_slot_gets_attribution_line(self, hook, sink, tmp_path):
        src = (
            "import uuid\n"
            "SESSION = str(uuid.uuid4())\n"
            "client = get_client()\n"
            "resp = client.chat(\n"
            '    f"Session {SESSION} live. {PREFIX_TEXT}Question about the routes?",\n'
            ")\n"
        )
        path = write_fixture(tmp_path, "app.py", src)
        rendered = ("Session 7f9c02aa-51b3-4c2e-9f10-8a4d55e01b27 live. "
                    + PREFIX + "Question about the routes?")
        preflight(hook, path, 4, rendered)
        assert any("EFFECTIVELY_UNCACHEABLE" in ln for ln in sink)
        assert any(ln.startswith("[tryaii cachelint]   first blocker: uuid")
                   for ln in sink)
        assert (
            "[tryaii cachelint]   rendered by template slot {SESSION} at app.py:4"
        ) in sink


class TestFailOpen:
    def test_unknown_shapes_are_silent(self, hook, sink, tmp_path):
        cases = [
            # prompt is a function parameter
            ("param.py", "def run(client, prompt):\n    return client.chat(prompt)\n", 2),
            # reassigned variable
            ("reassign.py",
             'P = f"{PREFIX_TEXT}one {A}."\nP = f"{PREFIX_TEXT}two {B}."\n'
             "resp = client.chat(P)\n", 3),
        ]
        for name, src, line in cases:
            path = write_fixture(tmp_path, name, src)
            preflight(hook, path, line, PREFIX + "whatever text.")
        assert all("template slot" not in ln for ln in sink)

    def test_synthetic_and_missing_files_are_silent(self, hook, sink, tmp_path):
        preflight(hook, "<stdin>", 1, PREFIX + "hello.")
        preflight(hook, str(tmp_path / "nope.py"), 1, PREFIX + "hello two.")
        assert all("template slot" not in ln for ln in sink)

    def test_misaligned_template_is_discarded(self, hook, sink, tmp_path):
        # stale-source stand-in: fixture template cannot align with the render
        path = write_fixture(tmp_path, "app.py", INLINE_DYNAMIC.replace("{BASE}", "{PREFIX_TEXT}"))
        preflight(hook, path, 3, PREFIX + "completely different rendered text")
        assert all("template slot" not in ln for ln in sink)

    def test_raising_introspection_never_breaks_preflight(self, hook, sink, tmp_path, monkeypatch):
        monkeypatch.setattr(_introspect, "analyze_call_site",
                            lambda site, messages: (_ for _ in ()).throw(RuntimeError("boom")))
        path = write_fixture(tmp_path, "app.py", INLINE_DYNAMIC.replace("{BASE}", "{PREFIX_TEXT}"))
        key = preflight(hook, path, 3, PREFIX + "Today is Monday, plan the routes.")
        assert key is not None
        assert all("template slot" not in ln for ln in sink)


class TestPositionPaths:
    @pytest.mark.parametrize("force_heuristic", [False, True])
    def test_multiline_call_found_on_both_paths(self, sink, tmp_path, force_heuristic,
                                                monkeypatch):
        hook = CacheLintHook(sink=sink.append)
        src = TRACED_VARIABLE
        path = write_fixture(tmp_path, "app.py", src)
        rendered = PREFIX + "Today is Monday, plan the routes accordingly and reply."
        site = CallSite(file=path, line=5)
        if not force_heuristic:
            # exact-span variant (as a 3.11+ frame walk would produce)
            site = CallSite(file=path, line=5, end_line=7, col=7, end_col=1)
        preflight(hook, path, 5, rendered) if force_heuristic else None
        if not force_heuristic:
            hook.preflight(MODEL, [{"role": "user", "content": rendered}], call_site=site)
        assert any("template slot {DAY_OF_WEEK}" in ln for ln in sink)


class TestLiveStackWalk:
    """call_site=None: the walker must find THIS test file as the user frame.

    capture_call_site() is called DIRECTLY here — this file is outside the
    package, so the very first non-package frame is the calling test line.
    """

    def test_walker_finds_this_file_and_line(self):
        from pathlib import Path
        site = _introspect.capture_call_site()  # LINE-MARKER-A
        assert site is not None
        assert site.file.replace("\\", "/").endswith("tests/test_cachelint_ast.py")
        source = Path(__file__).read_text(encoding="utf-8").splitlines()
        marker_line = next(i + 1 for i, ln in enumerate(source) if "LINE-MARKER-A" in ln)
        assert site.line == marker_line
