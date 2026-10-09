"""Read endpoints for the dashboard: ranking, review queue, retry, CSV export, and the demo table viewer."""

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response

from app import deps
from app.routes.access import owned_job
from app.routes.auth import require_admin
from app.services import candidate_service as svc
from app.services import read_models
from app.db import PgConnection

router = APIRouter(prefix="/api", tags=["analysis"])


def _require_job(db: PgConnection, job_id: int) -> None:
    if svc.get_job(db, job_id) is None:
        raise HTTPException(404, "Job not found")


@router.get("/jobs/{job_id}/candidates")
def candidates(job_id: int = Depends(owned_job), db: PgConnection = Depends(deps.get_db)):
    _require_job(db, job_id)
    return read_models.candidate_cards(db, job_id)


@router.get("/jobs/{job_id}/review")
def review_queue(job_id: int = Depends(owned_job), db: PgConnection = Depends(deps.get_db)):
    _require_job(db, job_id)
    return read_models.needs_review(db, job_id)


@router.post("/jobs/{job_id}/rescore")
def rescore(job_id: int = Depends(owned_job), db: PgConnection = Depends(deps.get_db)):
    """Retry analyses whose AI step failed earlier (rate limit, outage) and rebuild any missing search embeddings."""
    _require_job(db, job_id)
    kw = {"score_llm": deps.PIPELINE_LLMS["score_llm"]} if "score_llm" in deps.PIPELINE_LLMS else {}
    results = svc.rescore_unavailable(db, job_id, **kw)
    reindexed = svc.reindex_missing_embeddings(db, job_id, embed_llm=deps.PIPELINE_LLMS.get("embed_llm"))   # also repair the search index
    return {"results": results, "reindexed_chunks": reindexed}


@router.get("/jobs/{job_id}/export.csv")
def export_csv(job_id: int = Depends(owned_job), db: PgConnection = Depends(deps.get_db)):
    _require_job(db, job_id)
    return Response(read_models.ranking_csv(db, job_id), media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="ranking_job_{job_id}.csv"'})


@router.get("/jobs/{job_id}/export.xlsx")
def export_xlsx(job_id: int = Depends(owned_job), db: PgConnection = Depends(deps.get_db)):
    _require_job(db, job_id)
    return Response(read_models.ranking_xlsx(db, job_id),
                    media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f'attachment; filename="ranking_job_{job_id}.xlsx"'})


@router.get("/db")
def db_overview(_: dict = Depends(require_admin), db: PgConnection = Depends(deps.get_db)):
    return read_models.table_counts(db)


@router.get("/db/{table}")
def db_table(table: str, limit: int = Query(100, ge=1, le=500), _: dict = Depends(require_admin), db: PgConnection = Depends(deps.get_db)):
    try:
        return read_models.table_rows(db, table, limit)
    except KeyError:
        raise HTTPException(404, "Unknown table") from None
