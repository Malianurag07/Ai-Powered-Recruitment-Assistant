"""Pydantic schemas: the contract for what the LLM must return. Invalid output raises ValidationError."""
import re

from pydantic import BaseModel, Field, field_validator

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class Education(BaseModel):
    degree: str | None = None
    institution: str | None = None
    year: str | None = None


class Job(BaseModel):
    title: str | None = None
    company: str | None = None
    duration: str | None = None
    summary: str | None = None


class CandidateProfile(BaseModel):
    name: str | None = None
    email: str | None = None
    phone: str | None = None
    education: list[Education] = Field(default_factory=list)
    experience_years: float | None = None
    experience: list[Job] = Field(default_factory=list)
    internships: list[Job] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=list)
    soft_skills: list[str] = Field(default_factory=list)
    skill_levels: dict[str, str] = Field(default_factory=dict)
    projects: list[str] = Field(default_factory=list)
    certifications: list[str] = Field(default_factory=list)

    # LLMs sometimes return null instead of [] for empty lists; treat as empty.
    @field_validator("education", "experience", "internships", "skills", "soft_skills", "projects", "certifications", mode="before")
    @classmethod
    def none_to_list(cls, v):
        if v is None:
            return []
        # Also drop null items inside a list, e.g. ["Teamwork", null], which small models sometimes emit.
        return [x for x in v if x is not None] if isinstance(v, list) else v

    @field_validator("email")
    @classmethod
    def valid_email(cls, v):
        if v is None:
            return None
        v = v.strip().lower()
        if not EMAIL_RE.match(v):
            raise ValueError(f"'{v}' is not a valid email")
        return v

    @field_validator("skills")
    @classmethod
    def clean_skills(cls, v):
        seen, out = set(), []
        for s in v:
            s = str(s).strip()
            if s.count("(") != s.count(")") or len(s) > 40:  # fragment such as "Power BI (DAX" or a sentence
                continue
            if s and s.lower() not in seen:
                seen.add(s.lower())
                out.append(s)
        return out

    @field_validator("soft_skills")
    @classmethod
    def clean_soft_skills(cls, v):
        seen, out = set(), []
        for s in v:
            s = str(s).strip()
            if s and len(s) <= 40 and s.lower() not in seen:
                seen.add(s.lower())
                out.append(s)
        return out

    @field_validator("skill_levels", mode="before")
    @classmethod
    def none_to_dict(cls, v):
        if v is None:
            return {}
        # Small models sometimes emit {"SQL": null}; drop entries without a real level.
        return {k: lv for k, lv in v.items() if isinstance(lv, str) and lv} if isinstance(v, dict) else {}

    @field_validator("experience_years")
    @classmethod
    def sane_years(cls, v):
        if v is not None and not (0 <= v <= 60):
            raise ValueError("experience_years must be between 0 and 60")
        return v


def _dedupe(items) -> list[str]:
    seen, out = set(), []
    for x in items or []:
        x = str(x).strip()
        if x and len(x) <= 60 and x.lower() not in seen:
            seen.add(x.lower())
            out.append(x)
    return out


class JobProfile(BaseModel):
    """Structured job description."""
    title: str | None = None
    required_skills: list[str] = Field(default_factory=list)
    preferred_skills: list[str] = Field(default_factory=list)
    min_experience_years: float | None = None
    soft_skills: list[str] = Field(default_factory=list)
    summary: str | None = None
    weights: dict[str, float] | None = None      # recruiter's own score weights in percent; set from the database only, never by the AI
    cutoffs: dict[str, float] | None = None      # recruiter's own {"shortlist": 70, "consider": 45}; database only, never the AI
    gates: list[str] = Field(default_factory=list)   # required skills the recruiter marked as must-haves ("hard gates"); database only

    @field_validator("required_skills", "preferred_skills", "soft_skills", mode="before")
    @classmethod
    def clean_lists(cls, v):
        return _dedupe(v if isinstance(v, list) else [])


class ScoreJudgment(BaseModel):
    """The LLM's part of the score. Out-of-range numbers are rejected so the caller can retry."""
    fit_score: float = Field(ge=0, le=100)
    projects_education_score: float = Field(ge=0, le=100)
    strengths: list[str] = Field(default_factory=list)
    weaknesses: list[str] = Field(default_factory=list)
    summary: str = ""
    interview_questions: list[str] = Field(default_factory=list)

    @field_validator("strengths", "weaknesses", "interview_questions", mode="before")
    @classmethod
    def none_to_list(cls, v):
        return [str(x) for x in v if x] if isinstance(v, list) else []
