"""Tests of Postgres behaviour itself. Like every test that touches the database they are skipped unless TEST_DATABASE_URL is set, e.g.

    TEST_DATABASE_URL=postgresql://user:password@localhost:5432/postgres  python -m pytest tests/test_postgres.py -v

Each test gets its own throw-away schema (dropped afterwards), so nothing existing in that database is touched. The database role
needs permission to CREATE SCHEMA. NOTE: these tests were written without a Postgres server to run them on; the first real run may
reveal mistakes in the tests themselves as well as in the app. See docs/POSTGRES.md.
"""
import json
import re
import threading
import time

import pytest

from app import config, database, db
from app.services import candidate_service as svc
from app.services import read_models
from conftest import JD_TEXT, NEEDS_DB, TEST_DATABASE_URL, _judge, add_candidate, make_resume_pdf, schema_url

@pytest.fixture
def pg_pool(pg):
    job_id, msg = svc.create_job(pg, JD_TEXT, canon_llm=_judge, jd_llm=_judge)
    assert job_id, msg
    add_candidate(pg, job_id, "Jeevan Raj", "jeevan@x.com", "9000000001", ["Python", "Machine Learning", "Docker", "SQL", "FastAPI", "AWS"], 0)
    add_candidate(pg, job_id, "Priya Sharma", "priya@x.com", "9000000002", ["Python", "TensorFlow", "SQL", "FastAPI"], 4)
    add_candidate(pg, job_id, "Rahul Verma", "rahul@x.com", "9000000003", ["Java", "SQL", "Python"], 2)
    return pg, job_id


# ---------------------------------------------------------------- schema
def test_schema_is_created_twice_without_error_and_has_every_table(pg):
    database.init_db()                                                              # idempotent
    names = {r[0] for r in pg.execute("SELECT table_name FROM information_schema.tables WHERE table_schema = current_schema()")}
    assert {"candidates", "job_descriptions", "job_required_skills", "applications", "application_skills", "resume_chunks",
            "analysis_results", "verification_log", "skill_aliases", "users", "chat_history", "ai_usage"} <= names
    assert pg.execute("SELECT COUNT(*) FROM skill_aliases").fetchone()[0] == len(database.SEED_ALIASES)


def test_column_types_are_what_the_schema_promises(pg):
    types = {(r["table_name"], r["column_name"]): r["data_type"] for r in pg.execute(
        "SELECT table_name, column_name, data_type FROM information_schema.columns WHERE table_schema = current_schema()")}
    assert types[("analysis_results", "match_score")] == "double precision"        # not 4-byte "real"
    assert types[("applications", "experience_years")] == "double precision"
    assert types[("resume_chunks", "embedding")] == "bytea"
    assert types[("candidates", "created_at")] == "text"


def test_identity_ids_and_timestamp_default(pg):
    a = pg.execute("INSERT INTO candidates (name, email) VALUES (?,?)", ("A", "a@x.com")).lastrowid
    b = pg.execute("INSERT INTO candidates (name, email) VALUES (?,?)", ("B", "b@x.com")).lastrowid
    pg.commit()
    assert isinstance(a, int) and b == a + 1
    stamp = pg.execute("SELECT created_at FROM candidates WHERE id=?", (a,)).fetchone()["created_at"]
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", stamp)


# ---------------------------------------------------------------- constraints
def test_unique_email_and_the_one_active_resume_rule(pg_pool):
    pg, job_id = pg_pool
    with pytest.raises(db.INTEGRITY_ERRORS):
        with pg:
            pg.execute("INSERT INTO candidates (name, email) VALUES (?,?)", ("Dup", "jeevan@x.com"))
    app_row = pg.execute("SELECT candidate_id FROM applications LIMIT 1").fetchone()
    with pytest.raises(db.INTEGRITY_ERRORS):                                        # a second ACTIVE resume for the same person and job
        with pg:
            pg.execute("INSERT INTO applications (candidate_id, job_description_id, resume_hash) VALUES (?,?,?)", (app_row[0], job_id, "other"))
    with pg:                                                                        # but a pending one is allowed
        pg.execute("INSERT INTO applications (candidate_id, job_description_id, resume_hash, application_status) VALUES (?,?,?,?)",
                   (app_row[0], job_id, "other", "pending_choice"))


def test_connection_is_usable_after_a_failed_statement(pg):
    with pytest.raises(db.INTEGRITY_ERRORS):
        with pg:
            pg.execute("INSERT INTO users (email, password_hash, role) VALUES (?,?,?)", ("x@x.com", "h", "wizard"))   # breaks the CHECK
    assert pg.execute("SELECT 1").fetchone()[0] == 1                               # rolled back cleanly, not stuck in an aborted transaction


def test_deleting_a_job_cascades_to_everything_under_it(pg_pool):
    pg, job_id = pg_pool
    assert pg.execute("SELECT COUNT(*) FROM application_skills").fetchone()[0] > 0
    with pg:
        pg.execute("DELETE FROM job_descriptions WHERE id=?", (job_id,))
    for table in ("job_required_skills", "applications", "application_skills", "analysis_results", "verification_log", "resume_chunks"):
        assert pg.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0, table


# ---------------------------------------------------------------- queries the app relies on
def test_ranking_and_job_list(pg_pool):
    pg, job_id = pg_pool
    ranked = svc.ranking(pg, job_id)
    assert len(ranked) == 3 and ranked[0]["match_score"] >= ranked[-1]["match_score"]
    jobs = read_models.list_jobs(pg)
    assert jobs[0]["scored"] == 3 and jobs[0]["pending"] == 0


def test_saving_a_score_twice_updates_one_row(pg_pool):
    pg, job_id = pg_pool
    from app.llm.scoring import ScoreResult
    app_id = pg.execute("SELECT application_id FROM analysis_results LIMIT 1").fetchone()[0]
    s = ScoreResult(match_score=12.3, recommendation="Reject", skill_match_ratio=0.1, components={}, skill_breakdown=[],
                    matching_skills=[], missing_skills=[], summary="changed")
    with pg:
        svc._store_analysis(pg, app_id, job_id, s)
        svc._store_analysis(pg, app_id, job_id, s)
    assert pg.execute("SELECT COUNT(*) FROM analysis_results WHERE application_id=?", (app_id,)).fetchone()[0] == 1
    assert pg.execute("SELECT match_score FROM analysis_results WHERE application_id=?", (app_id,)).fetchone()[0] == 12.3


def test_keyword_search_ignores_case(pg_pool):
    from app.llm import query_tools
    pg, job_id = pg_pool
    add_candidate(pg, job_id, "Zed Quinn", "zed@x.com", "9000000077", ["Python"], 1, extra="Led the PROJECT-X rollout, 100% uptime")
    ctx = query_tools.Ctx(pg, job_id, {}, svc.get_job(pg, job_id))
    assert query_tools.search_resume_text(ctx, "project-x")["count"] == 1
    assert query_tools.search_resume_text(ctx, "100%")["count"] == 1


def test_embeddings_round_trip_as_bytes(pg_pool):
    from app.llm.retrieval import pack, unpack
    pg, job_id = pg_pool
    app_id = pg.execute("SELECT id FROM applications LIMIT 1").fetchone()[0]
    with pg:
        pg.execute("INSERT INTO resume_chunks (application_id, chunk_index, text, embedding) VALUES (?,?,?,?)", (app_id, 99, "t", pack([0.5, -1.25, 3.0])))
    blob = pg.execute("SELECT embedding FROM resume_chunks WHERE chunk_index=99").fetchone()["embedding"]
    assert isinstance(blob, bytes) and unpack(blob) == [0.5, -1.25, 3.0]


# ---------------------------------------------------------------- the settings built on top
def test_weights_cutoffs_and_must_haves_recompute(pg_pool):
    pg, job_id = pg_pool
    before = {r["application_id"]: r["match_score"] for r in svc.ranking(pg, job_id)}
    svc.set_job_weights(pg, job_id, {"skills": 100, "experience": 0, "projects_education": 0, "fit": 0})
    for r in svc.ranking(pg, job_id):
        assert r["match_score"] == pytest.approx(json.loads(pg.execute("SELECT component_scores FROM analysis_results WHERE application_id=?",
                                                                         (r["application_id"],)).fetchone()[0])["skills"], abs=0.11)
    svc.set_job_weights(pg, job_id, None)
    svc.set_job_cutoffs(pg, job_id, {"shortlist": 1, "consider": 0.5})
    assert {r["recommendation"] for r in svc.ranking(pg, job_id)} == {"Shortlist"}
    required = svc.get_job(pg, job_id).required_skills
    svc.set_job_gates(pg, job_id, required[:1])
    assert svc.get_job(pg, job_id).gates == required[:1]
    assert len(before) == 3


def test_accounts(pg):
    from app.services import auth_service as auth
    first = auth.register(pg, "1.2.3.4", "first@x.com", "Passw0rd!long", "First")
    second = auth.register(pg, "1.2.3.4", "second@x.com", "Passw0rd!long", "Second")
    assert first["role"] == "admin" and second["role"] == "recruiter"
    with pytest.raises(auth.AuthError, match="already exists"):
        auth.create_user(pg, "first@x.com", "Passw0rd!long", "Again", "recruiter")
    assert auth.authenticate(pg, "first@x.com", "Passw0rd!long")["email"] == "first@x.com"
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", pg.execute("SELECT last_login_at FROM users WHERE email='first@x.com'").fetchone()[0])


# ---------------------------------------------------------------- the race the duplicate-resume lock exists for
def test_same_person_uploaded_concurrently_yields_one_active_and_one_pending(pg):
    job_id, _ = svc.create_job(pg, JD_TEXT, canon_llm=_judge, jd_llm=_judge)
    pg.commit()
    profile = {"name": "Race Person", "email": "race@x.com", "phone": "9222222222", "skills": ["Python", "SQL"], "experience_years": 0}
    barrier = threading.Barrier(2)

    def slow_score(system, user):
        barrier.wait(timeout=10)
        time.sleep(0.2)
        return _judge(system, user)

    def upload(extra):
        conn = database.get_connection()
        try:
            pdf = make_resume_pdf("Race Person", "race@x.com", "9222222222", ["Python", "SQL"], extra=extra)
            return svc.process_resume(conn, pdf, f"{extra}.pdf", job_id, extract_llm=lambda s, u: json.dumps(profile),
                                      verify_llm=_judge, canon_llm=_judge, score_llm=slow_score).status
        except Exception as exc:
            return f"CRASH {type(exc).__name__}: {exc}"
        finally:
            conn.close()

    results = []
    threads = [threading.Thread(target=lambda e=e: results.append(upload(e))) for e in ("version one", "version two")]
    [t.start() for t in threads]
    [t.join(30) for t in threads]
    assert sorted(results) == ["conflict_pending", "stored"], results
    assert sorted(r[0] for r in pg.execute("SELECT application_status FROM applications")) == ["active", "pending_choice"]
    assert pg.execute("SELECT COUNT(*) FROM candidates").fetchone()[0] == 1


# ---------------------------------------------------------------- the whole app over HTTP
def test_api_end_to_end(pg_pool, monkeypatch):
    from fastapi.testclient import TestClient
    from app import deps
    from app.main import app
    pg, job_id = pg_pool
    pg.commit()
    monkeypatch.setattr(config, "AUTH_ENABLED", False)
    with TestClient(app) as client:
        jobs = client.get("/api/jobs").json()
        assert jobs and jobs[0]["id"] == job_id
        cards = client.get(f"/api/jobs/{job_id}/candidates").json()
        assert len(cards) == 3 and {"skills", "components", "gate_missing"} <= set(cards[0])
        assert client.put(f"/api/jobs/{job_id}/weights", json={"skills": 50, "experience": 20, "projects_education": 15, "fit": 15}).status_code == 200
        assert client.delete(f"/api/applications/{cards[0]['application_id']}").status_code == 200
        assert client.delete(f"/api/jobs/{job_id}").status_code == 200
        assert client.get("/api/db").status_code == 200


# ---------------------------------------------------------------- upgrading a database made by an earlier release
def test_a_database_from_an_earlier_release_is_upgraded_and_keeps_its_rows(monkeypatch):
    """Tables that predate weights, cutoffs, must-haves, owners and login revocation gain those columns; existing rows survive."""
    import uuid
    if not TEST_DATABASE_URL:
        pytest.skip(NEEDS_DB)
    schema = "shortlist_old_" + uuid.uuid4().hex[:8]
    admin = db.connect_pg(TEST_DATABASE_URL)
    try:
        admin.execute(f"CREATE SCHEMA {schema}")
        monkeypatch.setattr(config, "DATABASE_URL", schema_url(schema))
        old = database.get_connection()
        old.executescript("""
            CREATE TABLE users (id INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY, email TEXT NOT NULL UNIQUE, name TEXT NOT NULL DEFAULT '',
                password_hash TEXT NOT NULL, role TEXT NOT NULL DEFAULT 'recruiter', is_active INTEGER NOT NULL DEFAULT 1, created_at TEXT, last_login_at TEXT);
            CREATE TABLE job_descriptions (id INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY, title TEXT, raw_text TEXT, min_experience_years DOUBLE PRECISION,
                soft_skills TEXT, summary TEXT, created_at TEXT NOT NULL DEFAULT 'x');
            CREATE TABLE job_required_skills (id INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                job_description_id INTEGER NOT NULL REFERENCES job_descriptions(id) ON DELETE CASCADE, skill_name TEXT NOT NULL, importance TEXT NOT NULL DEFAULT 'required');
            INSERT INTO users (email, password_hash) VALUES ('old@x.com', 'h');
            INSERT INTO job_descriptions (title) VALUES ('Old job');
            INSERT INTO job_required_skills (job_description_id, skill_name) VALUES (1, 'Python');
        """)
        old.close()
        database.init_db()
        conn = database.get_connection()
        cols = {(r["table_name"], r["column_name"]) for r in conn.execute(
            "SELECT table_name, column_name FROM information_schema.columns WHERE table_schema = current_schema()")}
        assert {("users", "session_epoch"), ("job_descriptions", "owner_id"), ("job_descriptions", "weights"), ("job_descriptions", "cutoffs"),
                ("job_required_skills", "is_gate")} <= cols
        assert conn.execute("SELECT session_epoch FROM users WHERE email='old@x.com'").fetchone()[0] == 0
        assert conn.execute("SELECT is_gate FROM job_required_skills").fetchone()[0] == 0
        assert svc.get_job(conn, 1).required_skills == ["Python"]
        conn.close()
    finally:
        admin.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        admin.close()
