"""Generieke voorraad-bronnen voor webshops met een PUBLIEKE product-API.

Twee platforms dekken de meeste kleine TCG-shops:
- Shopify   -> GET /products.json?limit=250&page=N   (publiek, bedoeld om op te halen)
- WooCommerce Store API -> GET /wp-json/wc/store/v1/products?per_page=100&page=N

Geen scraping-trucs, geen headless browser, geen proxies. Elke fetch levert
genormaliseerde producten:
    {retailer, ean (= stabiele shop-sleutel), name, price, in_stock, listed, url}
zodat ze door dezelfde diff/events/Discord-pijplijn kunnen als de bol-bron.
"""

import html
import re

HTTP_TIMEOUT = 20
MAX_PAGES = 5  # beleefd: max ~1250 (Shopify) / 500 (Woo) producten per shop

# Alleen echte Pokémon sealed-producten, geen accessoires/singles.
ACCESSORY = ["case", "acryl", "hoes", "sleeve", "protector", "portfolio", "binder",
             "toploader", "deck box", "stand", "playmat", "dobbel", "sorter", "album",
             "map ", "single", "losse kaart"]
SEALED = ["booster box", "booster bundle", "elite trainer", "etb", "booster pack",
          "boosterpack", "boosterbox", "tin", "premium collection", "collection box",
          "blister", "display", "build & battle", "surprise box", "ultra premium"]


def is_pokemon_sealed(title):
    t = (title or "").lower()
    if any(k in t for k in ACCESSORY):
        return False
    if "pok" not in t and "pokemon" not in t and "pokémon" not in t:
        # shop kan Pokémon-only zijn; dan mag de naam de merknaam missen —
        # maar eis dan wel een duidelijk sealed-type om ruis te weren.
        return any(k in t for k in SEALED)
    return any(k in t for k in SEALED)


def _clean(name):
    return html.unescape(re.sub(r"\s+", " ", str(name or "")).strip())


def fetch_shopify(base, retailer, session):
    """Shopify /products.json -> genormaliseerde producten."""
    base = base.rstrip("/")
    out = []
    for page in range(1, MAX_PAGES + 1):
        r = session.get(base + "/products.json", params={"limit": 250, "page": page}, timeout=HTTP_TIMEOUT)
        if r.status_code != 200 or "json" not in r.headers.get("content-type", ""):
            break
        products = (r.json() or {}).get("products") or []
        if not products:
            break
        for p in products:
            variants = p.get("variants") or []
            in_stock = any(v.get("available") for v in variants)
            price = None
            for v in variants:
                try:
                    price = float(v.get("price"))
                    break
                except (TypeError, ValueError):
                    continue
            handle = p.get("handle") or str(p.get("id"))
            out.append({
                "retailer": retailer, "ean": handle, "name": _clean(p.get("title")),
                "price": price, "in_stock": bool(in_stock), "listed": True,
                "url": base + "/products/" + handle,
            })
        if len(products) < 250:
            break
    return out


def fetch_woo(base, retailer, session):
    """WooCommerce Store API -> genormaliseerde producten."""
    base = base.rstrip("/")
    out = []
    for page in range(1, MAX_PAGES + 1):
        r = session.get(base + "/wp-json/wc/store/v1/products",
                        params={"per_page": 100, "page": page}, timeout=HTTP_TIMEOUT)
        if r.status_code != 200 or "json" not in r.headers.get("content-type", ""):
            break
        products = r.json() or []
        if not isinstance(products, list) or not products:
            break
        for p in products:
            prices = p.get("prices") or {}
            price = None
            raw = prices.get("price")
            if raw is not None:
                try:
                    minor = int(prices.get("currency_minor_unit", 2))
                    price = int(raw) / (10 ** minor)
                except (TypeError, ValueError):
                    price = None
            out.append({
                "retailer": retailer, "ean": str(p.get("id")), "name": _clean(p.get("name")),
                "price": price, "in_stock": bool(p.get("is_in_stock")), "listed": True,
                "url": p.get("permalink") or base,
            })
        if len(products) < 100:
            break
    return out


FETCHERS = {"shopify": fetch_shopify, "woo": fetch_woo}


def fetch_shop(shop, session):
    """shop = {domain, platform, retailer}. Geeft gefilterde Pokémon-sealed-producten."""
    fetcher = FETCHERS.get(shop.get("platform"))
    if not fetcher:
        return []
    base = "https://www." + shop["domain"] if not shop["domain"].startswith("http") else shop["domain"]
    retailer = shop.get("retailer") or shop["domain"]
    products = fetcher(base, retailer, session)
    return [p for p in products if is_pokemon_sealed(p["name"])]
