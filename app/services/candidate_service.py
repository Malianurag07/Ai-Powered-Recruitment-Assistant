"""Ties the pipeline together: file -> text -> profile -> verify -> canonical skills -> dedupe -> score -> SQLite.

All LLM callables are injectable (extract_llm, verify_llm, canon_llm, score_llm) so tests need no API keys.
"""
import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass

from app.config import SEMANTIC_INDEXING
from app.llm import client
from app.llm.extraction import extract_profile
from app.llm.jd_parsing import extract_job
from app.llm.retrieval import chunk_text, pack
from app.llm.scoring import (ScoreResult, default_cutoffs, default_weights_percent, effective_cutoffs, effective_weights, final_label,
                             score_candidate, validate_cutoffs, validate_weights_percent, weighted_score)
from app.llm.skill_canonicalizer import canonicalize_profile, canonicalize_skills, save_learned
from app.llm.verification import verify_profile
from app.models import CandidateProfile, JobProfile
from app.parsing.document_extractor import extract_document
from app.parsing.ocr import default_reader
from app.services import dedupe
from app.services.skill_normalizer import load_aliases


@dataclass
class Outcome:
    filename: str
    status: str            # stored | duplicate_ignored | conflict_pending | needs_review | rejected_file
    message: str = ""
    application_id: int | None = None
    candidate_id: int | None = None
    score: float | None = None
    recommendation: str | None = None
    verification_status: str | None = None
    ocr_used: bool = False        # the resume was a scan and its text was read by OCR


@contextmanager
def _write_lock(conn: sqlite3.Connection):
    """Take SQLite's write lock up front (BEGIN IMMEDIATE), so the duplicate check and the insert happen atomically.

    Without it, two uploads of the same person could both see "no active resume yet" and both insert.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


def _kw(llm):
    return {} if llm is None else {"llm": llm}


# ---------------------------------------------------------------- jobs
def create_job(conn: sqlite3.Connection, text: str, canon_llm=None, jd_llm=None, owner_id: int | None = None) -> tuple[int | None, str]:
    """Parse a job description and store it. Returns (job_id, message); job_id is None on failure."""
    res = extract_job(text, **_kw(jd_llm))
    if res.status != "ok":
        return None, res.error or "Could not understand the job description"
    return _store_job(conn, res.job, text, canon_llm, owner_id)


def build_job_text(job: JobProfile, responsibilities: str = "") -> str:
    """Readable job description written from the answers of the job builder (kept as the job's raw text)."""
    parts = [f"Job Title: {job.title}"]
    if job.summary:
        parts.append(f"About the role\n{job.summary}")
    if responsibilities.strip():
        parts.append(f"Responsibilities\n{responsibilities.strip()}")
    parts.append("Requirements\n" + "\n".join(f"- {s}" for s in job.required_skills))
    if job.preferred_skills:
        parts.append("Nice to have\n" + "\n".join(f"- {s}" for s in job.preferred_skills))
    if job.min_experience_years is not None:
        parts.append(f"Experience: {job.min_experience_years:g}+ years" if job.min_experience_years else "Experience: freshers welcome")
    if job.soft_skills:
        parts.append("We value: " + ", ".join(job.soft_skills))
    return "\n\n".join(parts)


def skills_from_description(summary: str, responsibilities: str, jd_llm=None) -> tuple[list[str], list[str]]:
    """Skills the recruiter described in words (summary and duties) but did not list, e.g. "deploy using Streamlit".
    Returns ([], []) when there is no such text or the AI is unavailable: the typed skills are always kept."""
    text = "\n".join(t.strip() for t in (summary, responsibilities) if t.strip())
    if len(text) < 30:
        return [], []
    res = extract_job("Job Title: Role\n" + text, **_kw(jd_llm))
    return (res.job.required_skills, res.job.preferred_skills) if res.status == "ok" else ([], [])


def create_job_from_profile(conn: sqlite3.Connection, job: JobProfile, responsibilities: str = "", canon_llm=None,
                            owner_id: int | None = None) -> tuple[int | None, str]:
    """Store a job whose requirements the recruiter typed in directly (no AI reading step)."""
    if not job.required_skills:
        return None, "Add at least one required skill"
    return _store_job(conn, job, build_job_text(job, responsibilities), canon_llm, owner_id)


def _store_job(conn: sqlite3.Connection, job: JobProfile, text: str, canon_llm, owner_id: int | None) -> tuple[int | None, str]:
    aliases = load_aliases(conn)
    mapping, learned = canonicalize_skills(job.required_skills + job.preferred_skills, aliases, **_kw(canon_llm))
    with conn:
        save_learned(conn, learned)
        cur = conn.execute(
            "INSERT INTO job_descriptions (title, raw_text, min_experience_years, soft_skills, summary, owner_id) VALUES (?,?,?,?,?,?)",
            (job.title, text, job.min_experience_years, json.dumps(job.soft_skills), job.summary, owner_id))
        job_id, seen = cur.lastrowid, set()
        for importance, skills in (("required", job.required_skills), ("preferred", job.preferred_skills)):
            for s in skills:
                canon = mapping[s]
                if canon.lower() not in seen:
                    seen.add(canon.lower())
                    conn.execute("INSERT INTO job_required_skills (job_description_id, skill_name, importance) VALUES (?,?,?)",
                                 (job_id, canon, importance))
    return job_id, "ok"


def get_job(conn: sqlite3.Connection, job_id: int) -> JobProfile | None:
    row = conn.execute("SELECT * FROM job_descriptions WHERE id=?", (job_id,)).fetchone()
    if row is None:
        return None
    skills = conn.execute("SELECT skill_name, importance, is_gate FROM job_required_skills WHERE job_description_id=? ORDER BY id",
                          (job_id,)).fetchall()
    return JobProfile(
        gates=[r["skill_name"] for r in skills if r["is_gate"] and r["importance"] == "required"],
        title=row["title"], min_experience_years=row["min_experience_years"], summary=row["summary"],
        soft_skills=json.loads(row["soft_skills"] or "[]"),
        required_skills=[r["skill_name"] for r in skills if r["importance"] == "required"],
        preferred_skills=[r["skill_name"] for r in skills if r["importance"] == "preferred"],
        weights=json.loads(row["weights"]) if row["weights"] else None,
        cutoffs=json.loads(row["cutoffs"]) if row["cutoffs"] else None)


def set_job_gates(conn: sqlite3.Connection, job_id: int, skills: list[str]) -> dict:
    """Mark which REQUIRED skills are must-haves and relabel every stored candidate. No AI call. Raises ValueError on unknown skills."""
    job = get_job(conn, job_id)
    by_lower = {s.lower(): s for s in job.required_skills}
    chosen = []
    for s in skills:
        canon = by_lower.get(str(s).strip().lower())
        if canon is None:
            raise ValueError(f"'{s}' is not one of this job's required skills")
        if canon not in chosen:
            chosen.append(canon)
    job.gates = chosen
    with conn:
        conn.execute("UPDATE job_required_skills SET is_gate = 0 WHERE job_description_id=?", (job_id,))
        for s in chosen:
            conn.execute("UPDATE job_required_skills SET is_gate = 1 WHERE job_description_id=? AND importance='required' AND skill_name=?", (job_id, s))
        _relabel(conn, job, job_id)
    return {"gates": chosen, "relabelled": conn.execute("SELECT COUNT(*) FROM analysis_results WHERE job_description_id=?", (job_id,)).fetchone()[0]}


def _relabel(conn: sqlite3.Connection, job: JobProfile, job_id: int) -> int:
    """Recompute every stored recommendation from the stored score, the job's cutoffs and its must-haves. Caller holds the transaction."""
    rows = conn.execute("SELECT application_id, match_score, skill_breakdown FROM analysis_results WHERE job_description_id=?", (job_id,)).fetchall()
    for r in rows:
        conn.execute("UPDATE analysis_results SET recommendation=? WHERE application_id=?",
                     (final_label(job, r["match_score"], json.loads(r["skill_breakdown"] or "[]")), r["application_id"]))
    return len(rows)


def set_job_cutoffs(conn: sqlite3.Connection, job_id: int, cutoffs: dict | None) -> dict:
    """Save a job's Shortlist/Consider cutoffs (None resets) and relabel every stored score. No AI call. Raises ValueError."""
    if cutoffs is not None:
        cutoffs = validate_cutoffs(cutoffs)
        if cutoffs == default_cutoffs():
            cutoffs = None
    job = get_job(conn, job_id)
    job.cutoffs = cutoffs
    with conn:
        conn.execute("UPDATE job_descriptions SET cutoffs=? WHERE id=?", (json.dumps(cutoffs) if cutoffs else None, job_id))
        relabelled = _relabel(conn, job, job_id)
    return {"cutoffs": effective_cutoffs(job), "custom": cutoffs is not None, "relabelled": relabelled}


def set_job_weights(conn: sqlite3.Connection, job_id: int, percent: dict | None) -> dict:
    """Save a job's score weights (None resets to the defaults) and recompute every stored score from the stored component
    scores. No AI call is made: only the final score and the recommendation change. Raises ValueError for invalid weights."""
    if percent is not None:
        percent = validate_weights_percent(percent)
        if percent == default_weights_percent():
            percent = None
    job = get_job(conn, job_id)
    job.weights = percent
    weights = effective_weights(job)
    with conn:
        conn.execute("UPDATE job_descriptions SET weights=? WHERE id=?", (json.dumps(percent) if percent else None, job_id))
        rows = conn.execute("SELECT application_id, component_scores, skill_breakdown FROM analysis_results WHERE job_description_id=?", (job_id,)).fetchall()
        for r in rows:
            score = weighted_score(json.loads(r["component_scores"] or "{}"), weights)
            conn.execute("UPDATE analysis_results SET match_score=?, recommendation=? WHERE application_id=?",
                         (score, final_label(job, score, json.loads(r["skill_breakdown"] or "[]")), r["application_id"]))
    return {"weights": {k: round(v * 100, 1) for k, v in weights.items()}, "custom": percent is not None, "rescored": len(rows)}


# ---------------------------------------------------------------- resumes
def _store_analysis(conn, app_id: int, job_id: int, s: ScoreResult) -> None:
    conn.execute(
        """INSERT INTO analysis_results
           (application_id, job_description_id, match_score, skill_match_ratio, component_scores, skill_breakdown,
            llm_status, matching_skills, missing_skills, strengths, weaknesses, summary, interview_questions, recommendation)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT (application_id) DO UPDATE SET
             job_description_id = excluded.job_description_id, match_score = excluded.match_score,
             skill_match_ratio = excluded.skill_match_ratio, component_scores = excluded.component_scores,
             skill_breakdown = excluded.skill_breakdown, llm_status = excluded.llm_status,
             matching_skills = excluded.matching_skills, missing_skills = excluded.missing_skills,
             strengths = excluded.strengths, weaknesses = excluded.weaknesses, summary = excluded.summary,
             interview_questions = excluded.interview_questions, recommendation = excluded.recommendation""",
        (app_id, job_id, s.match_score, s.skill_match_ratio, json.dumps(s.components),
         json.dumps([r.__dict__ for r in s.skill_breakdown]), s.llm_status, json.dumps(s.matching_skills),
         json.dumps(s.missing_skills), json.dumps(s.strengths), json.dumps(s.weaknesses), s.summary,
         json.dumps(s.interview_questions), s.recommendation))


def process_resume(conn: sqlite3.Connection, data: bytes, filename: str, job_id: int, **kwargs) -> Outcome:
    """Run one resume through the whole pipeline (see _process_resume). Adds an 'OCR' note when the file was a scan."""
    flag: list[bool] = []
    outcome = _process_resume(conn, data, filename, job_id, _ocr_flag=flag, **kwargs)
    if flag and flag[0]:
        outcome.ocr_used = True
        outcome.message = (outcome.message + " " if outcome.message else "") + "Read by OCR (scanned resume)."
    return outcome


def _process_resume(conn: sqlite3.Connection, data: bytes, filename: str, job_id: int, *, _ocr_flag: list,
                    extract_llm=None, verify_llm=None, canon_llm=None, score_llm=None, embed_llm=None, ocr_llm=None) -> Outcome:
    job = get_job(conn, job_id)
    if job is None:
        return Outcome(filename, "rejected_file", f"Unknown job id {job_id}")

    doc = extract_document(data, filename, ocr=ocr_llm or default_reader())      # OCR only ever runs for a PDF with no text layer
    _ocr_flag.append(doc.ocr)
    if not doc.ok:
        return Outcome(filename, "rejected_file", doc.message)

    ext = extract_profile(doc.text, **_kw(extract_llm))
    if ext.profile is None:
        return Outcome(filename, "needs_review", f"Could not extract data: {ext.error}")

    aliases = load_aliases(conn)
    ver = verify_profile(ext.profile, doc.text, aliases, **_kw(verify_llm))
    profile, pairs, learned = canonicalize_profile(ver.profile, aliases, **_kw(canon_llm))
    aliases.update(learned)

    # Fast path (no lock): skip needless AI scoring for obvious duplicates and conflicts.
    early = dedupe.decide(conn, profile.email, profile.phone, job_id, doc.text)
    if early.kind == dedupe.IDENTICAL:
        return Outcome(filename, "duplicate_ignored", "Identical resume already submitted for this job",
                       early.existing_application_id, early.candidate_id)
    scorable = early.kind != dedupe.CONFLICT and ext.status == "ok" and ver.status != "needs_review"
    result = score_candidate(profile, job, aliases, text=doc.text, **_kw(score_llm)) if scorable else None

    with _write_lock(conn):
        # Authoritative check under the write lock: another upload may have won the race while we were scoring.
        decision = dedupe.decide(conn, profile.email, profile.phone, job_id, doc.text)
        if decision.kind == dedupe.IDENTICAL:
            conn.rollback()
            return Outcome(filename, "duplicate_ignored", "Identical resume already submitted for this job",
                           decision.existing_application_id, decision.candidate_id)
        pending = decision.kind == dedupe.CONFLICT
        if pending:
            result = None                      # a pending resume is not scored until the applicant chooses it
        save_learned(conn, learned)
        cid = decision.candidate_id or conn.execute(
            "INSERT INTO candidates (name, email, phone) VALUES (?,?,?)",
            (profile.name, profile.email, profile.phone)).lastrowid
        app_id = conn.execute(
            """INSERT INTO applications
               (candidate_id, job_description_id, resume_filename, resume_hash, raw_text, education, experience_years,
                experience_detail, internships, soft_skills, profile_json, projects, certifications,
                extraction_status, verification_status, application_status)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (cid, job_id, filename, dedupe.resume_hash(doc.text), doc.text,
             json.dumps([e.model_dump() for e in profile.education]), profile.experience_years,
             json.dumps([j.model_dump() for j in profile.experience]),
             json.dumps([j.model_dump() for j in profile.internships]), json.dumps(profile.soft_skills),
             profile.model_dump_json(), json.dumps(profile.projects), json.dumps(profile.certifications),
             ext.status, ver.status, "pending_choice" if pending else "active")).lastrowid
        levels = {k.lower(): v for k, v in profile.skill_levels.items()}
        conn.executemany(
            "INSERT INTO application_skills (application_id, skill_name, raw_skill, level) VALUES (?,?,?,?)",
            [(app_id, canon, raw, levels.get(canon.lower())) for canon, raw in pairs])
        conn.executemany(
            "INSERT INTO verification_log (application_id, field, old_value, new_value, evidence_quote) VALUES (?,?,?,?,?)",
            [(app_id, c.field, c.old, c.new, f"[{c.source}] {c.evidence}") for c in ver.changes])
        if doc.hidden_text:                        # auditable: shown with the other automatic changes
            conn.execute("INSERT INTO verification_log (application_id, field, old_value, new_value, evidence_quote) VALUES (?,?,?,?,?)",
                         (app_id, "hidden_text", None, None, f"[deterministic] {len(doc.hidden_text)} characters of invisible text ignored: {doc.hidden_text[:200]!r}"))
        if result:
            _store_analysis(conn, app_id, job_id, result)

    _index_chunks(conn, app_id, doc.text, embed_llm)      # best-effort search index; never blocks an upload

    if pending:
        return Outcome(filename, "conflict_pending",
                       "A different resume already exists for this job; the applicant must choose which to keep",
                       app_id, cid, verification_status=ver.status)
    if ext.status != "ok" or ver.status == "needs_review":
        why = ext.error or "; ".join(ver.notes) or "verification flagged this resume"
        return Outcome(filename, "needs_review", why, app_id, cid, verification_status=ver.status)
    note = f"Ignored {len(doc.hidden_text)} characters of invisible text (possible keyword stuffing)." if doc.hidden_text else ""
    return Outcome(filename, "stored", note, app_id, cid, result.match_score, result.recommendation, ver.status)


# ---------------------------------------------------------------- search index (hybrid retrieval)
def _embedder(embed_llm):
    """An explicit embedder always wins (tests); otherwise use the real one only if semantic indexing is enabled."""
    if embed_llm is not None:
        return embed_llm
    return client.embed if SEMANTIC_INDEXING else None


def _index_chunks(conn: sqlite3.Connection, application_id: int, text: str, embed_llm=None) -> int:
    """Store the resume as searchable chunks. Embeddings are optional: if the API is down, keyword search still works."""
    chunks = chunk_text(text)
    vecs: list = [None] * len(chunks)
    embed = _embedder(embed_llm)
    if embed and chunks:
        try:
            vecs = embed(chunks, "RETRIEVAL_DOCUMENT")
        except client.LLMError:
            pass
    with conn:
        conn.executemany("INSERT INTO resume_chunks (application_id, chunk_index, text, embedding) VALUES (?,?,?,?)",
                         [(application_id, i, c, pack(v) if v else None) for i, (c, v) in enumerate(zip(chunks, vecs))])
    return len(chunks)


def reindex_missing_embeddings(conn: sqlite3.Connection, job_id: int, embed_llm=None) -> int:
    """Create chunks for resumes that have none, and add embeddings where they are missing. Returns chunks embedded."""
    for row in conn.execute("""SELECT a.id, a.raw_text FROM applications a WHERE a.job_description_id = ?
                               AND NOT EXISTS (SELECT 1 FROM resume_chunks c WHERE c.application_id = a.id)""", (job_id,)).fetchall():
        _index_chunks(conn, row["id"], row["raw_text"] or "", embed_llm=lambda t, k: [None] * len(t))   # chunks first, no vectors yet
    embed = _embedder(embed_llm)
    if embed is None:
        return 0
    rows = conn.execute("""SELECT c.id, c.text FROM resume_chunks c JOIN applications a ON a.id = c.application_id
                           WHERE a.job_description_id = ? AND c.embedding IS NULL""", (job_id,)).fetchall()
    if not rows:
        return 0
    try:
        vecs = embed([r["text"] for r in rows], "RETRIEVAL_DOCUMENT")
    except client.LLMError:
        return 0
    with conn:
        conn.executemany("UPDATE resume_chunks SET embedding = ? WHERE id = ?", [(pack(v), r["id"]) for r, v in zip(rows, vecs)])
    return len(rows)


def resolve_conflict(conn: sqlite3.Connection, keep_application_id: int, *, score_llm=None) -> Outcome:
    """The applicant chose which resume to keep. Activate it and score it if it has not been scored."""
    row = conn.execute("SELECT * FROM applications WHERE id=?", (keep_application_id,)).fetchone()
    if row is None:
        return Outcome("", "rejected_file", "Unknown application")
    with conn:
        dedupe.resolve_conflict(conn, keep_application_id)
    has_score = conn.execute("SELECT 1 FROM analysis_results WHERE application_id=?", (keep_application_id,)).fetchone()
    if not has_score and row["extraction_status"] == "ok" and row["verification_status"] != "needs_review":
        profile = CandidateProfile.model_validate_json(row["profile_json"])
        job = get_job(conn, row["job_description_id"])
        result = score_candidate(profile, job, load_aliases(conn), text=row["raw_text"] or "", **_kw(score_llm))
        with conn:
            _store_analysis(conn, keep_application_id, row["job_description_id"], result)
    score = conn.execute("SELECT match_score, recommendation FROM analysis_results WHERE application_id=?",
                         (keep_application_id,)).fetchone()
    return Outcome(row["resume_filename"], "stored", "Applicant's choice applied", keep_application_id,
                   row["candidate_id"], score["match_score"] if score else None,
                   score["recommendation"] if score else None, row["verification_status"])


def rescore_unavailable(conn: sqlite3.Connection, job_id: int, *, score_llm=None) -> list[dict]:
    """Re-run scoring for analyses whose LLM step failed earlier (rate limit, outage). Uses the stored profile."""
    job = get_job(conn, job_id)
    aliases = load_aliases(conn)
    rows = conn.execute(
        """SELECT r.application_id, r.match_score, a.profile_json, a.raw_text, c.name FROM analysis_results r
           JOIN applications a ON a.id = r.application_id JOIN candidates c ON c.id = a.candidate_id
           WHERE r.job_description_id = ? AND r.llm_status = 'unavailable'""", (job_id,)).fetchall()
    out = []
    for r in rows:
        result = score_candidate(CandidateProfile.model_validate_json(r["profile_json"]), job, aliases, text=r["raw_text"] or "", **_kw(score_llm))
        if result.llm_status == "ok":                      # only replace when the retry actually worked
            with conn:
                _store_analysis(conn, r["application_id"], job_id, result)
        out.append({"name": r["name"], "old": r["match_score"], "new": result.match_score, "llm_status": result.llm_status})
    return out


def preview_pending_scores(conn: sqlite3.Connection, job_id: int, *, score_llm=None) -> list[dict]:
    """Score resumes waiting in `pending_choice` WITHOUT activating them, so the choice can be an informed one."""
    job = get_job(conn, job_id)
    aliases = load_aliases(conn)
    rows = conn.execute(
        """SELECT a.id, a.resume_filename, a.profile_json, a.raw_text, a.extraction_status, a.verification_status, c.name
           FROM applications a JOIN candidates c ON c.id = a.candidate_id
           WHERE a.job_description_id = ? AND a.application_status = 'pending_choice'""", (job_id,)).fetchall()
    out = []
    for r in rows:
        if r["extraction_status"] != "ok" or r["verification_status"] == "needs_review":
            continue
        res = score_candidate(CandidateProfile.model_validate_json(r["profile_json"]), job, aliases, text=r["raw_text"] or "", **_kw(score_llm))
        req = [b for b in res.skill_breakdown if b.importance == "required"]
        out.append({"application_id": r["id"], "name": r["name"], "file": r["resume_filename"], "score": res.match_score,
                    "recommendation": res.recommendation,
                    "required_matched": f"{sum(b.status != 'missing' for b in req)}/{len(req)}", "llm_status": res.llm_status})
    return sorted(out, key=lambda x: -x["score"])


# ---------------------------------------------------------------- reads
def ranking(conn: sqlite3.Connection, job_id: int, limit: int | None = None) -> list[dict]:
    """Active, scored applications for a job, best first. Pending and unscored ones are excluded."""
    sql = """SELECT a.id AS application_id, c.name, c.email, c.phone, a.resume_filename, a.experience_years,
                    r.match_score, r.recommendation, r.skill_match_ratio, r.summary
             FROM analysis_results r
             JOIN applications a ON a.id = r.application_id AND a.application_status = 'active'
             JOIN candidates c ON c.id = a.candidate_id
             WHERE r.job_description_id = ?
             ORDER BY r.match_score DESC, c.name"""
    rows = conn.execute(sql + (" LIMIT ?" if limit else ""), (job_id, limit) if limit else (job_id,)).fetchall()
    return [dict(r) for r in rows]


def pending_conflicts(conn: sqlite3.Connection, job_id: int) -> list[dict]:
    rows = conn.execute(
        """SELECT p.id AS pending_id, p.resume_filename AS new_file, act.id AS active_id, act.resume_filename AS current_file,
                  c.name, c.email
           FROM applications p JOIN candidates c ON c.id = p.candidate_id
           JOIN applications act ON act.candidate_id = p.candidate_id AND act.job_description_id = p.job_description_id
                                 AND act.application_status = 'active'
           WHERE p.job_description_id = ? AND p.application_status = 'pending_choice'""", (job_id,)).fetchall()
    return [dict(r) for r in rows]
