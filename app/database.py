"""SQLite connection helper and schema creation."""
import sqlite3

from app.config import DATABASE_PATH

SCHEMA = """
-- A candidate is a PERSON (identified by email / phone). Their resumes live in `applications`.
CREATE TABLE IF NOT EXISTS candidates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT,
    email TEXT UNIQUE,
    phone TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS job_descriptions (
    owner_id INTEGER,                            -- users.id of whoever created it; NULL for jobs made while login was off
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT,
    raw_text TEXT,
    min_experience_years REAL,
    soft_skills TEXT,
    summary TEXT,
    weights TEXT,                                -- JSON {"skills": 70, ...} in percent when the recruiter changed them; NULL = defaults
    cutoffs TEXT,                                -- JSON {"shortlist": 70, "consider": 45} when the recruiter changed them; NULL = defaults
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS job_required_skills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_description_id INTEGER NOT NULL REFERENCES job_descriptions(id) ON DELETE CASCADE,
    skill_name TEXT NOT NULL,
    is_gate INTEGER NOT NULL DEFAULT 0,           -- 1 = must-have: a candidate missing it cannot be labelled Shortlist
    importance TEXT NOT NULL DEFAULT 'required'   -- required | preferred
);

-- One row per resume submitted for one job. Different roles can carry different resumes.
CREATE TABLE IF NOT EXISTS applications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    candidate_id INTEGER NOT NULL REFERENCES candidates(id) ON DELETE CASCADE,
    job_description_id INTEGER NOT NULL REFERENCES job_descriptions(id) ON DELETE CASCADE,
    resume_filename TEXT,
    resume_hash TEXT NOT NULL,
    raw_text TEXT,
    education TEXT,
    experience_years REAL,
    experience_detail TEXT,
    internships TEXT,
    soft_skills TEXT,
    profile_json TEXT,           -- full verified profile, so it can be rebuilt without re-running the LLM
    projects TEXT,
    certifications TEXT,
    extraction_status TEXT NOT NULL DEFAULT 'ok',
    verification_status TEXT NOT NULL DEFAULT 'unverified',
    application_status TEXT NOT NULL DEFAULT 'active',   -- active | pending_choice | superseded
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
-- Only ONE active resume per person per job (partial unique index).
CREATE UNIQUE INDEX IF NOT EXISTS uq_active_application
    ON applications(candidate_id, job_description_id) WHERE application_status = 'active';

CREATE TABLE IF NOT EXISTS application_skills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    application_id INTEGER NOT NULL REFERENCES applications(id) ON DELETE CASCADE,
    skill_name TEXT NOT NULL,      -- canonical
    raw_skill TEXT,                -- as written on the resume
    level TEXT                     -- basic | working | NULL (used later for colour badges)
);
CREATE INDEX IF NOT EXISTS idx_app_skills_name ON application_skills(skill_name COLLATE NOCASE);

-- Resume text split into topic-sized chunks for hybrid search. embedding is NULL when the embedding API was unavailable.
CREATE TABLE IF NOT EXISTS resume_chunks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    application_id INTEGER NOT NULL REFERENCES applications(id) ON DELETE CASCADE,
    chunk_index INTEGER NOT NULL,
    text TEXT NOT NULL,
    embedding BLOB
);
CREATE INDEX IF NOT EXISTS idx_chunks_app ON resume_chunks(application_id);

CREATE TABLE IF NOT EXISTS analysis_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    application_id INTEGER NOT NULL UNIQUE REFERENCES applications(id) ON DELETE CASCADE,
    job_description_id INTEGER NOT NULL REFERENCES job_descriptions(id) ON DELETE CASCADE,
    match_score REAL,
    skill_match_ratio REAL,
    component_scores TEXT,     -- JSON: skills / experience / projects_education / fit
    skill_breakdown TEXT,      -- JSON: per-skill status + badge colour
    llm_status TEXT,
    matching_skills TEXT,
    missing_skills TEXT,
    strengths TEXT,
    weaknesses TEXT,
    summary TEXT,
    interview_questions TEXT,
    recommendation TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS verification_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    application_id INTEGER NOT NULL REFERENCES applications(id) ON DELETE CASCADE,
    field TEXT NOT NULL,
    old_value TEXT,
    new_value TEXT,
    evidence_quote TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS skill_aliases (
    alias TEXT PRIMARY KEY,
    canonical TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'seed'      -- seed | ai (learned from the LLM; auditable)
);

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT NOT NULL UNIQUE,                 -- stored lowercase
    name TEXT NOT NULL DEFAULT '',
    password_hash TEXT NOT NULL,                -- scrypt$n$r$p$salt$hash; never the password itself
    role TEXT NOT NULL DEFAULT 'recruiter' CHECK (role IN ('admin', 'recruiter')),
    is_active INTEGER NOT NULL DEFAULT 1,
    session_epoch INTEGER NOT NULL DEFAULT 0,   -- bumped on logout / password change: older login cookies stop working
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_login_at TEXT
);

CREATE TABLE IF NOT EXISTS chat_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_description_id INTEGER REFERENCES job_descriptions(id) ON DELETE CASCADE,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""

# Starter alias map: lowercase variant -> canonical name. Grows over time.
SEED_ALIASES = {
    "ml": "Machine Learning",
    "machine learning": "Machine Learning",
    "py": "Python",
    "python3": "Python",
    "js": "JavaScript",
    "fast api": "FastAPI",
    "fastapi": "FastAPI",
    "tf": "TensorFlow",
    "tensorflow": "TensorFlow",
    "docker": "Docker",
    "postgres": "PostgreSQL",
    "postgresql": "PostgreSQL",
    "sklearn": "Scikit-learn",
    "scikit learn": "Scikit-learn",
    "sql": "SQL",
    "numpy": "NumPy",
    "pandas": "Pandas",
    "opencv": "OpenCV",
    "yolo": "YOLO",
    "lstm": "LSTM",
    "cnn": "CNN",
    "cnns": "CNN",
    "llm": "LLMs",
    "llms": "LLMs",
    "large language models": "LLMs",
    "rag": "RAG",
    "retrieval-augmented generation": "RAG",
    "github": "Git/GitHub",
    "git": "Git/GitHub",
    "git/github": "Git/GitHub",
    "typescript": "TypeScript",
    "javascript": "JavaScript",
    "nextjs": "Next.js",
    "next.js": "Next.js",
    "react.js": "React",
    "power bi": "Power BI",
    "aws": "AWS",
    "k8s": "Kubernetes",
    "scikit-learn": "Scikit-learn",
}


def get_connection(check_same_thread: bool = True) -> sqlite3.Connection:
    """Open a connection with dict-like rows and foreign keys enforced.

    The web app passes check_same_thread=False: FastAPI may run a request's dependency and its endpoint on
    different worker threads, and each request still gets its own private connection.
    """
    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DATABASE_PATH, check_same_thread=check_same_thread)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")  # SQLite ignores FKs unless asked
    return conn


def init_db() -> None:
    """Create all tables (idempotent), upgrade older databases, and seed the alias table."""
    with get_connection() as conn:
        conn.executescript(SCHEMA)
        if "session_epoch" not in {r[1] for r in conn.execute("PRAGMA table_info(users)")}:      # database created before sessions could be revoked
            conn.execute("ALTER TABLE users ADD COLUMN session_epoch INTEGER NOT NULL DEFAULT 0")
        if "owner_id" not in {r[1] for r in conn.execute("PRAGMA table_info(job_descriptions)")}:     # database created before jobs had owners
            conn.execute("ALTER TABLE job_descriptions ADD COLUMN owner_id INTEGER")
        if "weights" not in {r[1] for r in conn.execute("PRAGMA table_info(job_descriptions)")}:      # database created before per-job weights
            conn.execute("ALTER TABLE job_descriptions ADD COLUMN weights TEXT")
        if "is_gate" not in {r[1] for r in conn.execute("PRAGMA table_info(job_required_skills)")}:      # database created before must-haves
            conn.execute("ALTER TABLE job_required_skills ADD COLUMN is_gate INTEGER NOT NULL DEFAULT 0")
        if "cutoffs" not in {r[1] for r in conn.execute("PRAGMA table_info(job_descriptions)")}:      # database created before per-job cutoffs
            conn.execute("ALTER TABLE job_descriptions ADD COLUMN cutoffs TEXT")
        conn.executemany(
            "INSERT OR IGNORE INTO skill_aliases (alias, canonical) VALUES (?, ?)",
            SEED_ALIASES.items(),
        )


if __name__ == "__main__":
    init_db()
    print(f"Database ready at {DATABASE_PATH}")
