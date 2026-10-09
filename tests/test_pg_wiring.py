"""DATABASE_URL selects the database and nothing else does. Tested with a fake driver: no Postgres server is contacted."""
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


def test_without_a_database_url_the_error_says_what_to_do(monkeypatch):
    monkeypatch.setattr(config, "DATABASE_URL", "")
    with pytest.raises(RuntimeError, match="DATABASE_URL is not set"):
        database.get_connection()


def test_database_url_gives_a_connection_opened_the_safe_way(fake_pg):
    conn = database.get_connection()
    assert isinstance(conn, PgConnection)
    url, kwargs = fake_pg.calls[0]
    assert url == "postgresql://u:p@db.example:5432/shortlist"
    assert kwargs["autocommit"] is True and kwargs["connect_timeout"] == 10        # transactions are managed by the adapter; no endless hangs
    assert kwargs["prepare_threshold"] is None                                      # safe behind a transaction-mode connection pooler


def test_missing_driver_gives_a_clear_message(monkeypatch):
    monkeypatch.setattr(db, "_psycopg", None)
    monkeypatch.setattr(config, "DATABASE_URL", "postgresql://u:p@h/db")
    with pytest.raises(RuntimeError, match="pip install"):
        database.get_connection()


def test_init_db_creates_the_schema_upgrades_and_seeds_aliases(fake_pg):
    database.init_db()
    raw = fake_pg.raw
    sqls = raw.sql()
    assert sqls[0] == database.SCHEMA                                               # the whole schema, in one go
    assert [s for s in sqls if s.startswith("ALTER TABLE")] == list(database.SCHEMA_UPGRADES)
    seed = [e for e in raw.log if e[0] == "executemany"]
    assert len(seed) == 1 and "ON CONFLICT (alias) DO NOTHING" in seed[0][1] and "%s" in seed[0][1]
    assert len(seed[0][2]) == len(database.SEED_ALIASES)
    assert sqls[-1] == "COMMIT" and raw.closed                                      # committed and released


def test_quota_counter_writes_one_upsert_and_never_creates_its_table(fake_pg):
    quota.record_call("gemini")
    sqls = fake_pg.raw.sql()
    assert not any("CREATE TABLE" in s for s in sqls)                               # ai_usage is part of the schema
    inserts = [s for s in sqls if s.startswith("INSERT INTO ai_usage")]
    assert inserts and "ON CONFLICT(day, bucket) DO UPDATE" in inserts[0] and "%s" in inserts[0] and "RETURNING" not in inserts[0]


def test_duplicate_account_is_reported_as_a_friendly_error(monkeypatch):
    """create_user turns the driver's duplicate-email error into AuthError."""
    from app.services import auth_service as auth

    class Dup(Exception):
        pass

    class Conn:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, *a, **k):
            raise Dup("duplicate key value violates unique constraint")

    monkeypatch.setattr(auth, "INTEGRITY_ERRORS", (Dup,))
    with pytest.raises(auth.AuthError, match="already exists"):
        auth.create_user(Conn(), "dup@x.com", "Passw0rd!long", "Dup", "recruiter")
