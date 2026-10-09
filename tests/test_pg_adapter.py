"""The Postgres adapter (app/db.py), tested with a fake driver: no Postgres server is needed or contacted."""
import re

import pytest

from app import db
from app.database import SCHEMA
from app.db import ID_TABLES, PgConnection, PgRow, translate_sql


class FakeCursor:
    def __init__(self, raw):
        self.raw, self.description, self._rows, self.rowcount = raw, None, [], 0

    def execute(self, sql, params=None):
        self.raw.log.append(("execute", sql, params))
        for needle, (cols, rows) in self.raw.script.items():
            if needle in sql:
                self.description, self._rows = [(c,) for c in cols], list(rows)
                break
        else:
            self.description, self._rows = None, []

    def executemany(self, sql, seq):
        self.raw.log.append(("executemany", sql, list(seq)))

    def fetchone(self):
        return self._rows.pop(0) if self._rows else None

    def fetchall(self):
        rows, self._rows = self._rows, []
        return rows

    def fetchmany(self, n):
        rows, self._rows = self._rows[:n], self._rows[n:]
        return rows

    def close(self):
        pass


class FakeRaw:
    def __init__(self, script=None):
        self.log, self.script, self.closed = [], script or {}, False

    def cursor(self):
        return FakeCursor(self)

    def execute(self, sql, params=None):
        self.log.append(("raw", sql, params))

    def close(self):
        self.closed = True

    def sql(self):
        return [e[1] for e in self.log]


def conn_with(script=None):
    raw = FakeRaw(script)
    return PgConnection(raw), raw


# ---------------------------------------------------------------- SQL translation
def test_placeholders_and_percent_signs():
    assert translate_sql("SELECT * FROM t WHERE a=? AND b=?") == "SELECT * FROM t WHERE a=%s AND b=%s"
    assert translate_sql("WHERE x LIKE '%abc%' AND y=?") == "WHERE x LIKE '%%abc%%' AND y=%s"
    assert translate_sql("WHERE x LIKE ? ESCAPE '\\'") == "WHERE x LIKE %s ESCAPE '\\'"


def test_question_marks_inside_text_and_comments_are_left_alone():
    assert translate_sql("SELECT 'what?' , ?") == "SELECT 'what?' , %s"
    assert translate_sql("SELECT 'it''s ? fine', ?") == "SELECT 'it''s ? fine', %s"
    assert translate_sql('SELECT "odd?col", ?') == 'SELECT "odd?col", %s'
    assert translate_sql("SELECT 1 -- is it 100%? yes\nWHERE a=?") == "SELECT 1 -- is it 100%%? yes\nWHERE a=%s"


# ---------------------------------------------------------------- rows
def test_row_reads_by_name_and_position_and_converts_to_dict():
    r = PgRow(["id", "name", "blob"], (7, "Ann", memoryview(b"\x01\x02")))
    assert r["id"] == 7 and r[1] == "Ann" and r[-1] == b"\x01\x02" and isinstance(r["blob"], bytes)
    assert dict(r) == {"id": 7, "name": "Ann", "blob": b"\x01\x02"} and r.keys() == ["id", "name", "blob"]
    assert len(r) == 3 and list(r) == [7, "Ann", b"\x01\x02"] and r[0:2] == (7, "Ann")
    with pytest.raises(IndexError):
        r["missing"]


def test_fetching_wraps_rows_and_handles_statements_without_results():
    conn, raw = conn_with({"FROM users": (["id", "email"], [(1, "a@x.com"), (2, "b@x.com")])})
    cur = conn.execute("SELECT id, email FROM users WHERE id > ?", (0,))
    assert cur.fetchone()["email"] == "a@x.com" and [r["id"] for r in cur.fetchall()] == [2]
    assert [r[1] for r in conn.execute("SELECT id, email FROM users")] == ["a@x.com", "b@x.com"]       # iteration works
    assert conn.execute("SELECT id, email FROM users").description[0][0] == "id"
    assert conn.execute("DELETE FROM things WHERE id=?", (1,)).fetchone() is None
    assert conn.execute("DELETE FROM things WHERE id=?", (1,)).fetchall() == []


# ---------------------------------------------------------------- lastrowid
def test_insert_into_a_table_with_an_id_reports_lastrowid():
    conn, raw = conn_with({"RETURNING id": (["id"], [(42,)])})
    cur = conn.execute("INSERT INTO candidates (name, email) VALUES (?,?)", ("A", "a@x.com"))
    assert cur.lastrowid == 42
    assert raw.log[-1][1] == "INSERT INTO candidates (name, email) VALUES (%s,%s) RETURNING id"


def test_inserts_that_already_say_returning_or_have_no_id_are_left_alone():
    conn, raw = conn_with()
    conn.execute("INSERT INTO skill_aliases (alias, canonical) VALUES (?, ?) ON CONFLICT (alias) DO NOTHING", ("ml", "Machine Learning"))
    conn.execute("INSERT INTO ai_usage (day, bucket, calls) VALUES (?,?,1) ON CONFLICT(day, bucket) DO UPDATE SET calls = calls + 1", ("d", "b"))
    conn.execute("INSERT INTO users (email) VALUES (?) RETURNING id", ("a@x.com",))
    statements = [s for s in raw.sql() if s.startswith("INSERT")]
    assert len(statements) == 3
    assert "RETURNING" not in statements[0] and "RETURNING" not in statements[1]
    assert statements[2].count("RETURNING") == 1                       # not added a second time


def test_insert_skipped_by_on_conflict_gives_no_id():
    conn, _ = conn_with({"RETURNING id": (["id"], [])})
    assert conn.execute("INSERT INTO analysis_results (application_id) VALUES (?) ON CONFLICT (application_id) DO NOTHING", (1,)).lastrowid is None


def test_every_table_with_an_identity_column_is_listed_in_id_tables():
    with_identity = set(re.findall(r"CREATE TABLE IF NOT EXISTS (\w+) \(\s*id INTEGER GENERATED", SCHEMA))
    assert with_identity == set(ID_TABLES)


# ---------------------------------------------------------------- transactions
def test_reads_do_not_start_a_transaction_but_the_first_write_does():
    conn, raw = conn_with()
    conn.execute("SELECT 1")
    assert not conn.in_transaction and "BEGIN" not in raw.sql()
    conn.execute("UPDATE users SET name=? WHERE id=?", ("x", 1))
    conn.execute("UPDATE users SET name=? WHERE id=?", ("y", 2))
    assert conn.in_transaction and raw.sql().count("BEGIN") == 1                   # begun once, not per statement
    conn.commit()
    assert not conn.in_transaction and raw.sql()[-1] == "COMMIT"
    conn.commit()                                                                  # nothing open: no second COMMIT
    assert raw.sql().count("COMMIT") == 1


def test_with_block_commits_on_success_rolls_back_on_error_and_keeps_the_connection_open():
    conn, raw = conn_with()
    with conn:
        conn.execute("DELETE FROM users WHERE id=?", (1,))
    assert raw.sql()[-1] == "COMMIT" and not raw.closed
    with pytest.raises(ValueError):
        with conn:
            conn.execute("DELETE FROM users WHERE id=?", (2,))
            raise ValueError("boom")
    assert raw.sql()[-1] == "ROLLBACK" and not raw.closed                           # the error is not swallowed, the connection stays usable
    commits_before = raw.sql().count("COMMIT")
    with conn:                                                                      # a with block over reads only: no transaction was open, so no COMMIT
        conn.execute("SELECT 1")
    assert raw.sql().count("COMMIT") == commits_before and raw.sql()[-1] == "SELECT 1"


def test_begin_exclusive_starts_a_transaction_and_takes_the_write_lock():
    conn, raw = conn_with()
    conn.begin_exclusive()
    assert conn.in_transaction and raw.sql() == ["BEGIN", "SELECT pg_advisory_xact_lock(%s)"]
    assert raw.log[-1][2] == (db.WRITE_LOCK_KEY,)
    conn.execute("INSERT INTO candidates (name) VALUES (?)", ("A",))
    assert raw.sql().count("BEGIN") == 1                                            # the insert joins the open transaction
    conn.commit()
    assert raw.sql()[-1] == "COMMIT"


def test_rollback_and_close():
    conn, raw = conn_with()
    conn.rollback()
    assert "ROLLBACK" not in raw.sql()
    conn.execute("UPDATE users SET name=?", ("x",))
    conn.close()
    assert raw.sql()[-1] == "ROLLBACK" and raw.closed                               # closing with work pending discards it


def test_executemany_joins_a_transaction_and_translates():
    conn, raw = conn_with()
    conn.executemany("INSERT INTO verification_log (application_id, field) VALUES (?,?)", [(1, "a"), (1, "b")])
    assert "BEGIN" in raw.sql()
    kind, sql, rows = raw.log[-1]
    assert kind == "executemany" and "%s,%s" in sql and rows == [(1, "a"), (1, "b")]


def test_executescript_commits_first_then_sends_the_script_whole():
    conn, raw = conn_with()
    conn.execute("UPDATE users SET name=?", ("x",))
    conn.executescript("CREATE TABLE a (id int); CREATE TABLE b (id int);")
    assert raw.sql()[-2:] == ["COMMIT", "CREATE TABLE a (id int); CREATE TABLE b (id int);"]


def test_utc_now_and_integrity_errors():
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", db.utc_now())
    assert isinstance(db.INTEGRITY_ERRORS, tuple) and db.INTEGRITY_ERRORS and all(issubclass(e, Exception) for e in db.INTEGRITY_ERRORS)


def test_a_row_equals_a_plain_tuple_of_its_values():
    r = PgRow(["a", "b"], (1, "x"))
    assert r == (1, "x") and r != (1, "y") and r == PgRow(["a", "b"], (1, "x"))


def test_nul_characters_are_removed_from_text_but_binary_data_is_untouched():
    conn, raw = conn_with()
    conn.execute("UPDATE applications SET raw_text=?, resume_hash=? WHERE id=?", ("ab\x00cd", "h\x00", 1))
    assert raw.log[-1][2] == ("abcd", "h", 1)
    conn.executemany("INSERT INTO resume_chunks (application_id, chunk_index, text, embedding) VALUES (?,?,?,?)", [(1, 0, "x\x00y", b"\x00\x01")])
    assert raw.log[-1][2] == [(1, 0, "xy", b"\x00\x01")]                           # the NUL inside binary embeddings must stay
