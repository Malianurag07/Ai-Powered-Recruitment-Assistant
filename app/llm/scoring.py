"""Hybrid candidate scoring. The rubric is documented in docs/SCORING.md.

Skills and experience are computed in code (repeatable). Project/education relevance and overall fit
come from the LLM, which is handed the computed facts so it cannot contradict them.
"""
import json
import math
import re
import statistics
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date
from typing import Callable

from pydantic import ValidationError

from app.config import JUDGE_SAMPLES
from app.llm import client, prompts
from app.llm.verification import quote_in_text, skill_in_text
from app.models import CandidateProfile, JobProfile, ScoreJudgment

LLMFn = Callable[[str, str], str]

WEIGHTS = {"skills": 0.50, "experience": 0.20, "projects_education": 0.15, "fit": 0.15}      # defaults; a job can override them
COMPONENT_KEYS = tuple(WEIGHTS)
SHORTLIST_AT, CONSIDER_AT = 70, 45
PREFERRED_WEIGHT = 0.5
# "semantic" = the resume shows the requirement in different words (verified by a verbatim quote); "partial" = only adjacent experience.
CREDIT = {"solid": 1.0, "working": 0.75, "inferred": 0.75, "semantic": 0.75, "basic": 0.5, "partial": 0.4, "missing": 0.0}
COLOUR = {"solid": "green", "working": "yellow", "inferred": "yellow", "semantic": "yellow", "basic": "orange", "partial": "orange", "missing": "red"}

# A resume rarely lists every umbrella term ("Machine Learning") even when it lists the tools that imply it.
# Owning any listed tool earns partial credit ("inferred", yellow) for the umbrella skill.
IMPLIED_BY = {
    "machine learning": {"deep learning", "tensorflow", "pytorch", "scikit-learn", "keras", "xgboost", "lstm", "cnn", "yolo", "predictive modeling"},
    "deep learning": {"tensorflow", "pytorch", "keras", "lstm", "cnn", "yolo", "resnet"},
    "computer vision": {"opencv", "yolo", "cnn", "resnet"},
    "large language models": {"gemini api", "openai api", "langchain", "gemini", "openrouter", "llm api integration", "llms"},
    "vector databases": {"pinecone", "chroma", "chromadb", "faiss", "pgvector", "milvus", "vector embeddings"},
    "python": {"pandas", "numpy", "scikit-learn", "tensorflow", "pytorch", "flask", "django", "fastapi"},
    "rest apis": {"fastapi", "flask", "django", "rest api design & integration", "rest api testing"},
    "sql": {"mysql", "postgresql", "sqlite", "oracle"},
}
DEFAULT_INTERNSHIP_MONTHS = 3


@dataclass
class SkillResult:
    skill: str
    importance: str          # required | preferred
    status: str              # solid | working | inferred | semantic | basic | partial | missing
    colour: str
    evidence: str = ""      # verbatim resume quote for a 'semantic' or 'partial' match
    factor: float = 1.0     # credit multiplier from evidence of use and recency (1.0 = no adjustment)
    notes: str = ""         # why the factor is below 1, in words for the recruiter


@dataclass
class ScoreResult:
    match_score: float
    recommendation: str
    skill_match_ratio: float
    components: dict
    skill_breakdown: list[SkillResult]
    matching_skills: list[str]
    missing_skills: list[str]
    strengths: list[str] = field(default_factory=list)
    weaknesses: list[str] = field(default_factory=list)
    summary: str = ""
    interview_questions: list[str] = field(default_factory=list)
    llm_status: str = "ok"           # ok | unavailable
    llm_error: str = ""


# ---------- skills (code) ----------
def _canon(skill: str, aliases: dict[str, str]) -> str:
    return aliases.get(skill.strip().lower(), skill.strip()).lower()


def _level_status(level: str | None) -> str:
    lv = (level or "").strip().lower()
    if lv in ("basic", "beginner", "familiar"):
        return "basic"
    if lv in ("working", "intermediate", "working knowledge"):
        return "working"
    return "solid"


def _implied_tools(canon_skill: str, aliases: dict[str, str]) -> set[str]:
    """Tools implying this skill. Keys are canonicalized so 'large language models' and 'LLMs' share one entry."""
    out: set[str] = set()
    for umbrella, tools in IMPLIED_BY.items():
        if _canon(umbrella, aliases) == canon_skill:
            out |= {_canon(t, aliases) for t in tools}
    return out


def skill_match(profile: CandidateProfile, job: JobProfile, aliases: dict[str, str], today: date | None = None, text: str = ""):
    """Return (skills_score 0-100, required_match_ratio, breakdown)."""
    today = today or date.today()
    have = {_canon(s, aliases) for s in profile.skills}
    levels = {_canon(k, aliases): v for k, v in profile.skill_levels.items()}
    rows: list[SkillResult] = []
    for importance, skills in (("required", job.required_skills), ("preferred", job.preferred_skills)):
        for s in skills:
            c = _canon(s, aliases)
            factor, note = 1.0, ""
            if c in have:
                status = _level_status(levels.get(c))
                factor, note = _evidence_factor(s, profile, aliases, text, today)
            elif have & _implied_tools(c, aliases):
                status = "inferred"
            else:
                status = "missing"
            rows.append(SkillResult(s, importance, status, COLOUR[status], factor=factor, notes=note))

    score, ratio = _score_rows(rows)
    return score, ratio, rows


def _score_rows(rows: list[SkillResult]) -> tuple[float, float]:
    """(skills score 0-100, share of required skills matched). Recomputed after meaning-based matches are added."""
    total = sum(1.0 if r.importance == "required" else PREFERRED_WEIGHT for r in rows)
    earned = sum(CREDIT[r.status] * r.factor * (1.0 if r.importance == "required" else PREFERRED_WEIGHT) for r in rows)
    required = [r for r in rows if r.importance == "required"]
    ratio = sum(r.status not in ("missing", "partial") for r in required) / len(required) if required else 0.0
    return (100 * earned / total if total else 0.0), ratio


def semantic_cover(text: str, requirements: list[str], llm: LLMFn) -> dict[str, tuple[str, str]]:
    """Ask the AI which still-missing requirements the resume shows in other words. Returns {requirement: (strength, quote)}.

    Same grounding rule as verification: a match counts only if its quote appears verbatim in the resume, so the AI can
    point at evidence but cannot invent it. Any failure returns {} and the word-for-word result stands.
    """
    if not requirements or not text.strip():
        return {}
    user = prompts.SEMANTIC_USER.format(reqs="\n".join(f"- {r}" for r in requirements), text=text[:12000])
    try:
        data = json.loads(re.sub(r"^```(?:json)?\s*|\s*```$", "", llm(prompts.SEMANTIC_SYSTEM, user).strip()))
        matches = data.get("matches", []) if isinstance(data, dict) else []
    except (client.LLMError, ValueError):
        return {}
    wanted = {r.lower(): r for r in requirements}
    out: dict[str, tuple[str, str]] = {}
    for m in matches if isinstance(matches, list) else []:
        if not isinstance(m, dict):
            continue
        req = wanted.get(str(m.get("requirement", "")).strip().lower())
        quote, strength = str(m.get("evidence_quote") or "").strip(), m.get("strength")
        if req and strength in ("direct", "partial") and quote_in_text(quote, text):
            out[req] = (strength, quote[:200])
    return out


# ---------- degrees (code) ----------
# A requirement such as "Bachelor's degree in Computer Science" is not a skill word, so it can never match a skills list. A higher degree
# (M.Sc., MBA, Ph.D.) also satisfies it. Level is read from the degree text; a field named in the requirement must also appear in the degree.
_LEVELS = [
    (4, re.compile(r"\bph\.?\s?d\b|\bdoctor", re.I)),
    (3, re.compile(r"\bmaster|\bm\.?\s?(?:sc|tech|e|a|s|com|pharm|phil|ca)\b\.?|\bmba\b|\bmca\b|\bpost[- ]?graduate", re.I)),
    (2, re.compile(r"\bbachelor|\bb\.?\s?(?:tech|e|sc|a|com|pharm|ca|ba|s|des|arch)\b\.?|\bbca\b|\bbba\b|\bundergraduate|\bgraduate\b", re.I)),
    (1, re.compile(r"\bdiploma\b", re.I)),
]
_DEGREE_WORDS = re.compile(r"\bdegree\b|\bbachelor|\bmaster|\bph\.?\s?d\b|\bdiploma\b|\bb\.?tech\b|\bm\.?tech\b|\bgraduate\b|\bundergraduate\b", re.I)
_GENERIC_FIELD_WORDS = {"related", "field", "fields", "equivalent", "and", "similar", "science", "sciences", "engineering", "technology", "studies", "applications", "discipline"}   # too broad to tell fields apart
_FIELD = re.compile(r"\b(?:in|of)\s+([A-Za-z][A-Za-z &/,.\-]+)$", re.I)


def degree_level(text: str | None) -> int:
    """0 = none/other, 1 diploma, 2 bachelor, 3 master, 4 doctorate."""
    for level, rx in _LEVELS:
        if text and rx.search(text):
            return level
    return 0


def degree_met(requirement: str, education) -> tuple[bool, str]:
    """(met, evidence) for a degree requirement, judged by level and, when the requirement names a field, by field. A requirement that
    is not about a degree returns (False, "")."""
    if not _DEGREE_WORDS.search(requirement):
        return False, ""
    wanted = degree_level(requirement) or 2                      # a bare "degree" means a bachelor's
    m = _FIELD.search(requirement)
    field_words = {w for w in re.split(r"[^a-z]+", m.group(1).lower()) if len(w) > 3 and w not in _GENERIC_FIELD_WORDS} if m else set()
    for e in education:
        text = " ".join(x for x in (e.degree, e.institution) if x)
        if degree_level(e.degree) >= wanted and (not field_words or any(w in (e.degree or "").lower() for w in field_words)):
            return True, (e.degree or "")[:200]
    return False, ""


# ---------- evidence and recency (code) ----------
# A skill that is only listed in a skills section is a weaker claim than one a project or job shows in use, and a skill last used
# many years ago is weaker than a current one. Both only trim the credit a matched skill earns; neither removes it. When there is
# nothing to check against (no described work, no dates) the skill keeps full credit: unknown is not evidence of absence.
LISTED_FACTOR = 0.85          # credit for a skill that only appears in a list while the resume describes other work
DATED_FACTOR = {4: 0.95, 7: 0.85}        # age in years at which the factor applies (4-6 years: 0.95, 7 or more: 0.85)


def entry_end_year(duration: str | None, today: date | None = None) -> int | None:
    """End year of a job or internship range ('2019 - 2021', 'Jan 2023 - Present'); None if there is no parseable range."""
    today = today or date.today()
    m = _RANGE.search(duration or "")
    if not m:
        return None
    _m1, _y1, _m2, y2, now = m.groups()
    return today.year if now else int(y2)


def _mention_count(skill: str, text: str, aliases: dict[str, str]) -> int:
    forms = {skill} | {a for a, c in aliases.items() if c.lower() == skill.lower()}
    return sum(len(re.findall(r"(?<![A-Za-z0-9])" + re.escape(f) + r"(?![A-Za-z0-9])", text, re.I)) for f in forms if f.strip())


def _evidence_factor(skill: str, profile: CandidateProfile, aliases: dict[str, str], text: str, today: date) -> tuple[float, str]:
    """(credit multiplier, note) for a skill the candidate claims."""
    dated: list[tuple[int, str]] = []                         # (end year, label) of jobs and internships that mention the skill
    work = [(e, e.title, e.company, e.summary) for e in profile.experience + profile.internships]
    for e, *parts in work:
        if skill_in_text(skill, " ".join(p for p in parts if p), aliases):
            end = entry_end_year(e.duration, today)
            if end is not None:
                dated.append((end, e.title or e.company or "a job"))
            else:
                return 1.0, ""                                  # shown in use, no date: nothing to penalise
    other = " ".join(profile.projects + profile.certifications)
    if other.strip() and skill_in_text(skill, other, aliases):
        return 1.0, ""                                          # a project or certificate shows it, undated: counts as current
    if dated:
        age = today.year - max(y for y, _ in dated)
        factor = next((f for lim, f in sorted(DATED_FACTOR.items(), reverse=True) if age >= lim), 1.0)
        return (factor, f"last used in {max(y for y, _ in dated)}") if factor < 1 else (1.0, "")
    has_described_work = any(e.summary or e.title for e in profile.experience + profile.internships) or bool(profile.projects)
    if text and _mention_count(skill, text, aliases) >= 2:      # mentioned somewhere besides the skills list
        return 1.0, ""
    if has_described_work:
        return LISTED_FACTOR, "listed only: no project or job on the resume shows it in use"
    return 1.0, ""


# ---------- experience (code) ----------
_MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
_DATE = r"(?:([A-Za-z]{3})[A-Za-z]*\.?\s+)?(\d{4})"
_RANGE = re.compile(rf"{_DATE}\s*(?:-|–|—|to)\s*(?:{_DATE}|(present|current|now))", re.I)


def duration_months(text: str | None, today: date | None = None) -> int:
    """Parse 'Oct 2025 – May 2026', '2022 - 2024' or 'Jan 2026 - Present' into months; default if unparseable."""
    today = today or date.today()
    m = _RANGE.search(text or "")
    if not m:
        return DEFAULT_INTERNSHIP_MONTHS
    m1, y1, m2, y2, now = m.groups()
    start = int(y1) * 12 + (_MONTHS.get((m1 or "jan")[:3].lower(), 1) - 1)
    if now:
        end = today.year * 12 + today.month - 1
    elif not m1 and not m2:
        end = int(y2) * 12                       # year-only range "2022 - 2024": whole years, no month padding
    else:
        end = int(y2) * 12 + (_MONTHS.get((m2 or "dec")[:3].lower(), 12) - 1)
    months = end - start + (1 if (m1 or m2 or now) else 0)
    return months if 0 < months <= 120 else DEFAULT_INTERNSHIP_MONTHS


RELEVANCE_FLOOR = 0.2          # years in another field still count a little
RELEVANCE_FULL_AT = 50.0       # skills score at which the candidate's experience counts in full


def experience_relevance(skills_score: float) -> float:
    """0.2 to 1.0: how much of the candidate's experience to credit, judged by how much of this job's skill set they show."""
    return RELEVANCE_FLOOR + (1 - RELEVANCE_FLOOR) * min(1.0, max(0.0, skills_score) / RELEVANCE_FULL_AT)


def experience_score(profile: CandidateProfile, job: JobProfile, today: date | None = None, relevance: float = 1.0):
    """(score 0-100, effective years). `relevance` scales the score: years spent in an unrelated field earn less."""
    intern_years = sum(duration_months(i.duration, today) for i in profile.internships) / 12
    effective = (profile.experience_years or 0) + 0.5 * intern_years
    minimum = job.min_experience_years or 0
    score = min(100.0, effective / minimum * 100) if minimum > 0 else min(100.0, 60 + 20 * effective)
    return score * relevance, effective


# ---------- weights
def default_weights_percent() -> dict[str, float]:
    return {k: round(v * 100, 1) for k, v in WEIGHTS.items()}


def validate_weights_percent(raw: dict) -> dict[str, float]:
    """Four non-negative percentages that add up to 100. Raises ValueError with a message the recruiter can read."""
    if not isinstance(raw, dict) or set(raw) != set(COMPONENT_KEYS):
        raise ValueError("Give a percentage for each of: " + ", ".join(COMPONENT_KEYS))
    try:
        out = {k: float(raw[k]) for k in COMPONENT_KEYS}
    except (TypeError, ValueError):
        raise ValueError("Each weight must be a number") from None
    if any(not math.isfinite(v) or v < 0 or v > 100 for v in out.values()):
        raise ValueError("Each weight must be between 0 and 100")
    if abs(sum(out.values()) - 100) > 0.01:
        raise ValueError(f"The weights must add up to 100 (they add up to {sum(out.values()):g})")
    return out


def effective_weights(job: JobProfile) -> dict[str, float]:
    """Fractions that sum to 1: the job's own weights if the recruiter set them, else the defaults."""
    return {k: v / 100 for k, v in job.weights.items()} if job.weights else dict(WEIGHTS)


def weighted_score(components: dict, weights: dict[str, float]) -> float:
    return round(sum(weights[k] * components.get(k, 0) for k in COMPONENT_KEYS), 1)


# ---------- recommendation ----------
def recommend(score: float, shortlist_at: float = SHORTLIST_AT, consider_at: float = CONSIDER_AT) -> str:
    return "Shortlist" if score >= shortlist_at else "Consider" if score >= consider_at else "Reject"


def default_cutoffs() -> dict[str, float]:
    return {"shortlist": float(SHORTLIST_AT), "consider": float(CONSIDER_AT)}


def validate_cutoffs(raw: dict) -> dict[str, float]:
    """Two score cutoffs: Shortlist at or above the first, Consider at or above the second, Reject below. Raises ValueError."""
    if not isinstance(raw, dict) or set(raw) != {"shortlist", "consider"}:
        raise ValueError("Give both a Shortlist and a Consider cutoff")
    try:
        out = {k: float(raw[k]) for k in ("shortlist", "consider")}
    except (TypeError, ValueError):
        raise ValueError("Each cutoff must be a number") from None
    if not all(math.isfinite(v) for v in out.values()) or not (0 < out["consider"] < out["shortlist"] <= 100):
        raise ValueError("Consider must be above 0 and below Shortlist, and Shortlist at most 100")
    return out


def effective_cutoffs(job: JobProfile) -> dict[str, float]:
    return dict(job.cutoffs) if job.cutoffs else default_cutoffs()


def recommend_for(job: JobProfile, score: float) -> str:
    c = effective_cutoffs(job)
    return recommend(score, c["shortlist"], c["consider"])


GATE_FAILS = ("missing", "partial")      # a must-have shown only in adjacent form ("partial") is not met


def gate_failures(gates: list[str], rows) -> list[str]:
    """Must-have skills this candidate does not meet. `rows` are SkillResult objects or their stored dict form."""
    wanted = {g.strip().lower() for g in gates}
    out = []
    for r in rows:
        skill, status = (r["skill"], r["status"]) if isinstance(r, dict) else (r.skill, r.status)
        if skill.strip().lower() in wanted and status in GATE_FAILS:
            out.append(skill)
    return out


def final_label(job: JobProfile, score: float, rows) -> str:
    """The recommendation shown to the recruiter: from the score and cutoffs, but never Shortlist while a must-have is missing."""
    label = recommend_for(job, score)
    return "Consider" if label == "Shortlist" and gate_failures(job.gates, rows) else label


# ---------- LLM judgment ----------
def _default_llm(system: str, user: str) -> str:
    return client.complete_with_fallback(system, user, [("groq", client.GROQ_MODEL), ("gemini", None)])


def _candidate_brief(p: CandidateProfile) -> str:
    return json.dumps({
        "experience_years": p.experience_years,
        "experience": [j.model_dump(exclude_none=True) for j in p.experience],
        "internships": [j.model_dump(exclude_none=True) for j in p.internships],
        "education": [e.model_dump(exclude_none=True) for e in p.education],
        "projects": p.projects[:8], "certifications": p.certifications[:6],
        "soft_skills": p.soft_skills, "skills": p.skills,
    }, indent=1)


RESUME_CHARS_FOR_JUDGE = 5000


def judge(profile: CandidateProfile, job: JobProfile, facts: str, llm: LLMFn, text: str = "") -> ScoreJudgment:
    """Ask the LLM, retrying once on invalid output. Raises LLMError/ValueError if it cannot."""
    job_txt = json.dumps({"title": job.title, "summary": job.summary, "min_experience_years": job.min_experience_years,
                          "soft_skills": job.soft_skills})
    user = prompts.SCORE_USER.format(job=job_txt, candidate=_candidate_brief(profile), facts=facts,
                                  resume=text[:RESUME_CHARS_FOR_JUDGE] or "(not available)")
    error = ""
    for attempt in range(2):
        try:
            raw = llm(prompts.SCORE_SYSTEM, user if attempt == 0 else user + prompts.RETRY_SUFFIX.format(error=error))
            return ScoreJudgment.model_validate(json.loads(re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip())))
        except (json.JSONDecodeError, ValidationError, ValueError) as exc:
            error = str(exc)[:200]
    raise ValueError(error)


def judge_consensus(profile: CandidateProfile, job: JobProfile, facts: str, llm: LLMFn, samples: int = JUDGE_SAMPLES,
                    text: str = "") -> ScoreJudgment:
    """Ask the LLM several times in parallel and keep the median, rounded to the nearest 10.

    A single LLM judgment varies from run to run even at temperature 0 (measured: up to ~7 points on the final score).
    The median of 3, rounded, roughly halves that noise; the calls run in parallel so latency is unchanged.
    Text (strengths, summary, questions) comes from the judgment closest to the consensus.
    """
    def one(_):
        try:
            return judge(profile, job, facts, llm, text)
        except (client.LLMError, ValueError):
            return None

    with ThreadPoolExecutor(max_workers=max(1, samples)) as pool:
        results = [r for r in pool.map(one, range(max(1, samples))) if r is not None]
    if not results:
        raise ValueError("no valid AI judgment was returned")
    consensus = lambda xs: float(round(statistics.median(xs) / 10) * 10)   # noqa: E731
    fit = consensus([r.fit_score for r in results])
    pe = consensus([r.projects_education_score for r in results])
    closest = min(results, key=lambda r: abs(r.fit_score - fit) + abs(r.projects_education_score - pe))
    return closest.model_copy(update={"fit_score": fit, "projects_education_score": pe})


def score_candidate(profile: CandidateProfile, job: JobProfile, aliases: dict[str, str] | None = None,
                    llm: LLMFn = _default_llm, today: date | None = None, text: str = "") -> ScoreResult:
    """`text` is the resume text. When given, requirements the word-for-word match missed get one grounded AI pass
    (semantic_cover), so a resume that says the same thing in other words is not scored as missing it."""
    aliases = aliases or {}
    skills_score, ratio, rows = skill_match(profile, job, aliases, today, text)
    for r in rows:                                   # a higher degree meets a lower-degree requirement (decided in code, no AI)
        if r.status == "missing":
            met, evidence = degree_met(r.skill, profile.education)
            if met:
                r.status, r.colour, r.evidence = "semantic", COLOUR["semantic"], evidence
    skills_score, ratio = _score_rows(rows)
    still_missing = [r for r in rows if r.status == "missing"]
    if text and still_missing:
        found = semantic_cover(text, [r.skill for r in still_missing], llm)
        for r in still_missing:
            if r.skill in found:
                strength, quote = found[r.skill]
                r.status = "semantic" if strength == "direct" else "partial"
                r.colour, r.evidence = COLOUR[r.status], quote
        skills_score, ratio = _score_rows(rows)
    relevance = experience_relevance(skills_score)
    exp_score, eff_years = experience_score(profile, job, today, relevance)
    matching = [r.skill for r in rows if r.status != "missing"]
    missing = [r.skill for r in rows if r.status == "missing"]
    req = [r for r in rows if r.importance == "required"]

    facts = (f"Required skills matched: {sum(r.status != 'missing' for r in req)} of {len(req)}.\n"
             f"Matched (with level): {[f'{r.skill} ({r.status})' for r in rows if r.status != 'missing']}\n"
             f"Missing: {missing}\n"
             f"Employment years: {profile.experience_years}; effective years incl. internships: {eff_years:.1f}; "
             f"job minimum: {job.min_experience_years}; share of experience credited for field relevance: {relevance:.0%}")
    try:
        j = judge_consensus(profile, job, facts, llm, text=text)
        pe, fit, llm_status, err = j.projects_education_score, j.fit_score, "ok", ""
    except (client.LLMError, ValueError) as exc:
        # LLM unavailable: fall back to the code-computed skills score so a score still exists, and say so.
        j, pe, fit, llm_status, err = ScoreJudgment(fit_score=0, projects_education_score=0), skills_score, skills_score, "unavailable", str(exc)[:200]

    parts = {"skills": skills_score, "experience": exp_score, "projects_education": pe, "fit": fit}
    final = weighted_score(parts, effective_weights(job))
    return ScoreResult(
        match_score=final, recommendation=final_label(job, final, rows), skill_match_ratio=round(ratio, 3),
        components={k: round(v, 1) for k, v in parts.items()}, skill_breakdown=rows,
        matching_skills=matching, missing_skills=missing, strengths=j.strengths, weaknesses=j.weaknesses,
        summary=j.summary, interview_questions=j.interview_questions, llm_status=llm_status, llm_error=err)
