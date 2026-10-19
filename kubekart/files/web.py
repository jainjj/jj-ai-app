"""web: serves the shop's single-page app and is the API gateway.

/api/<service>/<path> is proxied to that service (payments is internal and
deliberately not routable), and /api/status reports every service's health.
"""
import http.client
import json
import os
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor

from lib import SERVICES, Handler, serve, url_of

STATIC = os.environ.get("STATIC_DIR", os.path.dirname(os.path.abspath(__file__)))
PUBLIC = {"users", "catalog", "orders", "assistant"}
FORWARD = ("Content-Type", "Authorization")


def check(name):
    u = urllib.parse.urlparse(url_of(name))
    t0 = time.time()
    try:
        conn = http.client.HTTPConnection(u.hostname, u.port or 80, timeout=3)
        conn.request("GET", "/healthz")
        ok = conn.getresponse().status == 200
        conn.close()
    except Exception:
        ok = False
    return {"name": name, "ok": ok, "ms": round((time.time() - t0) * 1000)}


_pool = ThreadPoolExecutor(8)


class WebHandler(Handler):
    def _dispatch(self, method):
        path = urllib.parse.urlparse(self.path).path
        if path == "/healthz":
            return self.send_json(200, {"ok": True, "service": "web"})
        if path == "/api/status":
            services = [{"name": "web", "ok": True, "ms": 0}] + list(_pool.map(check, SERVICES[1:]))
            return self.send_json(200, {"services": services, "ok": all(s["ok"] for s in services)})
        if path.startswith("/api/"):
            return self.proxy(method, path)
        if method == "GET" and path in ("/", "/index.html"):
            with open(os.path.join(STATIC, "index.html"), "rb") as f:
                data = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            return self.wfile.write(data)
        self.send_json(404, {"error": "not found"})

    def proxy(self, method, path):
        _, _, svc, *rest = path.split("/", 3)
        if svc not in PUBLIC:
            return self.send_json(404, {"error": "not found"})
        n = int(self.headers.get("Content-Length") or 0)
        if n > 64_000:
            return self.send_json(413, {"error": "request too large"})
        body = self.rfile.read(n) if n else None
        query = urllib.parse.urlparse(self.path).query
        target = "/" + (rest[0] if rest else "") + (f"?{query}" if query else "")
        headers = {k: self.headers[k] for k in FORWARD if self.headers.get(k)}
        headers["X-Forwarded-For"] = self.client_ip

        u = urllib.parse.urlparse(url_of(svc))
        try:
            conn = http.client.HTTPConnection(u.hostname, u.port or 80, timeout=600 if svc == "assistant" else 15)
            conn.request(method, target, body=body, headers=headers)
            resp = conn.getresponse()
        except Exception:
            return self.send_json(503, {"error": f"the {svc} service is unavailable right now"})
        try:
            ctype = resp.getheader("Content-Type", "application/json")
            self.send_response(resp.status)
            self.send_header("Content-Type", ctype)
            if "ndjson" in ctype:
                # Stream assistant replies line by line as they arrive.
                self.send_header("Cache-Control", "no-cache")
                self.send_header("X-Accel-Buffering", "no")
                self.end_headers()
                for line in resp:
                    self.wfile.write(line)
                    self.wfile.flush()
            else:
                data = resp.read()
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            conn.close()


if __name__ == "__main__":
    serve(WebHandler)
