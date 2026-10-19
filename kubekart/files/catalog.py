"""catalog: the product list. Stateless; Plus members see a 10% member price."""
from lib import ApiError, Handler, serve

MEMBER_DISCOUNT = 0.10

PRODUCTS = [
    {"id": "kk-hoodie", "name": "Chameleon Hoodie", "emoji": "🦎", "category": "apparel", "price_cents": 4900,
     "description": "Heavyweight organic cotton hoodie. Changes colour with your mood (it doesn't, but it's very green)."},
    {"id": "kk-tee", "name": "Pod Life Tee", "emoji": "👕", "category": "apparel", "price_cents": 2400,
     "description": "Soft tee for people who restart, but never give up."},
    {"id": "kk-socks", "name": "CrashLoop Socks", "emoji": "🧦", "category": "apparel", "price_cents": 1200,
     "description": "One always goes missing, then comes back. Again. And again."},
    {"id": "kk-cap", "name": "Helm Cap", "emoji": "🧢", "category": "apparel", "price_cents": 2200,
     "description": "Embroidered cap. Upgrades your look, rolls back on request."},
    {"id": "kk-mug", "name": "kubectl Coffee Mug", "emoji": "☕", "category": "desk", "price_cents": 1600,
     "description": "350 ml ceramic mug. `kubectl apply -f coffee.yaml` every morning."},
    {"id": "kk-keyboard", "name": "Control Plane Keyboard", "emoji": "⌨️", "category": "desk", "price_cents": 12900,
     "description": "Hot-swappable mechanical keyboard with tactile switches and a highly available Enter key."},
    {"id": "kk-lamp", "name": "Ingress Desk Lamp", "emoji": "💡", "category": "desk", "price_cents": 5900,
     "description": "Dimmable LED lamp that routes light exactly where you need it."},
    {"id": "kk-plant", "name": "Sidecar Succulent", "emoji": "🪴", "category": "desk", "price_cents": 1800,
     "description": "A low-maintenance companion that lives right next to your main container."},
    {"id": "kk-stickers", "name": "Sticker Cluster (20 pack)", "emoji": "🏷️", "category": "accessories", "price_cents": 800,
     "description": "Twenty vinyl stickers to scale your laptop horizontally."},
    {"id": "kk-bottle", "name": "Persistent Volume Bottle", "emoji": "🧴", "category": "accessories", "price_cents": 2600,
     "description": "Insulated steel bottle. Keeps state (and drinks) cold for 24 hours."},
    {"id": "kk-backpack", "name": "Multi-Node Backpack", "emoji": "🎒", "category": "accessories", "price_cents": 8900,
     "description": "28 L backpack with a padded laptop bay and enough compartments to schedule anything."},
    {"id": "kk-duck", "name": "Rubber Duck Debugger", "emoji": "🦆", "category": "accessories", "price_cents": 1400,
     "description": "Explain your YAML to the duck. The duck understands."},
]
BY_ID = {p["id"]: p for p in PRODUCTS}


def priced(p):
    return {**p, "member_price_cents": round(p["price_cents"] * (1 - MEMBER_DISCOUNT))}


def list_products(h):
    q, cat = h.query.get("q", "").lower(), h.query.get("category", "")
    items = [priced(p) for p in PRODUCTS
             if (not cat or p["category"] == cat)
             and (not q or q in p["name"].lower() or q in p["description"].lower())]
    return 200, {"products": items, "member_discount": MEMBER_DISCOUNT}


def get_product(h, pid):
    if pid not in BY_ID:
        raise ApiError(404, "no such product")
    return 200, priced(BY_ID[pid])


def categories(h):
    return 200, {"categories": sorted({p["category"] for p in PRODUCTS})}


class CatalogHandler(Handler):
    routes = {
        ("GET", "/products"): list_products,
        ("GET", "/products/*"): get_product,
        ("GET", "/categories"): categories,
    }


if __name__ == "__main__":
    serve(CatalogHandler)
