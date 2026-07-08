"""Client voor de officiële bol.com Retailer API v10 (client-credentials flow).

Gebruik:
    client = BolRetailerClient(client_id, client_secret)
    info = client.get_offers("0820650858079")
    # -> {"listed": True, "in_stock": True, "price": 54.99}

Gebaseerd op de officiële documentatie/OpenAPI-spec (geverifieerd 2026-07-07):
- Token:  POST https://login.bol.com/token, form-body grant_type=client_credentials,
          credentials via HTTP Basic auth, Accept: application/json.
          Response: access_token/token_type/expires_in (~299 s).
- API:    Accept: application/vnd.retailer.v10+json (verplicht vendor media type;
          een verkeerde Accept-header kan een 404/406 opleveren!).
          Ongeldige/ontbrekende bearer geeft 403; het token-endpoint geeft 401
          bij foute credentials.
- Offers: GET /retailer/products/{ean}/offers — "competing offers", werkt voor
          elke EAN in de bol-catalogus. Rate limit: 900 requests/minuut.
- BELANGRIJK: tokens cachen! Het token-endpoint heeft een veel lagere,
  ongepubliceerde rate limit en overschrijding kan een IP-ban geven.

De base-URLs zijn via environment variables te overschrijven zodat tests een
lokale mock kunnen gebruiken:
    BOL_TOKEN_URL  (default: https://login.bol.com/token)
    BOL_API_BASE   (default: https://api.bol.com/retailer, of /retailer-demo bij demo=True)
    BOL_COUNTRY    (default: NL; alternatief BE)
"""

import os
import time

import requests

DEFAULT_TOKEN_URL = "https://login.bol.com/token"
DEFAULT_API_BASE = "https://api.bol.com/retailer"
DEFAULT_DEMO_BASE = "https://api.bol.com/retailer-demo"
ACCEPT = "application/vnd.retailer.v10+json"


class BolApiError(Exception):
    """Algemene fout bij het aanroepen van de Retailer API."""


class BolAuthError(BolApiError):
    """Client credentials zijn ongeldig of geweigerd."""


class BolRetailerClient:
    def __init__(self, client_id, client_secret, demo=False, session=None, timeout=20):
        if not client_id or not client_secret:
            raise BolAuthError("client_id en client_secret zijn verplicht")
        self.client_id = client_id
        self.client_secret = client_secret
        self.timeout = timeout
        self.session = session or requests.Session()
        default_base = DEFAULT_DEMO_BASE if demo else DEFAULT_API_BASE
        self.base = os.environ.get("BOL_API_BASE", default_base).rstrip("/")
        self.token_url = os.environ.get("BOL_TOKEN_URL", DEFAULT_TOKEN_URL)
        self.country = os.environ.get("BOL_COUNTRY", "NL")
        self._token = None
        self._token_expires_at = 0.0

    # ------------------------------------------------------------------ #
    # Authenticatie
    # ------------------------------------------------------------------ #
    def _get_token(self):
        """Haalt (en cachet) een bearer token op; vernieuwt 30 s voor expiry.

        Cachen is verplicht gedrag: het token-endpoint heeft een lage rate
        limit en te vaak aanvragen kan een IP-ban opleveren.
        """
        if self._token and time.time() < self._token_expires_at - 30:
            return self._token
        resp = self.session.post(
            self.token_url,
            data={"grant_type": "client_credentials"},  # form-urlencoded body
            auth=(self.client_id, self.client_secret),   # HTTP Basic
            headers={"Accept": "application/json"},
            timeout=self.timeout,
        )
        if resp.status_code in (400, 401, 403):
            raise BolAuthError(
                f"token geweigerd (HTTP {resp.status_code}); controleer BOL_CLIENT_ID/BOL_CLIENT_SECRET"
            )
        resp.raise_for_status()
        data = resp.json()
        self._token = data["access_token"]
        self._token_expires_at = time.time() + float(data.get("expires_in", 299))
        return self._token

    # ------------------------------------------------------------------ #
    # HTTP-helper met retry op 429 en token-refresh op 401/403
    # ------------------------------------------------------------------ #
    def _get(self, path, params=None, retries=2):
        resp = None
        for attempt in range(retries + 1):
            token = self._get_token()
            resp = self.session.get(
                self.base + path,
                params=params,
                headers={"Accept": ACCEPT, "Authorization": "Bearer " + token},
                timeout=self.timeout,
            )
            if resp.status_code == 429 and attempt < retries:
                wait = resp.headers.get("Retry-After", "5")
                try:
                    wait = float(wait)
                except ValueError:
                    wait = 5.0
                time.sleep(min(wait, 60))
                continue
            # Ongeldige bearer geeft 403 (soms 401): één keer token vernieuwen.
            if resp.status_code in (401, 403) and attempt < retries:
                self._token = None
                continue
            break
        return resp

    # ------------------------------------------------------------------ #
    # Endpoints
    # ------------------------------------------------------------------ #
    def get_offers(self, ean):
        """'Competing offers' voor een EAN (nieuwstaat, beste aanbieding).

        Returns dict:
            listed   - EAN bekend in de bol-catalogus (404 = onbekend)
            in_stock - minstens één NEW-aanbieding beschikbaar
            price    - prijs van de beste aanbieding (incl. btw)
        """
        resp = self._get(
            "/products/" + str(ean) + "/offers",
            params={
                "country-code": self.country,
                "best-offer-only": "true",
                "condition": "NEW",
            },
        )
        if resp.status_code == 404:
            return {"listed": False, "in_stock": False, "price": None}
        if resp.status_code == 400:
            # Verzoek afgekeurd — vrijwel altijd een ongeldige EAN in de watchlist.
            raise BolApiError(f"ongeldige EAN of verzoek voor '{ean}' (HTTP 400)")
        if resp.status_code == 429:
            raise BolApiError("rate limit (429) hield aan na retries")
        if resp.status_code in (401, 403):
            raise BolAuthError(
                f"API weigert het token (HTTP {resp.status_code}); "
                "controleer of de credentials Retailer API-toegang hebben"
            )
        if resp.status_code >= 400:
            raise BolApiError(f"offers-request faalde (HTTP {resp.status_code}): {resp.text[:120]}")
        data = resp.json() or {}
        offers = data.get("offers") or []
        # condition kan null zijn; we vroegen om NEW, dus behandel null als NEW.
        new_offers = [o for o in offers if str(o.get("condition") or "NEW").upper() == "NEW"]
        pool = new_offers or offers
        price = None
        for o in pool:
            if o.get("bestOffer") and isinstance(o.get("price"), (int, float)):
                price = float(o["price"])
                break
        if price is None:
            prices = [float(o["price"]) for o in pool if isinstance(o.get("price"), (int, float))]
            price = min(prices) if prices else None
        return {"listed": True, "in_stock": bool(pool), "price": price}
