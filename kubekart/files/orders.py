"""orders: checkout and order history.

Prices come from catalog, the buyer's current tier from users, and the money
from the payments demo gateway, so the browser can't set any of them.
"""
import json
import secrets
import time

from lib import DB, ApiError, Handler, call, serve, url_of

SHIPPING_CENTS = 495
FREE_SHIPPING_FROM = 5000

db = DB("orders", """
CREATE TABLE IF NOT EXISTS orders (
  id TEXT PRIMARY KEY, user_id INTEGER, items TEXT, subtotal_cents INTEGER, discount_cents INTEGER,
  shipping_cents INTEGER, total_cents INTEGER, tier TEXT, payment_id TEXT, status TEXT, created REAL);
""")


def place(h):
    claims = h.user()
    b = h.body()
    wanted = b.get("items")
    if not isinstance(wanted, list) or not wanted or len(wanted) > 50:
        raise ApiError(400, "your cart is empty")

    status, cat = call(url_of("catalog") + "/products")
    if status != 200:
        raise ApiError(503, "the catalog is unavailable, try again shortly")
    status, user = call(url_of("users") + f"/internal/users/{claims['sub']}", internal=True)
    if status != 200:
        raise ApiError(503 if status >= 500 else 401, user.get("error", "can't check your account"))
    products, plus = {p["id"]: p for p in cat["products"]}, user["tier"] == "plus"

    lines, subtotal, member = [], 0, 0
    for it in wanted:
        p, qty = products.get(str(it.get("id"))), it.get("qty")
        if not p:
            raise ApiError(400, f"unknown product {it.get('id')!r}")
        if not isinstance(qty, int) or not 1 <= qty <= 10:
            raise ApiError(400, "quantities must be between 1 and 10")
        lines.append({"id": p["id"], "name": p["name"], "emoji": p["emoji"], "qty": qty,
                      "price_cents": p["price_cents"]})
        subtotal += p["price_cents"] * qty
        member += p["member_price_cents"] * qty
    discount = subtotal - member if plus else 0
    shipping = 0 if plus or subtotal - discount >= FREE_SHIPPING_FROM else SHIPPING_CENTS
    total = subtotal - discount + shipping

    oid = "ord_" + secrets.token_hex(6)
    status, pay = call(url_of("payments") + "/charges", "POST", internal=True, body={
        "user_id": user["id"], "amount_cents": total, "currency": "EUR",
        "description": f"KubeKart order {oid}", "simulate_decline": bool(b.get("simulate_decline"))})
    if status != 201:
        raise ApiError(402 if status == 402 else 502, pay.get("error", "payment failed"))

    order = {"id": oid, "user_id": user["id"], "items": lines, "subtotal_cents": subtotal,
             "discount_cents": discount, "shipping_cents": shipping, "total_cents": total,
             "tier": user["tier"], "payment_id": pay["id"], "status": "paid", "created": time.time()}
    db.q("INSERT INTO orders VALUES (:id,:user_id,:items,:subtotal_cents,:discount_cents,:shipping_cents,"
         ":total_cents,:tier,:payment_id,:status,:created)", {**order, "items": json.dumps(lines)})
    return 201, order


def history(h):
    rows = db.q("SELECT * FROM orders WHERE user_id=? ORDER BY created DESC LIMIT 50", (h.user()["sub"],))
    return 200, {"orders": [{**r, "items": json.loads(r["items"])} for r in rows]}


class OrdersHandler(Handler):
    routes = {("POST", "/orders"): place, ("GET", "/orders"): history}


if __name__ == "__main__":
    serve(OrdersHandler)
