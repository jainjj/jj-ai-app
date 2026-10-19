"""assistant: "Kiko", the shopping assistant, backed by SUSE AI's Ollama on this cluster.

Free shoppers (and guests) get FREE_DAILY messages a day; Plus members are unlimited.
Replies stream as NDJSON: {"status"}, {"c": text}, then {"done": ...} or {"error"}.
"""
import http.client
import json
import os
import threading
import time
import urllib.parse
import urllib.request

from lib import ApiError, Handler, call, serve, url_of

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://ollama.simple-chatbot-with-rag-system.svc.cluster.local:11434")
MODEL = os.environ.get("MODEL", "gpt-oss:latest")
FREE_DAILY = int(os.environ.get("FREE_DAILY", "5"))
SLOTS = threading.BoundedSemaphore(int(os.environ.get("MAX_CONCURRENT", "2")))

# Message counts per user (or guest IP) per UTC day. In memory, so they reset on
# restart; fine for a demo, and the assistant runs as a single replica.
_usage, _usage_lock = {}, threading.Lock()


def quota_key(h, claims):
    return f"u{claims['sub']}" if claims else f"ip{h.client_ip}"


def used(key):
    day = time.strftime("%Y-%m-%d", time.gmtime())
    with _usage_lock:
        return _usage.get((day, key), 0)


def bump(key):
    day = time.strftime("%Y-%m-%d", time.gmtime())
    with _usage_lock:
        for k in [k for k in _usage if k[0] != day]:
            del _usage[k]
        _usage[(day, key)] = _usage.get((day, key), 0) + 1


def tier_of(claims):
    """The buyer's *current* tier, asked of users so a stale token can't keep Plus."""
    if not claims:
        return "guest"
    status, u = call(url_of("users") + f"/internal/users/{claims['sub']}", internal=True)
    return u.get("tier", "free") if status == 200 else claims.get("tier", "free")


# ---------------------------------------------------------------- model + prompt
_model = {"t": 0, "name": MODEL}
_catalog = {"t": 0, "text": ""}


def active_model():
    """MODEL if Ollama has it, otherwise its first local model, so any cluster works."""
    if time.time() - _model["t"] < 60:
        return _model["name"]
    try:
        with urllib.request.urlopen(OLLAMA_URL + "/api/tags", timeout=5) as r:
            models = json.load(r).get("models", [])
    except Exception:
        return _model["name"]
    names = [m["name"] for m in models]
    local = [m["name"] for m in models if not m.get("remote_host")]
    name = MODEL if MODEL in names or f"{MODEL}:latest" in names else (local or names or [MODEL])[0]
    if name != _model["name"]:
        print(f"model {MODEL} not in Ollama, using {name}", flush=True)
    _model.update(t=time.time(), name=name)
    return name


def catalog_text():
    if time.time() - _catalog["t"] > 300 or not _catalog["text"]:
        status, cat = call(url_of("catalog") + "/products")
        if status == 200:
            _catalog.update(t=time.time(), text="\n".join(
                f"- [[{p['id']}]] {p['name']} ({p['category']}): €{p['price_cents'] / 100:.2f}, "
                f"Plus €{p['member_price_cents'] / 100:.2f}. {p['description']}" for p in cat["products"]))
    return _catalog["text"] or "(catalog unavailable right now)"


def system_prompt(tier, name):
    return (
        "You are Kiko, the friendly shopping assistant of KubeKart, an online shop for cloud-native gear. "
        "You run on SUSE AI, inside the shop's own Kubernetes cluster. "
        "Help shoppers find products, compare them, and understand orders and KubeKart Plus. "
        "Only recommend products from the catalog below, and whenever you mention one, write its tag "
        "exactly as shown, e.g. [[kk-mug]], so the shop can show an add-to-cart button. "
        "Keep answers under 90 words, warm and a little playful. "
        "Payments on this site are a demo: no real money, and you never ask for card or payment details. "
        "KubeKart Plus costs €4.99/month: 10% off everything, free shipping, unlimited chats with you. "
        "Without Plus, shipping is €4.95 (free from €50) and chats are limited to "
        f"{FREE_DAILY} a day.\n\n"
        f"The shopper is {name or 'a guest'} with tier: {tier}.\n\nCatalog:\n{catalog_text()}"
    )


def ollama_stream(messages):
    u = urllib.parse.urlparse(OLLAMA_URL)
    conn = http.client.HTTPConnection(u.hostname, u.port or 80, timeout=600)
    model = active_model()
    conn.request("POST", "/api/chat", headers={"Content-Type": "application/json"}, body=json.dumps({
        # gpt-oss can't switch reasoning off, only down; other models (qwen3...) can.
        "model": model, "messages": messages, "stream": True, "think": "low" if "gpt-oss" in model else False,
        "keep_alive": "30m", "options": {"num_predict": 400, "temperature": 0.7},
    }))
    resp = conn.getresponse()
    if resp.status != 200:
        raise RuntimeError(f"ollama HTTP {resp.status}: {resp.read()[:200]!r}")
    try:
        for line in resp:
            if line.strip():
                yield json.loads(line)
    finally:
        conn.close()


# ---------------------------------------------------------------- routes
def info(h):
    return 200, {"model": active_model(), "ollama": urllib.parse.urlparse(OLLAMA_URL).hostname,
                 "free_daily": FREE_DAILY}


def quota(h):
    claims = h.user(required=False)
    tier = tier_of(claims)
    return 200, {"tier": tier, "used": used(quota_key(h, claims)),
                 "limit": None if tier == "plus" else FREE_DAILY}


def chat(h):
    claims = h.user(required=False)
    b = h.body()
    history = [m for m in b.get("messages", []) if isinstance(m, dict)
               and m.get("role") in ("user", "assistant") and isinstance(m.get("content"), str)][-8:]
    if not history or history[-1]["role"] != "user" or not history[-1]["content"].strip():
        raise ApiError(400, "say something first!")
    for m in history:
        m["content"] = m["content"][:2000]

    tier, key = tier_of(claims), quota_key(h, claims)
    if tier != "plus" and used(key) >= FREE_DAILY:
        raise ApiError(429, f"You've used today's {FREE_DAILY} free chats. Join KubeKart Plus for unlimited Kiko!")
    if not SLOTS.acquire(timeout=1):
        raise ApiError(503, "Kiko is helping other shoppers right now, try again in a moment.")
    try:
        bump(key)
        h.send_response(200)
        h.send_header("Content-Type", "application/x-ndjson")
        h.send_header("Cache-Control", "no-cache")
        h.send_header("X-Accel-Buffering", "no")
        h.end_headers()

        def emit(obj):
            h.wfile.write((json.dumps(obj) + "\n").encode())
            h.wfile.flush()

        emit({"status": "thinking", "model": active_model()})
        t0 = time.time()
        try:
            msgs = [{"role": "system", "content": system_prompt(tier, claims and claims.get("name"))}] + history
            for chunk in ollama_stream(msgs):
                text = chunk.get("message", {}).get("content")
                if text:
                    emit({"c": text})
                if chunk.get("done"):
                    emit({"done": True, "secs": round(time.time() - t0, 1), "model": chunk.get("model"),
                          "used": used(key), "limit": None if tier == "plus" else FREE_DAILY})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            try:
                emit({"error": f"Kiko couldn't reach SUSE AI: {e}"})
            except Exception:
                pass
    finally:
        SLOTS.release()


class AssistantHandler(Handler):
    routes = {("GET", "/info"): info, ("GET", "/quota"): quota, ("POST", "/chat"): chat}


if __name__ == "__main__":
    serve(AssistantHandler)
