import pytest

from app.services import dedupe as d
from conftest import resync_ids


@pytest.fixture
def conn(pg):
    c = pg
    c.execute("INSERT INTO job_descriptions (id, title) VALUES (1, 'ML Engineer'), (2, 'Tester')")
    c.execute("INSERT INTO candidates (id, name, email, phone) VALUES (1, 'A', 'a@x.com', '+91 98765 43210')")
    c.execute("INSERT INTO applications (candidate_id, job_description_id, resume_hash) VALUES (1, 1, ?)",
              (d.resume_hash("Resume ONE"),))
    resync_ids(c)                              # the fixture used explicit ids for the first rows
    return c


def test_unknown_person(conn):
    assert d.decide(conn, "new@x.com", "1111111111", 1, "t").kind == d.NEW_CANDIDATE


def test_same_job_identical_text_ignored(conn):
    assert d.decide(conn, "a@x.com", None, 1, "resume   one").kind == d.IDENTICAL  # spacing/case ignored


def test_same_job_different_text_conflict(conn):
    assert d.decide(conn, "a@x.com", None, 1, "Totally different resume").kind == d.CONFLICT


def test_other_job_allowed(conn):
    assert d.decide(conn, "a@x.com", None, 2, "Different resume").kind == d.NEW_APPLICATION


def test_matched_by_phone_when_email_differs(conn):
    r = d.decide(conn, "other@x.com", "098765 43210", 1, "Something else")
    assert r.kind == d.CONFLICT and r.candidate_id == 1


def test_resolve_conflict_keeps_chosen(conn):
    conn.execute("INSERT INTO applications (id, candidate_id, job_description_id, resume_hash, application_status) "
                 "VALUES (99, 1, 1, 'h2', 'pending_choice')")
    d.resolve_conflict(conn, 99)
    rows = {r["id"]: r["application_status"] for r in conn.execute("SELECT id, application_status FROM applications")}
    assert rows[99] == "active" and rows[1] == "superseded"
