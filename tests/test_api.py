import json

import pytest
from fastapi.testclient import TestClient

from app import deps
from app.models import JobProfile
from app.main import app
from conftest import _judge, make_resume_pdf


@pytest.fixture
def client(pool):
    conn, job_id = pool
    app.dependency_overrides[deps.get_db] = lambda: conn
    deps.JOB_LLMS.update(jd_llm=_judge, canon_llm=_judge)
    deps.CHAT_LLMS.update(plan_llm=lambda s, u: json.dumps({"calls": [{"tool": "top_candidates", "args": {"n": 2}}]}),
                          answer_llm=lambda s, u: "Top two shown.")
    with TestClient(app) as c:
        c.job_id = job_id
        yield c
    app.dependency_overrides.clear()
    for d in (deps.JOB_LLMS, deps.CHAT_LLMS, deps.PIPELINE_LLMS):
        d.clear()


def test_health_and_docs(client):
    assert client.get("/api/health").json()["status"] == "ok"
    assert client.get("/openapi.json").status_code == 200


def test_list_and_detail(client):
    jobs = client.get("/api/jobs").json()
    assert jobs[0]["id"] == client.job_id and jobs[0]["scored"] == 5
    d = client.get(f"/api/jobs/{client.job_id}").json()
    assert d["scored"] == 5 and sum(d["by_recommendation"].values()) == 5 and d["required_skills"]
    assert client.get("/api/jobs/999").status_code == 404


def test_create_job_from_text_and_file(client):
    text = "AI engineer role requiring Python and Docker and SQL. Freshers welcome, apply now please. " * 2
    r = client.post("/api/jobs", data={"text": text})
    assert r.status_code == 200 and r.json()["title"] == "AI Engineer"
    r = client.post("/api/jobs", files={"file": ("jd.txt", text.encode(), "text/plain")})
    assert r.status_code == 200
    assert len(client.get("/api/jobs").json()) == 3


def test_create_job_rejects_bad_input(client):
    assert client.post("/api/jobs", data={"text": ""}).status_code == 422
    assert client.post("/api/jobs", files={"file": ("jd.exe", b"x", "application/octet-stream")}).status_code == 422
    assert client.post("/api/jobs", files={"file": ("bad.pdf", b"not a pdf", "application/pdf")}).status_code == 422
    assert client.post("/api/jobs", data={"text": "too short"}).status_code == 422


def test_candidate_cards_have_everything_the_ui_needs(client):
    cards = client.get(f"/api/jobs/{client.job_id}/candidates").json()
    top = cards[0]
    assert top["rank"] == 1 and top["name"] == "Jeevan Raj" and top["recommendation"] == "Shortlist"
    assert {"skills", "components", "strengths", "weaknesses", "interview_questions", "verification_log"} <= set(top)
    assert {s["colour"] for s in top["skills"]} <= {"green", "yellow", "orange", "red"}
    assert top["required_matched"] == 3 and top["required_total"] == 3
    assert {"AWS", "Machine Learning"} <= {s["skill"] for s in top["all_skills"]}          # every extracted skill, not just the job's
    assert top["education"] == [] and top["phone"]


def test_upload_multiple_resumes_isolates_failures(client):
    profile = {"name": "New Person", "email": "new@x.com", "phone": "9111111111", "skills": ["Python", "SQL"], "experience_years": 0}
    deps.PIPELINE_LLMS.update(extract_llm=lambda s, u: json.dumps(profile), verify_llm=_judge, canon_llm=_judge, score_llm=_judge)
    good = make_resume_pdf("New Person", "new@x.com", "9111111111", ["Python", "SQL"])
    r = client.post(f"/api/jobs/{client.job_id}/resumes",
                    files=[("files", ("good.pdf", good, "application/pdf")), ("files", ("bad.pdf", b"junk", "application/pdf")),
                           ("files", ("notes.txt", b"hello", "text/plain"))])
    statuses = [o["status"] for o in r.json()["outcomes"]]
    assert statuses == ["stored", "rejected_file", "rejected_file"]
    stored = r.json()["outcomes"][0]
    assert stored["name"] == "New Person" and stored["email"] == "new@x.com" and stored["skills_found"] == 2   # extraction shown live
    assert len(client.get(f"/api/jobs/{client.job_id}/candidates").json()) == 6
    assert client.post("/api/jobs/999/resumes", files=[("files", ("a.pdf", good, "application/pdf"))]).status_code == 404


def test_conflict_flow_over_http(client):
    deps.PIPELINE_LLMS.update(verify_llm=_judge, canon_llm=_judge, score_llm=_judge,
                              extract_llm=lambda s, u: json.dumps({"name": "Priya Sharma", "email": "priya@x.com", "phone": "9000000002",
                                                                     "skills": ["Python", "Docker", "SQL"], "experience_years": 4}))
    pdf = make_resume_pdf("Priya Sharma", "priya@x.com", "9000000002", ["Python", "Docker", "SQL"], extra="A different version.")
    out = client.post(f"/api/jobs/{client.job_id}/resumes", files=[("files", ("priya_v2.pdf", pdf, "application/pdf"))]).json()["outcomes"][0]
    assert out["status"] == "conflict_pending"
    items = client.get(f"/api/jobs/{client.job_id}/conflicts?preview=true").json()
    assert items[0]["new_file"] == "priya_v2.pdf" and items[0]["preview"]["required_matched"] == "3/3"
    r = client.post("/api/conflicts/resolve", json={"keep_application_id": items[0]["pending_id"]}).json()
    assert r["status"] == "stored" and client.get(f"/api/jobs/{client.job_id}/conflicts").json() == []
    assert client.post("/api/conflicts/resolve", json={"keep_application_id": 99999}).status_code == 404


def test_chat_roundtrip_and_history(client):
    r = client.post(f"/api/jobs/{client.job_id}/chat", json={"question": "top 2?"}).json()
    assert r["text"] == "Top two shown." and r["calls"][0]["tool"] == "top_candidates"
    assert [m["role"] for m in client.get(f"/api/jobs/{client.job_id}/chat").json()] == ["user", "assistant"]
    assert client.delete(f"/api/jobs/{client.job_id}/chat").json() == {"cleared": True}
    assert client.get(f"/api/jobs/{client.job_id}/chat").json() == []
    assert client.post(f"/api/jobs/{client.job_id}/chat", json={"question": ""}).status_code == 422
    assert client.post("/api/jobs/999/chat", json={"question": "hi"}).status_code == 404


def test_csv_export(client):
    r = client.get(f"/api/jobs/{client.job_id}/export.csv")
    lines = r.text.strip().splitlines()
    assert r.headers["content-type"].startswith("text/csv") and "attachment" in r.headers["content-disposition"]
    assert lines[0].startswith("rank,name,email") and len(lines) == 6 and "Jeevan Raj" in lines[1]


def test_rescore_and_review_endpoints(client):
    assert client.get(f"/api/jobs/{client.job_id}/review").json() == []
    assert client.post(f"/api/jobs/{client.job_id}/rescore").json() == {"results": [], "reindexed_chunks": 0}
    assert client.post("/api/jobs/999/rescore").status_code == 404


def test_db_viewer_is_whitelisted(client):
    assert client.get("/api/db").json()["candidates"] == 5
    t = client.get("/api/db/candidates?limit=2").json()
    assert t["total"] == 5 and len(t["rows"]) == 2 and "email" in t["columns"]
    assert client.get("/api/db/sqlite_master").status_code == 404
    assert client.get("/api/db/candidates;DROP TABLE candidates").status_code == 404
    assert client.get("/api/db/candidates?limit=99999").status_code == 422


def test_build_job_from_answers(client):
    body = {"title": "Java Developer Intern", "summary": "Backend internship.", "responsibilities": "Write Core Java code.",
            "required_skills": ["Java", "SQL", "java"], "preferred_skills": ["Spring Boot"], "education": "Bachelor's Degree",
            "min_experience_years": 0, "soft_skills": ["Teamwork"]}
    r = client.post("/api/jobs/build", json=body)
    assert r.status_code == 200 and r.json()["title"] == "Java Developer Intern"
    d = client.get(f"/api/jobs/{r.json()['id']}").json()
    assert {"Java", "SQL", "Bachelor's Degree"} <= set(d["required_skills"]) and "Spring Boot" in d["preferred_skills"]
    assert "Responsibilities" in d.get("raw_text", "Responsibilities")


def test_build_job_adds_skills_described_in_words_but_not_listed(client):
    body = {"title": "ML Intern", "summary": "Train and evaluate models, then deploy them with Streamlit for the team.",
            "required_skills": ["Python"]}
    d = client.get(f"/api/jobs/{client.post('/api/jobs/build', json=body).json()['id']}").json()
    assert "Python" in d["required_skills"] and len(d["required_skills"]) > 1       # the fake AI adds its skills from the text
    nothing = client.post("/api/jobs/build", json={"title": "ML Intern", "required_skills": ["Python"]}).json()
    assert client.get(f"/api/jobs/{nothing['id']}").json()["required_skills"] == ["Python"]       # no description: only what was typed


def test_build_job_needs_title_and_a_required_skill(client):
    assert client.post("/api/jobs/build", json={"title": " ", "required_skills": ["Java"]}).status_code == 422
    assert client.post("/api/jobs/build", json={"title": "Nurse", "required_skills": []}).status_code == 422
    assert client.post("/api/jobs/build", json={"title": "Nurse", "required_skills": ["x" * 80]}).status_code == 422


def test_extract_preview_endpoint(client):
    r = client.post("/api/extract-preview", files={"file": ("jd.txt", b"Requirements: RN licence", "text/plain")}).json()
    assert r["status"] == "ok" and "RN licence" in r["text"]
    assert client.post("/api/extract-preview", files={"file": ("x.exe", b"zzz", "application/octet-stream")}).json()["status"] == "unsupported"


def test_delete_resume_and_job(client):
    from app import deps as d
    cards = client.get(f"/api/jobs/{client.job_id}/candidates").json()
    app_id = cards[0]["application_id"]
    assert client.delete(f"/api/applications/{app_id}").status_code == 200
    assert len(client.get(f"/api/jobs/{client.job_id}/candidates").json()) == len(cards) - 1
    assert client.delete(f"/api/applications/{app_id}").status_code == 404
    assert client.delete(f"/api/jobs/{client.job_id}").status_code == 200
    assert client.get(f"/api/jobs/{client.job_id}").status_code == 404
    assert client.delete("/api/jobs/999").status_code == 404


def test_job_weights_change_scores_without_ai_and_can_be_reset(client):
    before = {c["application_id"]: c for c in client.get(f"/api/jobs/{client.job_id}/candidates").json()}
    d = client.get(f"/api/jobs/{client.job_id}").json()
    assert d["weights_custom"] is False and round(sum(d["weights"].values())) == 100
    r = client.put(f"/api/jobs/{client.job_id}/weights", json={"skills": 100, "experience": 0, "projects_education": 0, "fit": 0})
    assert r.status_code == 200 and r.json()["custom"] is True and r.json()["rescored"] == 5
    after = client.get(f"/api/jobs/{client.job_id}/candidates").json()
    for c in after:                                         # score is now exactly the skills component
        assert c["score"] == pytest.approx(c["components"]["skills"], abs=0.11)
    assert client.get(f"/api/jobs/{client.job_id}").json()["weights_custom"] is True
    assert client.delete(f"/api/jobs/{client.job_id}/weights").json()["custom"] is False
    back = {c["application_id"]: c["score"] for c in client.get(f"/api/jobs/{client.job_id}/candidates").json()}
    for k, v in before.items():                              # recomputed from components rounded to 1 decimal: at most 0.1 apart
        assert back[k] == pytest.approx(v["score"], abs=0.11)


def test_job_weights_validation(client):
    url = f"/api/jobs/{client.job_id}/weights"
    ok = {"skills": 50, "experience": 20, "projects_education": 15, "fit": 15}
    assert client.put(url, json=ok).status_code == 200
    assert client.put(url, json={**ok, "fit": 20}).status_code == 422          # adds up to 105
    assert client.put(url, json={**ok, "skills": -5, "fit": 35}).status_code == 422
    assert client.put(url, json={"skills": 100}).status_code == 422             # missing parts
    assert client.put("/api/jobs/999/weights", json=ok).status_code == 404


def test_job_cutoffs_relabel_without_ai_and_validate(client):
    url = f"/api/jobs/{client.job_id}/cutoffs"
    cards = client.get(f"/api/jobs/{client.job_id}/candidates").json()
    top = max(c["score"] for c in cards)
    r = client.put(url, json={"shortlist": 99.9, "consider": 99.0})
    assert r.status_code == 200 and r.json()["custom"] is True and r.json()["relabelled"] == 5
    assert {c["recommendation"] for c in client.get(f"/api/jobs/{client.job_id}/candidates").json()} == {"Reject"}
    client.put(url, json={"shortlist": top, "consider": 1})
    labels = {c["score"]: c["recommendation"] for c in client.get(f"/api/jobs/{client.job_id}/candidates").json()}
    assert labels[top] == "Shortlist" and "Reject" not in labels.values()
    d = client.get(f"/api/jobs/{client.job_id}").json()
    assert d["cutoffs"]["shortlist"] == top and d["cutoffs_custom"] is True and d["default_cutoffs"] == {"shortlist": 70.0, "consider": 45.0}
    assert client.delete(url).json()["custom"] is False
    assert client.put(url, json={"shortlist": 40, "consider": 50}).status_code == 422       # consider must be below shortlist
    assert client.put(url, json={"shortlist": 101, "consider": 50}).status_code == 422
    assert client.put(url, json={"shortlist": 70}).status_code == 422
    assert client.put("/api/jobs/999/cutoffs", json={"shortlist": 70, "consider": 45}).status_code == 404


def test_new_scores_use_the_jobs_cutoffs():
    from app.llm.scoring import recommend_for
    job = JobProfile(title="x", required_skills=["a"], cutoffs={"shortlist": 90, "consider": 60})
    assert recommend_for(job, 85) == "Consider" and recommend_for(job, 95) == "Shortlist" and recommend_for(job, 59) == "Reject"
    assert recommend_for(JobProfile(title="x", required_skills=["a"]), 72) == "Shortlist"      # defaults unchanged


def test_must_have_gates_cap_the_recommendation_and_can_be_cleared(client):
    job = client.get(f"/api/jobs/{client.job_id}").json()
    cards = client.get(f"/api/jobs/{client.job_id}/candidates").json()
    shortlisted = [c for c in cards if c["recommendation"] == "Shortlist"]
    assert shortlisted and all(c["gate_missing"] == [] for c in cards)
    # gate every required skill: anyone who lacks one can no longer be Shortlist
    r = client.put(f"/api/jobs/{client.job_id}/gates", json={"skills": job["required_skills"]})
    assert r.status_code == 200 and set(r.json()["gates"]) == set(job["required_skills"])
    after = client.get(f"/api/jobs/{client.job_id}/candidates").json()
    for c in after:
        if c["gate_missing"]:
            assert c["recommendation"] != "Shortlist"
        assert set(c["gate_missing"]) <= set(job["required_skills"])
    capped = [c for c in after if c["gate_missing"] and c["score"] >= 70]
    assert all(c["recommendation"] == "Consider" for c in capped)          # high score, missing a must-have: held at Consider
    assert client.get(f"/api/jobs/{client.job_id}").json()["gates"] == r.json()["gates"]
    client.put(f"/api/jobs/{client.job_id}/gates", json={"skills": []})
    again = {c["application_id"]: c["recommendation"] for c in client.get(f"/api/jobs/{client.job_id}/candidates").json()}
    assert again == {c["application_id"]: c["recommendation"] for c in cards}      # clearing the gates restores the original labels


def test_gates_validate_skill_names_and_survive_weight_changes(client):
    url = f"/api/jobs/{client.job_id}/gates"
    assert client.put(url, json={"skills": ["Underwater Basket Weaving"]}).status_code == 422
    assert client.put("/api/jobs/999/gates", json={"skills": []}).status_code == 404
    job = client.get(f"/api/jobs/{client.job_id}").json()
    client.put(url, json={"skills": [job["required_skills"][0].lower()]})            # case-insensitive
    assert client.get(f"/api/jobs/{client.job_id}").json()["gates"] == [job["required_skills"][0]]
    client.put(f"/api/jobs/{client.job_id}/weights", json={"skills": 100, "experience": 0, "projects_education": 0, "fit": 0})
    for c in client.get(f"/api/jobs/{client.job_id}/candidates").json():
        if c["gate_missing"]:
            assert c["recommendation"] != "Shortlist"


def test_gate_rules_unit():
    from app.llm.scoring import SkillResult, final_label, gate_failures
    job = JobProfile(title="x", required_skills=["Java", "SQL"], gates=["Java"])
    rows = [SkillResult("Java", "required", "missing", "red"), SkillResult("SQL", "required", "solid", "green")]
    assert gate_failures(job.gates, rows) == ["Java"]
    assert final_label(job, 95, rows) == "Consider" and final_label(job, 50, rows) == "Consider" and final_label(job, 20, rows) == "Reject"
    rows[0].status = "partial"
    assert gate_failures(job.gates, rows) == ["Java"]                       # adjacent experience does not meet a must-have
    rows[0].status = "semantic"
    assert gate_failures(job.gates, rows) == [] and final_label(job, 95, rows) == "Shortlist"
    assert gate_failures(job.gates, [{"skill": "java", "status": "missing"}]) == ["java"]      # stored dict form, any case


def test_job_builder_can_name_must_haves(client):
    body = {"title": "Java Intern", "required_skills": ["Java", "SQL"], "gate_skills": ["java", "Docker"]}
    r = client.post("/api/jobs/build", json=body)
    assert r.status_code == 200
    d = client.get(f"/api/jobs/{r.json()['id']}").json()
    assert "Docker" in d["required_skills"]                                   # a must-have is also a required skill
    assert set(d["gates"]) == {"Java", "Docker"}
    plain = client.post("/api/jobs/build", json={"title": "Java Intern", "required_skills": ["Java"]}).json()
    assert client.get(f"/api/jobs/{plain['id']}").json()["gates"] == []
