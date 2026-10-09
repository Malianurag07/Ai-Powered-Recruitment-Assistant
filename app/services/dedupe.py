"""Duplicate-resume policy, judged per job description.

Same person = same email OR same phone. Then, for one job:
  - resume text identical to the active one  -> IDENTICAL   (keep one, ignore the extra)
  - resume text different                    -> CONFLICT    (hold as pending; applicant chooses)
  - no active application for this job yet   -> NEW_APPLICATION (other-job resumes are fine)
  - person unknown                           -> NEW_CANDIDATE
"""
import hashlib
import re
from dataclasses import dataclass
from app.db import PgConnection

NEW_CANDIDATE, NEW_APPLICATION, IDENTICAL, CONFLICT = "new_candidate", "new_application", "identical", "conflict"


def resume_hash(text: str) -> str:
    """Hash of normalized text, so re-exports of the same resume (spacing, case) still match."""
    normalized = re.sub(r"\s+", " ", text).strip().lower()
    return hashlib.sha256(normalized.encode()).hexdigest()


def normalize_phone(phone: str | None) -> str | None:
    digits = re.sub(r"\D", "", phone or "")
    return digits[-10:] if len(digits) >= 10 else None  # ignore country code (+91 etc.)


@dataclass
class DedupeDecision:
    kind: str
    candidate_id: int | None = None
    existing_application_id: int | None = None


def find_candidate(conn: PgConnection, email: str | None, phone: str | None) -> int | None:
    if email:
        row = conn.execute("SELECT id FROM candidates WHERE email = ?", (email.lower(),)).fetchone()
        if row:
            return row["id"]
    want = normalize_phone(phone)
    if want:
        for row in conn.execute("SELECT id, phone FROM candidates WHERE phone IS NOT NULL"):
            if normalize_phone(row["phone"]) == want:
                return row["id"]
    return None


def decide(conn: PgConnection, email: str | None, phone: str | None,
           job_id: int, text: str) -> DedupeDecision:
    cid = find_candidate(conn, email, phone)
    if cid is None:
        return DedupeDecision(NEW_CANDIDATE)
    active = conn.execute(
        "SELECT id, resume_hash FROM applications WHERE candidate_id=? AND job_description_id=? "
        "AND application_status='active'", (cid, job_id)).fetchone()
    if active is None:
        return DedupeDecision(NEW_APPLICATION, cid)
    if active["resume_hash"] == resume_hash(text):
        return DedupeDecision(IDENTICAL, cid, active["id"])
    return DedupeDecision(CONFLICT, cid, active["id"])


def resolve_conflict(conn: PgConnection, keep_application_id: int) -> None:
    """Applicant picked which resume to keep: it becomes active, the other becomes superseded."""
    row = conn.execute("SELECT candidate_id, job_description_id FROM applications WHERE id=?",
                       (keep_application_id,)).fetchone()
    if row is None:
        raise ValueError("Unknown application")
    # Demote first so the partial unique index (one active per person per job) is never violated.
    conn.execute("UPDATE applications SET application_status='superseded' "
                 "WHERE candidate_id=? AND job_description_id=? AND application_status IN ('active','pending_choice')",
                 (row["candidate_id"], row["job_description_id"]))
    conn.execute("UPDATE applications SET application_status='active' WHERE id=?", (keep_application_id,))
