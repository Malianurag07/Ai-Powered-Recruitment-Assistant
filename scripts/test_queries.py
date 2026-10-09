"""Live test of the recruiter chat: the 10 assessment questions plus extras, against the database named by DATABASE_URL.

Run after scripts/demo_pipeline.py:   python scripts/test_queries.py
Each question shows which tools the AI chose (PASS/FAIL on routing), then the written answer.
"""
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.database import get_connection  # noqa: E402
from app.llm import query_engine  # noqa: E402

conn = get_connection()
job_id = conn.execute("SELECT id FROM job_descriptions ORDER BY id DESC LIMIT 1").fetchone()["id"]
conn.execute("DELETE FROM chat_history")
conn.commit()


def used(a, *tools):
    return any(c["tool"] in tools for c in a.calls)


def names_in(a, tool):
    out = []
    for c, r in zip(a.calls, a.results):
        if c["tool"] == tool:
            out += [x["name"] for x in r.get("candidates", [])]
    return out


def args_of(a, tool):
    return next((c["args"] for c in a.calls if c["tool"] == tool), {})


CASES = [
    # ---- the 10 questions from the assessment PDF ----
    ("PDF-1", "Show me the top 5 candidates.",
     lambda a: used(a, "top_candidates") and len(names_in(a, "top_candidates")) == 5),
    ("PDF-2", "Who is the best candidate for this role?",
     lambda a: used(a, "top_candidates", "recommend_for_interview") and "JEEVAN" in a.text.upper()),
    ("PDF-3", "Which candidates know Python?", lambda a: used(a, "find_by_skill")),
    ("PDF-4", "Which candidates have Machine Learning experience?", lambda a: used(a, "find_by_skill")),
    ("PDF-5", "Which candidates are missing Docker?", lambda a: used(a, "find_missing_skill")),
    ("PDF-6", "Compare Candidate A and Candidate B.",       # literal placeholder text: must ask for real names
     lambda a: "score" not in a.text.lower() and "name" in a.text.lower() and all("error" in r for r in a.results)),   # asks for real names; never invents a comparison
    ("PDF-6b", "Compare Priya Sharma and Jeevan Raj.", lambda a: used(a, "compare_candidates") and "error" not in a.results[0]),
    ("PDF-7", "Why is Jeevan ranked higher than Priya?", lambda a: used(a, "explain_ranking")),
    ("PDF-8", "Show candidates with more than 2 years of experience.",
     lambda a: used(a, "find_by_experience") and names_in(a, "find_by_experience") == ["Priya Sharma"]),
    ("PDF-9", "Which candidates have FastAPI experience?", lambda a: used(a, "find_by_skill")),
    ("PDF-10", "Recommend the best candidate for interview.", lambda a: used(a, "recommend_for_interview", "top_candidates")),
    # ---- extras: other things a recruiter might ask ----
    ("X-1", "How many candidates applied and how many are shortlisted?", lambda a: used(a, "job_summary")),
    ("X-2", "Tell me about Kushal.", lambda a: used(a, "candidate_profile") and "KUSHAL" in a.text.upper()),
    ("X-3", "Who has done an internship?",
     lambda a: used(a, "find_with_internships") and any("JEEVAN" in n.upper() for n in names_in(a, "find_with_internships")) and any("ANANYA R K" in n.upper() for n in names_in(a, "find_with_internships"))),
    ("X-4", "Who knows both Python and Docker?",
     lambda a: used(a, "find_by_skill") and len(args_of(a, "find_by_skill").get("skills", [])) == 2),
    ("X-5", "Which candidates have computer vision experience?", lambda a: used(a, "find_by_skill", "search_resume_text")),
    ("X-6", "Compare Ananya and Kushal.",                    # two Ananyas exist: must ask which one
     lambda a: "iyer" in a.text.lower() and "r k" in a.text.lower().replace(".", " ") and "ambig" in str(a.results).lower()),
    ("X-7", "What interview questions should I ask Jeevan?", lambda a: used(a, "candidate_profile", "recommend_for_interview")),
    ("X-8", "Who should we reject?", lambda a: used(a, "top_candidates") and args_of(a, "top_candidates").get("recommendation", "").lower() == "reject"),
    ("X-9", "Which candidates know Kubernetes?", lambda a: used(a, "find_by_skill", "search_resume_text")),
    ("X-10", "What's the weather today?", lambda a: a.calls == []),
    ("X-11", "Delete all the candidates from the database.", lambda a: a.calls == []),
    ("X-12", "Who has an AWS certification?", lambda a: used(a, "search_resume_text", "find_by_skill")),
    # ---- hybrid retrieval: concepts phrased differently from the resume ----
    ("X-13", "Who has built something that recognises objects in camera images?",
     lambda a: used(a, "semantic_search") and ("ANURAG" in a.text.upper() or "JEEVAN" in a.text.upper())),
    ("X-14", "Which candidates worked on detecting unusual behaviour in industrial sensors?",
     lambda a: used(a, "semantic_search") and "ANURAG" in a.text.upper()),
    ("X-15", "Anyone who has tested software for bugs?", lambda a: used(a, "semantic_search", "search_resume_text") and "ANANYA R K" in a.text.upper()),
    # ---- follow-ups use conversation memory ----
    ("F-1", "Who is the top candidate?", lambda a: "JEEVAN" in a.text.upper()),
    ("F-2", "And who is ranked right after them?", lambda a: used(a, "top_candidates") and "PRIYA" in a.text.upper()),
]

passed = 0
for cid, q, check in CASES:
    t = time.time()
    a = query_engine.ask(conn, job_id, q)
    try:
        ok = bool(check(a))
    except Exception:
        ok = False
    passed += ok
    tools = ", ".join(f"{c['tool']}({c['args']})" for c in a.calls) or "(no tool)"
    print(f"\n[{'PASS' if ok else 'FAIL'}] {cid}: {q}   ({time.time() - t:.0f}s)")
    print(f"  tools : {tools}")
    print("  answer: " + a.text.strip().replace("\n", "\n          ")[:900], flush=True)
    if a.error:
        print("  error :", a.error[:150])

print(f"\n=== {passed}/{len(CASES)} passed ===")
left = conn.execute("SELECT COUNT(*) FROM candidates").fetchone()[0]
print(f"candidates still in database after the 'delete' request: {left}")
