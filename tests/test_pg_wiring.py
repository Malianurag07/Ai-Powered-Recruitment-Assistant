"""DATABASE_URL switches the app to Postgres; with it unset nothing changes. Tested with a fake driver (no Postgres server)."""
import sqlite3

import pytest

from app import config, database, db
from app.db import PgConnection
from app.services import quota
from test_pg_adapter import FakeRaw


class FakePsycopg:
    """Stands in for the psycopg module: records how it was asked to connect."""
    IntegrityError = type("IntegrityError", (Exception,), {})

    def __init__(self):
        self.calls, self.raw = [], FakeRaw()

    def connect(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.raw


@pytest.fixture
def fake_pg(monkeypatch):
    fake = FakePsycopg()
    monkeypatch.setattr(db, "_psycopg", fake)
    monkeypatch.setattr(config, "DATABASE_URL", "postgresql://u:p@db.example:5432/shortlist")
    return fake


def test_default_is_sqlite():
    assert config.DATABASE_URL == ""
    conn = database.get_connection()
    assert isinstance(conn, sqlite3.Connection) and not db.is_postgres(conn)
    conn.close()


def test_database_url_gives_a_postgres_connection_opened_the_safe_way(fake_pg):
    conn = database.get_connection()
    assert isinstance(conn, PgConnection) and db.is_postgres(conn)
    url, kwargs = fake_pg.calls[0]
    assert url == "postgresql://u:p@db.example:5432/shortlist"
    assert kwargs["autocommit"] is True and kwargs["connect_timeout"] == 10        # transactions are managed by the adapter; no endless hangs


def test_missing_driver_gives_a_clear_message(monkeypatch):
    monkeypatch.setattr(db, "_psycopg", None)
    monkeypatch.setattr(config, "DATABASE_URL", "postgresql://u:p@h/db")
    with pytest.raises(RuntimeError, match="pip install"):
        database.get_connection()


def test_init_db_creates_the_schema_upgrades_and_seeds_aliases(fake_pg):
    database.init_db()
    raw = fake_pg.raw
    sqls = raw.sql()
    assert sqls[0] == database.SCHEMA_PG                                            # the whole schema, in one go
    upgrades = [s for s in sqls if s.startswith("ALTER TABLE")]
    assert upgrades == list(database.PG_UPGRADES)
    seed = [e for e in raw.log if e[0] == "executemany"]
    assert len(seed) == 1 and "ON CONFLICT (alias) DO NOTHING" in seed[0][1] and "%s" in seed[0][1]
    assert len(seed[0][2]) == len(database.SEED_ALIASES)
    assert sqls[-1] == "COMMIT" and raw.closed                                      # committed and released


def test_init_db_on_sqlite_does_not_touch_postgres(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DATABASE_PATH", tmp_path / "x.db")
    database.init_db()
    c = sqlite3.connect(tmp_path / "x.db")
    assert c.execute("SELECT COUNT(*) FROM skill_aliases").fetchone()[0] == len(database.SEED_ALIASES)


def test_quota_counter_does_not_create_its_table_on_postgres(fake_pg):
    conn = quota._conn()
    assert db.is_postgres(conn)
    assert not any("CREATE TABLE" in s for s in fake_pg.raw.sql())                  # it is part of SCHEMA_PG
    quota.record_call("gemini")
    inserts = [s for s in fake_pg.raw.sql() if s.startswith("INSERT INTO ai_usage")]
    assert inserts and "ON CONFLICT(day, bucket) DO UPDATE" in inserts[0] and "%s" in inserts[0] and "RETURNING" not in inserts[0]


def test_duplicate_account_is_reported_the_same_way_on_both_databases(monkeypatch):
    """create_user must turn the driver's duplicate-email error into AuthError, whichever driver raised it."""
    from app.services import auth_service as auth

    class PgDup(Exception):
        pass

    class Conn:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, *a, **k): raise PgDup("duplicate key value violates unique constraint")

    monkeypatch.setattr(auth, "INTEGRITY_ERRORS", (sqlite3.IntegrityError, PgDup))
    with pytest.raises(auth.AuthError, match="already exists"):
        auth.create_user(Conn(), "dup@x.com", "Passw0rd!long", "Dup", "recruiter")
