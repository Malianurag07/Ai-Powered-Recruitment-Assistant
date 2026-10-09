import json

from app.llm.client import LLMError
from app.llm.skill_canonicalizer import canonicalize_profile, canonicalize_skills, save_learned
from app.models import CandidateProfile

AL = {"ml": "Machine Learning", "machine learning": "Machine Learning", "sql": "SQL"}


def answer(mapping):
    return lambda s, u: json.dumps({"mapping": mapping})


def test_known_skills_need_no_llm_call():
    def must_not_call(s, u):
        raise AssertionError("LLM should not be called")
    m, learned = canonicalize_skills(["ML", "Sql"], AL, llm=must_not_call)
    assert m == {"ML": "Machine Learning", "Sql": "SQL"} and learned == {}


def test_unknown_skills_batched_in_one_call_and_learned():
    calls = []

    def llm(s, u):
        calls.append(u)
        return json.dumps({"mapping": {"nodejs": "Node.js", "Postgres": "PostgreSQL"}})
    m, learned = canonicalize_skills(["nodejs", "Postgres", "ML"], AL, llm=llm)
    assert len(calls) == 1 and "nodejs" in calls[0] and "ML" not in calls[0].split("SKILLS:")[1]
    assert m["nodejs"] == "Node.js" and m["ML"] == "Machine Learning"
    assert learned["nodejs"] == "Node.js" and learned["node.js"] == "Node.js"   # canonical resolves to itself


def test_bad_llm_values_fall_back_to_original():
    m, _ = canonicalize_skills(["Foo"], AL, llm=answer({"Foo": "x" * 80}))
    assert m["Foo"] == "Foo"
    m, _ = canonicalize_skills(["Foo"], AL, llm=answer({}))
    assert m["Foo"] == "Foo"


def test_llm_down_keeps_skills_and_learns_nothing():
    def boom(s, u):
        raise LLMError("429")
    m, learned = canonicalize_skills(["Zig"], AL, llm=boom)
    assert m == {"Zig": "Zig"} and learned == {}
    assert canonicalize_skills(["Zig"], AL, llm=lambda s, u: "garbage")[1] == {}


def test_profile_dedupes_after_canonicalizing_and_maps_levels():
    p = CandidateProfile(email="a@b.co", skills=["ML", "Machine Learning", "Docker"], skill_levels={"ML": "basic"})
    new, pairs, _ = canonicalize_profile(p, AL, llm=answer({"Docker": "Docker"}))
    assert new.skills == ["Machine Learning", "Docker"] and pairs[0] == ("Machine Learning", "ML")
    assert new.skill_levels == {"Machine Learning": "basic"}


def test_learned_aliases_persist_and_are_audited(pg):
    conn = pg
    save_learned(conn, {"nodejs": "Node.js"})
    assert conn.execute("SELECT canonical, source FROM skill_aliases WHERE alias='nodejs'").fetchone() == ("Node.js", "ai")
