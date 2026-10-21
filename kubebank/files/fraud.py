"""fraud: real-time risk scoring for outgoing payments and card purchases.

Rule-based on purpose: it sits on the payment path, so it must answer in
milliseconds. (The LLM is far too slow for that; the advisor uses it afterwards
to *explain* a hold to the customer.) Only other KubeBank services can call it.
"""
import time

from lib import ApiError, Handler, serve

REVIEW_AT = 50
SCAM_WORDS = ("gift card", "giftcard", "crypto", "bitcoin", "btc", "urgent", "investment", "customs",
              "fee to release", "lottery", "prize", "refund", "tax office", "wallet")


def score(h):
    h.require_internal()
    b = h.body()
    amount = int(b.get("amount_cents") or 0)
    balance = max(int(b.get("balance_cents") or 0), 1)
    avg_out = max(int(b.get("avg_out_cents") or 0), 1)
    text = f"{b.get('note', '')} {b.get('merchant', '')}".lower()
    hour = time.gmtime().tm_hour

    rules = []  # (points, reason)
    if amount >= balance * 0.5:
        rules.append((30, f"it would move {amount * 100 // balance}% of the account balance"))
    if amount >= avg_out * 5 and amount >= 20_000:
        rules.append((25, f"it is {amount // avg_out}× your usual payment size"))
    if b.get("new_payee"):
        rules.append((20, "you've never paid this payee before"))
    if int(b.get("recent_count") or 0) >= 3:
        rules.append((25, f"{b['recent_count']} payments in the last 10 minutes"))
    if hits := [w for w in SCAM_WORDS if w in text]:
        rules.append((35, f"the description mentions \"{hits[0]}\", common in scams"))
    if b.get("foreign"):
        rules.append((25, "the merchant is overseas and unfamiliar"))
    if hour < 5:
        rules.append((10, "it's the middle of the night (UTC)"))

    points = min(sum(p for p, _ in rules), 100)
    kind = b.get("kind", "transfer")
    # Card purchases can't be held, so a risky one is declined; transfers are held for the customer to confirm.
    action = "allow" if points < REVIEW_AT else "decline" if kind == "card" else "review"
    return 200, {"score": points, "action": action, "reasons": [r for _, r in sorted(rules, reverse=True)]}


class FraudHandler(Handler):
    routes = {("POST", "/score"): score}


if __name__ == "__main__":
    serve(FraudHandler)
