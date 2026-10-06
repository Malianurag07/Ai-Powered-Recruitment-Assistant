import json
from datetime import date

from app.llm.client import LLMError
from app.llm.jd_parsing import extract_job
from app.llm.scoring import WEIGHTS, duration_months, experience_score, recommend, score_candidate, skill_match
from app.models import CandidateProfile, JobProfile

TODAY = date(2026, 9, 20)
AL = {"ml": "Machine Learning", "deep learning": "Machine Learning", "sql": "SQL"}
JOB = JobProfile(title="ML Engineer", required_skills=["Python", "Machine Learning", "Docker", "SQL"],
                 preferred_skills=["FastAPI"], min_experience_years=0, summary="Build ML services")
GOOD_JUDGE = json.dumps({"fit_score": 80, "projects_education_score": 70, "strengths": ["a"], "weaknesses": ["b"],
                         "summary": "ok", "interview_questions": ["q1"]})


def cand(**kw):
    return CandidateProfile(**{"email": "a@b.co", "skills": [], **kw})


# ---- skills ----
def test_alias_match_and_ratio():
    score, ratio, rows = skill_match(cand(skills=["Python", "ML", "Sql"]), JOB, AL)
    status = {r.skill: r.status for r in rows}
    assert status["Machine Learning"] == "solid" and status["SQL"] == "solid" and status["Docker"] == "missing"
    assert ratio == 0.75


def test_levels_reduce_credit_and_set_colours():
    _, _, rows = skill_match(cand(skills=["Python", "Docker"], skill_levels={"Docker": "basic", "Python": "working"}), JOB, AL)
    by = {r.skill: (r.status, r.colour) for r in rows}
    assert by["Docker"] == ("basic", "orange") and by["Python"] == ("working", "yellow") and by["SQL"] == ("missing", "red")


def test_preferred_weighs_less_than_required():
    job = JobProfile(required_skills=["Docker", "Terraform"], preferred_skills=["Kubernetes"])
    req_only, _, _ = skill_match(cand(skills=["Docker"]), job, AL)
    pref_only, _, _ = skill_match(cand(skills=["Kubernetes"]), job, AL)
    assert req_only == 100 * 1 / 2.5 and pref_only == 100 * 0.5 / 2.5


def test_no_skills_scores_zero():
    assert skill_match(cand(), JOB, AL)[0] == 0


# ---- experience ----
def test_duration_parsing():
    assert duration_months("Oct 2025 – May 2026", TODAY) == 8
    assert duration_months("2022 - 2024", TODAY) == 24
    assert duration_months("Jan 2026 - Present", TODAY) == 9
    assert duration_months("some summer", TODAY) == 3
    assert duration_months(None, TODAY) == 3


def test_fresher_baseline_and_internship_credit():
    none, _ = experience_score(cand(experience_years=0), JOB, TODAY)
    interns = cand(experience_years=0, internships=[{"title": "x", "duration": "Oct 2025 – Sep 2026"}])
    some, eff = experience_score(interns, JOB, TODAY)
    assert none == 60 and eff == 0.5 * 12 / 12 and some == 70


def test_minimum_experience_ratio():
    job = JobProfile(required_skills=["Python"], min_experience_years=4)
    assert experience_score(cand(experience_years=2), job, TODAY)[0] == 50
    assert experience_score(cand(experience_years=8), job, TODAY)[0] == 100


# ---- recommendation + full score ----
def test_recommendation_cutoffs():
    assert [recommend(x) for x in (100, 70, 69.9, 45, 44.9, 0)] == ["Shortlist", "Shortlist", "Consider", "Consider", "Reject", "Reject"]


def test_full_score_uses_weights():
    r = score_candidate(cand(skills=["Python", "Machine Learning", "Docker", "SQL", "FastAPI"], experience_years=0),
                        JOB, AL, llm=lambda s, u: GOOD_JUDGE, today=TODAY)
    expected = round(WEIGHTS["skills"] * 100 + WEIGHTS["experience"] * 60 + WEIGHTS["projects_education"] * 70 + WEIGHTS["fit"] * 80, 1)
    assert r.match_score == expected and r.recommendation == "Shortlist" and r.llm_status == "ok"
    assert r.skill_match_ratio == 1.0 and r.missing_skills == []


def test_llm_outage_falls_back_and_is_flagged():
    def boom(s, u):
        raise LLMError("429")
    r = score_candidate(cand(skills=["Python"]), JOB, AL, llm=boom, today=TODAY)
    assert r.llm_status == "unavailable" and r.components["fit"] == r.components["skills"]


def test_out_of_range_llm_score_retried_then_fallback():
    bad = json.dumps({"fit_score": 250, "projects_education_score": 10})
    r = score_candidate(cand(skills=["Python"]), JOB, AL, llm=lambda s, u: bad, today=TODAY)
    assert r.llm_status == "unavailable"


def test_llm_prompt_contains_computed_facts():
    seen = []
    score_candidate(cand(skills=["Python"]), JOB, AL, llm=lambda s, u: seen.append(u) or GOOD_JUDGE, today=TODAY)
    assert "Required skills matched: 1 of 4" in seen[0] and "Docker" in seen[0]


# ---- JD parsing ----
def test_jd_extraction_ok_and_empty_rejected():
    ok = extract_job("We need an ML engineer with Python and Docker experience for our team.",
                     llm=lambda s, u: json.dumps({"title": "ML Eng", "required_skills": ["Python", "python", "Docker"]}))
    assert ok.status == "ok" and ok.job.required_skills == ["Python", "Docker"]
    assert extract_job("hi").status == "needs_review"
    assert extract_job("x" * 100, llm=lambda s, u: json.dumps({"required_skills": []})).status == "needs_review"


def test_umbrella_skill_inferred_from_tools():
    _, ratio, rows = skill_match(cand(skills=["Python", "TensorFlow", "Scikit-learn"]), JOB, AL)
    by = {r.skill: (r.status, r.colour) for r in rows}
    assert by["Machine Learning"] == ("inferred", "yellow")
    assert by["Docker"] == ("missing", "red")            # nothing implies Docker


def test_python_inferred_from_pandas_but_worth_less_than_stated():
    inferred, _, _ = skill_match(cand(skills=["Pandas"]), JOB, AL)
    stated, _, _ = skill_match(cand(skills=["Python"]), JOB, AL)
    assert 0 < inferred < stated


def test_default_llm_falls_back_to_gemini(monkeypatch):
    from app.llm import scoring
    calls = []

    def fake_complete(system, user, provider="groq", model=None, **kw):
        calls.append(provider)
        if provider == "groq":
            raise LLMError("429")
        return "{}"
    monkeypatch.setattr(scoring.client, "complete", fake_complete)
    assert scoring._default_llm("s", "u") == "{}" and calls == ["groq", "gemini"]


def test_inference_works_when_alias_renames_the_umbrella_skill():
    # The real alias table maps "large language models" -> "LLMs"; inference must still find it.
    job = JobProfile(required_skills=["Large Language Models"])
    _, _, rows = skill_match(cand(skills=["Gemini API"]), job, {"large language models": "LLMs", "llms": "LLMs"})
    assert rows[0].status == "inferred"


def test_deep_learning_is_not_an_alias_of_machine_learning_but_implies_it():
    from app.database import SEED_ALIASES
    assert "deep learning" not in SEED_ALIASES
    job = JobProfile(required_skills=["Machine Learning", "Deep Learning"])
    _, _, rows = skill_match(cand(skills=["Deep Learning"]), job, SEED_ALIASES)
    by = {r.skill: r.status for r in rows}
    assert by["Deep Learning"] == "solid" and by["Machine Learning"] == "inferred"      # two separate skills


# ---------- meaning-based matching (found by testing a pharma resume: 21 relevant skills scored 0% skills) ----------
RESUME = ("Marcus Vance. Senior Medical Representative. Visit cardiologists to present and explain our drugs to doctors. "
          "Submit Daily Call Reports every evening. B.Pharm, 2018. Grew prescriptions 18% in my territory. ") * 2
PHARMA = JobProfile(title="Medical Representative", required_skills=["Physician Detailing", "Daily Call Reports", "Territory Management", "Nurse Engagement"],
                    preferred_skills=["B.Pharm"])
MARCUS = CandidateProfile(name="Marcus", email="m@x.com", skills=["Sales", "Prescription Growth"], experience_years=5)


def judge_json():
    return json.dumps({"fit_score": 70, "projects_education_score": 60, "strengths": [], "weaknesses": [], "summary": "s", "interview_questions": []})


def sem_llm(matches):
    def fn(system, user):
        return json.dumps({"matches": matches}) if system.startswith("You match job requirements") else judge_json()
    return fn


def test_word_for_word_only_gives_zero_when_the_resume_uses_other_words():
    r = score_candidate(MARCUS, PHARMA, AL, llm=sem_llm([]), text=RESUME)
    assert r.components["skills"] == 0.0


def test_grounded_meaning_matches_earn_credit_and_carry_their_evidence():
    llm = sem_llm([{"requirement": "Physician Detailing", "strength": "direct", "evidence_quote": "present and explain our drugs to doctors"},
                   {"requirement": "Daily Call Reports", "strength": "direct", "evidence_quote": "Submit Daily Call Reports every evening"},
                   {"requirement": "Territory Management", "strength": "partial", "evidence_quote": "Grew prescriptions 18% in my territory"},
                   {"requirement": "B.Pharm", "strength": "direct", "evidence_quote": "B.Pharm, 2018"}])
    r = score_candidate(MARCUS, PHARMA, AL, llm=llm, text=RESUME)
    by = {b.skill: b for b in r.skill_breakdown}
    assert (by["Physician Detailing"].status, by["Physician Detailing"].colour) == ("semantic", "yellow")
    assert by["Territory Management"].status == "partial" and by["Nurse Engagement"].status == "missing"
    assert by["Daily Call Reports"].evidence == "Submit Daily Call Reports every evening"
    assert r.components["skills"] > 40 and r.skill_match_ratio == 0.5           # 2 of 4 required count fully; partial does not


def test_invented_or_unrequested_matches_are_rejected():
    llm = sem_llm([{"requirement": "Physician Detailing", "strength": "direct", "evidence_quote": "won the national detailing award"},   # not in resume
                   {"requirement": "Nurse Engagement", "strength": "direct", "evidence_quote": ""},                                   # no quote
                   {"requirement": "Surgery", "strength": "direct", "evidence_quote": "Visit cardiologists"},                          # not asked for
                   {"requirement": "Daily Call Reports", "strength": "expert", "evidence_quote": "Submit Daily Call Reports"}])         # bad strength
    r = score_candidate(MARCUS, PHARMA, AL, llm=llm, text=RESUME)
    assert all(b.status == "missing" for b in r.skill_breakdown) and r.components["skills"] == 0.0


def test_semantic_step_failure_falls_back_to_word_matching():
    def broken(system, user):
        if system.startswith("You match job requirements"):
            raise LLMError("rate limited")
        return judge_json()
    r = score_candidate(MARCUS, PHARMA, AL, llm=broken, text=RESUME)
    assert r.llm_status == "ok" and r.components["skills"] == 0.0


def test_no_resume_text_means_no_extra_ai_call():
    seen = []
    score_candidate(MARCUS, PHARMA, AL, llm=lambda s, u: (seen.append(s), judge_json())[1])
    assert not any(x.startswith("You match job requirements") for x in seen)


def test_only_still_missing_requirements_are_sent_to_the_semantic_step():
    prompts_seen = []

    def spy(system, user):
        if system.startswith("You match job requirements"):
            prompts_seen.append(user)
            return json.dumps({"matches": []})
        return judge_json()
    profile = CandidateProfile(name="M", email="m@x.com", skills=["Daily Call Reports"], experience_years=1)
    score_candidate(profile, PHARMA, AL, llm=spy, text=RESUME)
    assert "Daily Call Reports" not in prompts_seen[0].split("RESUME TEXT")[0] and "Physician Detailing" in prompts_seen[0]


def test_job_prompt_is_not_limited_to_technical_jobs():
    from app.llm import prompts
    assert "ANY kind of job" in prompts.JD_SYSTEM and "soft_skills" in prompts.JD_SYSTEM
    assert "technical skills/tools" not in prompts.JD_SYSTEM
