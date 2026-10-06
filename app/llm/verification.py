"""Double verification of extracted resume data.

Stage 1 (free, deterministic): contact details and skills must appear in the resume text.
Stage 2 (Gemini, a different model family from the extractor): proposes corrections, each with a
verbatim evidence quote. The code applies a correction ONLY if that quote really exists in the text.
"""
import json
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Callable

from app.llm import client, prompts
from app.models import CandidateProfile

LLMFn = Callable[[str, str], str]
SOFT_SKILLS = {
    "adaptability", "quick learner", "problem solving", "problem-solving", "teamwork", "communication",
    "interpersonal communication", "leadership", "time management", "critical thinking", "creativity",
    "team player", "analytical skills", "hard working", "collaboration",
}


@dataclass
class Change:
    field: str
    old: str | None
    new: str | None
    evidence: str
    source: str          # "deterministic" | "llm"


@dataclass
class VerificationResult:
    status: str          # verified | corrected | needs_review | partial (LLM stage unavailable)
    profile: CandidateProfile
    changes: list[Change] = field(default_factory=list)
    rejected: list[dict] = field(default_factory=list)   # LLM suggestions we refused (no valid evidence)
    notes: list[str] = field(default_factory=list)


# ---------- text helpers ----------
def _collapse(t: str) -> str:
    return re.sub(r"\s+", " ", t).strip().lower()


def _alnum(t: str) -> str:
    return re.sub(r"[^a-z0-9+#]", "", t.lower())


def _digits(t: str) -> str:
    return re.sub(r"\D", "", t)


def quote_in_text(quote: str | None, text: str) -> bool:
    return bool(quote) and len(quote.strip()) >= 3 and _collapse(quote) in _collapse(text)


# A skill the resume only mentions to say the candidate lacks it ("no experience with Docker", "currently learning Docker")
# is not a skill they have. The cue must sit right before the skill, so "no downtime using Docker" is not read as a negation.
_FILLER = r"(?:(?:any|prior|previous|hands-on|practical|professional|direct|real|much|formal|work|commercial)\s+)*"
_ABOUT = r"(?:(?:experience|knowledge|exposure|familiarity|background|understanding|expertise)\s+(?:with|in|of|using|on|to)\s+)?(?:(?:the|a|an|any)\s+)?"
_NEGATED_BEFORE = re.compile(
    r"(?:\bno|\bzero|\bwithout|\bnever\s+(?:used|worked(?:\s+with)?|touched)|\bnot\s+(?:familiar|experienced|proficient|comfortable)(?:\s+(?:with|in))?"
    r"|\bunfamiliar\s+with|\black(?:s|ing)?(?:\s+of)?|\b(?:haven'?t|have\s+not|hasn'?t|has\s+not)\s+(?:used|worked(?:\s+with)?))\s+"
    + _FILLER + _ABOUT + r"$"
    r"|(?:\blearning|\b(?:want|wants|plan|plans|hope|hopes|wish|aim)(?:ing)?\s+to\s+learn|\b(?:starting|beginning|yet)\s+to\s+learn)\s+(?:(?:the|about|of)\s+)?$",
    re.I)


def _mentioned_positively(form: str, text: str) -> bool | None:
    """True/False if the form is found (as a whole word) with/without a non-negated mention; None if it cannot be located."""
    parts = [re.escape(p) for p in re.split(r"[^A-Za-z0-9+#]+", form) if p]
    if not parts:
        return None
    found = False
    for m in re.finditer(r"(?<![A-Za-z0-9])" + r"[\W_]*".join(parts) + r"(?![A-Za-z0-9])", text, re.I):
        found = True
        before = re.split(r"[.;\n|•]", text[max(0, m.start() - 80):m.start()])[-1]
        if not _NEGATED_BEFORE.search(re.sub(r"\bnot\s+only\b", "also", before + "", flags=re.I)):
            return True
    return False if found else None


def skill_in_text(skill: str, text: str, aliases: dict[str, str]) -> bool:
    """True if the skill (or a known alias of it) appears in the text, ignoring case and punctuation.
    A skill that is only mentioned as missing or being learned does not count."""
    forms = {skill} | {a for a, c in aliases.items() if c.lower() == skill.lower()}
    low = text.lower()
    present, verdicts = False, []
    for f in forms:
        if len(_alnum(f)) >= 3:
            hit = _alnum(f) in _alnum(text)
        else:
            hit = bool(re.search(rf"(?<![a-z0-9]){re.escape(f.lower())}(?![a-z0-9])", low))   # very short skills (e.g. "C") need word boundaries
        if hit:
            present = True
            verdicts.append(_mentioned_positively(f, text))
    if not present:
        return False
    return not verdicts or True in verdicts or all(v is None for v in verdicts)


# ---------- stage 1 ----------
def deterministic_checks(profile: CandidateProfile, text: str, aliases: dict[str, str]):
    """Return (cleaned_profile, changes, notes). Never calls an LLM."""
    p = profile.model_copy(deep=True)
    changes, notes = [], []

    if p.email and p.email.lower() not in text.lower():
        notes.append(f"email '{p.email}' not found in resume text")
    if p.phone and len(_digits(p.phone)) >= 7 and _digits(p.phone) not in _digits(text):
        notes.append(f"phone '{p.phone}' not found in resume text")

    kept = []
    for s in p.skills:
        if s.lower() in SOFT_SKILLS:
            # Soft skills are not thrown away: they move to their own list (not used in technical matching).
            if s.lower() not in (x.lower() for x in p.soft_skills):
                p.soft_skills.append(s)
            changes.append(Change("skills", s, None, "moved to soft_skills", "deterministic"))
        elif not skill_in_text(s, text, aliases):
            changes.append(Change("skills", s, None, "skill not present in resume text", "deterministic"))
        else:
            kept.append(s)
    p.skills = kept
    p.skill_levels = {k: v for k, v in p.skill_levels.items() if k in kept}
    return p, changes, notes


_YEARS_RE = re.compile(r"(\d+(?:\.\d+)?)\s*\+?\s*(?:years?|yrs?)", re.I)
_RANGE_RE = re.compile(r"((?:19|20)\d\d)\s*(?:-|–|—|to)\s*((?:19|20)\d\d|present|current|now|till date|ongoing)", re.I)


def years_supported_by(quote: str, new, today: date | None = None) -> bool:
    """Does the quote itself justify this many years? True if it states that number ("3 years") or its date ranges
    ("2023 - 2026", "2021 - present") add up to it (within a year, since resumes give years, not days)."""
    today = today or date.today()
    try:
        want = float(new)
    except (TypeError, ValueError):
        return False
    if any(abs(float(m) - want) <= 0.25 for m in _YEARS_RE.findall(quote)):
        return True
    ranges = _RANGE_RE.findall(quote)
    if ranges:
        span = sum(max(0.0, (today.year + today.month / 12 if not e.isdigit() else int(e)) - int(s)) for s, e in ranges)
        return abs(span - want) <= 1.0
    return False


# ---------- stage 2 ----------
def _apply_correction(p: CandidateProfile, c: dict, text: str, aliases: dict[str, str]) -> Change:
    """Apply one LLM correction if valid; raise ValueError with the reason if it must be refused."""
    action, fld, old, new, quote = (c.get(k) for k in ("action", "field", "old", "new", "evidence_quote"))
    if not quote_in_text(quote, text):
        raise ValueError("evidence quote not found in resume")

    if action == "remove_skill":
        match = next((s for s in p.skills if s.lower() == str(old).lower()), None)
        if not match:
            raise ValueError("skill to remove is not in the extracted list")
        p.skills.remove(match)
        p.skill_levels.pop(match, None)
        if match.lower() in SOFT_SKILLS and match.lower() not in (x.lower() for x in p.soft_skills):
            p.soft_skills.append(match)
        return Change("skills", match, None, quote, "llm")

    if action in ("add_skill", "replace_skill"):
        new = str(new or "").strip()
        if not new or not skill_in_text(new, quote, aliases):
            raise ValueError("new skill does not appear in the evidence quote")
        if action == "replace_skill":
            match = next((s for s in p.skills if s.lower() == str(old).lower()), None)
            if not match:
                raise ValueError("skill to replace is not in the extracted list")
            p.skills[p.skills.index(match)] = new
            if match in p.skill_levels:
                p.skill_levels[new] = p.skill_levels.pop(match)
            return Change("skills", match, new, quote, "llm")
        if new.lower() in (s.lower() for s in p.skills):
            raise ValueError("skill already present")
        p.skills.append(new)
        return Change("skills", None, new, quote, "llm")

    if action == "set" and fld in ("name", "email", "phone", "experience_years"):
        if fld == "email" and str(new).lower() not in quote.lower():
            raise ValueError("new email not in evidence quote")
        if fld == "phone" and _digits(str(new)) not in _digits(quote):
            raise ValueError("new phone not in evidence quote")
        if fld == "experience_years" and not years_supported_by(quote, new):
            raise ValueError("new experience_years is not supported by the evidence quote")
        before = getattr(p, fld)
        try:
            validated = CandidateProfile.model_validate({**p.model_dump(), fld: new})
        except Exception as exc:
            raise ValueError(f"invalid value: {str(exc)[:80]}") from None
        setattr(p, fld, getattr(validated, fld))
        return Change(fld, None if before is None else str(before), str(new), quote, "llm")

    raise ValueError(f"unsupported action/field: {action}/{fld}")


def _default_llm(system: str, user: str) -> str:
    # Deliberately Gemini only: a different model family from the extractor is the point of the second check.
    return client.complete_with_fallback(system, user, [("gemini", None)])


def verify_profile(profile: CandidateProfile, text: str, aliases: dict[str, str] | None = None,
                   llm: LLMFn = _default_llm) -> VerificationResult:
    aliases = aliases or {}
    cleaned, changes, notes = deterministic_checks(profile, text, aliases)
    rejected: list[dict] = []
    llm_ok = True

    user = prompts.VERIFY_USER.format(today=date.today().isoformat(), text=text[:12000], profile=cleaned.model_dump_json(indent=1))
    suggestions = []
    try:
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", llm(prompts.VERIFY_SYSTEM, user).strip())
        data = json.loads(raw)
        suggestions = data.get("corrections", []) if isinstance(data, dict) else []
    except client.LLMError as exc:
        llm_ok = False
        notes.append(f"LLM verifier unavailable: {exc}")
    except json.JSONDecodeError:
        llm_ok = False
        notes.append("LLM verifier returned invalid JSON")

    for c in suggestions if isinstance(suggestions, list) else []:
        try:
            changes.append(_apply_correction(cleaned, c, text, aliases))
        except (ValueError, AttributeError, TypeError) as exc:
            rejected.append({"suggestion": c, "reason": str(exc)})

    # An unresolved contact mismatch means we cannot trust who this person is.
    contact_problem = any(n.startswith(("email", "phone")) for n in notes) and not any(
        ch.field in ("email", "phone") for ch in changes)
    if contact_problem:
        status = "needs_review"
    elif not llm_ok:
        status = "partial"
    else:
        status = "corrected" if changes else "verified"
    return VerificationResult(status, cleaned, changes, rejected, notes)
