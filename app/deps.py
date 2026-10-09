"""Shared FastAPI dependencies: one private database connection per request, plus injectable LLM overrides."""
from typing import Iterator

from app.database import get_connection, init_db
from app.db import PgConnection

# Tests put fake LLM callables here (extract_llm=..., score_llm=..., plan_llm=...); in production these stay empty
# and the pipeline uses the real Groq/Gemini clients.
PIPELINE_LLMS: dict = {}
CHAT_LLMS: dict = {}
JOB_LLMS: dict = {}


def get_db() -> Iterator[PgConnection]:
    conn = get_connection()
    try:
        yield conn
    finally:
        conn.close()


def startup() -> None:
    init_db()
    from app.services import auth_service        # local import: auth is optional and needs the database ready
    with get_connection() as conn:
        auth_service.bootstrap_admin(conn)
