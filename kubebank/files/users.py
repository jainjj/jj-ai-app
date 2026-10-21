"""users: customer sign-up, sign-in tokens, and email lookup for payments between customers."""
import hashlib
import hmac
import re
import secrets
import time

from lib import DB, ApiError, Handler, serve, sign_token

db = DB("users", """
CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY AUTOINCREMENT, email TEXT UNIQUE NOT NULL, name TEXT NOT NULL,
  pw_hash TEXT NOT NULL, salt TEXT NOT NULL, created REAL NOT NULL);
""")


def hash_pw(password, salt):
    return hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 200_000).hex()


def public(u):
    return {"id": u["id"], "email": u["email"], "name": u["name"], "created": u["created"]}


def session(u):
    return {"token": sign_token({"sub": u["id"], "email": u["email"], "name": u["name"]}), "user": public(u)}


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


def me(h):
    u = db.one("SELECT * FROM users WHERE id=?", (h.user()["sub"],))
    if not u:
        raise ApiError(401, "account no longer exists")
    return 200, session(u)


def internal_lookup(h):
    """Resolve a payee's email for the accounts service (only id and name leave this service)."""
    h.require_internal()
    u = db.one("SELECT * FROM users WHERE email=?", (h.query.get("email", "").strip().lower(),))
    if not u:
        raise ApiError(404, "no KubeBank customer with that email")
    return 200, {"id": u["id"], "name": u["name"]}


class UsersHandler(Handler):
    routes = {
        ("POST", "/register"): register,
        ("POST", "/login"): login,
        ("GET", "/me"): me,
        ("GET", "/internal/lookup"): internal_lookup,
    }


if __name__ == "__main__":
    serve(UsersHandler)
