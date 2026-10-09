"""Central settings. Every other module imports from here instead of reading env vars itself."""
import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")

# Model roles (see IMPLEMENTATION_PLAN.md section 5c)
GROQ_FAST_MODEL = os.getenv("GROQ_FAST_MODEL", "qwen/qwen3.8-27b")
GROQ_MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.1-flash-lite")            # verification
GEMINI_CHAT_MODEL = os.getenv("GEMINI_CHAT_MODEL", "gemini-3.1-flash-lite")     # recruiter chat (3.8-flash measured 7.4s vs 1.6s)
GEMINI_FALLBACK_MODEL = os.getenv("GEMINI_FALLBACK_MODEL", "gemini-3.1-flash-lite")  # if the above is busy

# Local self-hosted model via Ollama (optional; bonus + offline fallback)
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3.2:3b")

# The PostgreSQL database, e.g. postgresql://user:password@host:5432/dbname (use ?sslmode=require on a hosted one). Required to run the app.
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()

# "cloud" = Groq + Gemini (default). "local" = every AI call goes to a self-hosted Ollama model (slower, offline, free).
LLM_MODE = os.getenv("LLM_MODE", "cloud").lower()
GEMINI_EMBED_MODEL = os.getenv("GEMINI_EMBED_MODEL", "gemini-embedding-001")
EMBED_DIMS = int(os.getenv("EMBED_DIMS", "768"))            # measured: same retrieval quality as 3072, a quarter of the storage
SEMANTIC_INDEXING = os.getenv("SEMANTIC_INDEXING", "1") not in ("0", "false", "False")   # embed resumes on upload

# How many AI judgments to take the median of when scoring (1 = fastest/cheapest, 3 = more repeatable)
JUDGE_SAMPLES = int(os.getenv("JUDGE_SAMPLES", "1" if LLM_MODE == "local" else "3"))

# Authentication (on by default; set AUTH_ENABLED=0 for a single-user setup with no login). See app/services/auth_service.py.
AUTH_ENABLED = os.getenv("AUTH_ENABLED", "1") in ("1", "true", "True")   # on by default: one link, login included
SESSION_SECRET = os.getenv("SESSION_SECRET", "")            # signs login cookies; if empty a random one is made per run
ALLOW_REGISTRATION = os.getenv("ALLOW_REGISTRATION", "1") not in ("0", "false", "False")   # self sign-up as recruiter
SESSION_HOURS = int(os.getenv("SESSION_HOURS", "12"))
ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", "")                  # first admin, created on startup when the users table is empty
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")

# PDF extraction thresholds
MIN_TEXT_CHARS = 100          # fewer characters than this => treat as scanned/empty
MAX_FILE_MB = 10

# Free-tier planning numbers used by the upload-time estimate (app/services/quota.py). Providers change these often, so they are
# settings, not facts: Groq's real remaining count is read from its response headers when available; Gemini reports none.
GROQ_FAST_CALLS_PER_DAY = int(os.getenv("GROQ_FAST_CALLS_PER_DAY", "1000"))
GROQ_CALLS_PER_DAY = int(os.getenv("GROQ_CALLS_PER_DAY", "1000"))
GEMINI_CALLS_PER_DAY = int(os.getenv("GEMINI_CALLS_PER_DAY", "500"))
BATCH_PAUSE_SECONDS = float(os.getenv("BATCH_PAUSE_SECONDS", "2"))             # gap between resumes of one batch
BATCH_PAUSE_AFTER_LIMIT_SECONDS = float(os.getenv("BATCH_PAUSE_AFTER_LIMIT_SECONDS", "12"))   # gap while the provider is rate-limiting

# OCR for scanned resumes: only used when a PDF has no text layer at all. Uses Gemini vision (free tier), so the page images leave
# this machine like the resume text already does; it is off in LLM_MODE=local.
OCR_ENABLED = os.getenv("OCR_ENABLED", "1") not in ("0", "false", "False")
OCR_MAX_PAGES = int(os.getenv("OCR_MAX_PAGES", "3"))
OCR_DPI = int(os.getenv("OCR_DPI", "150"))
