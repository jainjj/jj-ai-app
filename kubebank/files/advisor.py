"""advisor: "Penny", the AI money coach, backed by SUSE AI's Ollama on this cluster.

Every answer is grounded in the customer's own (demo) accounts, which the
accounts service hands over per request. Modes:
  chat      free conversation about your money
  month     "explain my month": a spending review
  txn       "what is this payment?" for one transaction
  goal      a savings plan for one goal
  transfer  why a payment was held, and scam warning signs
Replies stream as NDJSON: {"status"}, {"c": text}, then {"done": ...} or {"error"}.
"""
import http.client
import json
import os
import queue
import threading
import time
import urllib.parse
import urllib.request

from lib import ApiError, Handler, call, serve, url_of

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://ollama.simple-chatbot-with-rag-system.svc.cluster.local:11434")
MODEL = os.environ.get("MODEL", "gpt-oss:latest")
DAILY_LIMIT = int(os.environ.get("DAILY_LIMIT", "25"))
SLOTS = threading.BoundedSemaphore(int(os.environ.get("MAX_CONCURRENT", "2")))

# Requests per customer per UTC day, in memory (resets on restart; one replica).
_usage, _usage_lock = {}, threading.Lock()


def used(uid):
    with _usage_lock:
        return _usage.get((time.strftime("%Y-%m-%d", time.gmtime()), uid), 0)


def bump(uid):
    day = time.strftime("%Y-%m-%d", time.gmtime())
    with _usage_lock:
        for k in [k for k in _usage if k[0] != day]:
            del _usage[k]
        _usage[(day, uid)] = _usage.get((day, uid), 0) + 1


# ---------------------------------------------------------------- model
_model = {"t": 0, "name": MODEL}


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


def ollama_stream(messages, num_predict):
    u = urllib.parse.urlparse(OLLAMA_URL)
    conn = http.client.HTTPConnection(u.hostname, u.port or 80, timeout=600)
    model = active_model()
    conn.request("POST", "/api/chat", headers={"Content-Type": "application/json"}, body=json.dumps({
        # gpt-oss can't switch reasoning off, only down; other models (qwen3...) can.
        "model": model, "messages": messages, "stream": True, "think": "low" if "gpt-oss" in model else False,
        "keep_alive": "30m", "options": {"num_predict": num_predict, "temperature": 0.5},
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


def with_heartbeat(chunks, every=15):
    """Yield chunks, plus None whenever none arrived for `every` seconds.

    gpt-oss reasons silently for a minute or more on CPU, and proxies such as
    ingress-nginx drop a response that sends nothing for 60s.
    """
    q = queue.Queue()

    def pump():
        try:
            for c in chunks:
                q.put(("chunk", c))
        except Exception as e:
            q.put(("error", e))
        q.put(("end", None))

    threading.Thread(target=pump, daemon=True).start()
    while True:
        try:
            kind, val = q.get(timeout=every)
        except queue.Empty:
            yield None
            continue
        if kind == "end":
            return
        if kind == "error":
            raise val
        yield val


# ---------------------------------------------------------------- prompts
def eur(c):
    return f"€{c / 100:,.2f}"


def day(ts):
    return time.strftime("%d %b", time.gmtime(ts))


def context_text(s):
    ins = s["insights"]
    lines = ["Accounts:"] + [f"- {a['name']} ({a['kind']}): {eur(a['balance_cents'])}"
                              + (f" of {eur(a['target_cents'])} target" if a.get("target_cents") else "")
                              for a in s["accounts"]]
    tm, lm, l30 = ins["this_month"], ins["last_month"], ins["last_30_days"]
    lines += [f"\nSpending this month so far (day {tm['day']}): {eur(tm['total'])}; "
              + ", ".join(f"{k} {eur(v)}" for k, v in tm["by_category"].items()),
              f"Spending last month: {eur(lm['total'])}; " + ", ".join(f"{k} {eur(v)}" for k, v in lm["by_category"].items()),
              f"Last 30 days: income {eur(l30['income'])}, spending {eur(l30['total'])}. Top merchants: "
              + ", ".join(f"{m} {eur(v)}" for m, v in l30["top_merchants"]),
              "Recurring payments: " + ", ".join(ins["recurring"]),
              f"Card frozen: {'yes' if (s.get('card') or {}).get('frozen') else 'no'}",
              "\nRecent transactions:"]
    lines += [f"- {day(t['created'])} {t['merchant']} ({t['category']}) {eur(t['amount_cents'])}"
              + (f" [{t['status']}]" if t["status"] != "posted" else "") for t in s["recent"][:15]]
    return "\n".join(lines)


PERSONA = (
    "You are Penny, the friendly AI money coach of KubeBank, a demo bank. You run on SUSE AI inside the bank's own "
    "Kubernetes cluster, so the customer's data never leaves it. All money here is play money. "
    "Base everything on the customer's data below; never invent transactions or numbers. "
    "Be warm, practical and concise, with concrete euro amounts. Use short bullet points where helpful. "
    "You give general budgeting tips, not regulated financial or investment advice; never recommend specific "
    "investments. Never ask for passwords, card numbers or codes."
)


def build(mode, s, b, name):
    """Returns (system prompt, user message, max tokens) for one request."""
    ctx = f"{PERSONA}\n\nCustomer: {name}.\n{context_text(s)}"
    if mode == "month":
        return ctx, ("Review my spending this month against last month. Give: one headline sentence, the 3 biggest "
                     "changes or patterns, and 2 specific ways to save next month with estimated euro savings. "
                     "Under 150 words."), 450
    if mode == "txn":
        t = s.get("txn") or {}
        if not t:
            raise ApiError(404, "no such transaction")
        hist = ", ".join(f"{day(x['created'])} {eur(x['amount_cents'])}" for x in s.get("txn_history", []))
        return ctx, (f"Explain this transaction in plain words: {day(t['created'])}, {t['merchant']}, "
                     f"{eur(t['amount_cents'])}, category {t['category']}, status {t['status']}"
                     + (f", note '{t['note']}'" if t.get("note") else "") + f". History with this merchant: {hist}. "
                     "Say what it most likely is, whether it looks recurring, how it compares to usual, and one tip. "
                     "Under 90 words."), 300
    if mode == "goal":
        g = s.get("goal") or {}
        if not g:
            raise ApiError(404, "no such goal")
        return ctx, (f"Make a savings plan for my goal '{g['name']}': {eur(g['balance_cents'])} saved of "
                     f"{eur(g['target_cents'] or 0)}. Using my income and spending above, suggest a realistic monthly "
                     "amount, when I'd reach it, and two specific spending cuts that would fund it. Under 130 words."), 400
    if mode == "transfer":
        t = s.get("transfer") or {}
        if not t:
            raise ApiError(404, "no such payment")
        return ctx, (f"My payment of {eur(t['amount_cents'])} to {t['payee']} (note: '{t['note'] or 'none'}') was held "
                     f"by KubeBank's fraud checks, risk score {t['score']}/100, because: {'; '.join(t['reasons'])}. "
                     "Explain calmly why it was held, what scam patterns this resembles (if any), and 3 questions I "
                     "should ask myself before confirming. Don't tell me whether to send it. Under 130 words."), 400
    history = [m for m in b.get("messages", []) if isinstance(m, dict)
               and m.get("role") in ("user", "assistant") and isinstance(m.get("content"), str)][-8:]
    if not history or history[-1]["role"] != "user" or not history[-1]["content"].strip():
        raise ApiError(400, "ask Penny something first!")
    return ctx + "\nKeep chat answers under 110 words.", [{"role": m["role"], "content": m["content"][:2000]} for m in history], 400


# ---------------------------------------------------------------- routes
def info(h):
    claims = h.user(required=False)
    return 200, {"model": active_model(), "ollama": urllib.parse.urlparse(OLLAMA_URL).hostname,
                 "daily_limit": DAILY_LIMIT, "used": used(claims["sub"]) if claims else 0}


def ask(h):
    claims = h.user()
    b = h.body()
    mode = b.get("mode", "chat")
    if mode not in ("chat", "month", "txn", "goal", "transfer"):
        raise ApiError(400, "unknown mode")
    uid = claims["sub"]
    params = {"user_id": uid}
    for key in ("txn", "goal", "transfer"):
        if mode == key:
            params[key] = str(b.get("id", ""))
    status, summary = call(url_of("accounts") + "/internal/summary?" + urllib.parse.urlencode(params), internal=True)
    if status != 200:
        raise ApiError(502 if status != 404 else 404, summary.get("error", "couldn't load your accounts"))
    system, user, num_predict = build(mode, summary, b, claims.get("name", "customer"))
    messages = [{"role": "system", "content": system}] + (user if isinstance(user, list) else [{"role": "user", "content": user}])

    if used(uid) >= DAILY_LIMIT:
        raise ApiError(429, f"Penny has answered {DAILY_LIMIT} questions for you today. She'll be back tomorrow!")
    if not SLOTS.acquire(timeout=1):
        raise ApiError(503, "Penny is helping other customers right now, try again in a moment.")
    try:
        bump(uid)
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
            for chunk in with_heartbeat(ollama_stream(messages, num_predict)):
                if chunk is None:  # the model is still thinking; keep proxies from timing out
                    emit({"status": "thinking"})
                    continue
                text = chunk.get("message", {}).get("content")
                if text:
                    emit({"c": text})
                if chunk.get("done"):
                    emit({"done": True, "secs": round(time.time() - t0, 1), "model": chunk.get("model"),
                          "used": used(uid), "limit": DAILY_LIMIT})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            try:
                emit({"error": f"Penny couldn't reach SUSE AI: {e}"})
            except Exception:
                pass
    finally:
        SLOTS.release()


class AdvisorHandler(Handler):
    routes = {("GET", "/info"): info, ("POST", "/ask"): ask}


if __name__ == "__main__":
    serve(AdvisorHandler)
