#!/usr/bin/env python3
"""Boltracker — checkt producten en schrijft status + gebeurtenissen naar Supabase.

Draai lokaal met:
    SUPABASE_URL=... SUPABASE_SERVICE_KEY=... python tracker/tracker.py

Of automatisch via GitHub Actions (zie .github/workflows/tracker.yml).

Let op: dit is een best-effort scraper van openbare productpagina's voor
persoonlijk gebruik. Bol kan de opmaak wijzigen of verzoeken blokkeren; houd het
interval rustig (elke paar minuten) en de watchlist klein.
"""

import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

try:
    from bol_api import BolRetailerClient, BolApiError, BolAuthError
    import shops
except ImportError:  # als module (python -m tracker.tracker)
    from tracker.bol_api import BolRetailerClient, BolApiError, BolAuthError
    from tracker import shops

# --------------------------------------------------------------------------- #
# Configuratie
# --------------------------------------------------------------------------- #
SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
WATCHLIST_PATH = Path(os.environ.get("WATCHLIST", Path(__file__).with_name("watchlist.json")))
SHOPS_PATH = Path(os.environ.get("SHOPS", Path(__file__).with_name("shops.json")))

# Officiële bol Retailer API (aanbevolen). Zonder credentials valt de tracker
# terug op best-effort HTML-scraping, die bij bol vaak "onbekend" oplevert.
BOL_CLIENT_ID = os.environ.get("BOL_CLIENT_ID", "")
BOL_CLIENT_SECRET = os.environ.get("BOL_CLIENT_SECRET", "")
BOL_DEMO = os.environ.get("BOL_DEMO", "") == "1"

# Discord-webhook voor alerts (optioneel). Leeg = geen Discord.
DISCORD_WEBHOOK = os.environ.get("DISCORD_WEBHOOK", "")
# Welke events een alert waard zijn (out_of_stock slaan we over — geen koopmoment).
DISCORD_NOTIFY_TYPES = {"bol_drop", "restock", "price_drop", "drop_signal"}
DISCORD_LABELS = {"bol_drop": "🔥 BOL DROP", "restock": "📦 Restock",
                  "price_drop": "📉 Prijsdaling", "drop_signal": "👀 Klaargezet"}
DISCORD_COLORS = {"bol_drop": 0xFF5D73, "restock": 0x84CC16,
                  "price_drop": 0x0FA3B1, "drop_signal": 0xFBBF24}

# Beleefde pauze tussen twee productverzoeken (seconden).
REQUEST_DELAY = float(os.environ.get("TRACKER_DELAY", "1.5"))
HTTP_TIMEOUT = 20

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0 Safari/537.36"
)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def log(msg: str) -> None:
    print(f"[{now_iso()}] {msg}", flush=True)


# --------------------------------------------------------------------------- #
# Scrapers per winkel
# --------------------------------------------------------------------------- #
def _iter_jsonld(html: str):
    """Levert alle geparste JSON-LD-blokken uit een HTML-pagina."""
    for block in re.findall(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html,
        re.S | re.I,
    ):
        try:
            data = json.loads(block.strip())
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(data, list):
            yield from data
        elif isinstance(data, dict):
            # Soms zit het product in een @graph-lijst.
            if isinstance(data.get("@graph"), list):
                yield from data["@graph"]
            yield data


def _product_from_jsonld(html: str):
    """Zoekt prijs + beschikbaarheid uit schema.org Product-data. Geeft (price, in_stock)."""
    price, in_stock = None, None
    for node in _iter_jsonld(html):
        types = node.get("@type")
        types = types if isinstance(types, list) else [types]
        if "Product" not in types:
            continue
        offers = node.get("offers") or {}
        if isinstance(offers, list):
            offers = offers[0] if offers else {}
        if offers.get("price") is not None:
            try:
                price = float(str(offers["price"]).replace(",", "."))
            except ValueError:
                pass
        avail = str(offers.get("availability", "")).lower()
        if avail:
            in_stock = "instock" in avail or "limitedavailability" in avail
    return price, in_stock


def scrape_bol(item, session):
    """Scrapet een bol.com-productpagina.

    Geeft dict met listed/in_stock/price/url. `in_stock` en `price` kunnen None
    ("onbekend") zijn: bol rendert prijs en voorraad pas client-side via
    JavaScript, dus de losse HTML-response bevat ze meestal niet. We verzinnen
    dan liever niets — None betekent "niet betrouwbaar bepaald". Voor echt
    betrouwbare bol-data is een headless browser nodig (zie README).
    """
    url = item.get("url") or (
        f"https://www.bol.com/nl/nl/p/-/{item['product_id']}/"
        if item.get("product_id")
        else None
    )
    result = {"listed": False, "in_stock": None, "price": None, "url": url}
    if not url:
        log(f"  ! {item['name']}: geen url of product_id, overgeslagen")
        return result

    resp = session.get(url, timeout=HTTP_TIMEOUT, allow_redirects=True)
    result["url"] = resp.url
    if resp.status_code == 404:
        result["in_stock"] = False
        return result  # pagina bestaat (nog) niet -> niet listed
    resp.raise_for_status()
    result["listed"] = True

    # Alleen betrouwen op echte Product-structuurdata (schema.org). Sommige
    # bol-pagina's leveren die; veel niet. Bewust GEEN gok op losse tekst als
    # "Op voorraad", want dat zijn UI-vertaalstrings die altijd aanwezig zijn.
    price, in_stock = _product_from_jsonld(resp.text)
    result["price"] = price
    result["in_stock"] = in_stock  # None als niet gevonden
    if in_stock is None:
        log(f"  ? {item['name']}: prijs/voorraad niet in HTML (bol rendert client-side)")
    return result


# Nog niet geïmplementeerd: MediaMarkt/Coolblue hebben stevigere botbescherming.
# De structuur staat klaar zodat je ze later kunt invullen.
def scrape_unsupported(item, session):
    log(
        f"  ! {item['name']}: winkel '{item['retailer']}' nog niet ondersteund door de "
        "scraper — sla over. (Bol werkt wel.)"
    )
    return None


# Gedeelde API-client (lazy aangemaakt in main zodra credentials aanwezig zijn).
_bol_client = None


def bol_product_url(item):
    return item.get("url") or (
        f"https://www.bol.com/nl/nl/p/-/{item['product_id']}/"
        if item.get("product_id")
        else f"https://www.bol.com/nl/nl/s/?searchtext={item['ean']}"
    )


def check_bol(item, session):
    """Bol-check: officiële Retailer API als die er is, anders HTML-fallback."""
    if _bol_client is not None:
        result = _bol_client.get_offers(item["ean"])
        result["url"] = bol_product_url(item)
        return result
    return scrape_bol(item, session)


SCRAPERS = {
    "bol": check_bol,
    "mediamarkt": scrape_unsupported,
    "coolblue": scrape_unsupported,
    "amazon": scrape_unsupported,
}


# --------------------------------------------------------------------------- #
# Gebeurtenissen bepalen door nieuw met vorig te vergelijken
# --------------------------------------------------------------------------- #
def diff_events(prev, cur, item, ts):
    # Eerste waarneming van een product: alleen status vastleggen, geen events.
    # Anders zou elk nieuw watchlist-item meteen een (valse) "restock" melden.
    if prev is None:
        return []
    events = []
    base = {
        "retailer": item["retailer"],
        "ean": item["ean"],
        "name": item["name"],
        "product_id": item.get("product_id"),
        "url": cur.get("url"),
        "ts": ts,
    }
    prev_in = bool(prev["in_stock"])
    prev_listed = bool(prev["listed"])
    prev_price = prev.get("price")

    if cur["in_stock"] and not prev_in:
        events.append({**base, "type": "restock", "price": cur["price"]})
    elif not cur["in_stock"] and prev_in:
        events.append({**base, "type": "out_of_stock", "price": cur["price"]})
    elif cur["listed"] and not prev_listed and not cur["in_stock"]:
        events.append({**base, "type": "drop_signal"})

    if (
        cur["in_stock"]
        and cur["price"] is not None
        and prev_price is not None
        and float(cur["price"]) < float(prev_price)
    ):
        events.append(
            {**base, "type": "price_drop", "price": cur["price"], "old_price": prev_price}
        )
    return events


# --------------------------------------------------------------------------- #
# Supabase REST-helpers
# --------------------------------------------------------------------------- #
def sb_headers(extra=None):
    h = {
        "apikey": SUPABASE_SERVICE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
        "Content-Type": "application/json",
    }
    if extra:
        h.update(extra)
    return h


def fetch_prev_state():
    """Haalt de huidige tracker_state op, geïndexeerd op (retailer, ean).

    Pagineert met Range-headers: PostgREST geeft standaard max 1000 rijen terug.
    Zonder paginatie zouden producten voorbij rij 1000 als "nieuw" gezien worden
    en hun statuswijzigingen gemist.
    """
    result = {}
    page = int(os.environ.get("STATE_PAGE_SIZE", "1000"))
    offset = 0
    while True:
        r = requests.get(
            f"{SUPABASE_URL}/rest/v1/tracker_state?select=*&order=id",
            headers=sb_headers({"Range-Unit": "items",
                                "Range": f"{offset}-{offset + page - 1}"}),
            timeout=HTTP_TIMEOUT,
        )
        r.raise_for_status()
        rows = r.json()
        if not isinstance(rows, list):
            raise BolApiError("onverwacht antwoord van Supabase bij ophalen state")
        for row in rows:
            result[(row["retailer"], row["ean"])] = row
        if len(rows) < page:
            break
        offset += page
    return result


# Postgres tekst accepteert geen NUL/control-tekens; scraped shopdata bevat die
# soms (levert een 400 bij insert). Strip ze uit alle string-velden.
_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _sanitize_rows(rows):
    def clean(v):
        return _CTRL_RE.sub("", v) if isinstance(v, str) else v
    return [{k: clean(v) for k, v in row.items()} for row in rows]


def _raise_with_body(r, what):
    if r.status_code >= 400:
        raise requests.HTTPError(f"{what} faalde (HTTP {r.status_code}): {r.text[:300]}", response=r)


def upsert_state(rows):
    if not rows:
        return
    r = requests.post(
        f"{SUPABASE_URL}/rest/v1/tracker_state?on_conflict=retailer,ean",
        headers=sb_headers({"Prefer": "resolution=merge-duplicates,return=minimal"}),
        data=json.dumps(_sanitize_rows(rows)),
        timeout=HTTP_TIMEOUT,
    )
    _raise_with_body(r, "upsert_state")


def insert_events(rows):
    if not rows:
        return
    r = requests.post(
        f"{SUPABASE_URL}/rest/v1/tracker_events",
        headers=sb_headers({"Prefer": "return=minimal"}),
        data=json.dumps(_sanitize_rows(rows)),
        timeout=HTTP_TIMEOUT,
    )
    _raise_with_body(r, "insert_events")


# --------------------------------------------------------------------------- #
# Hoofdprogramma
# --------------------------------------------------------------------------- #
# --------------------------------------------------------------------------- #
# Discord-alerts
# --------------------------------------------------------------------------- #
def _eur(v):
    return "€" + ("%.2f" % float(v)).replace(".", ",") if v is not None else "—"


def _shop_label(retailer):
    return {"bol": "bol.com", "mediamarkt": "MediaMarkt", "coolblue": "Coolblue",
            "amazon": "Amazon", "intertoys": "Intertoys"}.get(retailer, str(retailer).title())


def event_buy_url(e):
    if e.get("url"):
        return e["url"]
    if e.get("product_id"):
        return "https://www.bol.com/nl/nl/p/-/" + str(e["product_id"]) + "/"
    return "https://www.bol.com/nl/nl/s/?searchtext=" + str(e.get("ean") or e.get("name") or "")


def build_discord_payload(events):
    """Bouwt één Discord-webhook-bericht (max 10 embeds) uit een lijst events."""
    embeds = []
    for e in events:
        t = e.get("type")
        if t not in DISCORD_NOTIFY_TYPES:
            continue
        if t == "price_drop":
            money = _eur(e.get("old_price")) + " → " + _eur(e.get("price"))
        else:
            money = _eur(e.get("price")) if e.get("price") is not None else ""
        desc = "**" + str(e.get("name", "")) + "**"
        if money:
            desc += "\n" + money
        desc += "\n[Koop bij " + _shop_label(e.get("retailer", "bol")) + " →](" + event_buy_url(e) + ")"
        embeds.append({
            "title": DISCORD_LABELS.get(t, t) + " · " + _shop_label(e.get("retailer", "bol")),
            "description": desc,
            "color": DISCORD_COLORS.get(t, 0x1E2245),
            "url": event_buy_url(e),
        })
    if not embeds:
        return None
    return {"username": "PocketPop", "embeds": embeds[:10]}


def notify_discord(events):
    """Post alle alert-waardige events naar Discord (in blokken van 10 embeds)."""
    if not DISCORD_WEBHOOK:
        return
    fresh = [e for e in events if e.get("type") in DISCORD_NOTIFY_TYPES]
    for i in range(0, len(fresh), 10):
        payload = build_discord_payload(fresh[i:i + 10])
        if not payload:
            continue
        for attempt in range(2):
            try:
                r = requests.post(DISCORD_WEBHOOK, json=payload, timeout=HTTP_TIMEOUT)
                if r.status_code == 429 and attempt == 0:
                    try:
                        wait = float(r.json().get("retry_after", 2))
                    except (ValueError, KeyError, AttributeError):
                        wait = 2.0
                    time.sleep(min(wait, 10))
                    continue
                if r.status_code >= 400:
                    log(f"  ! Discord-webhook faalde (HTTP {r.status_code})")
                break
            except requests.RequestException as ex:
                log(f"  ! Discord-webhook netwerkfout ({ex})")
                break


def delete_state(keys):
    """Verwijder tracker_state-rijen (retailer, ean) die niet meer gevolgd worden."""
    from collections import defaultdict
    by_ret = defaultdict(list)
    for retailer, ean in keys:
        by_ret[retailer].append(str(ean))
    for retailer, eans in by_ret.items():
        for i in range(0, len(eans), 50):
            inlist = ",".join(eans[i:i + 50])
            r = requests.delete(
                f"{SUPABASE_URL}/rest/v1/tracker_state?retailer=eq.{retailer}&ean=in.({inlist})",
                headers=sb_headers({"Prefer": "return=minimal"}),
                timeout=HTTP_TIMEOUT,
            )
            r.raise_for_status()


def main():
    global _bol_client
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        sys.exit("FOUT: zet SUPABASE_URL en SUPABASE_SERVICE_KEY als environment variables.")
    if not WATCHLIST_PATH.exists():
        sys.exit(f"FOUT: watchlist niet gevonden op {WATCHLIST_PATH}")

    watchlist = json.loads(WATCHLIST_PATH.read_text())
    log(f"Watchlist: {len(watchlist)} producten")

    if BOL_CLIENT_ID and BOL_CLIENT_SECRET:
        _bol_client = BolRetailerClient(BOL_CLIENT_ID, BOL_CLIENT_SECRET, demo=BOL_DEMO)
        log("Bol-bron: officiële Retailer API" + (" (DEMO-omgeving)" if BOL_DEMO else ""))
    else:
        log("Bol-bron: HTML-fallback (beperkt betrouwbaar) — zet BOL_CLIENT_ID/BOL_CLIENT_SECRET voor de officiële API")

    prev_state = fetch_prev_state()
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "nl-NL,nl;q=0.9"})

    state_rows, event_rows = [], []
    for item in watchlist:
        # Eén kapotte watchlist-regel mag de hele run niet laten crashen.
        if not isinstance(item, dict) or not item.get("ean") or not item.get("name"):
            log(f"  ! ongeldige watchlist-regel overgeslagen (ean en name verplicht): {item!r}")
            continue
        item["ean"] = str(item["ean"])
        item.setdefault("retailer", "bol")
        scraper = SCRAPERS.get(item["retailer"], scrape_unsupported)
        ts = now_iso()
        try:
            cur = scraper(item, session)
        except BolAuthError as e:
            sys.exit(f"FOUT: bol-authenticatie mislukt: {e}")
        except (BolApiError, requests.RequestException) as e:
            log(f"  ! {item['name']}: fout bij checken ({e}); overgeslagen")
            continue
        if cur is None:  # niet-ondersteunde winkel
            continue

        # Bol Drop Status: voor drop_watch-items telt ALLEEN bol's eigen verkoop
        # (retailerId 0), niet de marketplace-verkopers. We richten in_stock/price
        # op bol's aanbieding, zodat een "restock" hier letterlijk = bol gaat live.
        drop_watch = bool(item.get("drop_watch")) and "bol_in_stock" in cur
        if drop_watch:
            cur = {**cur, "in_stock": cur["bol_in_stock"], "price": cur["bol_price"]}

        prev = prev_state.get((item["retailer"], item["ean"]))

        # Bij onbekende voorraad/prijs: geen gebeurtenissen verzinnen en de
        # laatst bekende waarden behouden i.p.v. ze op None/False te zetten.
        known = cur["in_stock"] is not None
        if known:
            events = diff_events(prev, cur, item, ts)
            for ev in events:
                # Een restock op een drop_watch-item is een échte bol-drop.
                if drop_watch and ev["type"] == "restock":
                    ev["type"] = "bol_drop"
                log(f"  → {ev['type'].upper()}: {item['name']} ({item['retailer']})")
            event_rows.extend(events)
        in_stock_val = cur["in_stock"] if known else (prev["in_stock"] if prev else False)
        price_val = cur["price"] if cur["price"] is not None else (prev.get("price") if prev else None)

        state_rows.append(
            {
                "retailer": item["retailer"],
                "ean": item["ean"],
                "name": item["name"],
                "product_id": item.get("product_id"),
                "url": cur.get("url"),
                "price": price_val,
                "in_stock": bool(in_stock_val),
                "listed": cur["listed"],
                "last_check": ts,
            }
        )
        status = "op voorraad" if in_stock_val else ("klaargezet" if cur["listed"] else "weg")
        extra = "" if known else " (onbekend, laatst bekende behouden)"
        log(f"  · {item['name']}: {status}, prijs {price_val}{extra}")
        time.sleep(REQUEST_DELAY)

    # ---- Extra winkels via publieke Shopify/Woo product-API's ----
    # Hele catalogus per shop ophalen, filteren op Pokémon-sealed en door dezelfde
    # diff/events-machinerie halen (sleutel = shop + product-handle i.p.v. EAN).
    if SHOPS_PATH.exists():
        try:
            shop_cfg = json.loads(SHOPS_PATH.read_text())
        except (json.JSONDecodeError, ValueError):
            shop_cfg = []
        for shop in shop_cfg:
            if not isinstance(shop, dict) or not shop.get("enabled", True) or not shop.get("domain"):
                continue
            try:
                products = shops.fetch_shop(shop, session)
            except requests.RequestException as e:
                log(f"  ! shop {shop.get('domain')}: netwerkfout ({e}); overgeslagen")
                continue
            log(f"  · winkel {shop.get('retailer', shop['domain'])}: {len(products)} sealed-producten")
            for cur in products:
                ts_s = now_iso()
                prev = prev_state.get((cur["retailer"], cur["ean"]))
                events = diff_events(prev, cur, cur, ts_s)  # cur dient als cur én item
                for ev in events:
                    log(f"    → {ev['type'].upper()}: {cur['name'][:40]} ({cur['retailer']})")
                event_rows.extend(events)
                state_rows.append({
                    "retailer": cur["retailer"], "ean": cur["ean"], "name": cur["name"],
                    "product_id": None, "url": cur.get("url"),
                    "price": cur["price"], "in_stock": bool(cur["in_stock"]),
                    "listed": cur["listed"], "last_check": ts_s,
                })
            time.sleep(REQUEST_DELAY)

    # Volgorde is bewust: eerst events wegschrijven, daarna pas de state
    # bijwerken. Zou de state eerst geschreven worden en het event-insert
    # daarna falen, dan zou de volgende run de events niet opnieuw genereren
    # (de state is dan al "vooruit") en zou je een restock definitief missen.
    # Andersom is het ergste geval een dubbele melding bij de volgende run —
    # veel minder erg dan een gemiste drop.
    insert_events(event_rows)
    upsert_state(state_rows)
    # Ruim rijen op van producten die niet meer gevolgd worden (filter aangescherpt
    # of uit de watchlist gehaald). Alléén voor winkels die deze run data gaven,
    # zodat een tijdelijke fetch-fout niet per ongeluk een hele winkel wist.
    processed = {r["retailer"] for r in state_rows}
    seen = {(r["retailer"], r["ean"]) for r in state_rows}
    stale = [k for k in prev_state if k[0] in processed and k not in seen]
    if stale:
        delete_state(stale)
        log(f"Opgeruimd: {len(stale)} verouderde rijen verwijderd.")
    notify_discord(event_rows)  # alerts pas na het persisteren van de events
    log(f"Klaar: {len(state_rows)} statussen bijgewerkt, {len(event_rows)} gebeurtenissen gelogd.")


if __name__ == "__main__":
    main()
