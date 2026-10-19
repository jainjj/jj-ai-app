#!/usr/bin/env python3
"""End-to-end smoke test for KubeKart, through the web gateway.

    python3 tests/kubekart_smoke.py http://localhost:8080 [--chat]

Creates a throwaway test account (random email and password, generated here and
never printed), then checks catalog, checkout, a declined payment, Plus, member
pricing, assistant quota and that the payments service isn't publicly routable.
--chat also sends one real message to the assistant (slow on CPU-only clusters).
"""
import json
import secrets
import sys
import time
import urllib.error
import urllib.request

BASE = sys.argv[1].rstrip("/") if len(sys.argv) > 1 else "http://localhost:8080"
token = None
failures = 0


def api(path, method="GET", body=None, raw=False, timeout=30):
    req = urllib.request.Request(BASE + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json"})
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        r = urllib.request.urlopen(req, timeout=timeout)
        if raw:  # caller streams the body, so it must stay open
            return r.status, r
        with r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def check(name, cond, detail=""):
    global failures
    failures += not cond
    print(f"{'PASS' if cond else 'FAIL'}  {name}{'' if cond else f'  ({detail})'}")


s, st = api("/api/status")
check("all services healthy", s == 200 and st.get("ok"), st)
s, cat = api("/api/catalog/products")
check("catalog lists products", s == 200 and len(cat.get("products", [])) >= 10, s)
p = {x["id"]: x for x in cat.get("products", [])}

s, r = api("/api/payments/charges", "POST", {"amount_cents": 100})
check("payments is not routable from outside", s == 404, s)

email, pw = f"smoke-{secrets.token_hex(4)}@example.test", secrets.token_urlsafe(12)
s, r = api("/api/users/register", "POST", {"email": email, "name": "Smoke Test", "password": pw})
check("register", s == 201 and r.get("token"), r)
token = r.get("token")
s, r = api("/api/users/login", "POST", {"email": email, "password": "wrong-password"})
check("wrong password rejected", s == 401, s)

cart = [{"id": "kk-mug", "qty": 2}]
s, r = api("/api/orders/orders", "POST", {"items": cart})
want = p["kk-mug"]["price_cents"] * 2 + 495
check("free-tier order: full price + shipping", s == 201 and r["total_cents"] == want, (s, r.get("total_cents"), want))
s, r = api("/api/orders/orders", "POST", {"items": cart, "simulate_decline": True})
check("simulated decline returns 402", s == 402, s)
s, r = api("/api/orders/orders", "POST", {"items": [{"id": "kk-mug", "qty": 99}]})
check("bad quantity rejected", s == 400, s)

s, r = api("/api/users/membership", "POST", {"simulate_decline": True})
check("Plus with declined payment stays free", s == 402, s)
s, r = api("/api/users/membership", "POST", {})
check("join Plus", s == 200 and r["user"]["tier"] == "plus" and r["payment"]["status"] == "succeeded", r)
old_token, token = token, r.get("token")
s, r = api("/api/orders/orders", "POST", {"items": cart})
want = p["kk-mug"]["member_price_cents"] * 2
check("Plus order: member price, free shipping", s == 201 and r["total_cents"] == want, (s, r.get("total_cents"), want))

s, r = api("/api/users/membership", "DELETE")
check("cancel Plus", s == 200 and r["user"]["tier"] == "free", r)
token = old_token  # a token minted while on Plus must not keep Plus pricing
s, r = api("/api/orders/orders", "POST", {"items": cart})
check("stale Plus token gets free-tier price", s == 201 and r["total_cents"] == p["kk-mug"]["price_cents"] * 2 + 495,
      r.get("total_cents"))
s, r = api("/api/orders/orders")
check("order history", s == 200 and len(r.get("orders", [])) == 3, len(r.get("orders", [])))

s, q = api("/api/assistant/quota")
check("assistant quota for free tier", s == 200 and q.get("limit") == 5, q)
s, i = api("/api/assistant/info")
check("assistant reports its SUSE AI model", s == 200 and i.get("model"), i)

if "--chat" in sys.argv:
    t0 = time.time()
    s, resp = api("/api/assistant/chat", "POST", {"messages": [{"role": "user", "content": "Suggest one gift under €20."}]},
                  raw=True, timeout=900)
    text, err = "", None
    if s == 200:
        for line in resp:
            ev = json.loads(line)
            text += ev.get("c", "")
            err = ev.get("error", err)
    check(f"assistant replies ({round(time.time() - t0)}s)", s == 200 and text and not err, err or s)
    print("      Kiko:", text.strip()[:300])

print("\nall checks passed" if not failures else f"\n{failures} check(s) failed")
sys.exit(1 if failures else 0)
