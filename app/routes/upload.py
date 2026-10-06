"""Jobs and resume intake: create a job from text/file, upload resumes, resolve duplicate-resume choices."""
import sqlite3
from dataclasses import asdict

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from pydantic import BaseModel, Field

from app import config, deps
from app.llm import client
from app.llm.scoring import _canon as scoring_canon
from app.services import quota
from app.routes.access import can_access_job, owned_job
from app.routes.auth import current_user
from app.models import JobProfile
from app.parsing.document_extractor import extract_document
from app.parsing.ocr import default_reader
from app.services import candidate_service as svc
from app.services import read_models

router = APIRouter(prefix="/api", tags=["upload"])


@router.get("/jobs")
def list_jobs(user: dict = Depends(current_user), db: sqlite3.Connection = Depends(deps.get_db)):
    admin = user["role"] == "admin"
    return read_models.list_jobs(db, None if admin else user["id"], include_owner=admin and user["id"] != 0)


@router.post("/jobs")
async def create_job(text: str = Form(""), file: UploadFile | None = File(None), user: dict = Depends(current_user),
                     db: sqlite3.Connection = Depends(deps.get_db)):
    """Create a job from pasted text or an uploaded PDF/DOCX/TXT description."""
    body = text.strip()
    if file is not None and file.filename:
        data = await file.read()
        if file.filename.lower().endswith((".pdf", ".docx")):
            doc = extract_document(data, file.filename, ocr=default_reader())
            if not doc.ok:
                raise HTTPException(422, doc.message)
            body = doc.text
        elif file.filename.lower().endswith(".txt"):
            body = data.decode("utf-8", errors="replace")
        else:
            raise HTTPException(422, "Job description must be PDF, DOCX or TXT.")
    if not body:
        raise HTTPException(422, "Provide the job description as text or a file.")
    job_id, msg = svc.create_job(db, body, owner_id=user["id"] or None, **deps.JOB_LLMS)
    if job_id is None:
        raise HTTPException(422, msg)
    return read_models.job_detail(db, job_id)


class JobBuild(BaseModel):
    title: str = Field(max_length=120)
    summary: str = Field("", max_length=1000)
    responsibilities: str = Field("", max_length=4000)
    required_skills: list[str] = Field(default_factory=list, max_length=40)
    preferred_skills: list[str] = Field(default_factory=list, max_length=40)
    education: str = Field("", max_length=100)
    gate_skills: list[str] = Field(default_factory=list, max_length=40)       # must-haves: a subset of the skills above
    min_experience_years: float | None = Field(None, ge=0, le=50)
    soft_skills: list[str] = Field(default_factory=list, max_length=20)


@router.post("/jobs/build")
def build_job(body: JobBuild, user: dict = Depends(current_user), db: sqlite3.Connection = Depends(deps.get_db)):
    """Create a job from the job builder's answers instead of free text; the rest of the flow is identical."""
    if not body.title.strip():
        raise HTTPException(422, "Give the job a title")
    required = ([body.education] if body.education.strip() else []) + body.required_skills + [g for g in body.gate_skills if g.strip()]
    extra_req, extra_pref = svc.skills_from_description(body.summary, body.responsibilities, deps.JOB_LLMS.get("jd_llm"))
    typed = {s.strip().lower() for s in required + body.preferred_skills}
    extra_req = [s for s in extra_req if s.strip().lower() not in typed]          # skills only mentioned in the description
    extra_pref = [s for s in extra_pref if s.strip().lower() not in typed]
    job = JobProfile(title=body.title.strip(), summary=body.summary.strip() or None, required_skills=required + extra_req,
                     preferred_skills=body.preferred_skills + extra_pref, min_experience_years=body.min_experience_years,
                     soft_skills=body.soft_skills)
    job_id, msg = svc.create_job_from_profile(db, job, body.responsibilities, canon_llm=deps.JOB_LLMS.get("canon_llm"),
                                              owner_id=user["id"] or None)
    if job_id is None:
        raise HTTPException(422, msg)
    if body.gate_skills:                                                  # match typed names to the stored (canonical) ones
        aliases = svc.load_aliases(db)
        stored = svc.get_job(db, job_id).required_skills
        wanted = {scoring_canon(g, aliases) for g in body.gate_skills}
        svc.set_job_gates(db, job_id, [s for s in stored if scoring_canon(s, aliases) in wanted])
    return read_models.job_detail(db, job_id)


@router.post("/extract-preview")
async def extract_preview(file: UploadFile = File(...)):
    """Show exactly what the reader pulls out of a resume or job description. Nothing is saved and no AI is called."""
    data = await file.read()
    name = file.filename or "file"
    if name.lower().endswith(".txt"):                       # same handling as a TXT job description
        text = data.decode("utf-8", errors="replace")
        return {"filename": name, "status": "ok", "message": "", "pages": 0, "chars": len(text), "text": text, "hidden_text": ""}
    doc = extract_document(data, name, ocr=default_reader())
    return {"filename": name, "status": doc.status, "message": doc.message, "pages": doc.pages, "chars": len(doc.text),
            "text": doc.text, "hidden_text": doc.hidden_text, "ocr": doc.ocr}


def _drop_orphan_candidates(db: sqlite3.Connection) -> None:
    db.execute("DELETE FROM candidates WHERE id NOT IN (SELECT candidate_id FROM applications)")


@router.delete("/jobs/{job_id}")
def delete_job(job_id: int = Depends(owned_job), db: sqlite3.Connection = Depends(deps.get_db)):
    """Delete a job with everything stored under it (resumes, scores, chat history). Foreign keys cascade."""
    with db:
        db.execute("DELETE FROM job_descriptions WHERE id=?", (job_id,))
        _drop_orphan_candidates(db)
    return {"deleted": job_id}


@router.delete("/applications/{application_id}")
def delete_application(application_id: int, user: dict = Depends(current_user), db: sqlite3.Connection = Depends(deps.get_db)):
    """Delete one uploaded resume with its extraction, score and log. Same access rule as the job it belongs to."""
    row = db.execute("SELECT job_description_id FROM applications WHERE id=?", (application_id,)).fetchone()
    if row is None or not can_access_job(db, user, row["job_description_id"]):
        raise HTTPException(404, "Resume not found")
    with db:
        db.execute("DELETE FROM applications WHERE id=?", (application_id,))
        _drop_orphan_candidates(db)
    return {"deleted": application_id}


class Weights(BaseModel):
    skills: float
    experience: float
    projects_education: float
    fit: float


@router.put("/jobs/{job_id}/weights")
def set_weights(body: Weights, job_id: int = Depends(owned_job), db: sqlite3.Connection = Depends(deps.get_db)):
    """Change how much each part of the score counts for this job; stored scores are recomputed at once, with no AI call."""
    try:
        return svc.set_job_weights(db, job_id, body.model_dump())
    except ValueError as exc:
        raise HTTPException(422, str(exc))


class Cutoffs(BaseModel):
    shortlist: float
    consider: float


@router.put("/jobs/{job_id}/cutoffs")
def set_cutoffs(body: Cutoffs, job_id: int = Depends(owned_job), db: sqlite3.Connection = Depends(deps.get_db)):
    """Change the scores at which a candidate is labelled Shortlist or Consider for this job; labels update at once, no AI call."""
    try:
        return svc.set_job_cutoffs(db, job_id, body.model_dump())
    except ValueError as exc:
        raise HTTPException(422, str(exc))


class Gates(BaseModel):
    skills: list[str] = Field(default_factory=list, max_length=40)


@router.put("/jobs/{job_id}/gates")
def set_gates(body: Gates, job_id: int = Depends(owned_job), db: sqlite3.Connection = Depends(deps.get_db)):
    """Choose which required skills are must-haves. A candidate missing one is never labelled Shortlist (capped at Consider)."""
    try:
        return svc.set_job_gates(db, job_id, body.skills)
    except ValueError as exc:
        raise HTTPException(422, str(exc))


@router.delete("/jobs/{job_id}/cutoffs")
def reset_cutoffs(job_id: int = Depends(owned_job), db: sqlite3.Connection = Depends(deps.get_db)):
    return svc.set_job_cutoffs(db, job_id, None)


@router.delete("/jobs/{job_id}/weights")
def reset_weights(job_id: int = Depends(owned_job), db: sqlite3.Connection = Depends(deps.get_db)):
    return svc.set_job_weights(db, job_id, None)


@router.get("/jobs/{job_id}")
def get_job(job_id: int = Depends(owned_job), db: sqlite3.Connection = Depends(deps.get_db)):
    detail = read_models.job_detail(db, job_id)
    if detail is None:
        raise HTTPException(404, "Job not found")
    return detail


@router.post("/jobs/{job_id}/resumes")
async def upload_resumes(job_id: int = Depends(owned_job), files: list[UploadFile] = File(...), db: sqlite3.Connection = Depends(deps.get_db)):
    """Process one or more resumes. Each file gets its own outcome, so one bad file never blocks the rest."""
    if svc.get_job(db, job_id) is None:
        raise HTTPException(404, "Job not found")
    outcomes = []
    for f in files:
        o = asdict(svc.process_resume(db, await f.read(), f.filename or "resume", job_id, **deps.PIPELINE_LLMS))
        outcomes.append(_with_extraction_summary(db, o))
    return {"outcomes": outcomes, "pause_seconds": quota.suggested_pause(), "out_of_calls": quota.out_of_calls()}


@router.get("/quota/estimate")
def quota_estimate(count: int = Query(1, ge=1, le=2000), probe: bool = True):
    """Before a batch: AI calls needed vs. free-tier calls left, time, and whether it fits. `probe` makes up to two tiny Groq
    requests to read the real remaining count when the last reading is old."""
    if probe:
        for bucket, model in (("groq_fast", config.GROQ_FAST_MODEL), ("groq_big", config.GROQ_MODEL)):
            if quota.remaining(bucket)[2] != "reported by Groq":
                client.probe_groq(model)
    return quota.estimate(count)


def _with_extraction_summary(db: sqlite3.Connection, outcome: dict) -> dict:
    """Add what was extracted (name, contact, skill count) so the UI can show it while the batch runs."""
    if outcome.get("application_id"):
        row = db.execute(
            """SELECT c.name, c.email, (SELECT COUNT(*) FROM application_skills s WHERE s.application_id = a.id) AS skills
               FROM applications a JOIN candidates c ON c.id = a.candidate_id WHERE a.id = ?""", (outcome["application_id"],)).fetchone()
        if row:
            outcome.update(name=row["name"], email=row["email"], skills_found=row["skills"])
    return outcome


@router.get("/jobs/{job_id}/conflicts")
def conflicts(job_id: int = Depends(owned_job), preview: bool = False, db: sqlite3.Connection = Depends(deps.get_db)):
    """Resumes waiting for the applicant's choice. preview=true also scores each pending one (uses the LLM)."""
    items = svc.pending_conflicts(db, job_id)
    if preview and items:
        scores = {p["application_id"]: p for p in svc.preview_pending_scores(db, job_id, **_score_kw())}
        for it in items:
            it["preview"] = scores.get(it["pending_id"])
    return items


class Resolve(BaseModel):
    keep_application_id: int


@router.post("/conflicts/resolve")
def resolve(body: Resolve, user: dict = Depends(current_user), db: sqlite3.Connection = Depends(deps.get_db)):
    row = db.execute("SELECT job_description_id FROM applications WHERE id=?", (body.keep_application_id,)).fetchone()
    if row is None or not can_access_job(db, user, row[0]):
        raise HTTPException(404, "Unknown application")
    outcome = svc.resolve_conflict(db, body.keep_application_id, **_score_kw())
    if outcome.status == "rejected_file":
        raise HTTPException(404, outcome.message)
    return asdict(outcome)


def _score_kw() -> dict:
    return {"score_llm": deps.PIPELINE_LLMS["score_llm"]} if "score_llm" in deps.PIPELINE_LLMS else {}
