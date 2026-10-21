#!/usr/bin/env python3
"""End-to-end smoke test for KubeBank, through the web gateway.

    python3 tests/kubebank_smoke.py http://localhost:8080 [--ai]

Creates two throwaway customers (random emails and passwords, generated here and
never printed), then checks seeded accounts, insights, own-account moves, a
payment between customers, a fraud hold with confirm, overdraft and double-pay
protection, card freeze, goals, and that the fraud service and internal APIs
aren't publicly routable. --ai also asks Penny one real question (slow on CPU).
"""
import json
import secrets
import sys
import time
import urllib.error
import urllib.request

BASE = sys.argv[1].rstrip("/") if len(sys.argv) > 1 else "http://localhost:8080"
failures = 0


def api(path, method="GET", body=None, token=None, raw=False, timeout=30):
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


def customer(name):
    email = f"smoke-{secrets.token_hex(4)}@example.test"
    s, r = api("/api/users/register", "POST", {"email": email, "name": name, "password": secrets.token_urlsafe(12)})
    check(f"register {name}", s == 201 and r.get("token"), r)
    return email, r.get("token")


s, st = api("/api/status")
check("all services healthy", s == 200 and st.get("ok"), st)
s, r = api("/api/fraud/score", "POST", {"amount_cents": 100})
check("fraud service is not routable from outside", s == 404, s)
s, r = api("/api/accounts/internal/summary?user_id=1")
check("internal summary needs the service key", s == 403, s)

alice_email, alice = customer("Alice Smoke")
bob_email, bob = customer("Bob Smoke")
s, ov = api("/api/accounts/overview", token=alice)
kinds = [a["kind"] for a in ov.get("accounts", [])]
check("accounts opened and seeded", s == 200 and kinds[:2] == ["checking", "savings"] and "goal" in kinds and ov["recent"], kinds)
acc = {a["kind"]: a for a in ov["accounts"]}
chk, sav = acc["checking"], acc["savings"]
s, tx = api("/api/accounts/transactions?limit=300", token=alice)
for a in ov["accounts"]:
    posted = sum(t["amount_cents"] for t in tx["transactions"] if t["account_id"] == a["id"] and t["status"] == "posted")
    check(f"{a['name']} balance matches its transactions", posted == a["balance_cents"], (posted, a["balance_cents"]))
s, ins = api("/api/accounts/insights", token=alice)
check("insights", s == 200 and ins["last_30_days"]["total"] > 0 and len(ins["last_30_days"]["daily"]) == 30, s)

s, r = api("/api/accounts/transfers", "POST", {"from_account": chk["id"], "to_account": sav["id"], "amount_cents": 1234}, token=alice)
s2, ov2 = api("/api/accounts/overview", token=alice)
b2 = {a["id"]: a["balance_cents"] for a in ov2["accounts"]}
check("move between own accounts", s == 201 and b2[chk["id"]] == chk["balance_cents"] - 1234 and b2[sav["id"]] == sav["balance_cents"] + 1234, r)
s, r = api("/api/accounts/transfers", "POST", {"from_account": sav["id"], "to_account": chk["id"], "amount_cents": 10_000_00}, token=alice)
check("can't overdraw", s in (400, 409), (s, r))
s, r = api("/api/accounts/transfers", "POST", {"from_account": chk["id"], "to_account": chk["id"], "amount_cents": 100}, token=alice)
check("same-account move rejected", s == 400, s)
s, r = api("/api/accounts/transfers", "POST", {"from_account": "acc_notmine", "to_account": chk["id"], "amount_cents": 100}, token=bob)
check("can't move from someone else's account", s == 404, s)

# Paying a brand-new payee a small amount: new payee alone (20 points) is allowed.
s, bob_ov = api("/api/accounts/overview", token=bob)
bob_chk = next(a for a in bob_ov["accounts"] if a["kind"] == "checking")
s, r = api("/api/accounts/transfers", "POST", {"from_account": chk["id"], "to_email": bob_email, "amount_cents": 2500, "note": "lunch"}, token=alice)
check("pay another customer", s == 201 and r["status"] == "done", (s, r))
s, bob_ov2 = api("/api/accounts/overview", token=bob)
got = next(a for a in bob_ov2["accounts"] if a["id"] == bob_chk["id"])["balance_cents"] - bob_chk["balance_cents"]
check("payee credited", got == 2500, got)

s, r = api("/api/accounts/transfers", "POST", {"from_account": chk["id"], "to_email": bob_email, "amount_cents": 50_000,
                                               "note": "URGENT gift card fee"}, token=alice)
check("scammy payment held by fraud checks", s == 202 and r["status"] == "held" and r["reasons"], (s, r))
held = r.get("id")
s, r = api(f"/api/accounts/transfers/{held}/confirm", "POST", {}, token=bob)
check("someone else can't confirm it", s == 404, s)
s, r = api(f"/api/accounts/transfers/{held}/confirm", "POST", {}, token=alice)
check("confirm held payment", s == 200 and r["status"] == "done", (s, r))
s, r = api(f"/api/accounts/transfers/{held}/confirm", "POST", {}, token=alice)
check("can't confirm twice", s == 404, s)
s, r = api("/api/accounts/transfers", "POST", {"from_account": chk["id"], "to_email": "nobody@example.test", "amount_cents": 100}, token=alice)
check("unknown payee", s == 404, s)

s, r = api("/api/accounts/card/freeze", "POST", {"frozen": True}, token=alice)
s, r = api("/api/accounts/card/purchase", "POST", {}, token=alice)
check("frozen card declines", s == 402 and r["reason"] == "card is frozen", (s, r))
api("/api/accounts/card/freeze", "POST", {"frozen": False}, token=alice)
s, r = api("/api/accounts/card/purchase", "POST", {}, token=alice)
check("card purchase approved", s == 201, (s, r))
s, r = api("/api/accounts/card/purchase", "POST", {"risky": True}, token=alice)
check("risky purchase blocked by fraud", s == 402 and r["reason"] == "blocked by fraud checks", (s, r))

s, r = api("/api/accounts/goals", "POST", {"name": "Smoke laptop", "emoji": "💻", "target_cents": 90000}, token=alice)
check("create goal", s == 201, r)
gid = r.get("id")
api("/api/accounts/transfers", "POST", {"from_account": chk["id"], "to_account": gid, "amount_cents": 4000}, token=alice)
s, r = api(f"/api/accounts/goals/{gid}/close", "POST", {}, token=alice)
check("close goal returns its money", s == 200 and r["returned_cents"] == 4000, r)

s, i = api("/api/advisor/info", token=alice)
check("advisor reports its SUSE AI model", s == 200 and i.get("model"), i)
s, r = api("/api/advisor/ask", "POST", {"mode": "nope"}, token=alice)
check("advisor rejects unknown mode", s == 400, s)
s, r = api("/api/advisor/ask", "POST", {"mode": "month"})
check("advisor needs sign-in", s == 401, s)

if "--ai" in sys.argv:
    t0 = time.time()
    s, resp = api("/api/advisor/ask", "POST", {"mode": "transfer", "id": held}, token=alice, raw=True, timeout=900)
    text, err = "", None
    if s == 200:
        for line in resp:
            ev = json.loads(line)
            text += ev.get("c", "")
            err = ev.get("error", err)
    check(f"Penny explains the held payment ({round(time.time() - t0)}s)", s == 200 and text and not err, err or s)
    print("      Penny:", text.strip()[:400])

print("\nall checks passed" if not failures else f"\n{failures} check(s) failed")
sys.exit(1 if failures else 0)
