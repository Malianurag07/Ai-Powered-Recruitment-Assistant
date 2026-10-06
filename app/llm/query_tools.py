"""The ONLY interface between the chat assistant and the database.

Every tool is a plain Python function running parameterized SQL, scoped to one job description. The LLM never
writes SQL; it only chooses a tool and its arguments. Tools return small JSON-able dicts, and every zero-result
case says so explicitly, so the assistant has nothing to invent.
"""
import difflib
import json
import re
import sqlite3
from dataclasses import dataclass
from typing import Callable

from app.llm import client
from app.llm.retrieval import hybrid_rank, unpack
from app.llm.scoring import COLOUR, _canon, _implied_tools, effective_weights
from app.models import JobProfile


@dataclass
class Ctx:
    conn: sqlite3.Connection
    job_id: int
    aliases: dict[str, str]
    job: JobProfile
    embed_fn: Callable | None = None      # injectable for tests; defaults to the real Gemini embedder


# ------------------------------------------------------------------ data loading
def load_candidates(ctx: Ctx) -> list[dict]:
    """Active, scored applications for the job, ranked best first."""
    rows = ctx.conn.execute(
        """SELECT a.id AS application_id, c.id AS candidate_id, c.name, c.email, c.phone, a.resume_filename,
                  a.experience_years, a.internships, a.soft_skills, a.projects, a.certifications, a.education,
                  a.verification_status, r.match_score, r.recommendation, r.skill_match_ratio, r.component_scores,
                  r.skill_breakdown, r.strengths, r.weaknesses, r.summary, r.interview_questions
           FROM analysis_results r
           JOIN applications a ON a.id = r.application_id AND a.application_status = 'active'
           JOIN candidates c ON c.id = a.candidate_id
           WHERE r.job_description_id = ? ORDER BY r.match_score DESC, c.name""", (ctx.job_id,)).fetchall()
    out = []
    for rank, r in enumerate(rows, 1):
        skills = {}
        for s in ctx.conn.execute("SELECT skill_name, level FROM application_skills WHERE application_id=?", (r["application_id"],)):
            skills[s["skill_name"].lower()] = {"name": s["skill_name"], "level": s["level"]}
        internships = json.loads(r["internships"] or "[]")
        out.append({
            "rank": rank, "application_id": r["application_id"], "name": r["name"], "email": r["email"], "phone": r["phone"],
            "file": r["resume_filename"], "score": r["match_score"], "recommendation": r["recommendation"],
            "ratio": r["skill_match_ratio"], "components": json.loads(r["component_scores"] or "{}"),
            "breakdown": json.loads(r["skill_breakdown"] or "[]"), "strengths": json.loads(r["strengths"] or "[]"),
            "weaknesses": json.loads(r["weaknesses"] or "[]"), "summary": r["summary"],
            "questions": json.loads(r["interview_questions"] or "[]"), "exp_years": r["experience_years"] or 0,
            "internships": internships, "soft_skills": json.loads(r["soft_skills"] or "[]"),
            "projects": json.loads(r["projects"] or "[]"), "certifications": json.loads(r["certifications"] or "[]"),
            "education": json.loads(r["education"] or "[]"), "skills": skills, "verification": r["verification_status"]})
    return out


def ctx_required(c: dict) -> list[dict]:
    return [b for b in c["breakdown"] if b["importance"] == "required"]


def _brief_row(c: dict) -> dict:
    req = ctx_required(c)
    return {"rank": c["rank"], "name": c["name"], "score": c["score"], "recommendation": c["recommendation"],
            "required_skills_matched": f"{sum(b['status'] != 'missing' for b in req)}/{len(req)}",
            "experience_years": c["exp_years"], "internships": len(c["internships"])}


# ------------------------------------------------------------------ helpers
def _skill_status(ctx: Ctx, c: dict, skill: str):
    """('direct', level) | ('inferred', via) | None for one candidate and one skill name."""
    canon = _canon(skill, ctx.aliases)
    if canon in c["skills"]:
        return "direct", c["skills"][canon]["level"]
    via = _implied_tools(canon, ctx.aliases) & set(c["skills"])
    if via:
        return "inferred", sorted(c["skills"][v]["name"] for v in via)
    return None


def resolve_names(cands: list[dict], queries: list[str]):
    """Fuzzy-match names. Returns (found candidates, problems). Ambiguity and misses are reported, never guessed."""
    found, problems = [], []
    for q in queries:
        norm = re.sub(r"\b(candidate|mr|ms|mrs)\b", " ", q.lower())
        toks = re.findall(r"[a-z]+", norm)                  # keep initials: "Ananya R K" must not match "Ananya Iyer"
        if not toks or all(len(t) < 3 for t in toks):
            problems.append({"query": q, "status": "not_found", "available": [c["name"] for c in cands]})
            continue
        scored = []
        for c in cands:
            name_toks = re.findall(r"[a-z]+", c["name"].lower())
            if " ".join(name_toks) == " ".join(toks):
                s = 1.0
            elif all(t in name_toks for t in toks):
                s = 0.9
            else:
                s = max((difflib.SequenceMatcher(None, t, n).ratio() for t in toks if len(t) >= 3 for n in name_toks if len(n) >= 3), default=0)
                s = s * 0.85 if s >= 0.8 else 0
            if s:
                scored.append((s, c))
        scored.sort(key=lambda x: -x[0])
        if not scored:
            problems.append({"query": q, "status": "not_found", "available": [c["name"] for c in cands]})
        elif len(scored) > 1 and scored[1][0] >= scored[0][0] - 0.05:
            problems.append({"query": q, "status": "ambiguous", "options": [c["name"] for s, c in scored if s >= scored[0][0] - 0.05]})
        else:
            found.append(scored[0][1])
    return found, problems


# ------------------------------------------------------------------ tools
def top_candidates(ctx: Ctx, n: int = 5, recommendation: str | None = None) -> dict:
    cands = load_candidates(ctx)
    if recommendation:
        cands = [c for c in cands if c["recommendation"].lower() == recommendation.lower()]
    n = max(1, min(int(n), 50))
    return {"count": min(n, len(cands)), "total_scored": len(cands), "candidates": [_brief_row(c) for c in cands[:n]]}


def find_by_skill(ctx: Ctx, skills: list[str], match: str = "all") -> dict:
    cands, rows = load_candidates(ctx), []
    for c in cands:
        hits = {s: _skill_status(ctx, c, s) for s in skills}
        ok = [h is not None for h in hits.values()]
        if (all(ok) if match == "all" else any(ok)):
            row = _brief_row(c)
            row["skills"] = {s: ({"how": h[0], "level": h[1] or "solid"} if h[0] == "direct" else {"how": "inferred", "from": h[1]})
                             for s, h in hits.items() if h}
            rows.append(row)
    note = None
    if not rows:                                   # last resort: partial text match, clearly labelled
        for c in cands:
            related = [v["name"] for k, v in c["skills"].items() if any(s.lower() in k for s in skills)]
            if related:
                row = _brief_row(c)
                row["related_skills"] = related
                rows.append(row)
        note = "No exact match; showing candidates with related skills." if rows else None
    return {"searched": skills, "match": match, "count": len(rows), "candidates": rows, "note": note,
            "total_candidates": len(cands)}


def find_missing_skill(ctx: Ctx, skills: list[str]) -> dict:
    cands, rows = load_candidates(ctx), []
    for c in cands:
        missing = [s for s in skills if _skill_status(ctx, c, s) is None]
        if missing:
            row = _brief_row(c)
            row["missing"] = missing
            rows.append(row)
    return {"searched": skills, "count": len(rows), "candidates": rows, "total_candidates": len(cands),
            "note": "Everyone has these skills." if not rows and cands else None}


def find_by_experience(ctx: Ctx, min_years: float | None = None, max_years: float | None = None,
                       inclusive: bool = False) -> dict:
    """Employment years only (internships are reported separately). 'more than 2' is strict unless inclusive."""
    rows = []
    for c in load_candidates(ctx):
        y = c["exp_years"]
        if min_years is not None and not (y >= min_years if inclusive else y > min_years):
            continue
        if max_years is not None and not (y <= max_years if inclusive else y < max_years):
            continue
        row = _brief_row(c)
        row["internship_list"] = [f"{i.get('title')} at {i.get('company')} ({i.get('duration')})" for i in c["internships"]]
        rows.append(row)
    return {"min_years": min_years, "max_years": max_years, "inclusive": inclusive, "count": len(rows), "candidates": rows,
            "note": "Experience means paid employment; internships are listed separately."}


def find_with_internships(ctx: Ctx, min_count: int = 1) -> dict:
    """Candidates with internships/trainee roles, from the structured data (not a keyword guess)."""
    rows = []
    for c in load_candidates(ctx):
        if len(c["internships"]) >= int(min_count):
            row = _brief_row(c)
            row["internship_list"] = [f"{i.get('title')} at {i.get('company')} ({i.get('duration')})" for i in c["internships"]]
            rows.append(row)
    return {"min_count": int(min_count), "count": len(rows), "candidates": rows}


def _detail(c: dict) -> dict:
    d = _brief_row(c)
    d.update({"components": c["components"], "email": c["email"], "summary": c["summary"], "strengths": c["strengths"],
              "weaknesses": c["weaknesses"],
              "skills_by_status": {st: [b["skill"] for b in c["breakdown"] if b["status"] == st]
                                   for st in ("solid", "working", "inferred", "basic", "missing")},
              "soft_skills": c["soft_skills"], "internship_list": [f"{i.get('title')} at {i.get('company')} ({i.get('duration')})"
                                                                    for i in c["internships"]]})
    return d


def compare_candidates(ctx: Ctx, names: list[str]) -> dict:
    found, problems = resolve_names(load_candidates(ctx), names)
    if problems or len(found) < 2:
        return {"error": "Could not identify the candidates to compare", "problems": problems,
                "found": [c["name"] for c in found]}
    mine = [{s for s, _ in [(b["skill"], 0) for b in c["breakdown"] if b["status"] != "missing"]} for c in found]
    return {"candidates": [_detail(c) for c in found],
            "only_in_first": sorted(mine[0] - mine[1]) if len(found) == 2 else None,
            "only_in_second": sorted(mine[1] - mine[0]) if len(found) == 2 else None}


def explain_ranking(ctx: Ctx, first: str, second: str) -> dict:
    """Why is `first` ranked where it is relative to `second`?"""
    found, problems = resolve_names(load_candidates(ctx), [first, second])
    if problems or len(found) < 2:
        return {"error": "Could not identify both candidates", "problems": problems}
    a, b = found
    weights = effective_weights(ctx.job)
    contrib = {k: {"first": round(weights[k] * a["components"].get(k, 0), 1), "second": round(weights[k] * b["components"].get(k, 0), 1)}
               for k in weights}
    ha = {x["skill"] for x in a["breakdown"] if x["status"] != "missing"}
    hb = {x["skill"] for x in b["breakdown"] if x["status"] != "missing"}
    return {"first": {"name": a["name"], "rank": a["rank"], "score": a["score"], "weaknesses": a["weaknesses"]},
            "second": {"name": b["name"], "rank": b["rank"], "score": b["score"], "weaknesses": b["weaknesses"]},
            "first_is_ranked_higher": a["rank"] < b["rank"], "score_difference": round(a["score"] - b["score"], 1),
            "weighted_points_by_component": contrib, "component_weights": weights,
            "skills_only_first_has": sorted(ha - hb), "skills_only_second_has": sorted(hb - ha),
            "required_skills_matched": {"first": _brief_row(a)["required_skills_matched"], "second": _brief_row(b)["required_skills_matched"]},
            "missing_first": [x["skill"] for x in a["breakdown"] if x["status"] == "missing"],
            "missing_second": [x["skill"] for x in b["breakdown"] if x["status"] == "missing"]}


def candidate_profile(ctx: Ctx, name: str) -> dict:
    found, problems = resolve_names(load_candidates(ctx), [name])
    if problems:
        return {"error": "Could not identify the candidate", "problems": problems}
    c = found[0]
    d = _detail(c)
    d.update({"interview_questions": c["questions"], "projects": c["projects"][:6], "certifications": c["certifications"][:6],
              "education": c["education"], "skill_badges": {b["skill"]: COLOUR[b["status"]] for b in c["breakdown"]}})
    return d


def recommend_for_interview(ctx: Ctx, n: int = 1) -> dict:
    cands = load_candidates(ctx)
    short = [c for c in cands if c["recommendation"] == "Shortlist"]
    pool = short or cands
    n = max(1, min(int(n), 10))
    out = []
    for c in pool[:n]:
        d = _detail(c)
        d["interview_questions"] = c["questions"]
        out.append(d)
    return {"count": len(out), "candidates": out, "shortlisted_available": len(short),
            "note": None if short else "No candidate reached Shortlist; these are the best of the rest."}


def search_resume_text(ctx: Ctx, keyword: str, limit: int = 10) -> dict:
    """Keyword search in the original resume text (parameterized LIKE, case-insensitive) for facts not in the schema."""
    kw = keyword.strip()
    if len(kw) < 2:
        return {"error": "Keyword too short"}
    like = "%" + kw.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_") + "%"
    rows = ctx.conn.execute(
        """SELECT c.name, a.raw_text FROM applications a JOIN candidates c ON c.id = a.candidate_id
           JOIN analysis_results r ON r.application_id = a.id
           WHERE a.job_description_id = ? AND a.application_status = 'active' AND a.raw_text LIKE ? ESCAPE '\\' LIMIT ?""",
        (ctx.job_id, like, max(1, min(int(limit), 25)))).fetchall()
    hits = []
    for r in rows:
        i = r["raw_text"].lower().find(kw.lower())
        hits.append({"name": r["name"], "snippet": r["raw_text"][max(0, i - 70): i + len(kw) + 70].replace("\n", " ")})
    return {"keyword": kw, "count": len(hits), "matches": hits}


def semantic_search(ctx: Ctx, query: str, limit: int = 5) -> dict:
    """Hybrid search (keyword + meaning) over resume text, for concepts phrased differently from the resume."""
    q = str(query).strip()
    if len(q) < 3:
        return {"error": "Query too short"}
    rows = ctx.conn.execute(
        """SELECT a.id AS application_id, c.name, ch.text, ch.embedding
           FROM resume_chunks ch JOIN applications a ON a.id = ch.application_id AND a.application_status = 'active'
           JOIN candidates c ON c.id = a.candidate_id JOIN analysis_results r ON r.application_id = a.id
           WHERE a.job_description_id = ?""", (ctx.job_id,)).fetchall()
    chunks = [{"owner": r["application_id"], "name": r["name"], "text": r["text"], "vec": unpack(r["embedding"])} for r in rows]
    qvec, method = None, "keyword only (embeddings unavailable)"
    if any(c["vec"] for c in chunks):
        try:
            qvec = (ctx.embed_fn or client.embed)([q], "RETRIEVAL_QUERY")[0]
            method = "hybrid (keyword + semantic)"
        except client.LLMError:
            pass
    ranks = {c["application_id"]: c["rank"] for c in load_candidates(ctx)}
    names = {c["owner"]: c["name"] for c in chunks}
    hits = hybrid_rank(q, qvec, chunks, limit=max(1, min(int(limit), 10)))
    return {"query": q, "method": method, "count": len(hits),
            "matches": [{"name": names[h["owner"]], "job_rank": ranks.get(h["owner"]),
                         "semantic_similarity": h["semantic_similarity"], "keyword_match": h["keyword_match"],
                         "snippet": h["snippet"][:300]} for h in hits],
            "note": "Closest matches by meaning and wording, not guaranteed hits; read the snippets." if hits else "Nothing relevant found."}


def job_summary(ctx: Ctx) -> dict:
    cands = load_candidates(ctx)
    by = {}
    for c in cands:
        by[c["recommendation"]] = by.get(c["recommendation"], 0) + 1
    pending = ctx.conn.execute("SELECT COUNT(*) FROM applications WHERE job_description_id=? AND application_status='pending_choice'", (ctx.job_id,)).fetchone()[0]
    review = ctx.conn.execute("SELECT COUNT(*) FROM applications WHERE job_description_id=? AND application_status='active' "
                              "AND (extraction_status!='ok' OR verification_status='needs_review')", (ctx.job_id,)).fetchone()[0]
    return {"job_title": ctx.job.title, "required_skills": ctx.job.required_skills, "preferred_skills": ctx.job.preferred_skills,
            "min_experience_years": ctx.job.min_experience_years, "scored_candidates": len(cands), "by_recommendation": by,
            "average_score": round(sum(c["score"] for c in cands) / len(cands), 1) if cands else None,
            "pending_resume_choices": pending, "needing_manual_review": review}


# ------------------------------------------------------------------ registry
@dataclass
class Tool:
    fn: Callable
    description: str
    params: dict[str, str]      # name -> "type: description"


TOOLS: dict[str, Tool] = {
    "top_candidates": Tool(top_candidates, "Ranked list of the best candidates by match score. Use for 'top N', 'best candidate', or 'who is shortlisted/rejected' (recommendation filter).",
                           {"n": "int, how many (default 5; use 1 for 'the best')", "recommendation": "optional: Shortlist | Consider | Reject"}),
    "find_by_skill": Tool(find_by_skill, "Candidates who HAVE one or more skills (also counts skills implied by related tools, and says so). For 'who knows X', 'has X experience', 'X and Y'.",
                          {"skills": "list of skill names", "match": "'all' (default) or 'any'"}),
    "find_missing_skill": Tool(find_missing_skill, "Candidates who LACK one or more skills. For 'who is missing X'.", {"skills": "list of skill names"}),
    "find_by_experience": Tool(find_by_experience, "Filter by years of paid employment (internships listed separately). 'more than 2 years' = min_years 2 (strict).",
                               {"min_years": "optional number", "max_years": "optional number", "inclusive": "bool, true for 'at least/at most'"}),
    "find_with_internships": Tool(find_with_internships, "Candidates who have done internships or traineeships, with details. For 'who has internship experience'.",
                                  {"min_count": "int, minimum number of internships (default 1)"}),
    "compare_candidates": Tool(compare_candidates, "Side-by-side comparison of two or more named candidates.", {"names": "list of candidate names as written by the user"}),
    "explain_ranking": Tool(explain_ranking, "Explain why one named candidate is ranked above/below another (component points, skills only one has, missing skills).",
                            {"first": "name of the candidate the user says is higher", "second": "name of the other candidate"}),
    "candidate_profile": Tool(candidate_profile, "Full details of one named candidate: scores, strengths, weaknesses, skills, interview questions.", {"name": "candidate name"}),
    "recommend_for_interview": Tool(recommend_for_interview, "Best candidate(s) to interview, with strengths and suggested interview questions.", {"n": "int, default 1"}),
    "search_resume_text": Tool(search_resume_text, "Keyword search in resume text for anything not covered by other tools (companies, certifications, tools, universities, 'internship').",
                               {"keyword": "word or short phrase", "limit": "int, default 10"}),
    "semantic_search": Tool(semantic_search, "Find candidates by MEANING when the question describes a concept in different words than a resume would (e.g. 'built applications that recognise objects in images', 'worked on recommending things to users'). Returns closest matches with snippets. Prefer find_by_skill for named skills and search_resume_text for an exact phrase.",
                            {"query": "the concept, in plain words", "limit": "int, default 5"}),
    "job_summary": Tool(job_summary, "Overview of the job and the applicant pool: counts by recommendation, average score, required skills, pending items.", {}),
}


def call_tool(ctx: Ctx, name: str, args: dict) -> dict:
    """Validate and run one tool call. Unknown tools or bad arguments return an error dict instead of raising."""
    tool = TOOLS.get(name)
    if tool is None:
        return {"error": f"Unknown tool '{name}'"}
    clean = {}
    for k, v in (args or {}).items():
        if k not in tool.params:
            continue
        if k in ("skills", "names") and isinstance(v, str):
            v = [v]
        clean[k] = v
    try:
        return tool.fn(ctx, **clean)
    except (TypeError, ValueError) as exc:
        return {"error": f"Invalid arguments for {name}: {exc}"}


def catalog_text() -> str:
    return "\n".join(f"- {n}({', '.join(f'{p}: {d}' for p, d in t.params.items())}): {t.description}" for n, t in TOOLS.items())
