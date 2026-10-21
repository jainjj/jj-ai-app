"""accounts: the ledger. Accounts, balances, transactions, transfers, savings goals and the virtual card.

All money is play money in integer cents. A customer's accounts open on their
first visit, seeded with two months of realistic demo activity so insights and
the AI advisor have something to talk about.
"""
import calendar
import json
import random
import secrets
import time
import urllib.parse

from lib import DB, ApiError, Handler, call, serve, url_of

MAX_TRANSFER = 1_000_000  # €10,000
MAX_GOALS = 8
SPEND_SKIP = ("Income", "Transfers")  # not spending

db = DB("accounts", """
CREATE TABLE IF NOT EXISTS accounts (
  id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, kind TEXT NOT NULL, name TEXT NOT NULL, emoji TEXT,
  iban TEXT, balance_cents INTEGER NOT NULL DEFAULT 0, target_cents INTEGER, closed INTEGER NOT NULL DEFAULT 0,
  created REAL NOT NULL);
CREATE INDEX IF NOT EXISTS acc_user ON accounts(user_id);
CREATE TABLE IF NOT EXISTS txns (
  id TEXT PRIMARY KEY, account_id TEXT NOT NULL, user_id INTEGER NOT NULL, amount_cents INTEGER NOT NULL,
  merchant TEXT NOT NULL, category TEXT NOT NULL, note TEXT, status TEXT NOT NULL DEFAULT 'posted',
  created REAL NOT NULL);
CREATE INDEX IF NOT EXISTS txn_user ON txns(user_id, created);
CREATE TABLE IF NOT EXISTS transfers (
  id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, from_acc TEXT NOT NULL, to_acc TEXT, to_user INTEGER,
  payee TEXT, amount_cents INTEGER NOT NULL, note TEXT, status TEXT NOT NULL, score INTEGER, reasons TEXT,
  created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS cards (user_id INTEGER PRIMARY KEY, last4 TEXT, frozen INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS payees (user_id INTEGER, payee_user INTEGER, PRIMARY KEY (user_id, payee_user));
""")


def new_id(prefix):
    return f"{prefix}_{secrets.token_hex(6)}"


# ---------------------------------------------------------------- demo data
RECURRING = {  # day of month: (merchant, category, euros, jitter)
    1: ("Harbour Lettings · Rent", "Housing", -1150, 0),
    3: ("Nimbus Mobile", "Bills", -25, 0),
    5: ("Kubeflix", "Subscriptions", -12.99, 0),
    8: ("Spotty Music", "Subscriptions", -9.99, 0),
    10: ("Pod Power Energy", "Bills", -85, 10),
    12: ("Geeko Gym", "Health", -35, 0),
    18: ("CloudBox Storage", "Subscriptions", -2.99, 0),
    25: ("Acme Cloud Ltd · Salary", "Income", 3200, 0),
}
DAILY = [  # (probability, merchants, category, min €, max €)
    (0.55, ["Geeko Coffee", "The Daily Grind", "Bean There Café"], "Coffee", 2.9, 5.6),
    (0.30, ["Pod Pantry", "FreshMart", "Green Basket Market"], "Groceries", 14, 96),
    (0.15, ["Rancher's Grill", "Sushi Sidecar", "Pizza Operator", "Taco Daemon"], "Eating out", 14, 64),
    (0.28, ["Node Express Rail", "MetroLink", "CityBike"], "Transport", 2.8, 14),
    (0.07, ["Helm & Co Outfitters", "Bytes Electronics", "YAML Books", "Container Store"], "Shopping", 15, 180),
    (0.06, ["Cluster Cinema", "Arcade 404", "Live at the Lighthouse"], "Entertainment", 11, 48),
    (0.04, ["Taxi Lambda"], "Transport", 12, 38),
]
CARD_MERCHANTS = [
    ("Geeko Coffee", "Coffee", 3, 6, False), ("Pod Pantry", "Groceries", 12, 70, False),
    ("Sushi Sidecar", "Eating out", 18, 55, False), ("Bytes Electronics", "Shopping", 25, 240, False),
    ("Node Express Rail", "Transport", 3, 15, False), ("Cluster Cinema", "Entertainment", 11, 26, False),
]
RISKY_MERCHANTS = [
    ("CRYPTO-XCHG 24/7 (overseas)", "Shopping", 480, 950, True),
    ("GIFTCARD WHOLESALE LTD (overseas)", "Shopping", 300, 700, True),
    ("PRIZE-CLAIM FEES (overseas)", "Shopping", 150, 400, True),
]


def cents(euros):
    return int(round(euros * 100))


def seed(conn, uid, name):
    """Open checking, savings and one goal, with 60 days of believable history."""
    rng, now = random.Random(), time.time()
    first = name.split()[0]
    chk, sav, goal = new_id("acc"), new_id("acc"), new_id("acc")
    iban = lambda: "KB42 " + " ".join(f"{rng.randrange(10000):04d}" for _ in range(3))  # noqa: E731
    events = {chk: [], sav: [], goal: []}  # account -> [(ts, cents, merchant, category, note)]

    for d in range(60, 0, -1):
        day = now - d * 86400
        mday = time.gmtime(day).tm_mday
        if mday in RECURRING:
            m, cat, eur, jit = RECURRING[mday]
            events[chk].append((day + 6 * 3600, cents(eur + rng.uniform(-jit, jit)), m, cat, None))
        if mday == 1:
            events[sav].append((day + 3 * 3600, cents(rng.uniform(4, 7)), "Interest", "Income", "2.1% AER"))
        if mday == 26:
            events[chk].append((day + 9 * 3600, -30000, "To Savings", "Transfers", "monthly save"))
            events[sav].append((day + 9 * 3600, 30000, "From Checking", "Transfers", "monthly save"))
            events[chk].append((day + 9 * 3600 + 60, -5000, f"To {first}'s Holiday fund", "Transfers", None))
            events[goal].append((day + 9 * 3600 + 60, 5000, "From Checking", "Transfers", None))
        for p, merchants, cat, lo, hi in DAILY:
            if rng.random() < p:
                when = day + rng.uniform(7, 22) * 3600
                events[chk].append((when, -cents(rng.uniform(lo, hi)), rng.choice(merchants), cat, None))

    opening = {chk: max(cents(rng.uniform(1900, 2600)) - sum(e[1] for e in events[chk]), 80000),
               sav: cents(rng.choice([2500, 3000, 4200])), goal: 32000}
    rows = [(chk, "checking", "Everyday", "💳", None), (sav, "savings", "Savings", "🏦", None),
            (goal, "goal", "Holiday fund", "🏝️", 150000)]
    for acc, kind, label, emoji, target in rows:
        bal = opening[acc] + sum(e[1] for e in events[acc])
        conn.execute("INSERT INTO accounts (id,user_id,kind,name,emoji,iban,balance_cents,target_cents,created) "
                     "VALUES (?,?,?,?,?,?,?,?,?)", (acc, uid, kind, label, emoji, iban(), bal, target, now - 61 * 86400))
        evs = [(now - 61 * 86400, opening[acc], "Welcome to KubeBank", "Transfers", "opening balance")] + events[acc]
        conn.executemany("INSERT INTO txns (id,account_id,user_id,amount_cents,merchant,category,note,created) "
                         "VALUES (?,?,?,?,?,?,?,?)", [(new_id("tx"), acc, uid, c, m, cat, n, ts) for ts, c, m, cat, n in evs])
    conn.execute("INSERT INTO cards (user_id,last4,frozen) VALUES (?,?,0)", (uid, f"{rng.randrange(10000):04d}"))


def ensure(uid, name):
    if db.one("SELECT 1 FROM accounts WHERE user_id=?", (uid,)):
        return
    with db.tx() as conn:  # re-check under the lock so two first visits can't both seed
        if not conn.execute("SELECT 1 FROM accounts WHERE user_id=?", (uid,)).fetchone():
            seed(conn, uid, name)


def me(h):
    c = h.user()
    ensure(c["sub"], c["name"])
    return c


# ---------------------------------------------------------------- reads
def accounts_of(uid):
    return db.q("SELECT id,kind,name,emoji,iban,balance_cents,target_cents FROM accounts "
                "WHERE user_id=? AND closed=0 ORDER BY CASE kind WHEN 'checking' THEN 0 WHEN 'savings' THEN 1 ELSE 2 END, created",
                (uid,))


def own_account(uid, acc_id, kinds=None):
    a = db.one("SELECT * FROM accounts WHERE id=? AND user_id=? AND closed=0", (acc_id, uid))
    if not a or (kinds and a["kind"] not in kinds):
        raise ApiError(404, "no such account")
    return a


def txns_of(uid, limit=50, account=None, search=None, since=None):
    sql, args = "SELECT t.*, a.name AS account FROM txns t JOIN accounts a ON a.id=t.account_id WHERE t.user_id=?", [uid]
    if account:
        sql += " AND t.account_id=?"
        args.append(account)
    if search:
        sql += " AND (t.merchant LIKE ? OR t.category LIKE ? OR t.note LIKE ?)"
        args += [f"%{search}%"] * 3
    if since:
        sql += " AND t.created>=?"
        args.append(since)
    return db.q(sql + " ORDER BY t.created DESC LIMIT ?", args + [limit])


def month_start(offset=0):
    t = time.gmtime()
    y, m = t.tm_year, t.tm_mon - offset
    while m < 1:
        y, m = y - 1, m + 12
    return calendar.timegm((y, m, 1, 0, 0, 0))


def insights_of(uid):
    now = time.time()
    rows = db.q("SELECT amount_cents,merchant,category,created FROM txns WHERE user_id=? AND status='posted' "
                "AND created>=?", (uid, month_start(1)))
    this_m, last_m = month_start(0), month_start(1)
    spend = [r for r in rows if r["amount_cents"] < 0 and r["category"] not in SPEND_SKIP]

    def by_cat(rs):
        out = {}
        for r in rs:
            out[r["category"]] = out.get(r["category"], 0) - r["amount_cents"]
        return dict(sorted(out.items(), key=lambda kv: -kv[1]))

    cur = [r for r in spend if r["created"] >= this_m]
    prev = [r for r in spend if last_m <= r["created"] < this_m]
    last30 = [r for r in spend if r["created"] >= now - 30 * 86400]
    daily = [0] * 30
    for r in last30:
        if r["category"] != "Housing":  # rent would dwarf every other day on the chart
            daily[min(29, int((now - r["created"]) // 86400))] -= r["amount_cents"]
    merchants = {}
    for r in last30:
        merchants[r["merchant"]] = merchants.get(r["merchant"], 0) - r["amount_cents"]
    recurring = sorted({r["merchant"] for r in spend if r["category"] in ("Subscriptions", "Bills", "Housing", "Health")})
    income30 = sum(r["amount_cents"] for r in rows if r["category"] == "Income" and r["created"] >= now - 30 * 86400)
    return {
        "this_month": {"total": sum(by_cat(cur).values()), "by_category": by_cat(cur), "day": time.gmtime().tm_mday},
        "last_month": {"total": sum(by_cat(prev).values()), "by_category": by_cat(prev)},
        "last_30_days": {"total": sum(-r["amount_cents"] for r in last30), "income": income30, "by_category": by_cat(last30),
                         "daily": daily[::-1], "top_merchants": sorted(merchants.items(), key=lambda kv: -kv[1])[:6]},
        "recurring": recurring,
    }


def overview(h):
    c = me(h)
    accs = accounts_of(c["sub"])
    card = db.one("SELECT last4, frozen FROM cards WHERE user_id=?", (c["sub"],))
    held = db.q("SELECT id,payee,amount_cents,note,score,reasons,created FROM transfers WHERE user_id=? AND status='held' "
                "ORDER BY created DESC", (c["sub"],))
    for t in held:
        t["reasons"] = json.loads(t["reasons"] or "[]")
    return 200, {"name": c["name"], "accounts": accs, "total_cents": sum(a["balance_cents"] for a in accs),
                 "card": {"last4": card["last4"], "frozen": bool(card["frozen"])}, "held": held,
                 "recent": txns_of(c["sub"], 8)}


def transactions(h):
    c, q = me(h), h.query
    return 200, {"transactions": txns_of(c["sub"], min(int(q.get("limit", 100) or 100), 300),
                                         q.get("account") or None, (q.get("q") or "").strip()[:60] or None)}


def insights(h):
    return 200, insights_of(me(h)["sub"])


# ---------------------------------------------------------------- moving money
def post_move(conn, uid, from_acc, amount, out_label, note, credit=None):
    """Debit from_acc (never below zero) and optionally credit (acc_id, user_id, in_label). Inside db.tx()."""
    now = time.time()
    cur = conn.execute("UPDATE accounts SET balance_cents=balance_cents-? WHERE id=? AND balance_cents>=? AND closed=0",
                       (amount, from_acc, amount))
    if cur.rowcount != 1:
        raise ApiError(409, "not enough money in that account")
    conn.execute("INSERT INTO txns (id,account_id,user_id,amount_cents,merchant,category,note,created) VALUES (?,?,?,?,?,?,?,?)",
                 (new_id("tx"), from_acc, uid, -amount, out_label, "Transfers", note, now))
    if credit:
        acc, to_uid, in_label = credit
        conn.execute("UPDATE accounts SET balance_cents=balance_cents+? WHERE id=?", (amount, acc))
        conn.execute("INSERT INTO txns (id,account_id,user_id,amount_cents,merchant,category,note,created) VALUES (?,?,?,?,?,?,?,?)",
                     (new_id("tx"), acc, to_uid, amount, in_label, "Transfers", note, now))


def avg_out(uid):
    r = db.one("SELECT AVG(-amount_cents) AS a FROM txns WHERE user_id=? AND amount_cents<0 AND status='posted' "
               "AND category NOT IN ('Transfers','Housing') AND created>=?", (uid, time.time() - 90 * 86400))
    return int(r["a"] or 0)


def execute_external(t, sender_name):
    """Pay another customer: debit the sender, credit the payee's everyday account."""
    payee_acc = db.one("SELECT id FROM accounts WHERE user_id=? AND kind='checking' AND closed=0", (t["to_user"],))
    with db.tx() as conn:
        # Claim the transfer first, so a double-clicked confirm can't pay twice.
        if conn.execute("UPDATE transfers SET status='done' WHERE id=? AND status IN ('held','pending')",
                        (t["id"],)).rowcount != 1:
            raise ApiError(409, "that payment was already handled")
        post_move(conn, t["user_id"], t["from_acc"], t["amount_cents"], f"To {t['payee']}", t["note"],
                  credit=(payee_acc["id"], t["to_user"], f"From {sender_name}"))
        conn.execute("INSERT OR IGNORE INTO payees VALUES (?,?)", (t["user_id"], t["to_user"]))


def create_transfer(h):
    c, b = me(h), h.body()
    uid = c["sub"]
    amount, note = b.get("amount_cents"), str(b.get("note") or "").strip()[:140]
    if not isinstance(amount, int) or not 0 < amount <= MAX_TRANSFER:
        raise ApiError(400, "enter an amount between €0.01 and €10,000")
    src = own_account(uid, str(b.get("from_account", "")))
    if src["balance_cents"] < amount:
        raise ApiError(409, "not enough money in that account")

    if b.get("to_account"):  # between the customer's own accounts: instant, nothing to score
        dst = own_account(uid, str(b["to_account"]))
        if dst["id"] == src["id"]:
            raise ApiError(400, "pick two different accounts")
        with db.tx() as conn:
            post_move(conn, uid, src["id"], amount, f"To {dst['name']}", note, credit=(dst["id"], uid, f"From {src['name']}"))
        return 201, {"status": "done", "amount_cents": amount}

    email = str(b.get("to_email", "")).strip().lower()
    if not email:
        raise ApiError(400, "choose an account or enter the payee's email")
    if email == c["email"].lower():
        raise ApiError(400, "that's you! Use 'between my accounts' instead")
    status, payee = call(url_of("users") + "/internal/lookup?email=" + urllib.parse.quote(email), internal=True)
    if status != 200:
        raise ApiError(404 if status == 404 else 502, payee.get("error", "couldn't look up that payee"))
    ensure(payee["id"], payee["name"])

    recent = db.one("SELECT COUNT(*) AS n FROM transfers WHERE user_id=? AND to_user IS NOT NULL AND created>=?",
                    (uid, time.time() - 600))["n"]
    known = db.one("SELECT 1 FROM payees WHERE user_id=? AND payee_user=?", (uid, payee["id"]))
    s, risk = call(url_of("fraud") + "/score", "POST", internal=True, body={
        "kind": "transfer", "amount_cents": amount, "balance_cents": src["balance_cents"], "avg_out_cents": avg_out(uid),
        "new_payee": not known, "recent_count": recent, "note": note})
    if s != 200:  # fail safe: if fraud checks are down, hold rather than wave payments through
        risk = {"score": None, "action": "review", "reasons": ["our fraud checks are unavailable right now"]}

    t = {"id": new_id("tr"), "user_id": uid, "from_acc": src["id"], "to_user": payee["id"], "payee": payee["name"],
         "amount_cents": amount, "note": note, "status": "held" if risk["action"] != "allow" else "pending",
         "score": risk["score"], "reasons": json.dumps(risk["reasons"]), "created": time.time()}
    db.q("INSERT INTO transfers (id,user_id,from_acc,to_user,payee,amount_cents,note,status,score,reasons,created) "
         "VALUES (:id,:user_id,:from_acc,:to_user,:payee,:amount_cents,:note,:status,:score,:reasons,:created)", t)
    if t["status"] == "held":
        return 202, {"status": "held", "id": t["id"], "payee": payee["name"], "amount_cents": amount,
                     "score": risk["score"], "reasons": risk["reasons"]}
    try:
        execute_external(t, c["name"])
    except ApiError:
        db.q("UPDATE transfers SET status='failed' WHERE id=?", (t["id"],))
        raise
    return 201, {"status": "done", "id": t["id"], "payee": payee["name"], "amount_cents": amount, "score": risk["score"]}


def transfer_action(h, rest):
    c = me(h)
    tid, _, action = rest.partition("/")
    t = db.one("SELECT * FROM transfers WHERE id=? AND user_id=?", (tid, c["sub"]))
    if not t or t["status"] != "held":
        raise ApiError(404, "no held payment with that id")
    if action == "cancel":
        db.q("UPDATE transfers SET status='cancelled' WHERE id=?", (tid,))
        return 200, {"status": "cancelled"}
    if action != "confirm":
        raise ApiError(404, "not found")
    execute_external(t, c["name"])
    return 200, {"status": "done", "payee": t["payee"], "amount_cents": t["amount_cents"]}


def create_goal(h):
    c, b = me(h), h.body()
    name = str(b.get("name", "")).strip()[:40]
    target = b.get("target_cents")
    if not name:
        raise ApiError(400, "give your goal a name")
    if not isinstance(target, int) or not 100 <= target <= 10_000_000:
        raise ApiError(400, "set a target between €1 and €100,000")
    if db.one("SELECT COUNT(*) AS n FROM accounts WHERE user_id=? AND kind='goal' AND closed=0", (c["sub"],))["n"] >= MAX_GOALS:
        raise ApiError(409, f"you can have up to {MAX_GOALS} goals")
    acc = new_id("acc")
    db.q("INSERT INTO accounts (id,user_id,kind,name,emoji,balance_cents,target_cents,created) VALUES (?,?,?,?,?,0,?,?)",
         (acc, c["sub"], "goal", name, str(b.get("emoji") or "🎯")[:4], target, time.time()))
    return 201, {"id": acc}


def goal_action(h, rest):
    c = me(h)
    gid, _, action = rest.partition("/")
    if action != "close":
        raise ApiError(404, "not found")
    g = own_account(c["sub"], gid, ("goal",))
    chk = db.one("SELECT id FROM accounts WHERE user_id=? AND kind='checking' AND closed=0", (c["sub"],))
    with db.tx() as conn:
        if g["balance_cents"] > 0:
            post_move(conn, c["sub"], g["id"], g["balance_cents"], "To Everyday", "goal closed",
                      credit=(chk["id"], c["sub"], f"From {g['name']}"))
        conn.execute("UPDATE accounts SET closed=1 WHERE id=?", (g["id"],))
    return 200, {"status": "closed", "returned_cents": g["balance_cents"]}


def card_freeze(h):
    c, b = me(h), h.body()
    db.q("UPDATE cards SET frozen=? WHERE user_id=?", (1 if b.get("frozen") else 0, c["sub"]))
    return 200, {"frozen": bool(b.get("frozen"))}


def card_purchase(h):
    """Simulate a card payment, so the demo can show card controls and fraud declines."""
    c, b = me(h), h.body()
    uid = c["sub"]
    m, cat, lo, hi, foreign = random.choice(RISKY_MERCHANTS if b.get("risky") else CARD_MERCHANTS)
    amount = cents(random.uniform(lo, hi))
    chk = db.one("SELECT * FROM accounts WHERE user_id=? AND kind='checking' AND closed=0", (uid,))
    card = db.one("SELECT * FROM cards WHERE user_id=?", (uid,))

    def decline(why, extra=None):
        db.q("INSERT INTO txns (id,account_id,user_id,amount_cents,merchant,category,note,status,created) "
             "VALUES (?,?,?,?,?,?,?,'declined',?)", (new_id("tx"), chk["id"], uid, -amount, m, cat, why, time.time()))
        return 402, {"status": "declined", "merchant": m, "amount_cents": amount, "reason": why, **(extra or {})}

    if card["frozen"]:
        return decline("card is frozen")
    s, risk = call(url_of("fraud") + "/score", "POST", internal=True, body={
        "kind": "card", "amount_cents": amount, "balance_cents": chk["balance_cents"], "avg_out_cents": avg_out(uid),
        "merchant": m, "foreign": foreign})
    if s == 200 and risk["action"] == "decline":
        return decline("blocked by fraud checks", {"score": risk["score"], "reasons": risk["reasons"]})
    if chk["balance_cents"] < amount:
        return decline("insufficient funds")
    with db.tx() as conn:
        conn.execute("UPDATE accounts SET balance_cents=balance_cents-? WHERE id=?", (amount, chk["id"]))
        conn.execute("INSERT INTO txns (id,account_id,user_id,amount_cents,merchant,category,note,created) VALUES (?,?,?,?,?,?,?,?)",
                     (new_id("tx"), chk["id"], uid, -amount, m, cat, f"card ••{card['last4']}", time.time()))
    return 201, {"status": "approved", "merchant": m, "amount_cents": amount}


# ---------------------------------------------------------------- for the advisor
def internal_summary(h):
    """Everything the AI advisor may see about one customer, plus the item they asked about."""
    h.require_internal()
    q = h.query
    uid = int(q.get("user_id") or 0)
    if not db.one("SELECT 1 FROM accounts WHERE user_id=?", (uid,)):
        raise ApiError(404, "no accounts")
    out = {"accounts": accounts_of(uid), "insights": insights_of(uid), "recent": txns_of(uid, 25),
           "card": db.one("SELECT frozen FROM cards WHERE user_id=?", (uid,))}
    if q.get("txn"):
        out["txn"] = db.one("SELECT * FROM txns WHERE id=? AND user_id=?", (q["txn"], uid))
        if out["txn"]:
            out["txn_history"] = db.q("SELECT amount_cents,created,status FROM txns WHERE user_id=? AND merchant=? "
                                      "ORDER BY created DESC LIMIT 12", (uid, out["txn"]["merchant"]))
    if q.get("goal"):
        out["goal"] = db.one("SELECT * FROM accounts WHERE id=? AND user_id=? AND kind='goal'", (q["goal"], uid))
    if q.get("transfer"):
        t = db.one("SELECT * FROM transfers WHERE id=? AND user_id=?", (q["transfer"], uid))
        if t:
            t["reasons"] = json.loads(t["reasons"] or "[]")
        out["transfer"] = t
    return 200, out


class AccountsHandler(Handler):
    routes = {
        ("GET", "/overview"): overview,
        ("GET", "/transactions"): transactions,
        ("GET", "/insights"): insights,
        ("POST", "/transfers"): create_transfer,
        ("POST", "/transfers/*"): transfer_action,
        ("POST", "/goals"): create_goal,
        ("POST", "/goals/*"): goal_action,
        ("POST", "/card/freeze"): card_freeze,
        ("POST", "/card/purchase"): card_purchase,
        ("GET", "/internal/summary"): internal_summary,
    }


if __name__ == "__main__":
    serve(AccountsHandler)
