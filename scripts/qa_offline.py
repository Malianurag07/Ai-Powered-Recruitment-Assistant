"""Offline QA: parsing of messy files, duplicate policy, and API / security / load. No AI quota used (fake models).

Run from the project root:   python scripts/qa_offline.py
Uses a throwaway database. Results: docs/qa_results/offline.json
"""
import os

os.environ.update(AUTH_ENABLED="1", SESSION_SECRET="qa-secret", SEMANTIC_INDEXING="0", ADMIN_EMAIL="", ALLOW_REGISTRATION="1")

from qa_common import ROOT, Recorder, docx_bytes, pdf_single, pdf_two_column, timed, use_throwaway_db  # noqa: E402

TMP = use_throwaway_db("qa_offline_")

import io  # noqa: E402
import json  # noqa: E402
import re  # noqa: E402
import statistics  # noqa: E402
import sys  # noqa: E402
import time  # noqa: E402
from concurrent.futures import ThreadPoolExecutor  # noqa: E402

import pymupdf  # noqa: E402

sys.path.insert(0, str(ROOT / "tests"))
from conftest import JD_TEXT, _judge  # noqa: E402  (fake AI models used by the test suite)

from app import deps  # noqa: E402
from app.database import get_connection, init_db  # noqa: E402
from app.parsing.document_extractor import extract_document  # noqa: E402
from app.services import auth_service, dedupe  # noqa: E402
from app.services import candidate_service as svc  # noqa: E402

rec = Recorder("offline")
SKILLS = "Python, SQL, Docker, Git, Machine Learning"
BODY = f"SKILLS: {SKILLS}\nEDUCATION: B.Tech Computer Science, 2024\n" + "Built several software projects and learned quickly on the job. " * 6

# =========================================================================== S1 parsing and messy files
S1 = "S1 Parsing and messy files"


def parse(data, name):
    r, dt = timed(extract_document, data, name)
    return r, dt


def case_text_has(data, name, needles, want_ok=True):
    def fn():
        r, dt = parse(data, name)
        missing = [n for n in needles if n.lower() not in r.text.lower()]
        return (r.ok == want_ok and not missing), f"status={r.status} chars={len(r.text)} missing={missing} {dt:.2f}s"
    return fn


def case_status(data, name, status):
    def fn():
        r, dt = parse(data, name)
        return r.status == status and bool(r.message), f"status={r.status} message='{r.message}'"
    return fn


def noise_pdf(mb):
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((50, 60), "Big file " * 30, fontsize=10)
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 2500, 2500), False)
    pix.set_rect(pix.irect, (10, 120, 200))
    page.insert_image(pymupdf.Rect(50, 100, 500, 550), stream=os.urandom(mb * 1024 * 1024), width=1, height=1) if False else None
    doc.embfile_add("junk", os.urandom(mb * 1024 * 1024))       # an attachment inflates the file without adding text
    return doc.tobytes()


def scanned_pdf():
    src = pymupdf.open(stream=pdf_single("Scanned Person", "s@x.com", "9999999999", BODY), filetype="pdf")
    pix = src[0].get_pixmap(dpi=100)
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_image(page.rect, pixmap=pix)
    return doc.tobytes()


def encrypted_pdf():
    doc = pymupdf.open(stream=pdf_single("Secret", "s@x.com", "9999999999", BODY), filetype="pdf")
    return doc.tobytes(encryption=pymupdf.PDF_ENCRYPT_AES_256, owner_pw="o", user_pw="u")


def multipage(n):
    doc = pymupdf.open()
    for i in range(n):
        doc.new_page().insert_textbox(pymupdf.Rect(50, 50, 545, 800), f"Page {i}\n" + BODY, fontsize=10)
    return doc.tobytes()


good = pdf_single("Asha Rao", "asha.rao@example.com", "9876500001", BODY)
rec.run(S1, "S1-01", "Plain single-column PDF", "text + email extracted", case_text_has(good, "a.pdf", ["asha.rao@example.com", "Docker"]))
rec.run(S1, "S1-02", "Two-column PDF keeps sidebar and main text", "both columns present",
        case_text_has(pdf_two_column("Ravi K", "ravi@example.com", "9876500002", "SKILLS\nGo\nRust\nKubernetes", "EXPERIENCE\nBackend engineer at Acme for 3 years building payment systems and reliable services. " * 3),
                      "b.pdf", ["ravi@example.com", "Kubernetes", "Backend engineer"]))
rec.run(S1, "S1-03", "PDF with 30 pages", "all pages read, under 3 s",
        lambda: (lambda r, dt: (r.ok and r.pages == 30 and dt < 3, f"pages={r.pages} chars={len(r.text)} {dt:.2f}s"))(*parse(multipage(30), "long.pdf")))
rec.run(S1, "S1-04", "Scanned (image-only) PDF", "clear 'scanned' message", case_status(scanned_pdf(), "scan.pdf", "empty_or_scanned"))
rec.run(S1, "S1-05", "Password-protected PDF", "clear 'password' message", case_status(encrypted_pdf(), "enc.pdf", "encrypted"))
rec.run(S1, "S1-06", "Truncated / corrupt PDF", "clear 'cannot open' message", case_status(good[: len(good) // 3], "cut.pdf", "corrupt"))
rec.run(S1, "S1-07", "Zero-byte file", "rejected with message", case_status(b"", "empty.pdf", "corrupt"))
rec.run(S1, "S1-08", "Plain text renamed to .pdf", "rejected as not a PDF", case_status(b"just some text " * 20, "fake.pdf", "corrupt"))
rec.run(S1, "S1-09", "Unsupported type (.txt)", "rejected: supported formats listed", case_status(b"hello " * 40, "cv.txt", "unsupported"))
rec.run(S1, "S1-10", "File over the 10 MB limit", "rejected as too large (before parsing)", case_status(noise_pdf(11), "big.pdf", "too_large"))
rec.run(S1, "S1-11", "DOCX with skills inside a table", "table cells extracted",
        case_text_has(docx_bytes(["Neha Singh", "neha@example.com | 9876500003", "x " * 60], table=[["Skills", "Python, Airflow"], ["Cloud", "AWS, GCP"]]), "t.docx", ["Airflow", "GCP", "neha@example.com"]))
rec.run(S1, "S1-12", "DOCX in Hindi, Arabic and Chinese", "Unicode survives, no crash",
        case_text_has(docx_bytes(["नाम: अनुराग माली", "الاسم: أحمد", "姓名：王伟", "Email: uni@example.com", "Python, SQL " * 20]), "u.docx", ["अनुराग", "أحمد", "王伟", "uni@example.com"]))
rec.run(S1, "S1-13", "Contact details only in the DOCX header/footer", "email still captured (resumes often put it there)",
        case_text_has(docx_bytes(["Body text about projects and skills " * 10], header="Meera Nair  meera.header@example.com", footer="Phone 9876500004"), "h.docx", ["meera.header@example.com"]))
rec.run(S1, "S1-14", "Email only inside a PDF hyperlink (visible text is 'Email me')", "email captured from the link target",
        lambda: (lambda r: ("hidden.link@example.com" in r.text, f"status={r.status} email_in_text={'hidden.link@example.com' in r.text}"))(
            extract_document((lambda d: (d[0].insert_link({"kind": pymupdf.LINK_URI, "from": pymupdf.Rect(50, 55, 150, 70), "uri": "mailto:hidden.link@example.com"}), d.tobytes())[1])(
                pymupdf.open(stream=pdf_single("Link Person", "Email me", "9876500005", BODY), filetype="pdf")), "l.pdf")))
rec.run(S1, "S1-15", "Filename with path tricks ('../../x.pdf') and double extension", "handled as a label, parsed normally",
        case_text_has(good, "../../etc/evil.pdf.PDF", ["asha.rao@example.com"]))

# =========================================================================== S6 duplicates and multi-job policy
S6 = "S6 Duplicates and multi-job policy"
init_db()
conn = get_connection()
JOB1, _ = svc.create_job(conn, JD_TEXT, canon_llm=_judge, jd_llm=_judge)
JOB2, _ = svc.create_job(conn, "Data analyst role. Requires Python, SQL and Excel. Freshers welcome, apply with a strong portfolio. " * 2, canon_llm=_judge, jd_llm=_judge)


def submit(job, name, email, phone, extra="", fname="r.pdf", skills=("Python", "SQL")):
    profile = {"name": name, "email": email, "phone": phone, "skills": list(skills), "experience_years": 1}
    data = pdf_single(name, email or "", phone or "", BODY + extra)
    return svc.process_resume(conn, data, fname, job, extract_llm=lambda s, u: json.dumps(profile),
                              verify_llm=_judge, canon_llm=_judge, score_llm=_judge)


def active(job, email):
    return conn.execute("""SELECT COUNT(*) FROM applications a JOIN candidates c ON c.id=a.candidate_id
                           WHERE c.email=? AND a.job_description_id=? AND a.application_status='active'""", (email, job)).fetchone()[0]


def n_candidates(email):
    return conn.execute("SELECT COUNT(*) FROM candidates WHERE email=?", (email,)).fetchone()[0]


def c1():
    a = submit(JOB1, "Dup One", "dup1@x.com", "9000000101")
    b = submit(JOB1, "Dup One", "dup1@x.com", "9000000101")
    return (a.status, b.status) == ("stored", "duplicate_ignored") and active(JOB1, "dup1@x.com") == 1, f"{a.status}, {b.status}"


def c2():
    a = submit(JOB1, "Dup Two", "dup2@x.com", "9000000102")
    profile = {"name": "Dup Two", "email": "dup2@x.com", "phone": "9000000102", "skills": ["Python", "SQL"], "experience_years": 1}
    data = pdf_single("DUP TWO", "dup2@x.com", "9000000102", (BODY + "").upper())      # same words, different case
    b = svc.process_resume(conn, data, "r2.pdf", JOB1, extract_llm=lambda s, u: json.dumps(profile), verify_llm=_judge, canon_llm=_judge, score_llm=_judge)
    return b.status == "duplicate_ignored", f"re-exported in UPPER CASE -> {b.status}"


def c3():
    submit(JOB1, "Dup Three", "dup3@x.com", "9000000103")
    b = submit(JOB1, "Dup Three", "dup3@x.com", "9000000103", extra="\nNew project: built a chatbot.")
    return b.status == "conflict_pending" and active(JOB1, "dup3@x.com") == 1, f"{b.status}, active={active(JOB1, 'dup3@x.com')}"


def c4():
    submit(JOB1, "Dup Four", "dup4a@x.com", "9000000104")
    b = submit(JOB1, "Dup Four", "dup4b@x.com", "9000000104", extra="\nDifferent email, same phone.")
    return b.status == "conflict_pending", f"same phone, new email -> {b.status}"


def c5():
    submit(JOB1, "Dup Five", "dup5a@x.com", "+91 98765 43210")
    b = submit(JOB1, "Dup Five", "dup5b@x.com", "098765-43210", extra="\nPhone written differently.")
    return b.status == "conflict_pending", f"+91 98765 43210 vs 098765-43210 -> {b.status}"


def c6():
    submit(JOB1, "Dup Six", "dup6@x.com", "9000000106")
    b = submit(JOB2, "Dup Six", "dup6@x.com", "9000000106")
    return b.status == "stored" and n_candidates("dup6@x.com") == 1 and active(JOB2, "dup6@x.com") == 1, f"other job -> {b.status}, candidates={n_candidates('dup6@x.com')}"


def c7():
    submit(JOB1, "Dup Seven", "dup7@x.com", "9000000107")
    b = submit(JOB1, "Dup Seven", "DUP7@X.COM", "9000000199", extra="\nEmail in capitals.")
    return b.status == "conflict_pending" and n_candidates("dup7@x.com") == 1, f"CAPS email -> {b.status}"


def c8():
    a = submit(JOB1, "Dup Eight", "dup8@x.com", "9000000108")
    b = submit(JOB1, "Dup Eight", "dup8@x.com", "9000000108", extra="\nNewer version.")
    svc.resolve_conflict(conn, b.application_id, score_llm=_judge)
    st = {r[0]: r[1] for r in conn.execute("SELECT id, application_status FROM applications WHERE id IN (?,?)", (a.application_id, b.application_id))}
    return st[b.application_id] == "active" and st[a.application_id] == "superseded", str(st)


def c9():
    a = submit(JOB1, "Dup Nine", "dup9@x.com", "9000000109")
    b = submit(JOB1, "Dup Nine", "dup9@x.com", "9000000109", extra="\nNewer version.")
    svc.resolve_conflict(conn, a.application_id, score_llm=_judge)
    st = {r[0]: r[1] for r in conn.execute("SELECT id, application_status FROM applications WHERE id IN (?,?)", (a.application_id, b.application_id))}
    return st[a.application_id] == "active" and st[b.application_id] == "superseded", str(st)


def c10():
    submit(JOB1, "Dup Ten", "dup10@x.com", "9000000110")
    submit(JOB1, "Dup Ten", "dup10@x.com", "9000000110", extra="\nVersion two.")
    c = submit(JOB1, "Dup Ten", "dup10@x.com", "9000000110", extra="\nVersion three.")
    return active(JOB1, "dup10@x.com") == 1, f"third version -> {c.status}, active={active(JOB1, 'dup10@x.com')}"


def c11():
    o = submit(JOB1, "No Contact", None, None)
    return o.status in ("needs_review", "rejected_file") or (o.status == "stored" and False), f"no email/phone -> {o.status}: {o.message[:80]}"


def c12():
    a = submit(JOB1, "Same Name", "same.a@x.com", "9000000112")
    b = submit(JOB1, "Same Name", "same.b@x.com", "9000000113", extra="\nA different person with the same name.")
    return (a.candidate_id != b.candidate_id) and b.status == "stored", f"two people, same name -> candidates {a.candidate_id},{b.candidate_id}"


def c13():
    submit(JOB1, "Phone Only", "phone.only@x.com", "9000000114")
    b = submit(JOB1, "Phone Only", None, "9000000114", extra="\nResume without an email.")
    return b.status == "conflict_pending", f"phone-only resume matched existing person -> {b.status}"


for cid, title, exp, fn in [
    ("S6-01", "Identical resume uploaded twice, same job", "second ignored, one active", c1),
    ("S6-02", "Same resume re-exported in different letter case", "treated as identical", c2),
    ("S6-03", "Different resume, same email, same job", "held as pending; still one active", c3),
    ("S6-04", "Same phone, different email, different text", "same person: pending", c4),
    ("S6-05", "Phone written '+91 98765 43210' vs '098765-43210'", "same person", c5),
    ("S6-06", "Same person applies to a second job", "allowed; one candidate, active in both", c6),
    ("S6-07", "Email in CAPITALS vs lowercase", "same person", c7),
    ("S6-08", "Resolve conflict: keep the new resume", "new active, old superseded", c8),
    ("S6-09", "Resolve conflict: keep the old resume", "old active, new superseded", c9),
    ("S6-10", "Third version while one is already pending", "never more than one active", c10),
    ("S6-11", "Resume with no email and no phone", "flagged for review, not silently stored", c11),
    ("S6-12", "Two different people with the same name", "kept as two candidates", c12),
    ("S6-13", "Resume without email matched by phone", "same person", c13),
]:
    rec.run(S6, cid, title, exp, fn)

# =========================================================================== S8 API, security, load
S8 = "S8 API, security and load"
from fastapi.testclient import TestClient  # noqa: E402

from app import config  # noqa: E402
from app.main import app  # noqa: E402

config.AUTH_ENABLED = True          # importing the test helpers switched login off; this suite tests it, so switch it back on
config.ALLOW_REGISTRATION = True
config.ADMIN_EMAIL, config.ADMIN_PASSWORD = "admin@qa.com", "admin-password-1"


def fake_extract(system, user):
    email = (re.search(r"[\w.+-]+@[\w-]+\.[\w.-]+", user) or [None])[0] if re.search(r"[\w.+-]+@[\w-]+\.[\w.-]+", user) else None
    phone = (re.search(r"\b\d{10}\b", user) or [None])[0] if re.search(r"\b\d{10}\b", user) else None
    name = user.strip().splitlines()[0][:60] if user.strip() else "Unknown"
    return json.dumps({"name": name, "email": email, "phone": phone, "skills": ["Python", "SQL"], "experience_years": 1})


deps.PIPELINE_LLMS.update(extract_llm=fake_extract, verify_llm=_judge, canon_llm=_judge, score_llm=_judge)
deps.JOB_LLMS.update(jd_llm=_judge, canon_llm=_judge)
deps.CHAT_LLMS.update(plan_llm=lambda s, u: json.dumps({"calls": [{"tool": "top_candidates", "args": {"n": 2}}]}), answer_llm=lambda s, u: "ok")

client_cm = TestClient(app)
client_cm.__enter__()          # runs startup: creates tables and the admin from ADMIN_EMAIL/ADMIN_PASSWORD


def new_client():
    return TestClient(app)


def login(c, email, pw):
    return c.post("/api/auth/login", json={"email": email, "password": pw})


admin = new_client()
assert login(admin, "admin@qa.com", "admin-password-1").status_code == 200
API_JOB = admin.post("/api/jobs", data={"text": JD_TEXT}).json()["id"]
reg = new_client()
r = reg.post("/api/auth/register", json={"email": "rec@qa.com", "password": "recruiter-pass-1", "name": "Rec"})
assert r.status_code == 201, r.text


def s8_01():
    public = {"/api/health", "/api/auth/status", "/api/auth/login", "/api/auth/register", "/api/auth/logout"}
    anon, bad = new_client(), []
    for route in app.routes:
        path = getattr(route, "path", "")
        if not path.startswith("/api") or path in public:
            continue
        url = path.replace("{job_id}", str(API_JOB)).replace("{user_id}", "1").replace("{table}", "candidates")
        for m in getattr(route, "methods", set()) - {"HEAD", "OPTIONS"}:
            code = anon.request(m, url).status_code
            if code != 401:
                bad.append(f"{m} {path} -> {code}")
    return not bad, f"unprotected: {bad}" if bad else f"every /api route except the {len(public)} public ones answered 401"


def s8_02():
    c = new_client()
    c.cookies.set("session", "1.AAAA")
    forged = c.get("/api/jobs").status_code
    tok = auth_service.make_token(1, hours=-1)
    c2 = new_client()
    c2.cookies.set("session", tok)
    return forged == 401 and c2.get("/api/jobs").status_code == 401, f"forged={forged}, expired={c2.get('/api/jobs').status_code}"


def s8_03():
    rc = new_client()
    login(rc, "rec@qa.com", "recruiter-pass-1")
    codes = (rc.get("/api/users").status_code, rc.patch("/api/users/2", json={"role": "admin"}).status_code,
             rc.post("/api/users", json={"email": "x@x.com", "password": "password-123"}).status_code, rc.delete("/api/users/1").status_code)
    return codes == (403, 403, 403, 403), f"recruiter on admin endpoints -> {codes}"


def s8_04():
    c = new_client()
    codes = [login(c, "rec@qa.com", "wrong-pass-xx").status_code for _ in range(5)]
    sixth = login(c, "rec@qa.com", "recruiter-pass-1").status_code
    auth_service._fails.clear()
    return codes == [401] * 5 and sixth == 429, f"5 bad logins {set(codes)}, then correct password -> {sixth}"


def s8_05():
    code = admin.get("/api/db/candidates;DROP TABLE candidates").status_code
    code2 = admin.get("/api/db/users").status_code
    still = admin.get("/api/db/candidates").status_code
    return code == 404 and code2 == 404 and still == 200, f"injected table name -> {code}, users table -> {code2}, candidates intact -> {still}"


def s8_06():
    a = admin.get("/api/jobs/1%20OR%201=1").status_code
    b = admin.get("/api/jobs/999999").status_code
    c = admin.get("/api/jobs/abc").status_code
    return (a, b, c) == (422, 404, 422), f"'1 OR 1=1' -> {a}, unknown id -> {b}, 'abc' -> {c}"


def s8_07():
    a = admin.post(f"/api/jobs/{API_JOB}/chat", json={"question": "x" * 1001}).status_code
    b = admin.post(f"/api/jobs/{API_JOB}/chat", content=b"{not json", headers={"Content-Type": "application/json"}).status_code
    c = admin.post(f"/api/jobs/{API_JOB}/chat", json={"question": ""}).status_code
    d = admin.post(f"/api/jobs/{API_JOB}/chat", json={}).status_code
    return (a, b, c, d) == (422, 422, 422, 422), f"too long {a}, malformed JSON {b}, empty {c}, missing field {d}"


def s8_08():
    files = [("files", (f"c{i}.pdf", pdf_single(f"Load Person {i}", f"load{i}@qa.com", f"91000000{i:02d}", BODY + f" unique-{i}"), "application/pdf")) for i in range(12)]

    def one(f):
        c = new_client()
        c.cookies.update(admin.cookies)
        return c.post(f"/api/jobs/{API_JOB}/resumes", files=[f]).json()

    t = time.perf_counter()
    with ThreadPoolExecutor(6) as ex:
        res = list(ex.map(one, files))
    dt = time.perf_counter() - t
    statuses = [o["status"] for r in res for o in r.get("outcomes", [])]
    actives = admin.get(f"/api/jobs/{API_JOB}/candidates").json()
    n = len(actives) if isinstance(actives, list) else len(actives.get("candidates", []))
    return statuses.count("stored") == 12 and n >= 12, f"12 uploads on 6 threads in {dt:.1f}s: {dict((s, statuses.count(s)) for s in set(statuses))}, ranked candidates={n}"


def s8_09():
    lat, codes = [], []

    def hit(_):
        c = new_client()
        c.cookies.update(admin.cookies)
        t = time.perf_counter()
        code = c.get("/api/jobs").status_code
        return code, time.perf_counter() - t

    with ThreadPoolExecutor(20) as ex:
        for code, dt in ex.map(hit, range(200)):
            codes.append(code)
            lat.append(dt)
    lat.sort()
    p50, p95 = statistics.median(lat), lat[int(len(lat) * .95)]
    return codes.count(200) == 200 and p95 < 2.0, f"200 requests, 20 at a time: {codes.count(200)} OK, median {p50*1000:.0f} ms, p95 {p95*1000:.0f} ms"


def s8_10():
    evil = '=HYPERLINK("http://evil.example","click")'
    data = pdf_single(evil, "csv.evil@qa.com", "9100000099", BODY + " csv")
    admin.post(f"/api/jobs/{API_JOB}/resumes", files=[("files", ("evil.pdf", data, "application/pdf"))])
    csv = admin.get(f"/api/jobs/{API_JOB}/export.csv").text
    raw_cell = re.search(r'(^|,)"?=HYPERLINK', csv, re.M)
    return raw_cell is None, "formula-like name is not written as a live formula" if raw_cell is None else "RAW FORMULA in CSV"


def s8_11():
    x = admin.get("/api/jobs")
    return x.headers.get("content-type", "").startswith("application/json"), f"content-type={x.headers.get('content-type')}"


def s8_12():
    before = {p.name for p in ROOT.rglob("evil_traversal*")}
    o = admin.post(f"/api/jobs/{API_JOB}/resumes", files=[("files", ("../../evil_traversal.pdf", pdf_single("Trav", "trav@qa.com", "9100000098", BODY), "application/pdf"))])
    after = {p.name for p in ROOT.rglob("evil_traversal*")}
    return o.status_code == 200 and before == after, f"status {o.status_code}; files written outside the DB: {after - before or 'none'}"


def s8_13():
    h = admin.get("/api/jobs").headers
    want = {"x-content-type-options": "nosniff", "x-frame-options": None, "content-security-policy": None}
    missing = [k for k in want if k not in {x.lower() for x in h}]
    return not missing, f"security headers missing: {missing}" if missing else "present"


def s8_14():
    c = new_client()
    tok = login(c, "rec@qa.com", "recruiter-pass-1").cookies.get("session")
    c.post("/api/auth/logout")
    replay = new_client()
    replay.cookies.set("session", tok)
    code = replay.get("/api/jobs").status_code
    return code == 401, f"old cookie replayed after logout -> {code} (stateless tokens stay valid until they expire)"


def s8_15():
    c = new_client()
    r1 = c.post("/api/auth/register", json={"email": "role@qa.com", "password": "password-123", "role": "admin"})
    role = r1.json()["user"]["role"] if r1.status_code == 201 else r1.status_code
    r2 = c.get("/api/users").status_code
    return role == "recruiter" and r2 == 403, f"self-registered with role=admin -> got {role}; can list users? {r2}"


for cid, title, exp, fn in [
    ("S8-01", "Every /api route (except 5 public) demands login", "all 401 when anonymous", s8_01),
    ("S8-02", "Forged and expired session cookies", "both rejected", s8_02),
    ("S8-03", "Recruiter tries admin endpoints", "all 403", s8_03),
    ("S8-04", "Brute-force login", "locked after 5 failures, even for the right password", s8_04),
    ("S8-05", "SQL-injection style table name; users table in viewer", "404, data intact", s8_05),
    ("S8-06", "Injection / garbage in job id", "422 or 404, never 500", s8_06),
    ("S8-07", "Chat input: too long, malformed JSON, empty, missing", "all 422", s8_07),
    ("S8-08", "12 resumes uploaded concurrently (6 threads)", "all stored, consistent DB", s8_08),
    ("S8-09", "200 requests, 20 concurrent", "no errors, p95 under 2 s", s8_09),
    ("S8-10", "Candidate name that is a spreadsheet formula", "neutralised in the CSV", s8_10),
    ("S8-11", "API responses are JSON, not HTML", "application/json", s8_11),
    ("S8-12", "Upload filename '../../evil_traversal.pdf'", "no file written anywhere", s8_12),
    ("S8-13", "Security response headers", "nosniff, frame and CSP headers present", s8_13),
    ("S8-14", "Session cookie replayed after logout", "rejected", s8_14),
    ("S8-15", "Self-registration asking for role=admin", "ignored: recruiter, no admin access", s8_15),
]:
    rec.run(S8, cid, title, exp, fn)

client_cm.__exit__(None, None, None)
rec.save()
