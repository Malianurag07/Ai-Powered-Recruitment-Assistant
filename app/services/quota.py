"""Free-tier budget: how many AI calls a batch of resumes needs, how many are left, and how long to wait between resumes.

Honest sources of the "remaining" number:
  * Groq sends its remaining request count in every response (headers); we keep the latest, and make one tiny probe call when
    the last reading is stale.
  * Gemini sends no remaining-quota information, so we count the calls this app made today and compare with GEMINI_CALLS_PER_DAY.
    That ignores use of the same key elsewhere, so it is an estimate and the UI says so.
"""
import sqlite3
import threading
import time
from datetime import date

from app import config
from app.database import get_connection
from app.db import is_postgres

# AI calls one resume costs, by quota bucket (measured about 6.6 to 8: extraction + name matching, meaning match + judge samples,
# second-check + embeddings). Used only for planning; the real pipeline is unchanged.
def calls_per_resume() -> dict[str, int]:
    return {"groq_fast": 2, "groq_big": 1 + config.JUDGE_SAMPLES, "gemini": 2}


SECONDS_PER_RESUME = 13.0
FRESH_SECONDS = 300
_lock = threading.Lock()
_headers: dict[str, dict] = {}        # bucket -> {"remaining": int, "limit": int, "seen": epoch seconds}
_last_rate_limited = 0.0


def _conn() -> sqlite3.Connection:
    conn = get_connection(check_same_thread=False)
    if is_postgres(conn):                      # on Postgres the table is created with the rest of the schema
        return conn
    conn.execute("CREATE TABLE IF NOT EXISTS ai_usage (day TEXT NOT NULL, bucket TEXT NOT NULL, calls INTEGER NOT NULL DEFAULT 0, "
                 "PRIMARY KEY (day, bucket))")
    return conn


def bucket_for(provider: str, model: str | None) -> str:
    if provider == "gemini":
        return "gemini"
    return "groq_big" if model == config.GROQ_MODEL else "groq_fast"


def record_call(bucket: str) -> None:
    """Count one successful AI call today. Never raises: counting must not break a real call."""
    try:
        conn = _conn()
        with conn:
            conn.execute("INSERT INTO ai_usage (day, bucket, calls) VALUES (?,?,1) "
                         "ON CONFLICT(day, bucket) DO UPDATE SET calls = calls + 1", (date.today().isoformat(), bucket))
        conn.close()
    except Exception:
        pass


def used_today(bucket: str) -> int:
    try:
        conn = _conn()
        row = conn.execute("SELECT calls FROM ai_usage WHERE day=? AND bucket=?", (date.today().isoformat(), bucket)).fetchone()
        conn.close()
        return row["calls"] if row else 0
    except Exception:
        return 0


def note_groq_headers(bucket: str, headers) -> None:
    """Keep Groq's own remaining-requests reading (headers x-ratelimit-remaining-requests / -limit-requests)."""
    try:
        remaining = int(headers.get("x-ratelimit-remaining-requests"))
        limit = int(headers.get("x-ratelimit-limit-requests"))
    except (TypeError, ValueError):
        return
    with _lock:
        _headers[bucket] = {"remaining": remaining, "limit": limit, "seen": time.time()}


def note_rate_limited() -> None:
    global _last_rate_limited
    _last_rate_limited = time.time()


def daily_limit(bucket: str) -> int:
    return {"groq_fast": config.GROQ_FAST_CALLS_PER_DAY, "groq_big": config.GROQ_CALLS_PER_DAY, "gemini": config.GEMINI_CALLS_PER_DAY}[bucket]


def remaining(bucket: str) -> tuple[int, int, str]:
    """(calls left today, daily limit, source) where source is 'reported by Groq' or 'counted by this app'."""
    with _lock:
        h = _headers.get(bucket)
    if h and time.time() - h["seen"] < FRESH_SECONDS:
        return h["remaining"], h["limit"], "reported by Groq"
    limit = daily_limit(bucket)
    return max(0, limit - used_today(bucket)), limit, "counted by this app"


def suggested_pause() -> float:
    """Seconds to wait before the next resume of a batch: a small gap, longer while the provider is pushing back."""
    recently = time.time() - _last_rate_limited < 60
    return config.BATCH_PAUSE_AFTER_LIMIT_SECONDS if recently else config.BATCH_PAUSE_SECONDS


def out_of_calls() -> str | None:
    """Name of a bucket that cannot afford one more resume, else None. Lets a running batch stop cleanly instead of failing."""
    for bucket, need in calls_per_resume().items():
        left, _, source = remaining(bucket)
        if source == "reported by Groq" and left < need:
            return bucket
    return None


def estimate(count: int) -> dict:
    """What a batch of `count` resumes needs against what is left, per bucket, plus a verdict and the largest batch that fits."""
    per = calls_per_resume()
    rows, fits = [], []
    names = {"groq_fast": f"Groq · {config.GROQ_FAST_MODEL}", "groq_big": f"Groq · {config.GROQ_MODEL}", "gemini": f"Gemini · {config.GEMINI_MODEL}"}
    worst = 0.0
    for bucket, need_each in per.items():
        left, limit, source = remaining(bucket)
        need = need_each * count
        fits.append(left // need_each if need_each else count)
        worst = max(worst, need / left if left else float("inf"))
        rows.append({"bucket": bucket, "name": names[bucket], "needs": need, "left": left, "limit": limit, "source": source})
    max_now = min(fits) if fits else count
    verdict = "over" if count > max_now else "tight" if worst > 0.8 else "ok"
    seconds = count * (SECONDS_PER_RESUME + config.BATCH_PAUSE_SECONDS)
    return {"count": count, "calls_total": sum(r["needs"] for r in rows), "minutes": round(seconds / 60, 1), "pause_seconds": config.BATCH_PAUSE_SECONDS,
            "rows": rows, "verdict": verdict, "max_resumes_now": max_now}
