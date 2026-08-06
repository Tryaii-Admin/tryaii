"""Tests for the cache_lint="warn" SDK hook (pre-flight lint + verification).

The hook is warn-only and fail-open: nothing it does may ever break the
user's API call. These tests pin the trigger set, the once-per-shape dedup,
the first-call-vs-repeat-call verification logic, every fail-open path, and
the EXACT warning literals — the Node suite (cachelint-hook.test.ts) contains
the same literals, which is how cross-SDK message parity is enforced (no
golden fixtures for the hook).

Wiring tests fake the HTTP layer by presetting the integration's lazily
created client attribute with a hand-written fake (house style: no mock
library, see FakeSentenceTransformer / FakeStdout precedents).
"""

from __future__ import annotations

import pytest

from tryaii.cachelint import hook as hook_mod
from tryaii.cachelint.hook import CacheLintHook

BIG = "You are the routing assistant for a logistics platform. " * 40
UUID = "7f9c02aa-51b3-4c2e-9f10-8a4d55e01b27"

# Shapes used across tests (verdicts verified against the merged engine).
TINY_FABLE = ("anthropic/claude-fable-5",
              [{"role": "user", "content": "Short prompt."}])
EARLY_UUID_FABLE = ("anthropic/claude-fable-5",
                    [{"role": "system", "content": f"Session {UUID}. " + BIG},
                     {"role": "user", "content": "Question?"}])
CLEAN_DEEPSEEK = ("deepseek/deepseek-chat",
                  [{"role": "user", "content": "Hello there, tell me about Oslo."}])
UUID_DEEPSEEK = ("deepseek/deepseek-chat",
                 [{"role": "user", "content": f"Continue session {UUID}."}])
CACHEABLE_GEMINI = ("google/gemini-2.5-pro",
                    [{"role": "system", "content": BIG * 4},
                     {"role": "user", "content": "Q?"}])

BELOW_LINE = (
    "[tryaii cachelint] BELOW_THRESHOLD for anthropic/claude-fable-5: "
    "Total prompt ~5 tok < 512 minimum — nothing will cache. "
    "Expanding the static content to reach the floor is often worthwhile."
)
NOTE_LINE = (
    "[tryaii cachelint] note: reported once per unique prompt shape per client; "
    'set cache_lint="off" or TRYAII_CACHE_LINT=off to disable'
)
BLOCKER_LINE = (
    "[tryaii cachelint]   first blocker: uuid at messages[0]:system+16 (0% in)"
)
VERIFY_MISS_LINE = (
    "[tryaii cachelint] VERIFY_MISS for google/gemini-2.5-pro: predicted ~2244 tok "
    "cacheable prefix, but usage reports 0 cached tokens on repeat call "
    "#2 of this prompt shape — a silent invalidator or missing "
    "provider support may be the cause"
)


@pytest.fixture()
def sink():
    return []


@pytest.fixture()
def hook(sink):
    return CacheLintHook(sink=sink.append)


class TestModeResolution:
    def _build(self, value):
        from tryaii.integrations.openrouter import _build_cache_lint_hook

        return _build_cache_lint_hook(value)

    def test_explicit_warn_builds_hook(self):
        assert isinstance(self._build("warn"), CacheLintHook)

    def test_explicit_off_and_default_are_none(self, monkeypatch):
        monkeypatch.delenv("TRYAII_CACHE_LINT", raising=False)
        assert self._build("off") is None
        assert self._build(None) is None

    def test_explicit_invalid_raises(self):
        with pytest.raises(ValueError, match='cache_lint must be "off" or "warn"'):
            self._build("strict")

    def test_env_enables_when_unset(self, monkeypatch):
        monkeypatch.setenv("TRYAII_CACHE_LINT", "WARN")
        assert isinstance(self._build(None), CacheLintHook)

    def test_env_garbage_is_off_not_error(self, monkeypatch):
        monkeypatch.setenv("TRYAII_CACHE_LINT", "banana")
        assert self._build(None) is None

    def test_explicit_off_beats_env_warn(self, monkeypatch):
        monkeypatch.setenv("TRYAII_CACHE_LINT", "warn")
        assert self._build("off") is None


class TestPreflight:
    def test_below_threshold_warns_with_exact_literal(self, hook, sink):
        key = hook.preflight(*TINY_FABLE)
        assert key is not None
        assert sink == [BELOW_LINE, NOTE_LINE]

    def test_early_uuid_warns_with_blocker_line(self, hook, sink):
        hook.preflight(*EARLY_UUID_FABLE)
        assert sink[0].startswith(
            "[tryaii cachelint] EFFECTIVELY_UNCACHEABLE for anthropic/claude-fable-5:")
        assert sink[1] == BLOCKER_LINE

    def test_unknown_threshold_clean_is_silent(self, hook, sink):
        # deepseek slug -> UNKNOWN_THRESHOLD is a knowledge-base gap, not a
        # prompt problem; without blockers it must stay silent.
        assert hook.preflight(*CLEAN_DEEPSEEK) is not None
        assert sink == []

    def test_unknown_threshold_with_blocker_warns(self, hook, sink):
        hook.preflight(*UUID_DEEPSEEK)
        assert sink and sink[0].startswith(
            "[tryaii cachelint] UNKNOWN_THRESHOLD for deepseek/deepseek-chat:")
        assert any("first blocker: uuid" in line for line in sink)

    def test_cacheable_is_silent(self, hook, sink):
        assert hook.preflight(*CACHEABLE_GEMINI) is not None
        assert sink == []

    def test_note_line_shown_exactly_once(self, hook, sink):
        hook.preflight(*TINY_FABLE)
        hook.preflight(*UUID_DEEPSEEK)      # second distinct warning shape
        assert sink.count(NOTE_LINE) == 1


class TestDedup:
    def test_same_shape_analyzed_once(self, hook, sink, monkeypatch):
        calls = {"n": 0}
        real_analyze = None

        def counting_analyze(data):
            calls["n"] += 1
            return real_analyze(data)

        from tryaii import cachelint as engine

        real_analyze = engine.analyze
        monkeypatch.setattr(engine, "analyze", counting_analyze)

        k1 = hook.preflight(*TINY_FABLE)
        k2 = hook.preflight(*TINY_FABLE)
        assert k1 == k2
        assert calls["n"] == 1
        assert sink.count(BELOW_LINE) == 1          # warned once, not twice
        assert hook._seen[k1].calls == 2

    def test_model_slug_is_part_of_the_key(self, hook):
        k1 = hook.preflight("anthropic/claude-fable-5", TINY_FABLE[1])
        k2 = hook.preflight("openai/gpt-5.2", TINY_FABLE[1])
        assert k1 != k2

    def test_fifo_cap(self, hook, monkeypatch):
        monkeypatch.setattr(hook_mod, "_MAX_SHAPES", 3)
        for i in range(5):
            hook.preflight("deepseek/deepseek-chat",
                           [{"role": "user", "content": f"prompt variant {i}"}])
        assert len(hook._seen) == 3

    def test_stale_shape_resets_call_counter(self, sink):
        clock = {"t": 0.0}
        hook = CacheLintHook(sink=sink.append, clock=lambda: clock["t"])
        k = hook.preflight(*CACHEABLE_GEMINI)
        clock["t"] = 4000.0                          # > _STALE_AFTER_S window
        hook.preflight(*CACHEABLE_GEMINI)
        assert hook._seen[k].calls == 1              # treated as a first call again
        hook.verify(k, {"cached_tokens": 0})
        assert sink == []                            # no VERIFY_MISS on a "first" call


class TestFailOpen:
    def test_engine_error_returns_none_silently(self, hook, sink, monkeypatch):
        from tryaii import cachelint as engine

        monkeypatch.setattr(engine, "analyze",
                            lambda data: (_ for _ in ()).throw(RuntimeError("boom")))
        assert hook.preflight(*TINY_FABLE) is None
        assert sink == []

    def test_import_error_emits_hint_once(self, hook, sink, monkeypatch):
        from tryaii import cachelint as engine

        def raising(data):
            raise ImportError(
                "tiktoken is required for cachelint. "
                "Install with: pip install tryaii[cachelint]")

        monkeypatch.setattr(engine, "analyze", raising)
        hook.preflight("openai/gpt-5.2", [{"role": "user", "content": "hello one"}])
        hook.preflight("openai/gpt-5.2", [{"role": "user", "content": "hello two"}])
        hints = [line for line in sink if "install tryaii[cachelint]" in line]
        assert len(hints) == 1
        assert hints[0] == (
            "[tryaii cachelint] note: cache lint skipped for an OpenAI/xAI-routed "
            "prompt — install tryaii[cachelint] to enable exact token analysis")

    def test_broken_sink_is_swallowed(self):
        def broken(_line):
            raise OSError("stderr is gone")

        hook = CacheLintHook(sink=broken)
        assert hook.preflight(*TINY_FABLE) is not None   # no exception escapes

    def test_verify_never_raises(self, hook):
        hook.verify("not-a-known-key", {"bad": "usage"})
        hook.verify(None, None)
        hook.verify(object(), 42)                        # type: ignore[arg-type]


class TestVerify:
    def _prime(self, hook, calls=2):
        key = None
        for _ in range(calls):
            key = hook.preflight(*CACHEABLE_GEMINI)
        return key

    def test_first_call_zero_cache_is_expected_miss(self, hook, sink):
        key = self._prime(hook, calls=1)
        hook.verify(key, {"prompt_tokens_details": {"cached_tokens": 0}})
        assert sink == []

    def test_second_call_zero_cache_warns_exact_literal(self, hook, sink):
        key = self._prime(hook, calls=2)
        hook.verify(key, {"prompt_tokens_details": {"cached_tokens": 0}})
        assert sink == [VERIFY_MISS_LINE]

    def test_verify_warns_once_per_shape(self, hook, sink):
        key = self._prime(hook, calls=2)
        hook.verify(key, {"cached_tokens": 0})
        hook.preflight(*CACHEABLE_GEMINI)
        hook.verify(key, {"cached_tokens": 0})
        assert len(sink) == 1

    def test_positive_cache_is_silent(self, hook, sink):
        key = self._prime(hook, calls=2)
        hook.verify(key, {"prompt_tokens_details": {"cached_tokens": 1800}})
        hook.verify(key, {"cache_discount": 0.42})
        assert sink == []

    def test_unverifiable_usage_is_silent(self, hook, sink):
        key = self._prime(hook, calls=2)
        hook.verify(key, {})
        hook.verify(key, None)
        hook.verify(key, {"prompt_tokens_details": "not-a-dict"})
        hook.verify(key, {"cached_tokens": True})        # bool is not a count
        assert sink == []

    def test_uncacheable_shapes_are_never_verified(self, hook, sink):
        key = hook.preflight(*TINY_FABLE)                # BELOW_THRESHOLD
        hook.preflight(*TINY_FABLE)
        del sink[:]
        hook.verify(key, {"cached_tokens": 0})
        assert sink == []                                # expect_cache is False


# ---------------------------------------------------------------------------
# Wiring: the integration calls the hook around a faked HTTP layer
# ---------------------------------------------------------------------------

CHAT_BODY = {
    "choices": [{"message": {"content": "Oslo is served by OSL-2."}}],
    "usage": {"prompt_tokens_details": {"cached_tokens": 0}, "total_tokens": 42},
}


class FakeResponse:
    def __init__(self, body):
        self._body = body
        self.status_code = 200

    def json(self):
        return self._body

    def raise_for_status(self):
        pass


class FakeHttpxClient:
    def __init__(self, body):
        self._body = body
        self.requests = []

    def request(self, method, url, **kwargs):
        self.requests.append((method, url, kwargs))
        return FakeResponse(self._body)


class TestSyncWiring:
    def _integration(self, cache_lint):
        from tryaii.integrations.openrouter import OpenRouterIntegration

        integ = OpenRouterIntegration(router=None, api_key="k", cache_lint=cache_lint)
        # Preset the lazily created client so _ensure_client returns early --
        # no httpx, no network (house style: hand-written fake + attr override).
        integ._client = FakeHttpxClient(CHAT_BODY)
        return integ

    def test_chat_warns_via_default_stderr_sink(self, capsys):
        integ = self._integration("warn")
        resp = integ.chat("Short prompt.", override_model="anthropic/claude-fable-5")
        assert resp.content == "Oslo is served by OSL-2."
        err = capsys.readouterr().err
        assert BELOW_LINE in err

    def test_chat_with_broken_engine_still_succeeds(self, capsys, monkeypatch):
        from tryaii import cachelint as engine

        monkeypatch.setattr(engine, "analyze",
                            lambda data: (_ for _ in ()).throw(RuntimeError("boom")))
        integ = self._integration("warn")
        resp = integ.chat("Short prompt.", override_model="anthropic/claude-fable-5")
        assert resp.content == "Oslo is served by OSL-2."
        assert "[tryaii cachelint]" not in capsys.readouterr().err

    def test_off_is_completely_silent(self, capsys):
        integ = self._integration("off")
        integ.chat("Short prompt.", override_model="anthropic/claude-fable-5")
        assert capsys.readouterr().err == ""

    def test_repeat_cacheable_chat_verify_misses(self, capsys):
        integ = self._integration("warn")
        for _ in range(2):
            integ.chat("Q?", system_message=BIG * 4,
                       override_model="google/gemini-2.5-pro")
        err = capsys.readouterr().err
        assert "VERIFY_MISS for google/gemini-2.5-pro" in err


class FakeAsyncResponse(FakeResponse):
    pass


class FakeAsyncHttpxClient:
    def __init__(self, body):
        self._body = body

    async def post(self, url, **kwargs):
        return FakeAsyncResponse(self._body)


class TestAsyncWiring:
    async def test_async_chat_warns_and_succeeds(self, capsys, monkeypatch):
        from tryaii import async_client as ac

        client = object.__new__(ac.AsyncDREClient)   # skip Router construction
        client._api_key = "k"
        client._default_priorities = None
        client._http_client = FakeAsyncHttpxClient(CHAT_BODY)
        client._cache_lint = CacheLintHook()

        async def fake_route(prompt, priorities=None, top_k=5):
            class R:
                best_model = "claude-fable-5"
                scores = []
            return R()

        monkeypatch.setattr(client, "route", fake_route)
        resp = await client.chat("Short prompt.")
        assert resp.content == "Oslo is served by OSL-2."
        assert BELOW_LINE in capsys.readouterr().err
