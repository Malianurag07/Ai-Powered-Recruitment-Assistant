"""Behaviour of the SQL the app depends on (case-insensitive text search, upserts, counts) and a guard that SQLite stays out of app/."""
import json
import re
from pathlib import Path

from app.llm import query_tools
from app.services import candidate_service as svc
from app.services import read_models
from conftest import add_candidate

APP = Path(__file__).resolve().parent.parent / "app"


def test_saving_a_score_twice_updates_one_row(pool):
    conn, job_id = pool
    app_id = conn.execute("SELECT application_id FROM analysis_results LIMIT 1").fetchone()[0]
    before = conn.execute("SELECT COUNT(*) FROM analysis_results").fetchone()[0]
    from app.llm.scoring import ScoreResult
    s = ScoreResult(match_score=12.3, recommendation="Reject", skill_match_ratio=0.1, components={"skills": 1}, skill_breakdown=[],
                    matching_skills=[], missing_skills=["x"], summary="changed")
    with conn:
        svc._store_analysis(conn, app_id, job_id, s)
        svc._store_analysis(conn, app_id, job_id, s)
    row = conn.execute("SELECT match_score, summary, recommendation FROM analysis_results WHERE application_id=?", (app_id,)).fetchone()
    assert conn.execute("SELECT COUNT(*) FROM analysis_results").fetchone()[0] == before
    assert (row["match_score"], row["summary"], row["recommendation"]) == (12.3, "changed", "Reject")


def test_keyword_search_ignores_case_and_escapes_wildcards(pool):
    conn, job_id = pool
    add_candidate(conn, job_id, "Zed Quinn", "zed@x.com", "9000000077", ["Python"], 1, extra="Led the PROJECT-X rollout, 100% uptime_goal")
    ctx = query_tools.Ctx(conn, job_id, {}, svc.get_job(conn, job_id))
    assert query_tools.search_resume_text(ctx, "project-x")["count"] == 1          # lower case finds upper case text
    assert query_tools.search_resume_text(ctx, "PROJECT-X")["count"] == 1
    assert query_tools.search_resume_text(ctx, "100%")["count"] == 1                # a literal percent sign, not a wildcard
    assert query_tools.search_resume_text(ctx, "uptime_goal")["count"] == 1
    assert query_tools.search_resume_text(ctx, "uptimeXgoal")["count"] == 0         # underscore is not a wildcard either


def test_job_list_counts_active_and_pending(pool):
    conn, job_id = pool
    jobs = read_models.list_jobs(conn)
    assert jobs[0]["id"] == job_id and jobs[0]["scored"] == 5 and jobs[0]["pending"] == 0
    conn.execute("UPDATE applications SET application_status='pending_choice' WHERE id=(SELECT MIN(id) FROM applications)")
    assert read_models.list_jobs(conn)[0]["pending"] == 1


def test_last_login_uses_the_shared_timestamp_format(pool):
    from app.db import utc_now
    from app.services import auth_service as auth
    conn, _ = pool
    auth.create_user(conn, "who@x.com", "Passw0rd!long", "Who", "recruiter")
    auth.authenticate(conn, "who@x.com", "Passw0rd!long")
    stamp = conn.execute("SELECT last_login_at FROM users WHERE email='who@x.com'").fetchone()[0]
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", stamp) and re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", utc_now())


# ---- guard: SQLite must not creep back into the application
GONE = [r"import\s+sqlite3", r"\bsqlite3\.",r"\bPRAGMA\b", r"INSERT\s+OR\s+(IGNORE|REPLACE)", r"AUTOINCREMENT", r"COLLATE\s+NOCASE", r"BEGIN\s+IMMEDIATE",
        r"sqlite_master", r"\blastrowid\b.*\bsqlite", r"SUM\(\s*\w+(\.\w+)?\s*="]


def test_no_sqlite_left_in_the_application():
    problems = []
    for path in APP.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for pattern in GONE:
            if re.search(pattern, text, re.I):
                problems.append((path.relative_to(APP).as_posix(), pattern))
    assert not problems, problems
