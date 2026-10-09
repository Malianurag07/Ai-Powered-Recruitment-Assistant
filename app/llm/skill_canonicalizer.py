"""AI-assisted skill normalization with a learning alias table.

Known skills are resolved from the alias table for free. Unknown ones are sent to the LLM in ONE batched
call; its answers are validated and saved back to the table (source='ai'), so each skill costs at most one lookup ever.
"""
import json
import re
from typing import Callable

from app.llm import client, prompts
from app.models import CandidateProfile
from app.db import PgConnection

LLMFn = Callable[[str, str], str]
MAX_KNOWN_SHOWN = 120


def _default_llm(system: str, user: str) -> str:
    return client.complete_with_fallback(system, user, [("groq", None), ("gemini", None)])


def _valid_name(name) -> bool:
    return isinstance(name, str) and 0 < len(name.strip()) <= 40 and not re.search(r"[,\n]", name)


def known_names_for(unknown: list[str], aliases: dict[str, str], limit: int = MAX_KNOWN_SHOWN) -> list[str]:
    """The canonical names to show the AI as "already used", at most `limit`. With a large table, cutting the alphabetical list at `limit`
    would hide most of it, so names that share a word with the skills being resolved come first ("Computer Vision" for "cv models"),
    then the rest alphabetically. This keeps the AI reusing existing names instead of inventing near-duplicates."""
    names = sorted(set(aliases.values()))
    if len(names) <= limit:
        return names
    words = {w for s in unknown for w in re.findall(r"[a-z0-9+#]+", s.lower()) if len(w) > 1}
    overlap = lambda n: sum(w in words for w in re.findall(r"[a-z0-9+#]+", n.lower()))     # noqa: E731
    return sorted(names, key=lambda n: (-overlap(n), n))[:limit]


def canonicalize_skills(skills: list[str], aliases: dict[str, str], llm: LLMFn = _default_llm):
    """Return (mapping {skill: canonical}, learned {alias_lower: canonical}). Never raises on LLM trouble."""
    mapping = {s: aliases.get(s.strip().lower(), s.strip()) for s in skills}
    unknown = sorted({s.strip() for s in skills if s.strip().lower() not in aliases})
    if not unknown:
        return mapping, {}

    known = known_names_for(unknown, aliases)
    user = prompts.CANON_USER.format(known=json.dumps(known), skills=json.dumps(unknown))
    try:
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", llm(prompts.CANON_SYSTEM, user).strip())
        answer = json.loads(raw).get("mapping", {})
        if not isinstance(answer, dict):
            return mapping, {}
    except (client.LLMError, json.JSONDecodeError, AttributeError):
        return mapping, {}          # AI unavailable or garbled: keep skills as written and learn nothing

    learned: dict[str, str] = {}
    for skill in unknown:
        canon = answer.get(skill)
        canon = canon.strip() if _valid_name(canon) else skill
        learned[skill.lower()] = canon
        learned.setdefault(canon.lower(), canon)      # the canonical name must resolve to itself next time
    for s in skills:
        mapping[s] = learned.get(s.strip().lower(), mapping[s])
    return mapping, learned


def canonicalize_profile(profile: CandidateProfile, aliases: dict[str, str], llm: LLMFn = _default_llm):
    """Return (profile with canonical skills, [(canonical, raw)] pairs, learned aliases)."""
    mapping, learned = canonicalize_skills(profile.skills, aliases, llm)
    pairs, seen = [], set()
    for raw in profile.skills:
        canon = mapping[raw]
        if canon.lower() not in seen:
            seen.add(canon.lower())
            pairs.append((canon, raw))
    new = profile.model_copy(deep=True)
    new.skills = [c for c, _ in pairs]
    new.skill_levels = {mapping.get(k, k): v for k, v in profile.skill_levels.items()}
    return new, pairs, learned


def save_learned(conn: PgConnection, learned: dict[str, str]) -> None:
    conn.executemany("INSERT INTO skill_aliases (alias, canonical, source) VALUES (?, ?, 'ai') ON CONFLICT (alias) DO NOTHING", learned.items())
