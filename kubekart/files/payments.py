"""payments: the demo payment gateway.

No real money moves and no card details are ever collected: a charge just
records a ledger entry and succeeds (or fails, when the caller asks for a
simulated decline). Only other KubeKart services can call it; the gateway
does not route to it.
"""
import secrets
import time

from lib import DB, ApiError, Handler, serve

db = DB("payments", """
CREATE TABLE IF NOT EXISTS charges (
  id TEXT PRIMARY KEY, user_id INTEGER, amount_cents INTEGER, currency TEXT,
  description TEXT, status TEXT, created REAL);
""")
MAX_CHARGE = 1_000_000  # €10,000


def charge(h):
    h.require_internal()
    b = h.body()
    amount = b.get("amount_cents")
    if not isinstance(amount, int) or not 0 < amount <= MAX_CHARGE:
        raise ApiError(400, "invalid amount")
    status = "declined" if b.get("simulate_decline") else "succeeded"
    row = {"id": "pay_" + secrets.token_hex(8), "user_id": b.get("user_id"), "amount_cents": amount,
           "currency": b.get("currency", "EUR"), "description": str(b.get("description", ""))[:200],
           "status": status, "created": time.time()}
    db.q("INSERT INTO charges VALUES (:id,:user_id,:amount_cents,:currency,:description,:status,:created)", row)
    if status == "declined":
        return 402, {**row, "error": "Payment declined by the demo gateway (simulated)."}
    return 201, row


def list_charges(h):
    h.require_internal()
    return 200, {"charges": db.q("SELECT * FROM charges WHERE user_id=? ORDER BY created DESC LIMIT 50",
                                 (h.query.get("user_id"),))}


class PaymentsHandler(Handler):
    routes = {("POST", "/charges"): charge, ("GET", "/charges"): list_charges}


if __name__ == "__main__":
    serve(PaymentsHandler)
