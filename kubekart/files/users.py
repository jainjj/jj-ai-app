"""users: accounts, sign-in tokens and the KubeKart Plus membership."""
import hashlib
import hmac
import re
import secrets
import time

from lib import DB, ApiError, Handler, call, serve, sign_token, url_of

PLUS_PRICE_CENTS = 499

db = DB("users", """
CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY AUTOINCREMENT, email TEXT UNIQUE NOT NULL, name TEXT NOT NULL,
  pw_hash TEXT NOT NULL, salt TEXT NOT NULL, tier TEXT NOT NULL DEFAULT 'free',
  plus_since REAL, created REAL NOT NULL);
""")


def hash_pw(password, salt):
    return hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 200_000).hex()


def public(u):
    return {"id": u["id"], "email": u["email"], "name": u["name"], "tier": u["tier"], "plus_since": u["plus_since"]}


def session(u):
    return {"token": sign_token({"sub": u["id"], "email": u["email"], "name": u["name"], "tier": u["tier"]}),
            "user": public(u)}


def register(h):
    b = h.body()
    email, name, pw = str(b.get("email", "")).strip().lower(), str(b.get("name", "")).strip(), str(b.get("password", ""))
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email) or len(email) > 200:
        raise ApiError(400, "enter a valid email address")
    if not name or len(name) > 80:
        raise ApiError(400, "enter your name")
    if len(pw) < 8:
        raise ApiError(400, "password must be at least 8 characters")
    if db.one("SELECT id FROM users WHERE email=?", (email,)):
        raise ApiError(409, "an account with that email already exists")
    salt = secrets.token_hex(16)
    db.q("INSERT INTO users (email,name,pw_hash,salt,created) VALUES (?,?,?,?,?)",
         (email, name, hash_pw(pw, salt), salt, time.time()))
    return 201, session(db.one("SELECT * FROM users WHERE email=?", (email,)))


def login(h):
    b = h.body()
    u = db.one("SELECT * FROM users WHERE email=?", (str(b.get("email", "")).strip().lower(),))
    if not u or not hmac.compare_digest(u["pw_hash"], hash_pw(str(b.get("password", "")), u["salt"])):
        raise ApiError(401, "wrong email or password")
    return 200, session(u)


def current(h):
    u = db.one("SELECT * FROM users WHERE id=?", (h.user()["sub"],))
    if not u:
        raise ApiError(401, "account no longer exists")
    return u


def me(h):
    # Re-issue the token so a tier change made elsewhere shows up straight away.
    return 200, session(current(h))


def join_plus(h):
    u = current(h)
    if u["tier"] == "plus":
        raise ApiError(409, "you're already a Plus member")
    b = h.body()
    status, pay = call(url_of("payments") + "/charges", "POST", internal=True, body={
        "user_id": u["id"], "amount_cents": PLUS_PRICE_CENTS, "currency": "EUR",
        "description": "KubeKart Plus, 1 month", "simulate_decline": bool(b.get("simulate_decline"))})
    if status != 201:
        raise ApiError(402 if status == 402 else 502, pay.get("error", "payment failed"))
    db.q("UPDATE users SET tier='plus', plus_since=? WHERE id=?", (time.time(), u["id"]))
    return 200, {**session(current(h)), "payment": pay}


def leave_plus(h):
    u = current(h)
    db.q("UPDATE users SET tier='free', plus_since=NULL WHERE id=?", (u["id"],))
    return 200, session(current(h))


def internal_user(h, uid):
    """Current tier for other services, so a stale token can't keep Plus pricing."""
    h.require_internal()
    u = db.one("SELECT * FROM users WHERE id=?", (uid,))
    if not u:
        raise ApiError(404, "no such user")
    return 200, public(u)


class UsersHandler(Handler):
    routes = {
        ("POST", "/register"): register,
        ("POST", "/login"): login,
        ("GET", "/me"): me,
        ("POST", "/membership"): join_plus,
        ("DELETE", "/membership"): leave_plus,
        ("GET", "/internal/users/*"): internal_user,
    }


if __name__ == "__main__":
    serve(UsersHandler)
