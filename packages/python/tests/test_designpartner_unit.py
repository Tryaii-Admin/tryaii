"""Unit tests for the designpartner engine — edges fixtures can't carry.

House style: no mock libraries. The transport is faked with a hand-written
opener object injected via the `opener=` seam; advance() flows run in real
temp dirs with injected diagnose loaders and senders.
"""

from __future__ import annotations

import json

import pytest

from tryaii.designpartner import advance, load_catalog, send_submission
from tryaii.designpartner.catalog import all_questions

OPTS = {"now": "2026-08-17T00:00:00Z", "stamp": "t1", "version": "9.9.9",
        "url": "http://example.invalid/api"}

GOOD_ANSWERS = {
    "name": "Dana", "email": "dana@example.com",
    "providers": ["openai"], "models_openai": "gpt-4o",
    "calls_per_day": "under_1k", "monthly_spend": "under_100",
    "deployment_stage": "exploring", "model_choice_process": "defaults",
    "cache_effort": "not_considered", "biggest_pain": "cost",
    "features_interest": ["routing"], "follow_up_call": False,
}


def _no_diagnose():
    return None


# ---------------------------------------------------------------------------
# catalog self-validity (SPEC §3.1: ask_if targets exist and precede)
# ---------------------------------------------------------------------------

def test_shipped_catalog_conditions_reference_earlier_questions():
    catalog = load_catalog()
    seen: set[str] = set()
    for question in all_questions(catalog):
        cond = question.get("ask_if")
        if cond is not None:
            assert cond["question"] in seen, (
                f"{question['id']} references {cond['question']}, which does "
                "not appear earlier in the catalog")
            assert cond["op"] in ("answered", "equals", "contains")
        seen.add(question["id"])


def test_shipped_catalog_tier_set():
    tiers = [t["id"] for t in load_catalog()["consent_tiers"]]
    assert tiers == ["contact_only", "summary_insights", "full_partnership"]
    for tier in load_catalog()["consent_tiers"]:
        assert tier["copy"].strip(), f"{tier['id']} has empty consent copy"


# ---------------------------------------------------------------------------
# transport (hand-written fake opener via the seam)
# ---------------------------------------------------------------------------

class FakeResponse:
    def __init__(self, status):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeOpener:
    """Records the request; returns a canned status or raises."""

    def __init__(self, status=200, error=None):
        self.status = status
        self.error = error
        self.requests = []

    def __call__(self, request, timeout=None):
        self.requests.append((request, timeout))
        if self.error is not None:
            raise self.error
        return FakeResponse(self.status)


def test_send_submission_success_and_headers():
    opener = FakeOpener(status=200)
    ok = send_submission("http://x/api", b'{"a":1}', version="9.9.9",
                         opener=opener)
    assert ok is True
    request, timeout = opener.requests[0]
    assert timeout == 10.0
    assert request.data == b'{"a":1}'
    assert request.get_method() == "POST"
    assert request.get_header("User-agent") == "tryaii/9.9.9"
    assert request.get_header("Content-type") == "application/json"


@pytest.mark.parametrize("outcome", [
    FakeOpener(status=500),
    FakeOpener(status=302),
    FakeOpener(error=OSError("connection refused")),
    FakeOpener(error=TimeoutError("timed out")),
])
def test_send_submission_collapses_all_failures(outcome):
    assert send_submission("http://x/api", b"{}", version="0.0.0",
                           opener=outcome) is False


# ---------------------------------------------------------------------------
# advance() flows (real temp dirs, injected diagnose + sender)
# ---------------------------------------------------------------------------

def test_full_flow_body_bytes_match_saved_file(tmp_path):
    dp = str(tmp_path / "dp")
    sent = []

    def fake_send(url, body):
        sent.append((url, body))
        return True

    advance(dp, {}, OPTS, load_latest_diagnose=_no_diagnose)
    advance(dp, {"answers": GOOD_ANSWERS}, OPTS, load_latest_diagnose=_no_diagnose)
    advance(dp, {"consent": "contact_only"}, OPTS, load_latest_diagnose=_no_diagnose)
    report = advance(dp, {"confirm": True}, OPTS,
                     load_latest_diagnose=_no_diagnose, send=fake_send)

    assert report["stage"] == "submitted"
    assert report["submission"]["delivered"] is True
    url, body = sent[0]
    assert url == OPTS["url"]
    saved = (tmp_path / "dp" / "submission-t1.json").read_bytes()
    assert body == saved  # SPEC §5: the POST body IS the saved file's bytes
    doc = json.loads(saved)
    assert doc["consent"]["confirmed_at"] == OPTS["now"]
    assert doc["diagnose"] is None


def test_insight_tier_attaches_diagnose_docs(tmp_path):
    dp = str(tmp_path / "dp")
    docs = {"run_id": "r1", "summary": {"site_count": 1},
            "findings": {"version": 1}, "inventory": {"sites": []}}

    advance(dp, {"answers": GOOD_ANSWERS}, OPTS, load_latest_diagnose=lambda: docs)
    advance(dp, {"consent": "summary_insights"}, OPTS,
            load_latest_diagnose=lambda: docs)
    preview = json.loads((tmp_path / "dp" / "preview.json").read_text(encoding="utf-8"))
    assert preview["diagnose"] == {"run_id": "r1", "summary": {"site_count": 1}}

    advance(dp, {"consent": "full_partnership"}, OPTS,
            load_latest_diagnose=lambda: docs)
    preview = json.loads((tmp_path / "dp" / "preview.json").read_text(encoding="utf-8"))
    assert preview["diagnose"]["inventory"] == {"sites": []}


def test_confirm_requires_confirm_stage(tmp_path):
    dp = str(tmp_path / "dp")
    advance(dp, {}, OPTS, load_latest_diagnose=_no_diagnose)
    with pytest.raises(ValueError, match="nothing to confirm"):
        advance(dp, {"confirm": True}, OPTS, load_latest_diagnose=_no_diagnose)


def test_confirm_rejects_stale_preview(tmp_path):
    dp = str(tmp_path / "dp")
    advance(dp, {"answers": GOOD_ANSWERS}, OPTS, load_latest_diagnose=_no_diagnose)
    advance(dp, {"consent": "contact_only"}, OPTS, load_latest_diagnose=_no_diagnose)
    # Tamper with the preview so it no longer matches the state.
    preview_path = tmp_path / "dp" / "preview.json"
    doc = json.loads(preview_path.read_text(encoding="utf-8"))
    doc["answers"]["name"] = "Somebody Else"
    preview_path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="preview is stale"):
        advance(dp, {"confirm": True}, OPTS, load_latest_diagnose=_no_diagnose,
                send=lambda url, body: True)


def test_unknown_tier_raises(tmp_path):
    dp = str(tmp_path / "dp")
    with pytest.raises(ValueError, match="unknown consent tier"):
        advance(dp, {"consent": "bogus"}, OPTS, load_latest_diagnose=_no_diagnose)


def test_new_consent_cycle_clears_submission(tmp_path):
    dp = str(tmp_path / "dp")
    advance(dp, {"answers": GOOD_ANSWERS}, OPTS, load_latest_diagnose=_no_diagnose)
    advance(dp, {"consent": "contact_only"}, OPTS, load_latest_diagnose=_no_diagnose)
    advance(dp, {"confirm": True}, OPTS, load_latest_diagnose=_no_diagnose,
            send=lambda url, body: True)
    report = advance(dp, {"consent": "contact_only"}, OPTS,
                     load_latest_diagnose=_no_diagnose)
    assert report["stage"] == "confirm"  # back in the cycle
    # ...but the submission RECORD file is preserved.
    assert (tmp_path / "dp" / "submission-t1.json").is_file()


def test_reset_removes_state_keeps_submissions(tmp_path):
    dp = str(tmp_path / "dp")
    advance(dp, {"answers": GOOD_ANSWERS}, OPTS, load_latest_diagnose=_no_diagnose)
    advance(dp, {"consent": "contact_only"}, OPTS, load_latest_diagnose=_no_diagnose)
    advance(dp, {"confirm": True}, OPTS, load_latest_diagnose=_no_diagnose,
            send=lambda url, body: False)
    report = advance(dp, {"reset": True}, OPTS, load_latest_diagnose=_no_diagnose)
    assert report["action"]["type"] == "reset"
    assert not (tmp_path / "dp" / "state.json").exists()
    assert not (tmp_path / "dp" / "preview.json").exists()
    assert (tmp_path / "dp" / "submission-t1.json").is_file()


def test_save_local_branch_records_delivered_false(tmp_path):
    dp = str(tmp_path / "dp")
    advance(dp, {"answers": GOOD_ANSWERS}, OPTS, load_latest_diagnose=_no_diagnose)
    advance(dp, {"consent": "contact_only"}, OPTS, load_latest_diagnose=_no_diagnose)
    report = advance(dp, {"confirm": True}, OPTS,
                     load_latest_diagnose=_no_diagnose,
                     send=lambda url, body: False)
    assert report["submission"]["delivered"] is False
    state = json.loads((tmp_path / "dp" / "state.json").read_text(encoding="utf-8"))
    assert state["submission"]["delivered"] is False


def test_answers_rejected_not_stored(tmp_path):
    dp = str(tmp_path / "dp")
    report = advance(dp, {"answers": {"name": "D"}}, OPTS,
                     load_latest_diagnose=_no_diagnose)
    assert report["action"]["type"] == "answers_rejected"
    assert report["stage"] == "questionnaire"
    state = json.loads((tmp_path / "dp" / "state.json").read_text(encoding="utf-8"))
    assert state["answers"] is None
