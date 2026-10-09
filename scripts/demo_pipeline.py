"""End-to-end demo: emptied database -> job description -> every sample resume -> ranking + DB summary.

Run from the project root:   python scripts/demo_pipeline.py --wipe
It EMPTIES every data table in the database named by DATABASE_URL first (accounts included), so it refuses to run without --wipe.
"""
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import DATABASE_URL  # noqa: E402
from app.database import get_connection, init_db  # noqa: E402
from app.services import candidate_service as svc  # noqa: E402

if "--wipe" not in sys.argv:
    sys.exit("This empties every table in the database named by DATABASE_URL. Run it again with --wipe if that is what you want.")
init_db()
conn = get_connection()
with conn:
    conn.execute("TRUNCATE users, candidates, job_descriptions, job_required_skills, applications, application_skills, resume_chunks, "
                 "analysis_results, verification_log, chat_history, ai_usage RESTART IDENTITY CASCADE")     # skill_aliases is kept
print("Emptied the database at", DATABASE_URL.split("@")[-1])

jd_text = (ROOT / "data" / "sample_job_description.txt").read_text(encoding="utf-8")
job_id, msg = svc.create_job(conn, jd_text)
print(f"JOB #{job_id}: {msg}")
job = svc.get_job(conn, job_id)
print(f"  required: {job.required_skills}\n  preferred: {job.preferred_skills}\n")

for path in sorted((ROOT / "data" / "sample_resumes").iterdir()):
    t = time.time()
    o = svc.process_resume(conn, path.read_bytes(), path.name, job_id)
    score = f"{o.score:5.1f} {o.recommendation}" if o.score is not None else "-"
    print(f"{path.name:32} {o.status:17} {score:16} verify={o.verification_status or '-':10} {time.time() - t:4.0f}s  {o.message[:60]}", flush=True)

print("\n=== RANKING (active, scored) ===")
for i, r in enumerate(svc.ranking(conn, job_id), 1):
    print(f"{i:2}. {r['match_score']:5.1f}  {r['recommendation']:9} {r['name']} <{r['email']}>  [{r['resume_filename']}]")

print("\n=== PENDING: applicant must choose which resume to keep ===")
for c in svc.pending_conflicts(conn, job_id):
    print(f"  {c['name']} <{c['email']}>: keep '{c['current_file']}' or switch to '{c['new_file']}'?")

print("\n=== DATABASE ===")
for table in ("candidates", "applications", "application_skills", "analysis_results", "verification_log"):
    print(f"  {table:20} {conn.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]} rows")
ai = conn.execute("SELECT alias, canonical FROM skill_aliases WHERE source='ai'").fetchall()
print(f"  AI-learned skill aliases: {len(ai)}  e.g. {[(r['alias'], r['canonical']) for r in ai[:8]]}")
