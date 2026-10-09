import json
import re

import pymupdf
import pytest

from app.services import candidate_service as svc

JD = "AI engineer role. Requires Python, Docker and SQL. Nice to have AWS. Freshers welcome. " * 2


def make_pdf(email, phone, skills, extra="") -> bytes:
    text = f"Name: Test Person\nEmail: {email}\nPhone: {phone}\nSKILLS: {skills}\n{extra}\n" + "Experienced in building things. " * 8
    doc = pymupdf.open()
    doc.new_page().insert_textbox(pymupdf.Rect(50, 50, 545, 800), text, fontsize=10)
    return doc.tobytes()


def fake_llm(system, user):
    """One fake answering every stage, chosen by which system prompt is calling."""
    if system.startswith("You extract structured data from a job"):
        return json.dumps({"title": "AI Engineer", "required_skills": ["Python", "Docker", "SQL"],
                           "preferred_skills": ["AWS"], "min_experience_years": 0, "soft_skills": ["Teamwork"], "summary": "s"})
    if system.startswith("You extract structured data from resume"):
        email = (re.search(r"Email: (\S+)", user) or [None, None])[1]
        phone = (re.search(r"Phone: ([+\d ]+)", user) or [None, None])[1]
        skills = [x.strip() for x in re.search(r"SKILLS: (.*)", user).group(1).split(",")]
        return json.dumps({"name": "Test Person", "email": email, "phone": phone, "skills": skills, "experience_years": 0})
    if system.startswith("You audit"):
        return json.dumps({"corrections": []})
    if system.startswith("You match job requirements"):
        return json.dumps({"matches": []})
    if system.startswith("You normalize"):
        return json.dumps({"mapping": {"Postgres": "PostgreSQL"}})
    if system.startswith("You are a careful technical recruiter"):
        return json.dumps({"fit_score": 70, "projects_education_score": 60, "strengths": ["s"], "weaknesses": ["w"],
                           "summary": "sum", "interview_questions": ["q"]})
    raise AssertionError("unexpected prompt: " + system[:40])


LLMS = dict(extract_llm=fake_llm, verify_llm=fake_llm, canon_llm=fake_llm, score_llm=fake_llm)


@pytest.fixture
def conn(pg):
    return pg


@pytest.fixture
def job(conn):
    job_id, msg = svc.create_job(conn, JD, canon_llm=fake_llm, jd_llm=fake_llm)
    assert job_id, msg
    return job_id


def submit(conn, job, pdf, name="r.pdf"):
    return svc.process_resume(conn, pdf, name, job, **LLMS)


def test_job_stored_with_importance(conn, job):
    j = svc.get_job(conn, job)
    assert j.required_skills == ["Python", "Docker", "SQL"] and j.preferred_skills == ["AWS"] and j.soft_skills == ["Teamwork"]


def test_bad_job_description_rejected(conn):
    assert svc.create_job(conn, "too short", jd_llm=fake_llm)[0] is None


def test_new_resume_stored_scored_and_ranked(conn, job):
    o = submit(conn, job, make_pdf("a@x.com", "9876543210", "Python, Docker, SQL"))
    assert o.status == "stored" and o.score is not None and o.recommendation
    rank = svc.ranking(conn, job)
    assert len(rank) == 1 and rank[0]["email"] == "a@x.com"
    skills = [r["skill_name"] for r in conn.execute("SELECT skill_name FROM application_skills ORDER BY id")]
    assert skills == ["Python", "Docker", "SQL"]


def test_better_candidate_ranks_first(conn, job):
    submit(conn, job, make_pdf("weak@x.com", "1111111111", "Excel"), "weak.pdf")
    submit(conn, job, make_pdf("strong@x.com", "2222222222", "Python, Docker, SQL, AWS"), "strong.pdf")
    assert [r["email"] for r in svc.ranking(conn, job)] == ["strong@x.com", "weak@x.com"]
    assert len(svc.ranking(conn, job, limit=1)) == 1


def test_identical_resume_ignored(conn, job):
    pdf = make_pdf("a@x.com", "9876543210", "Python")
    submit(conn, job, pdf)
    o = submit(conn, job, pdf, "again.pdf")
    assert o.status == "duplicate_ignored"
    assert conn.execute("SELECT COUNT(*) FROM applications").fetchone()[0] == 1


def test_different_resume_same_job_conflict_then_resolve(conn, job):
    first = submit(conn, job, make_pdf("a@x.com", "9876543210", "Python"), "v1.pdf")
    second = submit(conn, job, make_pdf("a@x.com", "9876543210", "Python, Docker, SQL"), "v2.pdf")
    assert second.status == "conflict_pending" and second.candidate_id == first.candidate_id
    assert [r["resume_filename"] for r in svc.ranking(conn, job)] == ["v1.pdf"]      # pending excluded
    conflicts = svc.pending_conflicts(conn, job)
    assert conflicts[0]["new_file"] == "v2.pdf" and conflicts[0]["current_file"] == "v1.pdf"

    chosen = svc.resolve_conflict(conn, second.application_id, score_llm=fake_llm)
    assert chosen.status == "stored" and chosen.score is not None
    assert [r["resume_filename"] for r in svc.ranking(conn, job)] == ["v2.pdf"]
    assert svc.pending_conflicts(conn, job) == []


def test_conflict_matched_by_phone_when_email_differs(conn, job):
    submit(conn, job, make_pdf("a@x.com", "98765 43210", "Python"))
    o = submit(conn, job, make_pdf("other@x.com", "+91 9876543210", "Python, SQL"))
    assert o.status == "conflict_pending"


def test_same_person_other_job_allowed(conn, job):
    job2, _ = svc.create_job(conn, JD, canon_llm=fake_llm, jd_llm=fake_llm)
    a = submit(conn, job, make_pdf("a@x.com", "9876543210", "Python"))
    b = submit(conn, job2, make_pdf("a@x.com", "9876543210", "Python, SQL"))
    assert b.status == "stored" and b.candidate_id == a.candidate_id
    assert conn.execute("SELECT COUNT(*) FROM candidates").fetchone()[0] == 1


def test_unusable_files_rejected_not_stored(conn, job):
    assert submit(conn, job, b"not a pdf", "bad.pdf").status == "rejected_file"
    assert submit(conn, job, b"hello", "notes.txt").status == "rejected_file"
    assert conn.execute("SELECT COUNT(*) FROM applications").fetchone()[0] == 0


def test_no_contact_stored_for_review_but_unscored(conn, job):
    o = submit(conn, job, make_pdf("", "", "Python"))
    assert o.status == "needs_review" and o.application_id
    assert svc.ranking(conn, job) == []


def test_ai_learned_alias_saved_and_used(conn, job):
    o = submit(conn, job, make_pdf("a@x.com", "9876543210", "Python, Postgres"))
    assert o.status == "stored"
    assert conn.execute("SELECT canonical, source FROM skill_aliases WHERE alias='postgres'").fetchone()[0] == "PostgreSQL"
    assert "PostgreSQL" in [r[0] for r in conn.execute("SELECT skill_name FROM application_skills")]


def test_hallucinated_skill_is_logged(conn, job):
    def ext(system, user):
        d = json.loads(fake_llm(system, user))
        d["skills"].append("Kubernetes")
        return json.dumps(d)
    o = svc.process_resume(conn, make_pdf("a@x.com", "9876543210", "Python"), "r.pdf", job, **{**LLMS, "extract_llm": ext})
    assert o.status == "stored" and o.verification_status == "corrected"
    log = conn.execute("SELECT field, old_value, evidence_quote FROM verification_log").fetchall()
    assert log[0]["old_value"] == "Kubernetes" and "deterministic" in log[0]["evidence_quote"]


def test_rescore_repairs_analyses_whose_llm_step_failed(conn, job):
    from app.llm.client import LLMError

    def down(system, user):
        raise LLMError("429")
    o = svc.process_resume(conn, make_pdf("a@x.com", "9876543210", "Python, Docker"), "r.pdf", job,
                           **{**LLMS, "score_llm": down})
    assert o.status == "stored"
    assert conn.execute("SELECT llm_status FROM analysis_results").fetchone()[0] == "unavailable"
    still_down = svc.rescore_unavailable(conn, job, score_llm=down)
    assert still_down[0]["llm_status"] == "unavailable"                                   # nothing replaced yet
    fixed = svc.rescore_unavailable(conn, job, score_llm=fake_llm)
    assert fixed[0]["llm_status"] == "ok"
    row = conn.execute("SELECT llm_status, strengths FROM analysis_results").fetchone()
    assert row[0] == "ok" and "s" in row[1]
    assert svc.rescore_unavailable(conn, job, score_llm=fake_llm) == []                    # idempotent


def test_preview_scores_pending_resumes_without_activating_them(conn, job):
    submit(conn, job, make_pdf("a@x.com", "9876543210", "Python"), "v1.pdf")
    submit(conn, job, make_pdf("a@x.com", "9876543210", "Python, Docker, SQL"), "v2.pdf")
    preview = svc.preview_pending_scores(conn, job, score_llm=fake_llm)
    assert len(preview) == 1 and preview[0]["file"] == "v2.pdf" and preview[0]["required_matched"] == "3/3"
    assert [r["resume_filename"] for r in svc.ranking(conn, job)] == ["v1.pdf"]            # still not active
    assert conn.execute("SELECT COUNT(*) FROM analysis_results").fetchone()[0] == 1
