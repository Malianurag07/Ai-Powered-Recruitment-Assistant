"""Login, logout, current user, and admin-only user management.

`current_user` is the dependency that guards every other router (see main.py). With AUTH_ENABLED off it returns a
built-in admin, so the app behaves exactly as before and needs no login.
"""
import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from app import config, deps
from app.services import auth_service as svc

COOKIE = "session"
ANONYMOUS = {"id": 0, "email": "", "name": "Local user", "role": "admin", "is_active": True}

router = APIRouter(prefix="/api", tags=["auth"])


def current_user(request: Request, db: sqlite3.Connection = Depends(deps.get_db)) -> dict:
    if not config.AUTH_ENABLED:
        return ANONYMOUS
    user = svc.user_for_token(db, request.cookies.get(COOKIE))   # checked against the database every time: disabling a user,
    if not user:                                                 # logging out or changing the password ends old cookies at once
        raise HTTPException(401, "Please sign in.")
    return user


def require_admin(user: dict = Depends(current_user)) -> dict:
    if user["role"] != "admin":
        raise HTTPException(403, "Admin access required.")
    return user


class Login(BaseModel):
    email: str = Field(max_length=200)
    password: str = Field(max_length=200)


class Register(BaseModel):
    email: str = Field(max_length=200)
    password: str = Field(max_length=200)
    name: str = Field("", max_length=100)


class NewUser(BaseModel):
    email: str = Field(max_length=200)
    password: str = Field(max_length=200)
    name: str = Field("", max_length=100)
    role: str = "recruiter"


class UserPatch(BaseModel):
    role: str | None = None
    is_active: bool | None = None
    name: str | None = Field(None, max_length=100)
    password: str | None = Field(None, max_length=200)


class PasswordChange(BaseModel):
    current_password: str = Field(max_length=200)
    new_password: str = Field(max_length=200)


def _fail(e: svc.AuthError, code: int = 422):
    raise HTTPException(code, str(e))


@router.get("/auth/status")
def status(request: Request, db: sqlite3.Connection = Depends(deps.get_db)):
    """Public: lets the UI know whether login is required, whether sign-up is open, and who is signed in (null if nobody;
    reported here rather than via a 401 on /auth/me so a visitor's first page load logs no error)."""
    open_ = config.AUTH_ENABLED and (config.ALLOW_REGISTRATION or not svc.has_users(db))
    user = svc.user_for_token(db, request.cookies.get(COOKIE)) if config.AUTH_ENABLED else None
    return {"auth_enabled": config.AUTH_ENABLED, "registration_open": open_, "user": user}


@router.post("/auth/login")
def login(body: Login, response: Response, db: sqlite3.Connection = Depends(deps.get_db)):
    if not config.AUTH_ENABLED:
        return {"user": ANONYMOUS}
    try:
        user = svc.authenticate(db, body.email, body.password)
    except svc.AuthError as e:
        _fail(e, 429 if str(e).startswith("Too many") else 401)
    response.set_cookie(COOKIE, svc.issue_token(db, user["id"]), max_age=3600 * config.SESSION_HOURS,
                        httponly=True, samesite="lax", path="/")
    return {"user": user}


@router.post("/auth/register", status_code=201)
def register(body: Register, request: Request, response: Response, db: sqlite3.Connection = Depends(deps.get_db)):
    """Anyone can create an account (when ALLOW_REGISTRATION is on) and is signed in straight away: a recruiter, or the admin if this is the first account."""
    if not (config.AUTH_ENABLED and (config.ALLOW_REGISTRATION or not svc.has_users(db))):
        raise HTTPException(403, "Registration is closed. Ask an admin to create your account.")
    try:
        user = svc.register(db, request.client.host if request.client else "?", body.email, body.password, body.name)
    except svc.AuthError as e:
        _fail(e, 429 if str(e).startswith("Too many") else 422)
    response.set_cookie(COOKIE, svc.issue_token(db, user["id"]), max_age=3600 * config.SESSION_HOURS,
                        httponly=True, samesite="lax", path="/")
    return {"user": user}


@router.post("/auth/logout")
def logout(request: Request, response: Response, db: sqlite3.Connection = Depends(deps.get_db)):
    """Signing out also revokes the copied cookie: a stolen or saved cookie stops working (on all this user's devices)."""
    user = svc.user_for_token(db, request.cookies.get(COOKIE)) if config.AUTH_ENABLED else None
    if user:
        svc.revoke_sessions(db, user["id"])
    response.delete_cookie(COOKIE, path="/")
    return {"ok": True}


@router.get("/auth/me")
def me(user: dict = Depends(current_user)):
    return {"user": user}


@router.post("/auth/password")
def change_own_password(body: PasswordChange, response: Response, user: dict = Depends(current_user), db: sqlite3.Connection = Depends(deps.get_db)):
    """A signed-in user changes their own password; the current one must be supplied."""
    if not config.AUTH_ENABLED:
        raise HTTPException(400, "Authentication is turned off.")
    row = db.execute("SELECT password_hash FROM users WHERE id=?", (user["id"],)).fetchone()
    if not svc.verify_password(body.current_password, row["password_hash"]):
        raise HTTPException(401, "Current password is wrong.")
    try:
        svc.update_user(db, user["id"], password=body.new_password)      # bumps the epoch: every other session is signed out
    except svc.AuthError as e:
        _fail(e)
    response.set_cookie(COOKIE, svc.issue_token(db, user["id"]), max_age=3600 * config.SESSION_HOURS,
                        httponly=True, samesite="lax", path="/")       # this browser stays signed in
    return {"ok": True}


# ---------- admin only ----------
@router.get("/users")
def list_users(_: dict = Depends(require_admin), db: sqlite3.Connection = Depends(deps.get_db)):
    return svc.list_users(db)


@router.post("/users", status_code=201)
def create_user(body: NewUser, _: dict = Depends(require_admin), db: sqlite3.Connection = Depends(deps.get_db)):
    try:
        return svc.create_user(db, body.email, body.password, body.name, body.role)
    except svc.AuthError as e:
        _fail(e)


@router.patch("/users/{user_id}")
def update_user(user_id: int, body: UserPatch, admin: dict = Depends(require_admin), db: sqlite3.Connection = Depends(deps.get_db)):
    if user_id == admin["id"] and (body.is_active is False or (body.role and body.role != "admin")):
        raise HTTPException(422, "You cannot demote or deactivate yourself.")
    try:
        return svc.update_user(db, user_id, role=body.role, is_active=body.is_active, name=body.name, password=body.password)
    except svc.AuthError as e:
        _fail(e, 404 if "not found" in str(e) else 422)


@router.delete("/users/{user_id}")
def delete_user(user_id: int, admin: dict = Depends(require_admin), db: sqlite3.Connection = Depends(deps.get_db)):
    if user_id == admin["id"]:
        raise HTTPException(422, "You cannot delete yourself.")
    try:
        svc.delete_user(db, user_id)
    except svc.AuthError as e:
        _fail(e, 404 if "not found" in str(e) else 422)
    return {"deleted": True}
