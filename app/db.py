"""PostgreSQL access for the whole app: a thin adapter over the psycopg driver.

The app's ~100 queries are written with `?` placeholders and use `cursor.lastrowid`, so rather than rewrite them, `PgConnection` provides:

  * `?` placeholders (translated to the driver's `%s`)
  * rows readable by name and by position, and convertible with `dict(row)`
  * `cursor.lastrowid` after an INSERT (done with `RETURNING id`)
  * `with conn:` commits on success and rolls back on error, and does NOT close the connection
  * transactions that begin at the first write, so a read never leaves a connection "idle in transaction" while the AI is working
  * `conn.begin_exclusive()`: start a transaction and take the app-wide write lock (a Postgres advisory lock held until commit or rollback)
"""
import re
from datetime import datetime, timezone

try:                                              # the driver is installed from requirements.txt; imports of this module work without it
    import psycopg as _psycopg
except ImportError:                               # depends on the environment
    _psycopg = None


class _DriverMissing(Exception):
    """Placeholder so `except INTEGRITY_ERRORS` is valid even when the driver is not installed (it is never raised)."""


# Raised when a unique, foreign-key or check rule is broken: code catches this tuple.
INTEGRITY_ERRORS: tuple = (_psycopg.IntegrityError,) if _psycopg else (_DriverMissing,)

# Tables whose primary key is an integer `id` (identity column); an INSERT into one of these can report the new id.
ID_TABLES = frozenset({"candidates", "job_descriptions", "job_required_skills", "applications", "application_skills",
                       "resume_chunks", "analysis_results", "verification_log", "users", "chat_history"})
WRITE_LOCK_KEY = 7_340_001            # arbitrary constant: everything that takes the write lock uses this one advisory lock
_WRITES = {"INSERT", "UPDATE", "DELETE"}


def utc_now() -> str:
    """Current UTC time as 'YYYY-MM-DD HH:MM:SS': the same text format the schema's created_at columns default to."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------- SQL translation
def translate_sql(sql: str) -> str:
    """The app's SQL (written with `?`) -> driver style: `?` becomes `%s`, and a literal `%` becomes `%%` (the driver treats `%` as special).
    Text inside quotes and `--` comments is left alone except for `%`."""
    out: list[str] = []
    i, n, quote = 0, len(sql), None
    while i < n:
        ch = sql[i]
        if quote:
            out.append("%%" if ch == "%" else ch)
            if ch == quote:
                if i + 1 < n and sql[i + 1] == quote:          # doubled quote ('it''s') stays inside the string
                    out.append(sql[i + 1])
                    i += 1
                else:
                    quote = None
        elif ch in ("'", '"'):
            quote = ch
            out.append(ch)
        elif ch == "-" and sql.startswith("--", i):
            end = sql.find("\n", i)
            end = n if end == -1 else end
            out.append(sql[i:end].replace("%", "%%"))
            i = end - 1
        elif ch == "?":
            out.append("%s")
        elif ch == "%":
            out.append("%%")
        else:
            out.append(ch)
        i += 1
    return "".join(out)


def _clean_params(params) -> tuple:
    """Postgres refuses the NUL character (\x00) in text, which can appear in text pulled from a PDF. Drop it."""
    return tuple(p.replace(chr(0), "") if isinstance(p, str) else p for p in params)


def _first_word(sql: str) -> str:
    m = re.match(r"\s*(?:--[^\n]*\n\s*)*\(?\s*(\w+)", sql)
    return m.group(1).upper() if m else ""


def _insert_table(sql: str) -> str | None:
    m = re.match(r"\s*INSERT\s+INTO\s+(\w+)", sql, re.I)
    return m.group(1).lower() if m else None


# ---------------------------------------------------------------- rows
class PgRow:
    """A result row: `row["name"]`, `row[0]`, `row.keys()`, `dict(row)`, `len(row)`, iteration over values."""
    __slots__ = ("_names", "_values", "_index")

    def __init__(self, names: list[str], values: tuple):
        self._names = names
        self._values = tuple(bytes(v) if isinstance(v, memoryview) else v for v in values)      # BYTEA arrives as memoryview
        self._index = {name: i for i, name in enumerate(names)}

    def __getitem__(self, key):
        if isinstance(key, (int, slice)):
            return self._values[key]
        try:
            return self._values[self._index[key]]
        except KeyError:
            raise IndexError(f"No item with that key: {key!r}") from None

    def keys(self) -> list[str]:
        return list(self._names)

    def __iter__(self):
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)

    def __eq__(self, other):
        if isinstance(other, tuple):
            return self._values == other
        return isinstance(other, PgRow) and self._names == other._names and self._values == other._values

    def __hash__(self):
        return hash(self._values)

    def __repr__(self) -> str:
        return f"PgRow({dict(zip(self._names, self._values))})"


# ---------------------------------------------------------------- cursor and connection
class PgCursor:
    def __init__(self, conn: "PgConnection"):
        self._conn = conn
        self._cur = conn._raw.cursor()
        self.lastrowid: int | None = None

    # -- running statements
    def execute(self, sql: str, params=()):
        conn, kind = self._conn, _first_word(sql)
        self.lastrowid = None
        if kind in _WRITES:
            conn._begin()
        pg_sql, want_id = translate_sql(sql), False
        if kind == "INSERT" and _insert_table(sql) in ID_TABLES and not re.search(r"\bRETURNING\b", sql, re.I):
            pg_sql, want_id = pg_sql.rstrip().rstrip(";") + " RETURNING id", True
        self._cur.execute(pg_sql, _clean_params(params))
        if want_id:
            row = self._cur.fetchone()
            self.lastrowid = row[0] if row else None             # None when ON CONFLICT DO NOTHING skipped the insert
        return self

    def executemany(self, sql: str, seq):
        kind = _first_word(sql)
        if kind in _WRITES:
            self._conn._begin()
        self.lastrowid = None
        self._cur.executemany(translate_sql(sql), [_clean_params(p) for p in seq])
        return self

    # -- reading results
    @property
    def description(self):
        return self._cur.description

    @property
    def rowcount(self) -> int:
        return self._cur.rowcount

    def _names(self) -> list[str]:
        return [getattr(d, "name", None) or d[0] for d in self._cur.description]

    def fetchone(self):
        if self._cur.description is None:
            return None
        v = self._cur.fetchone()
        return None if v is None else PgRow(self._names(), v)

    def fetchall(self):
        if self._cur.description is None:
            return []
        names = self._names()
        return [PgRow(names, v) for v in self._cur.fetchall()]

    def fetchmany(self, size: int = 1):
        if self._cur.description is None:
            return []
        names = self._names()
        return [PgRow(names, v) for v in self._cur.fetchmany(size)]

    def __iter__(self):
        yield from self.fetchall()

    def close(self) -> None:
        self._cur.close()


class PgConnection:
    def __init__(self, raw):
        self._raw = raw                      # a psycopg connection opened with autocommit=True: transactions are managed here
        self._in_tx = False

    @property
    def in_transaction(self) -> bool:
        return self._in_tx

    def cursor(self) -> PgCursor:
        return PgCursor(self)

    def execute(self, sql: str, params=()) -> PgCursor:
        return self.cursor().execute(sql, params)

    def executemany(self, sql: str, seq) -> PgCursor:
        return self.cursor().executemany(sql, seq)

    def executescript(self, script: str) -> None:
        self.commit()
        self._raw.execute(script)            # no parameters, so several statements may be sent at once

    def begin_exclusive(self) -> None:
        """Start a transaction and take the app-wide write lock, held until commit or rollback. Used where a check and a write must be atomic."""
        self._begin(lock=True)

    def _begin(self, lock: bool = False) -> None:
        if not self._in_tx:
            self._raw.execute("BEGIN")
            self._in_tx = True
        if lock:
            self._raw.execute("SELECT pg_advisory_xact_lock(%s)", (WRITE_LOCK_KEY,))      # released automatically at COMMIT or ROLLBACK

    def commit(self) -> None:
        if self._in_tx:
            self._in_tx = False
            self._raw.execute("COMMIT")

    def rollback(self) -> None:
        if self._in_tx:
            self._in_tx = False
            self._raw.execute("ROLLBACK")

    def close(self) -> None:
        try:
            self.rollback()
        finally:
            self._raw.close()

    def __enter__(self) -> "PgConnection":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        self.commit() if exc_type is None else self.rollback()
        return False                          # never swallow the error, never close the connection


def connect_pg(url: str) -> PgConnection:
    if _psycopg is None:
        raise RuntimeError('DATABASE_URL is set but the Postgres driver is not installed. Run:  pip install "psycopg[binary]"')
    return PgConnection(_psycopg.connect(url, autocommit=True, connect_timeout=10, application_name="shortlist"))
