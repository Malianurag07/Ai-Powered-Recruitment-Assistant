"""Authentication logic with no web code in it: password hashing, signed session tokens, user CRUD, login throttling.

Uses only the standard library (hashlib.scrypt for passwords, hmac for signing), so it adds no dependency and no cost.
Roles: `admin` (everything, plus user management) and `recruiter` (screening, ranking, chat, export).
"""
import base64
import hashlib
import hmac
import json
import secrets
import time

from app import config
from app.db import INTEGRITY_ERRORS, utc_now, PgConnection

ROLES = ("admin", "recruiter")
MIN_PASSWORD = 8
_SCRYPT = (2 ** 14, 8, 1)                    # n, r, p: about 16 MB and 50 ms per hash
MAX_FAILS, LOCK_SECONDS = 5, 300

SECRET_FILE = config.BASE_DIR / "data" / "session_secret.txt"     # created on first run; git-ignored
_secret_cache: list[bytes] = []
_fails: dict[str, list[float]] = {}          # email -> timestamps of recent failed logins (in memory, per process)


MAX_SIGNUPS, SIGNUP_WINDOW = 10, 3600
_signups: dict[str, list[float]] = {}        # client address -> recent registration times (in memory)


class AuthError(ValueError):
    """A problem the user can fix (weak password, duplicate email...). The message is safe to show."""


# ---------- passwords ----------
def hash_password(password: str) -> str:
    n, r, p = _SCRYPT
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, n=n, r=r, p=p)
    return "$".join(["scrypt", str(n), str(r), str(p), base64.b64encode(salt).decode(), base64.b64encode(dk).decode()])


def verify_password(password: str, stored: str) -> bool:
    try:
        _, n, r, p, salt, dk = stored.split("$")
        got = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt), n=int(n), r=int(r), p=int(p))
        return hmac.compare_digest(got, base64.b64decode(dk))
    except (ValueError, TypeError):
        return False


_DUMMY_HASH = hash_password("not-a-real-password")   # checked when the email is unknown, so timing does not reveal which emails exist


def check_password_rules(password: str) -> None:
    if len(password) < MIN_PASSWORD:
        raise AuthError(f"Password must be at least {MIN_PASSWORD} characters.")
    if len(password) > 200:
        raise AuthError("Password is too long.")


# ---------- session tokens ----------
def _secret() -> bytes:
    if config.SESSION_SECRET:
        return config.SESSION_SECRET.encode()
    if not _secret_cache:
        # No SESSION_SECRET in .env: make one once and keep it, so restarting the server does not sign everybody out.
        try:
            if SECRET_FILE.exists():
                value = SECRET_FILE.read_text(encoding="utf-8").strip()
            else:
                value = secrets.token_hex(32)
                SECRET_FILE.parent.mkdir(parents=True, exist_ok=True)
                SECRET_FILE.write_text(value, encoding="utf-8")
            _secret_cache.append(value.encode() if len(value) >= 32 else secrets.token_bytes(32))
        except OSError:                                    # read-only disk: fall back to a per-run secret
            _secret_cache.append(secrets.token_bytes(32))
            print("WARNING: could not save a session secret; everyone is signed out when the server restarts.")
    return _secret_cache[0]


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def make_token(user_id: int, hours: float | None = None, epoch: int = 0) -> str:
    exp = int(time.time() + 3600 * (config.SESSION_HOURS if hours is None else hours))
    body = _b64(json.dumps({"uid": user_id, "ep": epoch, "exp": exp}).encode())
    return body + "." + _b64(hmac.new(_secret(), body.encode(), hashlib.sha256).digest())


def read_token(token: str | None) -> tuple[int, int] | None:
    """Return (user id, session epoch) if the token is genuine and unexpired, else None."""
    try:
        body, sig = (token or "").split(".")
        if not hmac.compare_digest(sig, _b64(hmac.new(_secret(), body.encode(), hashlib.sha256).digest())):
            return None
        data = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
        return (int(data["uid"]), int(data.get("ep", 0))) if data["exp"] > time.time() else None
    except (ValueError, KeyError, TypeError):
        return None


# ---------- users ----------
def _public(row) -> dict:
    d = dict(row)
    d.pop("password_hash", None)
    d.pop("session_epoch", None)
    d["is_active"] = bool(d["is_active"])
    return d


def issue_token(conn: PgConnection, user_id: int) -> str:
    """A login token bound to the user's current session epoch."""
    epoch = conn.execute("SELECT session_epoch FROM users WHERE id=?", (user_id,)).fetchone()[0]
    return make_token(user_id, epoch=epoch)


def user_for_token(conn: PgConnection, token: str | None) -> dict | None:
    """The active user this token belongs to, or None (forged, expired, revoked by logout or password change, or disabled)."""
    parsed = read_token(token)
    if parsed is None:
        return None
    row = conn.execute("SELECT * FROM users WHERE id=?", (parsed[0],)).fetchone()
    if row is None or not row["is_active"] or row["session_epoch"] != parsed[1]:
        return None
    return _public(row)


def revoke_sessions(conn: PgConnection, user_id: int) -> None:
    """Invalidate every login cookie this user has (used by logout and password changes)."""
    with conn:
        conn.execute("UPDATE users SET session_epoch = session_epoch + 1 WHERE id=?", (user_id,))


def get_user(conn: PgConnection, user_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    return _public(row) if row else None


def list_users(conn: PgConnection) -> list[dict]:
    return [_public(r) for r in conn.execute("SELECT * FROM users ORDER BY id")]


def _active_admins(conn) -> int:
    return conn.execute("SELECT COUNT(*) FROM users WHERE role='admin' AND is_active=1").fetchone()[0]


def create_user(conn, email: str, password: str, name: str = "", role: str = "recruiter") -> dict:
    email = (email or "").strip().lower()
    if "@" not in email or "." not in email.split("@")[-1] or len(email) > 200:
        raise AuthError("Enter a valid email address.")
    if role not in ROLES:
        raise AuthError("Role must be admin or recruiter.")
    check_password_rules(password)
    try:
        with conn:
            cur = conn.execute("INSERT INTO users (email, name, password_hash, role) VALUES (?,?,?,?)",
                               (email, (name or "").strip()[:100], hash_password(password), role))
    except INTEGRITY_ERRORS:
        raise AuthError("A user with that email already exists.")
    return get_user(conn, cur.lastrowid)


def update_user(conn, user_id: int, *, role: str | None = None, is_active: bool | None = None,
                name: str | None = None, password: str | None = None) -> dict:
    user = get_user(conn, user_id)
    if user is None:
        raise AuthError("User not found.")
    new_role = user["role"] if role is None else role
    new_active = user["is_active"] if is_active is None else is_active
    if new_role not in ROLES:
        raise AuthError("Role must be admin or recruiter.")
    losing_admin = user["role"] == "admin" and user["is_active"] and not (new_role == "admin" and new_active)
    if losing_admin and _active_admins(conn) <= 1:
        raise AuthError("There must always be at least one active admin.")
    if password is not None:
        check_password_rules(password)
    with conn:
        conn.execute("UPDATE users SET role=?, is_active=?, name=? WHERE id=?",
                     (new_role, int(new_active), user["name"] if name is None else name.strip()[:100], user_id))
        if password is not None:
            conn.execute("UPDATE users SET password_hash=?, session_epoch=session_epoch+1 WHERE id=?", (hash_password(password), user_id))
    return get_user(conn, user_id)


def delete_user(conn, user_id: int) -> None:
    user = get_user(conn, user_id)
    if user is None:
        raise AuthError("User not found.")
    if user["role"] == "admin" and user["is_active"] and _active_admins(conn) <= 1:
        raise AuthError("There must always be at least one active admin.")
    with conn:
        conn.execute("DELETE FROM users WHERE id=?", (user_id,))


# ---------- login ----------
def _recent_fails(email: str) -> list[float]:
    cutoff = time.time() - LOCK_SECONDS
    _fails[email] = [t for t in _fails.get(email, []) if t > cutoff]
    return _fails[email]


def authenticate(conn, email: str, password: str) -> dict:
    """Return the user, or raise AuthError with one generic message so attackers cannot tell which part was wrong."""
    email = (email or "").strip().lower()
    if len(_recent_fails(email)) >= MAX_FAILS:
        raise AuthError("Too many failed attempts. Try again in a few minutes.")
    row = conn.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
    ok = verify_password(password or "", row["password_hash"] if row else _DUMMY_HASH)
    if not (row and ok and row["is_active"]):
        _fails[email].append(time.time())
        raise AuthError("Invalid email or password.")
    _fails.pop(email, None)
    with conn:
        conn.execute("UPDATE users SET last_login_at=? WHERE id=?", (utc_now(), row["id"]))
    return get_user(conn, row["id"])


def register(conn, client: str, email: str, password: str, name: str = "") -> dict:
    """Self sign-up. A recruiter, except the very first account on a fresh install, which becomes the admin (otherwise nobody could
    manage users). The role is never chosen by the caller. Throttled per client address to slow down bulk sign-ups."""
    cutoff = time.time() - SIGNUP_WINDOW
    recent = _signups[client] = [t for t in _signups.get(client, []) if t > cutoff]
    if len(recent) >= MAX_SIGNUPS:
        raise AuthError("Too many sign-ups from this address. Try again later.")
    first = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0        # nobody can sign in yet: the first account becomes the admin
    user = create_user(conn, email, password, name, "admin" if first else "recruiter")
    recent.append(time.time())
    return user


def has_users(conn) -> bool:
    return conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] > 0


def bootstrap_admin(conn) -> None:
    """With auth on and no users yet, create the first admin from ADMIN_EMAIL / ADMIN_PASSWORD (else explain how)."""
    if not config.AUTH_ENABLED or conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]:
        return
    if config.ADMIN_EMAIL and config.ADMIN_PASSWORD:
        create_user(conn, config.ADMIN_EMAIL, config.ADMIN_PASSWORD, "Admin", "admin")
        print(f"Created first admin: {config.ADMIN_EMAIL}")
    else:
        print("Login is on and there are no accounts yet: open the app and register; the first account becomes the admin.")
