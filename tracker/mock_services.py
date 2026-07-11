"""Lokale mock van de bol Retailer API + Supabase REST, voor end-to-end tests.

Start een HTTP-server op een vrije poort die beide diensten nabootst:

  Bol:      POST /token                          -> access token
            GET  /retailer/products/{ean}/offers -> offers volgens scenario
  Supabase: GET  /rest/v1/tracker_state          -> huidige state
            POST /rest/v1/tracker_state          -> upsert (merge-duplicates)
            POST /rest/v1/tracker_events         -> insert events

Het scenario (welke EAN op voorraad is en voor welke prijs) is per test aan te
passen via `server.scenario[ean] = {...}`. De opgeslagen state en events zijn
direct te inspecteren via `server.state` en `server.events`.
"""

import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs


class MockState:
    def __init__(self):
        self.scenario = {}   # ean -> {"listed": bool, "in_stock": bool, "price": float|None}
        self.state = {}      # (retailer, ean) -> rij
        self.events = []     # lijst van event-rijen
        self.requests = []   # (method, path) log, voor assertions
        self.token_calls = 0
        self.fail_next_offers = 0   # aantal komende offers-calls dat 500 geeft
        self.reject_auth = False    # token-endpoint weigert credentials
        self.fail_events = False    # tracker_events-insert geeft 500
        self.page_size = 1000       # max rijen per state-GET (PostgREST-limiet)


def _make_handler(mock: MockState):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # stil in testoutput
            pass

        def _send(self, code, payload=None):
            body = json.dumps(payload if payload is not None else {}).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _read_raw(self):
            length = int(self.headers.get("Content-Length", 0))
            return self.rfile.read(length) if length else b""

        def _read_body(self):
            raw = self._read_raw() or b"{}"
            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                return {}

        # ---------------- bol Retailer API ---------------- #
        # Net zo streng als de echte API (geverifieerd tegen de docs):
        # - token: form-body grant_type=client_credentials + Basic auth vereist
        # - offers: vendor Accept-header vereist (anders 406), bearer vereist (403)
        def _bol_token(self):
            mock.token_calls += 1
            if mock.reject_auth:
                return self._send(401, {"error": "invalid_client"})
            if not self.headers.get("Authorization", "").startswith("Basic "):
                return self._send(401, {"error": "geen Basic auth"})
            ctype = self.headers.get("Content-Type", "")
            body = self._read_raw().decode()
            if "application/x-www-form-urlencoded" not in ctype or "grant_type=client_credentials" not in body:
                return self._send(400, {"error": "unsupported_grant_type",
                                        "detail": "grant_type hoort als form-body"})
            return self._send(200, {"access_token": "mock-token-" + str(mock.token_calls),
                                    "token_type": "Bearer", "expires_in": 299})

        def _bol_offers(self, ean):
            if self.headers.get("Accept") != "application/vnd.retailer.v10+json":
                return self._send(406, {"detail": "verkeerde Accept-header"})
            if not self.headers.get("Authorization", "").startswith("Bearer "):
                return self._send(403, {"detail": "geen geldig token"})
            if mock.fail_next_offers > 0:
                mock.fail_next_offers -= 1
                return self._send(500, {"detail": "tijdelijke fout"})
            sc = mock.scenario.get(ean)
            if sc is None or not sc.get("listed", True):
                return self._send(404, {
                    "type": "https://api.bol.com/problems", "status": 404,
                    "title": "Not Found", "detail": "onbekende ean",
                })
            # Scenario-velden:
            #   in_stock/price -> marketplace-aanbieding (retailerId 12345, FBR)
            #   bol/bol_price  -> bol's eigen aanbieding (retailerId "0", FBB)
            offers = []
            if sc.get("bol"):
                offers.append({
                    "offerId": "b8d1621a-0000-0000-0000-0000000000b0",
                    "retailerId": "0", "countryCode": "NL", "condition": "NEW",
                    "price": sc.get("bol_price", sc.get("price")),
                    "fulfilmentMethod": "FBB", "bestOffer": True,
                })
            if sc.get("in_stock") and not sc.get("bol"):
                offers.append({
                    "offerId": "b8d1621a-0000-0000-0000-000000000001",
                    "retailerId": "12345", "countryCode": "NL", "condition": "NEW",
                    "price": sc.get("price"),
                    "fulfilmentMethod": "FBR", "bestOffer": True,
                })
            return self._send(200, {"offers": offers})

        # ---------------- Supabase REST ---------------- #
        def _supabase_get_state(self):
            rows = list(mock.state.values())
            # Bootst PostgREST-paginatie na: respecteer een Range-header en geef
            # nooit meer dan `page_size` rijen in één keer terug.
            rng = self.headers.get("Range")
            start, end = 0, len(rows) - 1
            if rng and "-" in rng:
                try:
                    a, b = rng.split("-", 1)
                    start, end = int(a), int(b)
                except ValueError:
                    pass
            window = rows[start:end + 1][: mock.page_size]
            return self._send(200, window)

        def _supabase_upsert_state(self):
            rows = self._read_body()
            for row in rows if isinstance(rows, list) else [rows]:
                mock.state[(row["retailer"], row["ean"])] = row
            return self._send(201, [])

        def _supabase_insert_events(self):
            rows = self._read_body()
            if mock.fail_events:
                return self._send(500, {"detail": "insert mislukt"})
            rows = rows if isinstance(rows, list) else [rows]
            # PostgREST-regel: bij een bulk-insert moeten alle objecten dezelfde
            # keys hebben, anders PGRST102. De mock dwingt dit af zodat tests het vangen.
            if len({frozenset(r.keys()) for r in rows}) > 1:
                return self._send(400, {"code": "PGRST102", "message": "All object keys must match"})
            mock.events.extend(rows)
            return self._send(201, [])

        # ---------------- routing ---------------- #
        def do_POST(self):
            path = urlparse(self.path).path
            mock.requests.append(("POST", path))
            if path == "/token":
                return self._bol_token()
            if path == "/rest/v1/tracker_state":
                return self._supabase_upsert_state()
            if path == "/rest/v1/tracker_events":
                return self._supabase_insert_events()
            return self._send(404, {"detail": "onbekend pad " + path})

        def do_GET(self):
            path = urlparse(self.path).path
            mock.requests.append(("GET", path))
            if path.startswith("/retailer/products/") and path.endswith("/offers"):
                ean = path.split("/")[3]
                return self._bol_offers(ean)
            if path == "/rest/v1/tracker_state":
                return self._supabase_get_state()
            return self._send(404, {"detail": "onbekend pad " + path})

        def do_DELETE(self):
            parsed = urlparse(self.path)
            mock.requests.append(("DELETE", parsed.path))
            if parsed.path == "/rest/v1/tracker_state":
                q = parse_qs(parsed.query)
                retailer = q.get("retailer", ["eq."])[0].replace("eq.", "")
                m = re.match(r"in\.\((.*)\)", q.get("ean", [""])[0])
                eans = [x.strip('"') for x in m.group(1).split(",") if x] if m else []
                for e in eans:
                    mock.state.pop((retailer, e), None)
                return self._send(204, None)
            return self._send(404, {"detail": "onbekend pad " + parsed.path})

    return Handler


class MockServer:
    """Context manager: start de mock-server op een vrije poort."""

    def __init__(self):
        self.mock = MockState()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(self.mock))
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()

    @property
    def base(self):
        return f"http://127.0.0.1:{self.port}"

    def env(self):
        """Environment variables die tracker.py naar deze mock laten wijzen."""
        return {
            "SUPABASE_URL": self.base,
            "SUPABASE_SERVICE_KEY": "mock-service-key",
            "BOL_CLIENT_ID": "mock-id",
            "BOL_CLIENT_SECRET": "mock-secret",
            "BOL_TOKEN_URL": self.base + "/token",
            "BOL_API_BASE": self.base + "/retailer",
            "TRACKER_DELAY": "0",
        }
