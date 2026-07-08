#!/usr/bin/env python3
"""Ontdek Pokémon sealed-producten op bol en bouw een drop_watch-watchlist.

Werkwijze: zoek op bol.com → haal product-ids en EANs uit de pagina's →
verrijk elke EAN via de officiële Retailer API (catalogus: titel + merk) →
filter op échte Pokémon sealed-producten (geen hoesjes/cases/sleeves).

Draai af en toe handmatig:
    BOL_CLIENT_ID=... BOL_CLIENT_SECRET=... python tracker/discover.py [opties]

Opties:
    --write         schrijf/merge het resultaat in tracker/watchlist.json
    --terms "a,b"   eigen zoektermen (komma-gescheiden)
    --limit N       max product-ids per zoekterm (default 10)

Let op: dit scrapet de OPENBARE bol-zoekpagina. Als bol de opmaak wijzigt kan
het breken — het is een hulpmiddel dat je af en toe draait, geen onderdeel van
de 5-minuten-tracker. Bekijk de output altijd na; accessoire-filtering is ~goed,
niet perfect.
"""

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).parent))
from bol_api import BolRetailerClient  # noqa: E402

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/125.0 Safari/537.36")
DEFAULT_TERMS = [
    "pokemon booster box",
    "pokemon elite trainer box",
    "pokemon booster bundle",
    "pokemon premium collection",
]
WATCHLIST_PATH = Path(__file__).with_name("watchlist.json")

# Filter-woordenlijsten (titel, kleine letters).
ACCESSORY = ["case", "acryl", "hoes", "sleeve", "protector", "portfolio", "binder",
             "toploader", "deck box", "stand", "screen", "playmat", "dobbel", "map ",
             "sorter", "album", "display frame"]
SEALED = ["booster box", "booster bundle", "elite trainer", "etb", "booster pack",
          "tin", "premium collection", "collection box", "blister", "booster display",
          "build & battle", "build and battle", "surprise box", "booster -"]


def is_pokemon_sealed(title, brand):
    t = (title or "").lower()
    b = (brand or "").lower()
    if any(k in t for k in ACCESSORY):
        return False
    brand_pok = "pok" in b                      # 'The Pokémon Company' e.d.
    title_pok = "pokemon" in t or "pokémon" in t
    sealed = any(k in t for k in SEALED)
    if brand_pok:                               # officieel Pokémon-merk: sterk signaal
        return True
    return title_pok and sealed                 # distributeur (Asmodee): eis sealed-type


def search_ids(session, term, limit):
    r = session.get("https://www.bol.com/nl/nl/s/?searchtext=" + requests.utils.quote(term), timeout=20)
    ids = re.findall(r'/nl/nl/p/[^"\s]*?/(\d{16,})/', r.text)
    return list(dict.fromkeys(ids))[:limit]


def eans_from_product(session, pid):
    r = session.get("https://www.bol.com/nl/nl/p/-/" + pid + "/", timeout=20, allow_redirects=True)
    cands = set()
    for pat in [r'EAN[^0-9]{0,15}(\d{13})', r'"ean"\s*:\s*"?(\d{13})', r'gtin13[^0-9]{0,10}(\d{13})']:
        cands.update(re.findall(pat, r.text, re.I))
    return list(cands)


def catalog_info(client, ean):
    r = client._get("/content/catalog-products/" + ean)
    if r.status_code != 200:
        return None
    body = r.json()
    title = brand = None
    for a in body.get("attributes", []):
        vals = [v.get("value") for v in a.get("values", [])]
        if a.get("id") == "Title":
            title = vals[0] if vals else None
        if a.get("id") in ("Brand", "Manufacturer Name") and not brand:
            brand = vals[0] if vals else None
    if not brand:
        for p in body.get("parties", []):
            if p.get("role") in ("BRAND", "MANUFACTURER"):
                brand = p.get("name")
                break
    return {"title": title, "brand": brand}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--terms", default=None)
    ap.add_argument("--limit", type=int, default=10)
    args = ap.parse_args()

    cid, csec = os.environ.get("BOL_CLIENT_ID"), os.environ.get("BOL_CLIENT_SECRET")
    if not cid or not csec:
        sys.exit("FOUT: zet BOL_CLIENT_ID en BOL_CLIENT_SECRET als environment variables.")
    client = BolRetailerClient(cid, csec)
    session = requests.Session()
    session.headers.update({"User-Agent": UA, "Accept-Language": "nl-NL,nl;q=0.9"})

    terms = [t.strip() for t in (args.terms.split(",") if args.terms else DEFAULT_TERMS) if t.strip()]
    pids = []
    for t in terms:
        found = search_ids(session, t, args.limit)
        print(f"[zoek] {t!r}: {len(found)} product-ids", flush=True)
        pids += found
        time.sleep(0.5)
    pids = list(dict.fromkeys(pids))

    seen_ean, keep = set(), []
    for pid in pids:
        for ean in eans_from_product(session, pid):
            if ean in seen_ean:
                continue
            seen_ean.add(ean)
            info = catalog_info(client, ean) or {}
            title, brand = info.get("title"), info.get("brand")
            if is_pokemon_sealed(title, brand):
                keep.append({"name": title or ("Pokémon " + ean), "ean": ean,
                             "retailer": "bol", "drop_watch": True})
                print(f"  ✓ {ean} | {brand} | {str(title)[:60]}", flush=True)
            time.sleep(0.2)
        time.sleep(0.3)

    print(f"\n{len(keep)} Pokémon sealed-producten gevonden.")
    if args.write:
        existing = json.loads(WATCHLIST_PATH.read_text()) if WATCHLIST_PATH.exists() else []
        by_ean = {i["ean"]: i for i in existing if isinstance(i, dict) and i.get("ean")}
        for row in keep:
            by_ean.setdefault(row["ean"], row)
        merged = list(by_ean.values())
        WATCHLIST_PATH.write_text(json.dumps(merged, indent=2, ensure_ascii=False) + "\n")
        print(f"Weggeschreven naar {WATCHLIST_PATH} ({len(merged)} items totaal).")
    else:
        print("(niets weggeschreven; gebruik --write om te mergen in watchlist.json)")


if __name__ == "__main__":
    main()
