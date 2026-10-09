import json
import threading

import pytest

from app.llm import client
from app.llm.client import LLMError, complete_with_fallback
from app.llm.scoring import judge_consensus, score_candidate
from app.models import CandidateProfile, JobProfile

JOB = JobProfile(required_skills=["Python"], min_experience_years=0, summary="s")
PROF = CandidateProfile(email="a@b.co", skills=["Python"])


def judgment(fit, pe):
    return json.dumps({"fit_score": fit, "projects_education_score": pe, "strengths": [f"s{fit}"], "weaknesses": ["w"],
                       "summary": "x", "interview_questions": ["q"]})


def sequence_llm(replies):
    """Fake LLM that hands out `replies` in order, safe for the parallel calls consensus makes."""
    lock, it = threading.Lock(), iter(replies)

    def llm(system, user):
        with lock:
            r = next(it)
        if isinstance(r, Exception):
            raise r
        return r
    return llm


# ---------------------------------------------------------------- consensus scoring
def test_consensus_takes_the_median_and_rounds_to_tens():
    llm = sequence_llm([judgment(65, 40), judgment(85, 90), judgment(72, 60)])
    j = judge_consensus(PROF, JOB, "facts", llm, samples=3)
    assert j.fit_score == 70 and j.projects_education_score == 60          # median(65,72,85)=72 -> 70 ; median(40,60,90)=60


def test_consensus_survives_some_failed_calls():
    llm = sequence_llm([LLMError("429"), judgment(80, 70), judgment(80, 70)])
    assert judge_consensus(PROF, JOB, "f", llm, samples=3).fit_score == 80


def test_consensus_fails_only_when_every_call_fails():
    llm = sequence_llm([LLMError("x")] * 3)
    with pytest.raises(ValueError):
        judge_consensus(PROF, JOB, "f", llm, samples=3)


def test_noisy_judge_gives_a_stable_final_score():
    """Same profile, judge noise between runs: the consensus score moves far less than a single call would."""
    def noisy_run(values):
        return score_candidate(PROF, JOB, {}, llm=sequence_llm([judgment(v, v) for v in values] * 2)).match_score
    a = noisy_run([64, 74, 71])
    b = noisy_run([79, 69, 72])
    assert abs(a - b) <= 1.6                       # single calls 64 vs 79 would differ by 4.5 points in the final score


def test_consensus_text_comes_from_the_judgment_nearest_the_consensus():
    llm = sequence_llm([judgment(30, 30), judgment(70, 70), judgment(95, 95)])
    assert judge_consensus(PROF, JOB, "f", llm, samples=3).strengths == ["s70"]


# ---------------------------------------------------------------- retry with backoff
def test_fallback_order_then_success(monkeypatch):
    calls = []

    def fake(system, user, provider="groq", model=None, **kw):
        calls.append((provider, model))
        if provider == "groq":
            raise LLMError("429")
        return "ok"
    monkeypatch.setattr(client, "complete", fake)
    assert complete_with_fallback("s", "u", [("groq", None), ("gemini", "m")]) == "ok"
    assert calls == [("groq", None), ("gemini", "m")]


def test_waits_and_retries_when_everything_fails_once(monkeypatch):
    waits, attempts = [], {"n": 0}

    def fake(system, user, provider="groq", model=None, **kw):
        attempts["n"] += 1
        if attempts["n"] <= 2:                       # first full round (2 providers) fails
            raise LLMError("busy")
        return "recovered"
    monkeypatch.setattr(client, "complete", fake)
    monkeypatch.setattr(client.time, "sleep", waits.append)
    assert complete_with_fallback("s", "u", [("groq", None), ("gemini", None)], retries=1, backoff=3.0) == "recovered"
    assert len(waits) == 1 and 3.0 <= waits[0] <= 6.0        # backoff with jitter


def test_gives_up_after_retries_and_raises_the_last_error(monkeypatch):
    monkeypatch.setattr(client, "complete", lambda *a, **k: (_ for _ in ()).throw(LLMError("still down")))
    monkeypatch.setattr(client.time, "sleep", lambda s: None)
    with pytest.raises(LLMError, match="still down"):
        complete_with_fallback("s", "u", [("groq", None)], retries=2)


# ---------------------------------------------------------------- concurrency cap
def test_concurrent_calls_never_exceed_the_cap(monkeypatch):
    import time
    from concurrent.futures import ThreadPoolExecutor
    monkeypatch.setattr(client, "_SLOTS", threading.BoundedSemaphore(2))
    state = {"now": 0, "peak": 0}
    lock = threading.Lock()

    def slow_groq(system, user, model, json_mode, temperature):
        with lock:
            state["now"] += 1
            state["peak"] = max(state["peak"], state["now"])
        time.sleep(0.05)
        with lock:
            state["now"] -= 1
        return "ok"
    monkeypatch.setattr(client, "_groq", slow_groq)
    with ThreadPoolExecutor(8) as pool:
        results = list(pool.map(lambda _: client.complete("s", "u", provider="groq"), range(8)))
    assert results == ["ok"] * 8 and state["peak"] == 2          # queued, none dropped, never more than 2 in flight


# ---------------------------------------------------------------- self-hosted (local) mode
def test_local_mode_reroutes_every_provider_to_ollama_and_never_touches_the_cloud(monkeypatch):
    calls = []
    monkeypatch.setattr(client, "LLM_MODE", "local")
    monkeypatch.setattr(client, "_ollama", lambda system, user, model, json_mode, temperature: calls.append("ollama") or "local-answer")
    monkeypatch.setattr(client, "_groq", lambda *a, **k: calls.append("groq") or "cloud")
    monkeypatch.setattr(client, "_gemini", lambda *a, **k: calls.append("gemini") or "cloud")
    for provider in ("groq", "gemini", "ollama"):
        assert client.complete("s", "u", provider=provider, model="whatever") == "local-answer"
    assert calls == ["ollama"] * 3


def test_local_mode_has_no_embeddings_so_search_falls_back_to_keywords(monkeypatch):
    monkeypatch.setattr(client, "LLM_MODE", "local")
    with pytest.raises(LLMError, match="local mode"):
        client.embed(["anything"])


def test_health_reports_the_ai_mode(pg):
    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app) as c:
        assert c.get("/api/health").json() == {"status": "ok", "llm_mode": "cloud"}
