"""Shared helpers for the KubeBank microservices.

Stdlib only, so every service runs on the plain SUSE BCI Python image with no
image build. Each service's ConfigMap mounts this file next to its own script.
"""
import base64
import contextlib
import hashlib
import hmac
import json
import os
import sqlite3
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SERVICE = os.environ.get("SERVICE_NAME", "service")
SECRET = os.environ.get("BANK_SECRET", "dev-only-secret").encode()
# Proves a request came from another KubeBank service (fraud and the internal
# APIs accept nothing else).
SERVICE_KEY = hmac.new(SECRET, b"kubebank-internal", hashlib.sha256).hexdigest()


def url_of(name):
    """Base URL of another service, from <NAME>_URL (set by the Helm chart)."""
    return os.environ.get(f"{name.upper()}_URL", f"http://127.0.0.1:{8080 + SERVICES.index(name)}")


SERVICES = ["web", "users", "accounts", "fraud", "advisor"]


class ApiError(Exception):
    def __init__(self, code, msg):
        super().__init__(msg)
        self.code, self.msg = code, msg


# ---------------------------------------------------------------- tokens
def _b64(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _unb64(s):
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def sign_token(claims, ttl=7 * 86400):
    body = _b64(json.dumps({**claims, "exp": int(time.time()) + ttl}).encode())
    return f"{body}.{_b64(hmac.new(SECRET, body.encode(), hashlib.sha256).digest())}"


def verify_token(token):
    try:
        body, sig = token.split(".")
        good = _b64(hmac.new(SECRET, body.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(sig, good):
            return None
        claims = json.loads(_unb64(body))
        return claims if claims.get("exp", 0) > time.time() else None
    except Exception:
        return None


# ---------------------------------------------------------------- service calls
def call(url, method="GET", body=None, token=None, internal=False, timeout=10):
    """JSON request to another service. Returns (status, obj); never raises."""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json"})
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    if internal:
        req.add_header("X-Service-Key", SERVICE_KEY)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except ValueError:
            return e.code, {"error": f"HTTP {e.code}"}
    except Exception as e:
        return 503, {"error": f"{urllib.parse.urlparse(url).hostname} unavailable ({e})"}


# ---------------------------------------------------------------- storage
class DB:
    """Thread-safe SQLite, one file per service under DATA_DIR (a PVC in the cluster)."""

    def __init__(self, name, schema):
        path = os.path.join(os.environ.get("DATA_DIR", "/data"), f"{name}.db")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.Lock()
        with self.lock:
            self.conn.executescript(schema)

    def q(self, sql, args=()):
        with self.lock:
            cur = self.conn.execute(sql, args)
            self.conn.commit()
            return [dict(r) for r in cur.fetchall()]

    def one(self, sql, args=()):
        rows = self.q(sql, args)
        return rows[0] if rows else None

    @contextlib.contextmanager
    def tx(self):
        """Several statements as one unit (money moves): commits on success, rolls back on error."""
        with self.lock:
            try:
                yield self.conn
                self.conn.commit()
            except BaseException:
                self.conn.rollback()
                raise


# ---------------------------------------------------------------- http
class Handler(BaseHTTPRequestHandler):
    """Routes are {("GET", "/path"): fn(handler) -> (status, obj)}.

    A route ending in "/*" matches any deeper path and gets the rest as a second argument.
    """

    routes = {}
    server_version = "kubebank"

    def log_message(self, fmt, *args):
        if "/healthz" not in self.path:
            print(f"{SERVICE} {self.client_ip} {fmt % args}", flush=True)

    @property
    def client_ip(self):
        return (self.headers.get("X-Forwarded-For") or self.client_address[0]).split(",")[0].strip()

    @property
    def query(self):
        return {k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).items()}

    def send_json(self, code, obj):
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > 64_000:
            raise ApiError(413, "request too large")
        try:
            obj = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            raise ApiError(400, "bad json")
        if not isinstance(obj, dict):
            raise ApiError(400, "expected a JSON object")
        return obj

    @property
    def token(self):
        auth = self.headers.get("Authorization", "")
        return auth[7:] if auth.startswith("Bearer ") else ""

    def user(self, required=True):
        claims = verify_token(self.token) if self.token else None
        if required and not claims:
            raise ApiError(401, "please sign in")
        return claims

    def require_internal(self):
        if not hmac.compare_digest(self.headers.get("X-Service-Key", ""), SERVICE_KEY):
            raise ApiError(403, "internal API")

    def _dispatch(self, method):
        path = urllib.parse.urlparse(self.path).path
        if path == "/healthz":
            return self.send_json(200, {"ok": True, "service": SERVICE})
        fn, rest = self.routes.get((method, path)), None
        if not fn:
            for (m, p), f in self.routes.items():
                if m == method and p.endswith("/*") and path.startswith(p[:-1]) and len(path) > len(p) - 1:
                    fn, rest = f, path[len(p) - 1:]
                    break
        if not fn:
            return self.send_json(404, {"error": "not found"})
        try:
            res = fn(self) if rest is None else fn(self, rest)
        except ApiError as e:
            return self.send_json(e.code, {"error": e.msg})
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception:
            traceback.print_exc()
            return self.send_json(500, {"error": "internal error"})
        if res is not None:
            self.send_json(*res)

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_DELETE(self):
        self._dispatch("DELETE")


def serve(handler):
    port = int(os.environ.get("PORT", 8080 + SERVICES.index(SERVICE) if SERVICE in SERVICES else 8080))
    print(f"{SERVICE} listening on :{port}", flush=True)
    ThreadingHTTPServer(("", port), handler).serve_forever()
