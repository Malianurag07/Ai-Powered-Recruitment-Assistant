"""Recruiter chat endpoints, scoped to one job."""
from dataclasses import asdict

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app import deps
from app.routes.access import owned_job
from app.llm import query_engine
from app.services import candidate_service as svc
from app.db import PgConnection

router = APIRouter(prefix="/api", tags=["chat"])


class Question(BaseModel):
    question: str = Field(min_length=1, max_length=1000)


@router.post("/jobs/{job_id}/chat")
def chat(body: Question, job_id: int = Depends(owned_job), db: PgConnection = Depends(deps.get_db)):
    if svc.get_job(db, job_id) is None:
        raise HTTPException(404, "Job not found")
    answer = query_engine.ask(db, job_id, body.question, **deps.CHAT_LLMS)
    return asdict(answer)


@router.get("/jobs/{job_id}/chat")
def history(job_id: int = Depends(owned_job), db: PgConnection = Depends(deps.get_db)):
    rows = db.execute("SELECT role, content, created_at FROM chat_history WHERE job_description_id=? ORDER BY id", (job_id,)).fetchall()
    return [dict(r) for r in rows]


@router.delete("/jobs/{job_id}/chat")
def clear_history(job_id: int = Depends(owned_job), db: PgConnection = Depends(deps.get_db)):
    with db:
        db.execute("DELETE FROM chat_history WHERE job_description_id=?", (job_id,))
    return {"cleared": True}
