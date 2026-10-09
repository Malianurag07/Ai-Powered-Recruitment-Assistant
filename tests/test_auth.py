import time

import pytest
from fastapi.testclient import TestClient

from app import config, deps
from app.main import app
from app.services import auth_service as auth

PW = "correct horse"


@pytest.fixture
def conn(pg):
    auth._fails.clear()
    return pg


# ---------- service ----------
def test_password_hash_is_salted_and_verifies():
    a, b = auth.hash_password(PW), auth.hash_password(PW)
    assert a != b and PW not in a
    assert auth.verify_password(PW, a) and not auth.verify_password("wrong password", a)
    assert not auth.verify_password(PW, "garbage")


def test_token_roundtrip_tamper_and_expiry(monkeypatch):
    monkeypatch.setattr(config, "SESSION_SECRET", "s1")
    t = auth.make_token(7, epoch=3)
    assert auth.read_token(t) == (7, 3)
    body, sig = t.split(".")
    assert auth.read_token(body + "." + sig[:-2] + "xx") is None
    assert auth.read_token("nope") is None and auth.read_token(None) is None
    assert auth.read_token(auth.make_token(7, hours=-1)) is None
    monkeypatch.setattr(config, "SESSION_SECRET", "other")          # a different secret cannot read it
    assert auth.read_token(t) is None


def test_create_user_rules(conn):
    u = auth.create_user(conn, "  Ann@X.com ", PW, "Ann", "recruiter")
    assert u["email"] == "ann@x.com" and "password_hash" not in u
    with pytest.raises(auth.AuthError, match="already exists"):
        auth.create_user(conn, "ANN@x.com", PW)
    with pytest.raises(auth.AuthError, match="at least 8"):
        auth.create_user(conn, "b@x.com", "short")
    with pytest.raises(auth.AuthError, match="valid email"):
        auth.create_user(conn, "not-an-email", PW)
    with pytest.raises(auth.AuthError, match="Role"):
        auth.create_user(conn, "c@x.com", PW, role="boss")


def test_authenticate_generic_error_and_inactive(conn):
    auth.create_user(conn, "a@x.com", PW)
    assert auth.authenticate(conn, "A@x.com", PW)["email"] == "a@x.com"
    for email, pw in (("a@x.com", "bad password"), ("ghost@x.com", PW)):
        with pytest.raises(auth.AuthError) as e:
            auth.authenticate(conn, email, pw)
        assert str(e.value) == "Invalid email or password."         # same message: no hint which part was wrong
    auth.update_user(conn, 1, is_active=False)
    with pytest.raises(auth.AuthError, match="Invalid"):
        auth.authenticate(conn, "a@x.com", PW)


def test_lockout_after_repeated_failures_then_expires(conn, monkeypatch):
    auth.create_user(conn, "a@x.com", PW)
    for _ in range(auth.MAX_FAILS):
        with pytest.raises(auth.AuthError, match="Invalid"):
            auth.authenticate(conn, "a@x.com", "bad password")
    with pytest.raises(auth.AuthError, match="Too many"):
        auth.authenticate(conn, "a@x.com", PW)                     # even the right password is refused while locked
    real = time.time
    monkeypatch.setattr(time, "time", lambda: real() + auth.LOCK_SECONDS + 1)
    assert auth.authenticate(conn, "a@x.com", PW)


def test_last_admin_is_protected(conn):
    admin = auth.create_user(conn, "a@x.com", PW, role="admin")
    for bad in (dict(role="recruiter"), dict(is_active=False)):
        with pytest.raises(auth.AuthError, match="at least one active admin"):
            auth.update_user(conn, admin["id"], **bad)
    with pytest.raises(auth.AuthError, match="at least one active admin"):
        auth.delete_user(conn, admin["id"])
    second = auth.create_user(conn, "b@x.com", PW, role="admin")
    auth.update_user(conn, admin["id"], role="recruiter")           # fine now: another admin exists
    assert auth.get_user(conn, admin["id"])["role"] == "recruiter"
    with pytest.raises(auth.AuthError, match="at least one active admin"):
        auth.delete_user(conn, second["id"])                        # second is now the only admin


def test_bootstrap_admin(conn, monkeypatch):
    monkeypatch.setattr(config, "AUTH_ENABLED", False)
    auth.bootstrap_admin(conn)
    assert auth.list_users(conn) == []
    monkeypatch.setattr(config, "AUTH_ENABLED", True)
    monkeypatch.setattr(config, "ADMIN_EMAIL", "boss@x.com")
    monkeypatch.setattr(config, "ADMIN_PASSWORD", PW)
    auth.bootstrap_admin(conn)
    auth.bootstrap_admin(conn)                                      # idempotent
    users = auth.list_users(conn)
    assert len(users) == 1 and users[0]["role"] == "admin"


# ---------- API ----------
@pytest.fixture
def api(conn, monkeypatch):
    monkeypatch.setattr(config, "AUTH_ENABLED", True)
    monkeypatch.setattr(config, "SESSION_SECRET", "test-secret")
    monkeypatch.setattr(config, "ADMIN_EMAIL", "")                  # startup must never create an admin in the real database
    auth.create_user(conn, "admin@x.com", PW, "Admin", "admin")
    auth.create_user(conn, "rec@x.com", PW, "Rec", "recruiter")
    app.dependency_overrides[deps.get_db] = lambda: conn
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def login(client, email, pw=PW):
    return client.post("/api/auth/login", json={"email": email, "password": pw})


def test_protected_routes_need_login(api):
    for path in ("/api/jobs", "/api/db", "/api/auth/me", "/api/users"):
        assert api.get(path).status_code == 401
    assert api.get("/api/health").status_code == 200
    assert api.get("/api/auth/status").json() == {"auth_enabled": True, "registration_open": True, "user": None}


def test_login_logout_flow_and_cookie_flags(api):
    r = login(api, "rec@x.com")
    assert r.status_code == 200 and r.json()["user"]["role"] == "recruiter" and "password_hash" not in r.text
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=lax" in cookie
    assert api.get("/api/auth/me").json()["user"]["email"] == "rec@x.com"
    assert api.get("/api/jobs").status_code == 200
    api.post("/api/auth/logout")
    assert api.get("/api/jobs").status_code == 401


def test_bad_login_and_forged_cookie(api):
    assert login(api, "rec@x.com", "wrong password").status_code == 401
    api.cookies.set("session", "1.forged")
    assert api.get("/api/jobs").status_code == 401


def test_recruiter_cannot_manage_users_admin_can(api):
    login(api, "rec@x.com")
    assert api.get("/api/users").status_code == 403
    assert api.post("/api/users", json={"email": "n@x.com", "password": PW}).status_code == 403
    api.post("/api/auth/logout")
    login(api, "admin@x.com")
    listed = api.get("/api/users").json()
    assert {u["email"] for u in listed} == {"admin@x.com", "rec@x.com"} and all("password_hash" not in u for u in listed)
    made = api.post("/api/users", json={"email": "n@x.com", "password": PW, "role": "recruiter"})
    assert made.status_code == 201
    assert api.post("/api/users", json={"email": "n@x.com", "password": PW}).status_code == 422
    assert api.patch(f"/api/users/{made.json()['id']}", json={"role": "admin"}).json()["role"] == "admin"


def test_admin_cannot_lock_self_out(api):
    me = login(api, "admin@x.com").json()["user"]
    assert api.patch(f"/api/users/{me['id']}", json={"is_active": False}).status_code == 422
    assert api.patch(f"/api/users/{me['id']}", json={"role": "recruiter"}).status_code == 422
    assert api.delete(f"/api/users/{me['id']}").status_code == 422
    assert api.patch("/api/users/999", json={"role": "admin"}).status_code == 404


def test_deactivating_a_user_ends_their_session_immediately(api):
    rec = TestClient(app)
    login(rec, "rec@x.com")
    assert rec.get("/api/jobs").status_code == 200
    login(api, "admin@x.com")
    rec_id = next(u["id"] for u in api.get("/api/users").json() if u["email"] == "rec@x.com")
    api.patch(f"/api/users/{rec_id}", json={"is_active": False})
    assert rec.get("/api/jobs").status_code == 401


def test_change_own_password(api):
    login(api, "rec@x.com")
    assert api.post("/api/auth/password", json={"current_password": "nope nope", "new_password": "brand new pw"}).status_code == 401
    assert api.post("/api/auth/password", json={"current_password": PW, "new_password": "short"}).status_code == 422
    assert api.post("/api/auth/password", json={"current_password": PW, "new_password": "brand new pw"}).status_code == 200
    api.post("/api/auth/logout")
    assert login(api, "rec@x.com").status_code == 401
    assert login(api, "rec@x.com", "brand new pw").status_code == 200


def test_users_table_is_not_in_database_viewer(api):
    login(api, "admin@x.com")
    assert "users" not in api.get("/api/db").json()
    assert api.get("/api/db/users").status_code == 404


def test_auth_off_means_no_login_needed(conn, monkeypatch):
    monkeypatch.setattr(config, "AUTH_ENABLED", False)
    app.dependency_overrides[deps.get_db] = lambda: conn
    with TestClient(app) as c:
        assert c.get("/api/jobs").status_code == 200
        assert c.get("/api/auth/me").json()["user"]["role"] == "admin"
        assert c.get("/api/auth/status").json() == {"auth_enabled": False, "registration_open": False, "user": None}
    app.dependency_overrides.clear()


# ---------- self registration ----------
def test_register_creates_recruiter_and_signs_in(api):
    r = api.post("/api/auth/register", json={"name": "New Person", "email": "New@X.com", "password": "brand new pw"})
    assert r.status_code == 201 and r.json()["user"]["role"] == "recruiter" and r.json()["user"]["email"] == "new@x.com"
    assert "httponly" in r.headers["set-cookie"].lower()
    assert api.get("/api/jobs").status_code == 200                      # signed in straight away
    assert api.get("/api/users").status_code == 403                     # but not an admin


def test_register_cannot_pick_a_role_and_rejects_bad_input(api):
    r = api.post("/api/auth/register", json={"email": "sneaky@x.com", "password": "brand new pw", "role": "admin"})
    assert r.status_code == 201 and r.json()["user"]["role"] == "recruiter"   # role in the request is ignored
    assert api.post("/api/auth/register", json={"email": "sneaky@x.com", "password": "brand new pw"}).status_code == 422   # duplicate
    assert api.post("/api/auth/register", json={"email": "b@x.com", "password": "short"}).status_code == 422
    assert api.post("/api/auth/register", json={"email": "nope", "password": "brand new pw"}).status_code == 422


def test_registration_can_be_closed(api, monkeypatch):
    monkeypatch.setattr(config, "ALLOW_REGISTRATION", False)
    assert api.get("/api/auth/status").json()["registration_open"] is False
    assert api.post("/api/auth/register", json={"email": "c@x.com", "password": "brand new pw"}).status_code == 403


def test_registration_is_throttled_per_address(api):
    auth._signups.clear()
    codes = [api.post("/api/auth/register", json={"email": f"u{i}@x.com", "password": "brand new pw"}).status_code
             for i in range(auth.MAX_SIGNUPS + 1)]
    assert codes[:-1] == [201] * auth.MAX_SIGNUPS and codes[-1] == 429
    auth._signups.clear()


# ---------- session revocation (found by the QA suite: an old cookie still worked after logout) ----------
def test_logout_revokes_a_copied_cookie(api):
    r = login(api, "rec@x.com")
    saved = api.cookies.get("session")
    assert api.get("/api/jobs").status_code == 200
    api.post("/api/auth/logout")
    thief = TestClient(app)
    thief.cookies.set("session", saved)
    assert thief.get("/api/jobs").status_code == 401


def test_password_change_signs_out_other_sessions_but_not_this_one(api):
    other = TestClient(app)
    login(other, "rec@x.com")
    login(api, "rec@x.com")
    assert api.post("/api/auth/password", json={"current_password": PW, "new_password": "brand new pw"}).status_code == 200
    assert api.get("/api/jobs").status_code == 200          # this browser got a fresh cookie
    assert other.get("/api/jobs").status_code == 401        # the other session was ended


def test_admin_password_reset_signs_the_user_out(api):
    rec = TestClient(app)
    login(rec, "rec@x.com")
    login(api, "admin@x.com")
    rid = next(u["id"] for u in api.get("/api/users").json() if u["email"] == "rec@x.com")
    api.patch(f"/api/users/{rid}", json={"password": "reset by admin"})
    assert rec.get("/api/jobs").status_code == 401


def test_security_headers_on_every_response(api):
    for path in ("/api/health", "/api/jobs", "/"):
        h = api.get(path).headers
        assert h["x-content-type-options"] == "nosniff" and h["x-frame-options"] == "DENY"
        assert "frame-ancestors 'none'" in h["content-security-policy"]




# ---------- accounts must not see each other's jobs (reported by the user during a manual test) ----------
def _job_as(client, text=None):
    from conftest import JD_TEXT
    r = client.post("/api/jobs", data={"text": text or JD_TEXT})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def test_each_recruiter_sees_only_their_own_jobs(api, conn):
    from conftest import _judge, add_candidate
    deps.JOB_LLMS.update(jd_llm=_judge, canon_llm=_judge)
    try:
        auth.create_user(conn, "rec2@x.com", PW, "Rec Two", "recruiter")
        a, b, admin = TestClient(app), TestClient(app), TestClient(app)
        login(a, "rec@x.com"); login(b, "rec2@x.com"); login(admin, "admin@x.com")
        job = _job_as(a)
        app_id = add_candidate(conn, job, "Jeevan Raj", "j@x.com", "9000000001", ["Python"], 0).application_id

        assert [j["id"] for j in a.get("/api/jobs").json()] == [job]
        assert b.get("/api/jobs").json() == []                                       # the other account sees nothing
        listed = admin.get("/api/jobs").json()
        assert [j["id"] for j in listed] == [job] and listed[0]["owner"] == "rec@x.com"      # admins see all, with the owner

        # every route that takes a job id answers "not found" to the wrong account, and works for the owner
        for method, path in [("get", f"/api/jobs/{job}"), ("get", f"/api/jobs/{job}/candidates"), ("get", f"/api/jobs/{job}/review"),
                             ("post", f"/api/jobs/{job}/rescore"), ("get", f"/api/jobs/{job}/export.csv"), ("get", f"/api/jobs/{job}/export.xlsx"),
                             ("get", f"/api/jobs/{job}/chat"), ("delete", f"/api/jobs/{job}/chat"), ("get", f"/api/jobs/{job}/conflicts")]:
            assert getattr(b, method)(path).status_code == 404, ("leaked to another account", method, path)
            assert getattr(a, method)(path).status_code == 200, ("owner locked out", method, path)
            assert getattr(admin, method)(path).status_code == 200, ("admin locked out", method, path)
        assert b.post(f"/api/jobs/{job}/chat", json={"question": "top"}).status_code == 404
        assert b.post(f"/api/jobs/{job}/resumes", files=[("files", ("r.pdf", b"%PDF", "application/pdf"))]).status_code == 404
        assert b.post("/api/conflicts/resolve", json={"keep_application_id": app_id}).status_code == 404      # by application id
        assert a.post("/api/conflicts/resolve", json={"keep_application_id": app_id}).status_code == 200

        # their own job stays separate
        job_b = _job_as(b)
        assert [j["id"] for j in b.get("/api/jobs").json()] == [job_b] and a.get(f"/api/jobs/{job_b}").status_code == 404
    finally:
        deps.JOB_LLMS.clear()


def test_database_viewer_is_admin_only(api):
    rec = TestClient(app); admin = TestClient(app)
    login(rec, "rec@x.com"); login(admin, "admin@x.com")
    assert rec.get("/api/db").status_code == 403 and rec.get("/api/db/candidates").status_code == 403
    assert admin.get("/api/db").status_code == 200 and admin.get("/api/db/candidates").status_code == 200


def test_jobs_made_before_owners_existed_are_admin_only(api, conn):
    from conftest import JD_TEXT, _judge
    from app.services import candidate_service as svc
    old, _ = svc.create_job(conn, JD_TEXT, canon_llm=_judge, jd_llm=_judge)           # no owner: like a job made while login was off
    rec = TestClient(app); admin = TestClient(app)
    login(rec, "rec@x.com"); login(admin, "admin@x.com")
    assert rec.get("/api/jobs").json() == [] and rec.get(f"/api/jobs/{old}").status_code == 404
    assert admin.get(f"/api/jobs/{old}").status_code == 200




# ---------- login is on by default: the first account on a fresh install becomes the admin ----------
def test_login_is_on_by_default_in_the_config_file():
    import re
    src = open(config.__file__, encoding="utf-8").read()
    assert re.search(r'AUTH_ENABLED\s*=\s*os\.getenv\("AUTH_ENABLED",\s*"1"\)', src)


def test_first_registered_account_is_the_admin_then_recruiters(conn, monkeypatch):
    monkeypatch.setattr(config, "AUTH_ENABLED", True)
    monkeypatch.setattr(config, "SESSION_SECRET", "test-secret")
    monkeypatch.setattr(config, "ADMIN_EMAIL", "")
    auth._signups.clear()
    app.dependency_overrides[deps.get_db] = lambda: conn
    try:
        with TestClient(app) as c:
            assert c.get("/api/auth/status").json() == {"auth_enabled": True, "registration_open": True, "user": None}
            first = c.post("/api/auth/register", json={"email": "first@x.com", "password": PW, "name": "First"})
            assert first.status_code == 201 and first.json()["user"]["role"] == "admin"
            assert c.get("/api/users").status_code == 200                               # and can manage users straight away
            other = TestClient(app)
            second = other.post("/api/auth/register", json={"email": "second@x.com", "password": PW, "role": "admin"})
            assert second.json()["user"]["role"] == "recruiter"                          # everyone after that is a recruiter
    finally:
        app.dependency_overrides.clear()


def test_first_account_can_register_even_when_registration_is_closed(conn, monkeypatch):
    monkeypatch.setattr(config, "AUTH_ENABLED", True)
    monkeypatch.setattr(config, "ALLOW_REGISTRATION", False)
    monkeypatch.setattr(config, "SESSION_SECRET", "test-secret")
    monkeypatch.setattr(config, "ADMIN_EMAIL", "")
    auth._signups.clear()
    app.dependency_overrides[deps.get_db] = lambda: conn
    try:
        with TestClient(app) as c:
            assert c.get("/api/auth/status").json()["registration_open"] is True          # otherwise nobody could ever sign in
            assert c.post("/api/auth/register", json={"email": "first@x.com", "password": PW}).status_code == 201
            assert c.get("/api/auth/status").json()["registration_open"] is False         # closed once someone exists
            assert TestClient(app).post("/api/auth/register", json={"email": "late@x.com", "password": PW}).status_code == 403
    finally:
        app.dependency_overrides.clear()


def test_session_secret_is_generated_once_and_survives_restarts(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "SESSION_SECRET", "")
    monkeypatch.setattr(auth, "SECRET_FILE", tmp_path / "session_secret.txt")
    monkeypatch.setattr(auth, "_secret_cache", [])
    token = auth.make_token(5, epoch=2)
    assert (tmp_path / "session_secret.txt").exists()
    monkeypatch.setattr(auth, "_secret_cache", [])            # simulate a server restart: memory is empty, the file remains
    assert auth.read_token(token) == (5, 2)                   # the same login still works
    monkeypatch.setattr(auth, "_secret_cache", [])
    (tmp_path / "session_secret.txt").write_text("y" * 64, encoding="utf-8")    # a different secret rejects old tokens
    assert auth.read_token(token) is None


def test_new_job_settings_and_delete_endpoints_respect_ownership(api, conn):
    """Found worth pinning by the bug hunt: weights, cutoffs, must-haves and both deletes must be 404 for another account."""
    from conftest import _judge, add_candidate
    deps.JOB_LLMS.update(jd_llm=_judge, canon_llm=_judge)
    try:
        auth.create_user(conn, "rec2@x.com", PW, "Rec Two", "recruiter")
        a, b = TestClient(app), TestClient(app)
        login(a, "rec@x.com"); login(b, "rec2@x.com")
        job = _job_as(a)
        app_id = add_candidate(conn, job, "Jeevan Raj", "j@x.com", "9000000001", ["Python"], 0).application_id
        w = {"skills": 50, "experience": 20, "projects_education": 15, "fit": 15}
        attempts = [("put", f"/api/jobs/{job}/weights", w), ("delete", f"/api/jobs/{job}/weights", None),
                    ("put", f"/api/jobs/{job}/cutoffs", {"shortlist": 80, "consider": 40}), ("delete", f"/api/jobs/{job}/cutoffs", None),
                    ("put", f"/api/jobs/{job}/gates", {"skills": ["Python"]}), ("delete", f"/api/applications/{app_id}", None),
                    ("delete", f"/api/jobs/{job}", None)]
        for method, path, body in attempts:
            r = getattr(b, method)(path, json=body) if body is not None else getattr(b, method)(path)
            assert r.status_code == 404, ("leaked", method, path)
        assert a.get(f"/api/jobs/{job}").status_code == 200 and len(a.get(f"/api/jobs/{job}/candidates").json()) == 1        # nothing was changed or deleted
        assert a.put(f"/api/jobs/{job}/weights", json=w).status_code == 200
    finally:
        deps.JOB_LLMS.clear()
