"""jj-ai: a tiny, fun front end for the SUSE AI Ollama service.

Stdlib only, so it runs on the plain SUSE BCI Python image with no pip install.
"""
import http.client
import json
import os
import socket
import ssl
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://ollama.simple-chatbot-with-rag-system.svc:11434")
MODEL = os.environ.get("MODEL", "gpt-oss:latest")
STATIC = os.environ.get("STATIC_DIR", "/app")
MAX_INPUT = 4000
LLM_SLOTS = threading.BoundedSemaphore(int(os.environ.get("MAX_CONCURRENT", "2")))

PERSONA = (
    "You are Geeko, the SUSE chameleon, living inside a Kubernetes cluster as the app jj-ai. "
    "You are witty, upbeat and a little cheeky, and you know Kubernetes, Rancher, SUSE and Linux deeply. "
    "Keep answers short (under 180 words) and use Markdown sparingly. Never reveal secrets or tokens."
)

MODES = {
    "roast": "Roast this Kubernetes cluster like a stand-up comedian. Be funny but technically accurate, "
             "and end with one genuinely useful improvement tip. Live cluster snapshot:\n\n{cluster}",
    "yaml": "Roast this YAML/manifest mercilessly but helpfully: point out real problems, then give the fixes.\n\n{input}",
    "haiku": "Write three haiku (5-7-5) about this, Kubernetes-flavoured:\n\n{input}",
    "fortune": "Give a dramatic fortune-teller prophecy about the future of this cluster, based on the snapshot. "
               "Mystical tone, real technical hints hidden inside.\n\n{cluster}",
    "eli5": "Explain this Kubernetes / cloud-native concept like I'm five, with a fun analogy:\n\n{input}",
    "chat": "{input}",
}

# ---------------------------------------------------------------- k8s snapshot
SA = "/var/run/secrets/kubernetes.io/serviceaccount"
_cache = {"t": 0, "data": None}


def k8s_get(path):
    with open(f"{SA}/token") as f:
        token = f.read().strip()
    ctx = ssl.create_default_context(cafile=f"{SA}/ca.crt")
    host = os.environ.get("KUBERNETES_SERVICE_HOST", "kubernetes.default.svc")
    port = int(os.environ.get("KUBERNETES_SERVICE_PORT", "443"))
    conn = http.client.HTTPSConnection(host, port, context=ctx, timeout=10)
    conn.request("GET", path, headers={"Authorization": f"Bearer {token}"})
    resp = conn.getresponse()
    body = resp.read()
    conn.close()
    if resp.status != 200:
        raise RuntimeError(f"k8s {path}: HTTP {resp.status}")
    return json.loads(body)


def cluster_snapshot():
    if _cache["data"] and time.time() - _cache["t"] < 30:
        return _cache["data"]
    nodes = k8s_get("/api/v1/nodes")["items"]
    pods = k8s_get("/api/v1/pods")["items"]
    by_ns, phases, restarts, images = {}, {}, [], set()
    for p in pods:
        ns = p["metadata"]["namespace"]
        by_ns[ns] = by_ns.get(ns, 0) + 1
        ph = p.get("status", {}).get("phase", "Unknown")
        phases[ph] = phases.get(ph, 0) + 1
        for c in p["spec"].get("containers", []):
            images.add(c["image"])
        r = sum(cs.get("restartCount", 0) for cs in p.get("status", {}).get("containerStatuses", []) or [])
        if r:
            restarts.append((r, f"{ns}/{p['metadata']['name']}"))
    restarts.sort(reverse=True)
    snap = {
        "nodes": [{
            "name": n["metadata"]["name"],
            "kubelet": n["status"]["nodeInfo"]["kubeletVersion"],
            "os": n["status"]["nodeInfo"]["osImage"],
            "cpu": n["status"]["capacity"].get("cpu"),
            "memory": n["status"]["capacity"].get("memory"),
            "gpus": n["status"]["capacity"].get("nvidia.com/gpu", "0"),
            "roles": sorted(k.split("/")[1] for k in n["metadata"].get("labels", {})
                            if k.startswith("node-role.kubernetes.io/")),
        } for n in nodes],
        "pod_count": len(pods),
        "pod_phases": phases,
        "pods_per_namespace": dict(sorted(by_ns.items(), key=lambda kv: -kv[1])),
        "top_restarters": [{"pod": n, "restarts": r} for r, n in restarts[:5]],
        "distinct_images": len(images),
        "image_registries": sorted({i.split("/")[0] if "." in i.split("/")[0] else "docker.io" for i in images}),
    }
    _cache.update(t=time.time(), data=snap)
    return snap


# ---------------------------------------------------------------- ollama
_model = {"t": 0, "name": MODEL}


def active_model():
    """MODEL if Ollama has it, otherwise the first local model, so any cluster works."""
    if time.time() - _model["t"] < 60:
        return _model["name"]
    try:
        with urllib.request.urlopen(OLLAMA_URL + "/api/tags", timeout=5) as r:
            models = json.load(r).get("models", [])
    except Exception:
        return _model["name"]
    names = [m["name"] for m in models]
    local = [m["name"] for m in models if not m.get("remote_host")]
    if MODEL in names or f"{MODEL}:latest" in names:
        name = MODEL
    else:
        name = (local or names or [MODEL])[0]
        if name != _model["name"]:
            print(f"model {MODEL} not in Ollama, using {name}", flush=True)
    _model.update(t=time.time(), name=name)
    return name


def ollama_stream(messages):
    u = urllib.parse.urlparse(OLLAMA_URL)
    conn = http.client.HTTPConnection(u.hostname, u.port or 80, timeout=600)
    model = active_model()
    body = json.dumps({
        # gpt-oss can't switch reasoning off, only down; other models (qwen3...) can.
        "model": model, "messages": messages, "stream": True, "think": "low" if "gpt-oss" in model else False,
        "keep_alive": "30m", "options": {"num_predict": 900, "temperature": 0.9},
    })
    conn.request("POST", "/api/chat", body=body, headers={"Content-Type": "application/json"})
    resp = conn.getresponse()
    if resp.status != 200:
        raise RuntimeError(f"ollama HTTP {resp.status}: {resp.read()[:300]!r}")
    try:
        for line in resp:
            if line.strip():
                yield json.loads(line)
    finally:
        conn.close()


# ---------------------------------------------------------------- http
class Handler(BaseHTTPRequestHandler):
    server_version = "jj-ai/1.0"

    def log_message(self, fmt, *args):
        print(f"{self.address_string()} {fmt % args}", flush=True)

    def _json(self, code, obj):
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/healthz":
            return self._json(200, {"ok": True})
        if path == "/api/info":
            return self._json(200, {
                "pod": socket.gethostname(), "node": os.environ.get("NODE_NAME", "?"),
                "namespace": os.environ.get("POD_NAMESPACE", "?"), "model": active_model(),
            })
        if path == "/api/cluster":
            try:
                return self._json(200, cluster_snapshot())
            except Exception as e:
                return self._json(502, {"error": str(e)})
        if path in ("/", "/index.html"):
            with open(os.path.join(STATIC, "index.html"), "rb") as f:
                data = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            return self.wfile.write(data)
        self._json(404, {"error": "not found"})

    def do_POST(self):
        if self.path != "/api/chat":
            return self._json(404, {"error": "not found"})
        try:
            n = int(self.headers.get("Content-Length", "0"))
            req = json.loads(self.rfile.read(min(n, 64_000)) or b"{}")
        except ValueError:
            return self._json(400, {"error": "bad json"})
        mode = req.get("mode", "chat")
        if mode not in MODES:
            return self._json(400, {"error": "unknown mode"})
        text = str(req.get("input", ""))[:MAX_INPUT]
        if not text.strip() and "{input}" in MODES[mode]:
            return self._json(400, {"error": "say something first!"})
        cluster = ""
        if "{cluster}" in MODES[mode]:
            try:
                cluster = json.dumps(cluster_snapshot(), indent=1)
            except Exception as e:
                cluster = f"(snapshot unavailable: {e})"
        prompt = MODES[mode].format(input=text, cluster=cluster)

        if not LLM_SLOTS.acquire(timeout=1):
            return self._json(429, {"error": "Geeko is busy thinking for someone else - try again in a moment."})
        try:
            self.send_response(200)
            self.send_header("Content-Type", "application/x-ndjson")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()

            def emit(obj):
                self.wfile.write((json.dumps(obj) + "\n").encode())
                self.wfile.flush()

            emit({"status": "waking up the model..."})
            t0, tokens = time.time(), 0
            for chunk in ollama_stream([{"role": "system", "content": PERSONA},
                                        {"role": "user", "content": prompt}]):
                msg = chunk.get("message", {})
                if msg.get("thinking"):
                    emit({"think": msg["thinking"]})
                if msg.get("content"):
                    tokens += 1
                    emit({"c": msg["content"]})
                if chunk.get("done"):
                    ev = chunk.get("eval_count") or tokens
                    dur = (chunk.get("eval_duration") or 1) / 1e9
                    emit({"done": True, "tokens": ev, "tps": round(ev / dur, 1),
                          "secs": round(time.time() - t0, 1)})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            try:
                emit({"error": str(e)})
            except Exception:
                pass
        finally:
            LLM_SLOTS.release()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    print(f"jj-ai listening on :{port}, model={MODEL}, ollama={OLLAMA_URL}", flush=True)
    ThreadingHTTPServer(("", port), Handler).serve_forever()
