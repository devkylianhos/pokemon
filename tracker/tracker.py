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

# --------------------------------------------------------------------------- #
# Configuratie
# --------------------------------------------------------------------------- #
SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
WATCHLIST_PATH = Path(__file__).with_name("watchlist.json")

# Beleefde pauze tussen twee productverzoeken (seconden).
REQUEST_DELAY = 1.5
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


SCRAPERS = {
    "bol": scrape_bol,
    "mediamarkt": scrape_unsupported,
    "coolblue": scrape_unsupported,
    "amazon": scrape_unsupported,
}


# --------------------------------------------------------------------------- #
# Gebeurtenissen bepalen door nieuw met vorig te vergelijken
# --------------------------------------------------------------------------- #
def diff_events(prev, cur, item, ts):
    events = []
    base = {
        "retailer": item["retailer"],
        "ean": item["ean"],
        "name": item["name"],
        "product_id": item.get("product_id"),
        "url": cur.get("url"),
        "ts": ts,
    }
    prev_in = bool(prev["in_stock"]) if prev else False
    prev_listed = bool(prev["listed"]) if prev else False
    prev_price = prev.get("price") if prev else None

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
    """Haalt de huidige tracker_state op, geïndexeerd op (retailer, ean)."""
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/tracker_state?select=*",
        headers=sb_headers(),
        timeout=HTTP_TIMEOUT,
    )
    r.raise_for_status()
    return {(row["retailer"], row["ean"]): row for row in r.json()}


def upsert_state(rows):
    if not rows:
        return
    r = requests.post(
        f"{SUPABASE_URL}/rest/v1/tracker_state?on_conflict=retailer,ean",
        headers=sb_headers({"Prefer": "resolution=merge-duplicates,return=minimal"}),
        data=json.dumps(rows),
        timeout=HTTP_TIMEOUT,
    )
    r.raise_for_status()


def insert_events(rows):
    if not rows:
        return
    r = requests.post(
        f"{SUPABASE_URL}/rest/v1/tracker_events",
        headers=sb_headers({"Prefer": "return=minimal"}),
        data=json.dumps(rows),
        timeout=HTTP_TIMEOUT,
    )
    r.raise_for_status()


# --------------------------------------------------------------------------- #
# Hoofdprogramma
# --------------------------------------------------------------------------- #
def main():
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        sys.exit("FOUT: zet SUPABASE_URL en SUPABASE_SERVICE_KEY als environment variables.")
    if not WATCHLIST_PATH.exists():
        sys.exit(f"FOUT: watchlist niet gevonden op {WATCHLIST_PATH}")

    watchlist = json.loads(WATCHLIST_PATH.read_text())
    log(f"Watchlist: {len(watchlist)} producten")

    prev_state = fetch_prev_state()
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "nl-NL,nl;q=0.9"})

    state_rows, event_rows = [], []
    for item in watchlist:
        item.setdefault("retailer", "bol")
        scraper = SCRAPERS.get(item["retailer"], scrape_unsupported)
        ts = now_iso()
        try:
            cur = scraper(item, session)
        except requests.RequestException as e:
            log(f"  ! {item['name']}: netwerkfout ({e}); overgeslagen")
            continue
        if cur is None:  # niet-ondersteunde winkel
            continue

        prev = prev_state.get((item["retailer"], item["ean"]))

        # Bij onbekende voorraad/prijs: geen gebeurtenissen verzinnen en de
        # laatst bekende waarden behouden i.p.v. ze op None/False te zetten.
        known = cur["in_stock"] is not None
        if known:
            events = diff_events(prev, cur, item, ts)
            for ev in events:
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

    upsert_state(state_rows)
    insert_events(event_rows)
    log(f"Klaar: {len(state_rows)} statussen bijgewerkt, {len(event_rows)} gebeurtenissen gelogd.")


if __name__ == "__main__":
    main()
