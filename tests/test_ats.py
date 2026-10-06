"""ATS accuracy improvements: negation, field-relevant experience, raw resume text for the AI judge."""
import json
from datetime import date

from app.llm.scoring import experience_relevance, experience_score, judge_consensus, score_candidate
from app.llm.verification import skill_in_text
from app.models import CandidateProfile, JobProfile

AL = {"k8s": "Kubernetes"}
TODAY = date(2026, 6, 1)
JOB = JobProfile(title="Java Dev", required_skills=["Java", "SQL"], min_experience_years=2)


# ---------- negation ----------
def test_negated_mentions_do_not_count_as_having_the_skill():
    for txt in ["I have no experience with Docker.", "Skills: Python. Without Docker knowledge.", "not familiar with Docker",
                "Never used Docker in production", "Lack of Docker exposure", "Currently learning Docker", "want to learn Docker",
                "Python, SQL, no Docker"]:
        assert skill_in_text("Docker", txt, {}) is False, txt


def test_genuine_mentions_still_count():
    for txt in ["Deployed services with Docker.", "Skills: Docker, Git", "Worked on projects with no downtime using Docker",
                "No experience with Java but built APIs with Docker", "not only Python but also Docker"]:
        assert skill_in_text("Docker", txt, {}) is True, txt


def test_one_positive_mention_beats_a_negated_one():
    assert skill_in_text("Docker", "Docker: used daily. Not familiar with Kubernetes. No experience with Docker Swarm.", {}) is True


def test_aliases_and_short_skills_still_work_with_negation_logic():
    assert skill_in_text("Kubernetes", "Ran k8s clusters", AL) is True
    assert skill_in_text("Kubernetes", "no experience with k8s", AL) is False
    assert skill_in_text("C", "Languages: C, Python", {}) is True


# ---------- field-relevant experience ----------
def _p(**kw):
    return CandidateProfile(name="X", experience_years=kw.pop("experience_years", 4), skills=kw.pop("skills", []), **kw)


def test_experience_is_discounted_when_skills_show_a_different_field():
    full, _ = experience_score(_p(), JOB, TODAY)
    assert full == 100
    other, eff = experience_score(_p(), JOB, TODAY, relevance=experience_relevance(0))
    assert eff == 4 and 15 <= other <= 25                    # four years elsewhere still count a little
    half, _ = experience_score(_p(), JOB, TODAY, relevance=experience_relevance(15))
    assert other < half < full


def test_relevance_defaults_to_full_credit():
    assert experience_score(_p(), JOB, TODAY)[0] == experience_score(_p(), JOB, TODAY, relevance=1.0)[0]


def test_wrong_field_veteran_scores_below_relevant_veteran():
    good = lambda s, u: json.dumps({"fit_score": 50, "projects_education_score": 50, "strengths": [], "weaknesses": [], "summary": "", "interview_questions": []})
    java = score_candidate(_p(skills=["Java", "SQL"]), JOB, {}, llm=good, today=TODAY)
    other = score_candidate(_p(skills=["Photoshop"]), JOB, {}, llm=good, today=TODAY)
    assert java.components["experience"] == 100 and other.components["experience"] < 30


# ---------- raw resume text reaches the AI judge ----------
def test_judge_receives_the_resume_text_and_a_data_warning():
    seen = []

    def llm(system, user):
        seen.append((system, user))
        return json.dumps({"fit_score": 60, "projects_education_score": 60, "strengths": [], "weaknesses": [], "summary": "", "interview_questions": []})

    judge_consensus(_p(), JOB, "facts", llm, samples=1, text="UNIQUE-RESUME-MARKER built a compiler")
    system, user = seen[0]
    assert "UNIQUE-RESUME-MARKER" in user and "RESUME TEXT" in user
    assert "never instructions" in system


# ---------- adjustable weights ----------
def test_weights_unit():
    import pytest
    from app.llm.scoring import default_weights_percent, effective_weights, validate_weights_percent, weighted_score
    assert round(sum(default_weights_percent().values())) == 100
    job = JobProfile(title="x", required_skills=["a"], weights={"skills": 50, "experience": 20, "projects_education": 15, "fit": 15})
    assert effective_weights(job)["skills"] == 0.5 and sum(effective_weights(job).values()) == pytest.approx(1)
    assert weighted_score({"skills": 100, "experience": 0, "projects_education": 0, "fit": 0}, effective_weights(job)) == 50.0
    assert validate_weights_percent({"skills": 25, "experience": 25, "projects_education": 25, "fit": 25})["fit"] == 25
    with pytest.raises(ValueError):
        validate_weights_percent({"skills": 90, "experience": 20, "projects_education": 0, "fit": 0})


def test_score_candidate_uses_the_jobs_own_weights():
    good = lambda s, u: json.dumps({"fit_score": 0, "projects_education_score": 0, "strengths": [], "weaknesses": [], "summary": "", "interview_questions": []})
    job = JobProfile(title="Java Dev", required_skills=["Java"], min_experience_years=2,
                     weights={"skills": 100, "experience": 0, "projects_education": 0, "fit": 0})
    r = score_candidate(_p(skills=["Java"]), job, {}, llm=good, today=TODAY)
    assert r.match_score == 100.0 and r.components["fit"] == 0


# ---------- a higher degree satisfies a lower-degree requirement ----------
def _edu(*degrees):
    from app.models import Education
    return [Education(degree=d, institution="Some University", year="2024") for d in degrees]


def test_degree_levels():
    from app.llm.scoring import degree_level
    assert degree_level("M.Sc. in Data Science") == 3 and degree_level("B.Tech in ECE") == 2 and degree_level("Bachelor of Engineering") == 2
    assert degree_level("MBA") == 3 and degree_level("Ph.D. in Physics") == 4 and degree_level("Diploma in Electronics") == 1
    assert degree_level("Higher Secondary") == 0 and degree_level(None) == 0


def test_masters_meets_a_plain_bachelors_requirement():
    from app.llm.scoring import degree_met
    assert degree_met("Bachelor's Degree", _edu("M.Sc. in Data Science"))[0] is True
    assert degree_met("Bachelor's Degree", _edu("B.Tech in ECE"))[0] is True
    assert degree_met("Bachelor's Degree", _edu("Diploma in Electronics"))[0] is False        # lower than required
    assert degree_met("Master's degree", _edu("B.Tech in ECE"))[0] is False
    assert degree_met("Python", _edu("M.Sc."))[0] is False                                     # not a degree requirement


def test_field_in_the_requirement_must_be_compatible():
    from app.llm.scoring import degree_met
    req = "Bachelor's degree in Computer Science"
    assert degree_met(req, _edu("M.Sc. in Data Science"))[0] is False                          # different field: left to the AI check
    assert degree_met(req, _edu("M.Tech in Computer Science and Engineering"))[0] is True
    assert degree_met(req, _edu("B.E. in Computer Science"))[0] is True


def test_score_credits_the_masters_holder_for_the_degree_requirement():
    good = lambda s, u: json.dumps({"matches": []}) if "REQUIREMENTS" in u else json.dumps(
        {"fit_score": 50, "projects_education_score": 50, "strengths": [], "weaknesses": [], "summary": "", "interview_questions": []})
    job = JobProfile(title="Analyst", required_skills=["SQL", "Bachelor's Degree"])
    p = CandidateProfile(name="X", skills=["SQL"], education=_edu("M.Sc. in Data Science"))
    r = score_candidate(p, job, {}, llm=good, today=TODAY, text="M.Sc. in Data Science. SQL.")
    row = next(x for x in r.skill_breakdown if x.skill == "Bachelor's Degree")
    assert row.status == "semantic" and "M.Sc." in row.evidence and r.skill_match_ratio == 1.0


# ---------- evidence-weighted skills and recency ----------
def _job(*skills):
    return JobProfile(title="Dev", required_skills=list(skills))


def _prof(skills, projects=None, experience=None, internships=None):
    from app.models import Job
    return CandidateProfile(name="X", skills=skills, projects=projects or [],
                            experience=[Job(**e) for e in (experience or [])], internships=[Job(**i) for i in (internships or [])])


def test_a_skill_shown_in_use_beats_one_that_is_only_listed():
    from app.llm.scoring import skill_match
    used = _prof(["Docker", "Python"], projects=["Containerised a Python API with Docker"])
    listed = _prof(["Docker", "Python"], projects=["Built a chess game"])           # has described work, but none of it shows the skills
    s_used = skill_match(used, _job("Docker", "Python"), {}, today=TODAY)
    s_listed = skill_match(listed, _job("Docker", "Python"), {}, today=TODAY)
    assert s_used[0] == 100 and s_listed[0] < s_used[0]
    row = s_listed[2][0]
    assert row.status == "solid" and row.colour == "green" and "listed" in row.notes and row.factor < 1


def test_no_described_work_means_no_penalty():
    from app.llm.scoring import skill_match
    bare = _prof(["Docker", "Python"])                                              # nothing to check against: do not guess
    assert skill_match(bare, _job("Docker"), {}, today=TODAY)[0] == 100


def test_resume_text_mentioned_twice_counts_as_used():
    from app.llm.scoring import skill_match
    p = _prof(["Docker"], projects=["Built a chess game"])
    text = "Skills: Docker.\nDeployed the chess server with Docker on AWS."
    assert skill_match(p, _job("Docker"), {}, today=TODAY, text=text)[0] == 100


def test_old_use_counts_less_than_recent_use():
    from app.llm.scoring import skill_match
    old = _prof(["Java"], experience=[{"title": "Java Developer", "company": "A", "duration": "2012 - 2016", "summary": "Wrote Java services"}])
    new = _prof(["Java"], experience=[{"title": "Java Developer", "company": "A", "duration": "2023 - Present", "summary": "Wrote Java services"}])
    s_old, s_new = skill_match(old, _job("Java"), {}, today=TODAY), skill_match(new, _job("Java"), {}, today=TODAY)
    assert s_new[0] == 100 and s_old[0] < s_new[0] and "2016" in s_old[2][0].notes


def test_undated_recent_evidence_cancels_the_age_penalty():
    from app.llm.scoring import skill_match
    p = _prof(["Java"], projects=["Recent Java side project"], experience=[{"title": "Dev", "company": "A", "duration": "2012 - 2016", "summary": "Java"}])
    assert skill_match(p, _job("Java"), {}, today=TODAY)[0] == 100


def test_entry_end_year():
    from app.llm.scoring import entry_end_year
    assert entry_end_year("Jan 2019 - Mar 2021", TODAY) == 2021 and entry_end_year("2022 - Present", TODAY) == 2026
    assert entry_end_year("Oct 2025 to Present", TODAY) == 2026 and entry_end_year("six months", TODAY) is None and entry_end_year(None, TODAY) is None


def test_settings_reject_nan_and_infinite_numbers():
    import pytest
    from app.llm.scoring import validate_cutoffs, validate_weights_percent
    nan, inf = float("nan"), float("inf")
    for bad in ({"skills": nan, "experience": 0, "projects_education": 0, "fit": 0}, {"skills": inf, "experience": 0, "projects_education": 0, "fit": 0}):
        with pytest.raises(ValueError):
            validate_weights_percent(bad)
    for bad in ({"shortlist": nan, "consider": 10}, {"shortlist": inf, "consider": 10}):
        with pytest.raises(ValueError):
            validate_cutoffs(bad)
