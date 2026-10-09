# Implementation plan (v4, as built)

This document records the design and how the build actually went. Earlier drafts planned a different front end and
different models; both changed for reasons noted below, and this version matches the code.

## 1. Tech stack

| Layer | Choice | Reasoning |
|---|---|---|
| Backend | Python + FastAPI | Async-capable, automatic API docs, Pydantic validation |
| Frontend | HTML + Tailwind CSS (CDN) + vanilla JavaScript, served by FastAPI | No build step, one command to run, full control over design and behaviour |
| Database | PostgreSQL | Concurrent writers, durable storage, hosted anywhere (was SQLite in the first version) |
| Parsing | PyMuPDF (PDF), python-docx (Word) | Fast, handles multi-column layouts, reads Word tables in order |
| Search | BM25 keyword + Gemini embeddings, fused by weighted rank | Structured SQL first; semantic search only for concept questions |
| LLMs | Groq (`qwen3.8-27b`, `gpt-oss-120b`), Gemini (`gemini-3.1-flash-lite`), optional Ollama (local Llama) | Free tiers; swappable in `llm/client.py`; verified live against each account |
| Retrieval strategy | The LLM picks from 12 fixed Python tools running parameterised SQL | No raw text-to-SQL; debuggable; extensible |

## 2. Pipeline

```
Upload (resume PDF/DOCX, job PDF/DOCX/TXT/text)
  1. Text extraction          PyMuPDF / python-docx; bad, empty, encrypted, oversized, scanned files rejected with a reason
  2. AI extraction            Groq -> strict JSON, Pydantic-validated, retried once with the error, else needs_review
  3. Double verification      free code checks (email, phone, every skill must be in the text) + Gemini second opinion;
                              a correction applies only if its evidence quote exists verbatim in the resume; all logged
  4. Skill normalisation      alias table; unknown skills batched to the AI and saved back (source='ai')
  5. Duplicate check          same email OR phone, per job, under a write lock (see policy below)
  6. Scoring                  skills + experience in code; projects/education + fit judged by AI, median of 3, rounded
  7. Storage + search index   database rows, plus resume chunks with embeddings (best effort)
Chat: plan (AI -> JSON tool calls) -> execute (fixed SQL tools) -> answer (AI, using only the results)
```

Design rules: SQL first and AI only for reasoning; every ranking scoped to one job; no raw text-to-SQL, ever; a prompt
is a request and code is a guarantee (see `docs/PROMPTS.md`); ambiguity is reported, never guessed.

## 3. Database schema

A `candidate` is a person; each resume is an `application` to one job.

- `candidates`: id, name, email (unique), phone
- `job_descriptions`: title, raw text, min experience, soft skills, summary; `job_required_skills` (canonical name, `required` or `preferred`)
- `applications`: candidate, job, resume hash and raw text, education, experience years, internships, soft skills, projects, certifications, `profile_json`, extraction status, verification status, `application_status` (`active` / `pending_choice` / `superseded`); a partial unique index allows one active application per person per job
- `application_skills`: canonical skill, raw text as written, level (`basic` / `working` / none)
- `analysis_results`: score, component scores, per-skill breakdown, strengths, weaknesses, summary, interview questions, recommendation, LLM status
- `resume_chunks`: resume text in topic-sized chunks with optional embeddings, for hybrid search
- `verification_log`: every automatic correction with its evidence
- `skill_aliases` (`seed` or `ai`), `chat_history`

**Duplicate policy** (person = same email or phone; judged per job): another job is allowed; an identical resume for the
same job is ignored; a different resume for the same job is held as `pending_choice` and the applicant chooses.

## 4. Folder structure

```
app/          main, deps, config, database, models
  parsing/    pdf_extractor, docx_extractor, document_extractor
  llm/        client, prompts, extraction, verification, skill_canonicalizer, jd_parsing, scoring,
              retrieval, query_tools, query_engine
  routes/     upload, analysis, query
  services/   candidate_service, dedupe, skill_normalizer, read_models
frontend/     index.html, styles.css, app.js
scripts/      demo_pipeline, match_jd, test_queries, benchmark
tests/        automated tests (fake LLMs, no network)
docs/         SCORING, PROMPTS, BENCHMARK, COMPARISON
data/         sample_resumes/, job_descriptions/, sample job description
```

## 5. Build order and status

| Phase | State |
|---|---|
| 0 Setup and schema | Done |
| 1 PDF and DOCX extraction, including hostile files | Done |
| 2 AI extraction with validate and retry | Done, verified live |
| 2b Double verification with evidence quotes | Done, verified live |
| 3 Scoring (documented rubric, hybrid) and JD parsing | Done, verified live |
| 4 Storage, duplicate policy, concurrency safety | Done (a race that crashed simultaneous uploads was found and fixed) |
| 5 Chat: 12 tools, plan/execute/answer, memory | Done; 10 assessment questions plus 15 more exercised live |
| 6 FastAPI routes and guided web UI | Done, verified in the browser with real uploads |
| 7 README, error-handling pass, benchmark, vendor comparison | Done |
| 8 Bonus features | Mostly done (section 7) |

## 6. Model roles (verified live against the accounts used)

- Groq `qwen3.8-27b`: extraction, skill classification, routing. Falls over to `gpt-oss-120b`, then Gemini.
- Groq `gpt-oss-120b`: scoring (needs `reasoning_effort=low` and a token cap, or it can return nothing).
- Gemini `gemini-3.1-flash-lite`: verification (a different model family from the extractor) and chat answers (measured 1.6 s against 7.4 s for a larger model).
- Gemini `gemini-embedding-001` at 768 dimensions: semantic search (same quality as 3072 dimensions on our test).
- Not available on these accounts: `gemini-2.0-flash` (shut down) and the Llama models on Groq. Model names change often; check with the providers' model-list endpoints.

## 7. Assessment requirements and bonus status

Every functional requirement, documentation item and submission item in the assessment is covered; see the checklist in
`README.md` (section 12). The demo video and the GitHub push and submission form are the owner's to do.

| Bonus | Status |
|---|---|
| User-friendly web UI | Done: guided flow, ranking, drawer, chat, comparison, database viewer |
| Self-hosted LLM | Done: `LLM_MODE=local` routes every call to Ollama; one resume verified end to end (96 s, no cloud) |
| Multiple job descriptions | Done |
| Resume ranking dashboard | Done |
| Explainable AI responses | Done: score components, skill badges, correction log, tools shown per answer |
| Conversation history | Done: stored per job |
| Resume comparison | Done: chat tools and a side-by-side UI view for 2 or 3 candidates |
| Export to CSV or Excel | Done: both, Excel with coloured skill matrix; spreadsheet-injection safe |
| Better prompt engineering | Done and documented in `docs/PROMPTS.md` |
| Hybrid retrieval | Done: BM25 + embeddings, weighted fusion, measured on real resumes; degrades to keyword-only |
| Authentication and user management | Built and on by default (`AUTH_ENABLED=0` turns it off); first registered account becomes the admin |

## 8. Remaining work

1. Demo video (5-10 minutes) following the guided flow.
2. `git init`, public GitHub repository, submission form.
3. Optional next ideas: relevance-weighted experience score, an admin "my jobs only" view, OCR for scanned resumes.
