"""Shared fixtures: a small candidate pool with known facts, built through the real pipeline with fake LLMs."""
import os

os.environ["AUTH_ENABLED"] = "0"          # login is on by default; the general tests run without it (test_auth.py turns it on explicitly)
os.environ["SEMANTIC_INDEXING"] = "0"     # tests must never call the real embedding API (set before app.config loads)
os.environ["DATABASE_URL"] = ""          # never let a developer's .env point the tests at a real database: tests use TEST_DATABASE_URL, in throw-away schemas
os.environ["OCR_ENABLED"] = "0"           # tests must never call the real vision API; OCR tests inject a fake reader
import json
import uuid

import pymupdf
import pytest

from app import config, database, db
from app.db import ID_TABLES
from app.services import candidate_service as svc

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL", "")
NEEDS_DB = "needs a PostgreSQL server: set TEST_DATABASE_URL (see docs/POSTGRES.md)"

JD_TEXT = "AI engineer role. Requires Python, Docker and SQL. Nice to have AWS. Freshers welcome. " * 2


def make_resume_pdf(name, email, phone, skills, extra="") -> bytes:
    text = (f"Name: {name}\nEmail: {email}\nPhone: {phone}\nSKILLS: {', '.join(skills)}\n{extra}\n"
            + "Worked on many software projects and learned quickly. " * 6)
    doc = pymupdf.open()
    doc.new_page().insert_textbox(pymupdf.Rect(50, 50, 545, 800), text, fontsize=10)
    return doc.tobytes()


def _judge(system, user):
    if system.startswith("You extract structured data from a job"):
        return json.dumps({"title": "AI Engineer", "required_skills": ["Python", "Docker", "SQL"],
                           "preferred_skills": ["AWS"], "min_experience_years": 0, "soft_skills": ["Teamwork"], "summary": "s"})
    if system.startswith("You audit"):
        return json.dumps({"corrections": []})
    if system.startswith("You normalize"):
        return json.dumps({"mapping": {}})
    return json.dumps({"fit_score": 70, "projects_education_score": 60, "strengths": ["Solid projects"],
                       "weaknesses": ["Limited depth"], "summary": "Summary text.", "interview_questions": ["Tell me about a project."]})


def add_candidate(conn, job_id, name, email, phone, skills, years, internships=None, levels=None, extra=""):
    profile = {"name": name, "email": email, "phone": phone, "skills": skills, "experience_years": years,
               "internships": internships or [], "skill_levels": levels or {}}
    return svc.process_resume(
        conn, make_resume_pdf(name, email, phone, skills, extra), f"{name.split()[0].lower()}.pdf", job_id,
        extract_llm=lambda s, u: json.dumps(profile), verify_llm=_judge, canon_llm=_judge, score_llm=_judge)


def schema_url(schema: str) -> str:
    """The test database address, pointed at one throw-away schema (so nothing existing in that database is touched)."""
    return TEST_DATABASE_URL + ("&" if "?" in TEST_DATABASE_URL else "?") + "options=-c%20search_path%3D" + schema


def resync_ids(conn) -> None:
    """After inserting rows with explicit ids, move every identity counter past them so the next ordinary insert does not collide."""
    for table in ID_TABLES:
        conn.execute(f"SELECT setval(pg_get_serial_sequence('{table}', 'id'), COALESCE((SELECT MAX(id) FROM {table}), 0) + 1, false)")
    conn.commit()


@pytest.fixture
def pg(monkeypatch):
    """A fresh, empty schema with the app's tables, seeds and upgrades created by init_db(); yields a connection; drops the schema afterwards.
    Skipped when no test server is configured."""
    if not TEST_DATABASE_URL:
        pytest.skip(NEEDS_DB)
    schema = "shortlist_test_" + uuid.uuid4().hex[:10]
    admin = db.connect_pg(TEST_DATABASE_URL)
    admin.execute(f"CREATE SCHEMA {schema}")
    monkeypatch.setattr(config, "DATABASE_URL", schema_url(schema))
    database.init_db()
    conn = database.get_connection()
    yield conn
    conn.close()
    admin.execute(f"DROP SCHEMA {schema} CASCADE")
    admin.close()


@pytest.fixture
def pool(pg):
    """(conn, job_id) with five scored candidates. Two are named Ananya, to test ambiguity."""
    conn = pg
    job_id, msg = svc.create_job(conn, JD_TEXT, canon_llm=_judge, jd_llm=_judge)
    assert job_id, msg
    add_candidate(conn, job_id, "Jeevan Raj", "jeevan@x.com", "9000000001",
                  ["Python", "Machine Learning", "Docker", "SQL", "FastAPI", "AWS"], 0,
                  internships=[{"title": "AI Intern", "company": "Kinetrix", "duration": "Oct 2025 – May 2026"}],
                  extra="AWS Certified Cloud Practitioner")
    add_candidate(conn, job_id, "Priya Sharma", "priya@x.com", "9000000002", ["Python", "TensorFlow", "SQL", "FastAPI"], 4)
    add_candidate(conn, job_id, "Rahul Verma", "rahul@x.com", "9000000003", ["Java", "SQL", "Python"], 2)
    add_candidate(conn, job_id, "Ananya R K", "ananyark@x.com", "9000000004", ["Java", "Selenium"], 0)
    add_candidate(conn, job_id, "Ananya Iyer", "ananyai@x.com", "9000000005", ["Python", "Pandas"], 0)
    conn.commit()
    return conn, job_id
