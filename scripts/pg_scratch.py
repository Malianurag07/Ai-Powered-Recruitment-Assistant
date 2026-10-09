"""Throw-away Postgres schemas for the scripts that need a database of their own (qa_offline, qa_live, benchmark).

The scripts read TEST_DATABASE_URL (from the environment or .env): a Postgres database where the role may CREATE SCHEMA. Each script
works inside its own schema and drops it when it exits, so nothing already in that database is touched. Call use_scratch_schema()
BEFORE importing anything from `app` (the app reads DATABASE_URL when it is first imported).
"""
import atexit
import os
import sys
import uuid
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")


def _admin_url() -> str:
    url = os.environ.get("TEST_DATABASE_URL", "").strip()
    if not url:
        sys.exit("This script needs a PostgreSQL database it may create throw-away schemas in. Set TEST_DATABASE_URL "
                 "(e.g. postgresql://user:password@localhost:5432/postgres) in the environment or in .env.")
    return url


def _with_schema(url: str, schema: str) -> str:
    return url + ("&" if "?" in url else "?") + "options=-c%20search_path%3D" + schema


def _create(prefix: str) -> tuple[str, str]:
    import psycopg
    url, schema = _admin_url(), f"{prefix}_{uuid.uuid4().hex[:8]}"
    with psycopg.connect(url, autocommit=True) as admin:
        admin.execute(f"CREATE SCHEMA {schema}")

    def drop():
        try:
            with psycopg.connect(url, autocommit=True) as admin:
                admin.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        except Exception as exc:                       # never hide the script's own result behind a cleanup problem
            print(f"(could not drop schema {schema}: {exc})")

    atexit.register(drop)
    return _with_schema(url, schema), schema


def use_scratch_schema(prefix: str) -> str:
    """Point DATABASE_URL at a fresh empty schema. Call before importing app code. Returns the schema name."""
    url, schema = _create(prefix)
    os.environ["DATABASE_URL"] = url
    return schema


def switch_to_new_scratch_schema(prefix: str) -> str:
    """After the app is imported: move to another fresh schema (used by the benchmark's parallel run)."""
    import app.config as config
    url, schema = _create(prefix)
    config.DATABASE_URL = url
    return schema
