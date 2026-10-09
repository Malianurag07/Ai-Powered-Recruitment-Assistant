"""Who may touch which job. One rule, used by every route that takes a job id.

Recruiters see only jobs they created. Admins see everything. With login switched off the built-in user is an admin, so
nothing changes for a single-user demo. A job you may not see answers 404 (not 403), so its existence is not revealed.
"""

from fastapi import Depends, HTTPException

from app import deps
from app.routes.auth import current_user
from app.db import PgConnection


def can_access_job(db: PgConnection, user: dict, job_id: int) -> bool:
    row = db.execute("SELECT owner_id FROM job_descriptions WHERE id=?", (job_id,)).fetchone()
    if row is None:
        return False
    return user["role"] == "admin" or (row["owner_id"] is not None and row["owner_id"] == user["id"])


def owned_job(job_id: int, user: dict = Depends(current_user), db: PgConnection = Depends(deps.get_db)) -> int:
    """FastAPI dependency: returns the job id if the caller may use it, else 404."""
    if not can_access_job(db, user, job_id):
        raise HTTPException(404, "Job not found")
    return job_id
