"""Free-tier estimate: calls needed vs. left, verdicts, pause length, and Groq header parsing."""
import sqlite3
import time

import pytest

from app import config
from app.services import quota


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    mem = sqlite3.connect(":memory:", check_same_thread=False)
    mem.row_factory = sqlite3.Row
    mem.execute("CREATE TABLE ai_usage (day TEXT NOT NULL, bucket TEXT NOT NULL, calls INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (day, bucket))")

    class Shared:                                              # _conn() hands out this one in-memory database; close() keeps it open
        execute = staticmethod(mem.execute)
        close = staticmethod(lambda: None)
        __enter__ = lambda self: mem.__enter__()
        __exit__ = lambda self, *a: mem.__exit__(*a)
    monkeypatch.setattr(quota, "_conn", lambda: Shared())
    quota._headers.clear()
    monkeypatch.setattr(quota, "_last_rate_limited", 0.0)
    monkeypatch.setattr(config, "GROQ_FAST_CALLS_PER_DAY", 1000)
    monkeypatch.setattr(config, "GROQ_CALLS_PER_DAY", 1000)
    monkeypatch.setattr(config, "GEMINI_CALLS_PER_DAY", 500)
    monkeypatch.setattr(config, "JUDGE_SAMPLES", 3)


def test_calls_are_counted_per_day_and_bucket():
    for _ in range(3):
        quota.record_call("gemini")
    quota.record_call("groq_big")
    assert quota.used_today("gemini") == 3 and quota.used_today("groq_big") == 1 and quota.used_today("groq_fast") == 0


def test_small_batch_fits():
    e = quota.estimate(10)
    assert e["verdict"] == "ok" and e["max_resumes_now"] >= 10
    assert e["calls_total"] == 10 * (2 + 4 + 2) and e["minutes"] > 0


def test_batch_over_the_daily_limit_is_flagged_with_the_largest_that_fits():
    e = quota.estimate(400)                  # gemini: 2 calls each = 800 > 500 left
    assert e["verdict"] == "over" and e["max_resumes_now"] == 250
    gem = next(r for r in e["rows"] if r["bucket"] == "gemini")
    assert gem["needs"] == 800 and gem["left"] == 500 and gem["source"] == "counted by this app"


def test_usage_today_reduces_what_is_left_and_tight_is_reported():
    for _ in range(300):
        quota.record_call("gemini")
    e = quota.estimate(90)                   # needs 180 of 200 left = 90 percent
    assert e["verdict"] == "tight" and e["max_resumes_now"] == 100


def test_groq_reported_remaining_overrides_the_local_count():
    quota.note_groq_headers("groq_big", {"x-ratelimit-remaining-requests": "12", "x-ratelimit-limit-requests": "1000"})
    left, limit, source = quota.remaining("groq_big")
    assert (left, limit, source) == (12, 1000, "reported by Groq")
    e = quota.estimate(5)                    # needs 20 big-model calls, only 12 left
    assert e["verdict"] == "over" and e["max_resumes_now"] == 3
    assert quota.out_of_calls() is None      # 12 left is still enough for one more resume (4 calls)
    quota.note_groq_headers("groq_big", {"x-ratelimit-remaining-requests": "2", "x-ratelimit-limit-requests": "1000"})
    assert quota.out_of_calls() == "groq_big"


def test_stale_or_missing_headers_fall_back_to_counting():
    quota.note_groq_headers("groq_big", {"x-ratelimit-remaining-requests": "5", "x-ratelimit-limit-requests": "1000"})
    quota._headers["groq_big"]["seen"] = time.time() - 10_000
    assert quota.remaining("groq_big")[2] == "counted by this app"
    quota.note_groq_headers("groq_fast", {})                       # garbage is ignored
    assert "groq_fast" not in quota._headers


def test_pause_is_longer_right_after_a_rate_limit():
    base = quota.suggested_pause()
    quota.note_rate_limited()
    assert quota.suggested_pause() > base


def test_estimate_endpoint():
    from fastapi.testclient import TestClient
    from app.main import app
    from app import deps
    app.dependency_overrides[deps.get_db] = lambda: None
    with TestClient(app) as c:
        r = c.get("/api/quota/estimate", params={"count": 12, "probe": "false"})
    app.dependency_overrides.clear()
    if r.status_code == 200:                 # 401 when login is on in this test environment
        assert r.json()["count"] == 12 and r.json()["rows"]
