"""Tests voor de tracker: event-logica, bol API-client en end-to-end pijplijn.

Draaien:  pip install -r tracker/requirements-dev.txt && pytest tracker/ -v
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest
import requests

sys.path.insert(0, str(Path(__file__).parent))

import bol_api
import tracker
from mock_services import MockServer

TRACKER_PY = Path(__file__).parent / "tracker.py"


# --------------------------------------------------------------------------- #
# Unit: diff_events
# --------------------------------------------------------------------------- #
ITEM = {"retailer": "bol", "ean": "111", "name": "Testproduct", "product_id": None}
TS = "2026-07-07T12:00:00+00:00"


def ev_types(events):
    return sorted(e["type"] for e in events)


def test_eerste_waarneming_geeft_geen_events():
    cur = {"listed": True, "in_stock": True, "price": 50.0, "url": None}
    assert tracker.diff_events(None, cur, ITEM, TS) == []


def test_restock():
    prev = {"listed": True, "in_stock": False, "price": 50.0}
    cur = {"listed": True, "in_stock": True, "price": 50.0, "url": None}
    events = tracker.diff_events(prev, cur, ITEM, TS)
    assert ev_types(events) == ["restock"]
    assert events[0]["price"] == 50.0


def test_out_of_stock():
    prev = {"listed": True, "in_stock": True, "price": 50.0}
    cur = {"listed": True, "in_stock": False, "price": None, "url": None}
    assert ev_types(tracker.diff_events(prev, cur, ITEM, TS)) == ["out_of_stock"]


def test_drop_signal_bij_nieuwe_pagina_zonder_voorraad():
    prev = {"listed": False, "in_stock": False, "price": None}
    cur = {"listed": True, "in_stock": False, "price": None, "url": None}
    assert ev_types(tracker.diff_events(prev, cur, ITEM, TS)) == ["drop_signal"]


def test_price_drop_alleen_bij_lagere_prijs_en_voorraad():
    prev = {"listed": True, "in_stock": True, "price": 60.0}
    lager = {"listed": True, "in_stock": True, "price": 55.0, "url": None}
    hoger = {"listed": True, "in_stock": True, "price": 65.0, "url": None}
    gelijk = {"listed": True, "in_stock": True, "price": 60.0, "url": None}
    assert ev_types(tracker.diff_events(prev, lager, ITEM, TS)) == ["price_drop"]
    assert tracker.diff_events(prev, hoger, ITEM, TS) == []
    assert tracker.diff_events(prev, gelijk, ITEM, TS) == []
    ev = tracker.diff_events(prev, lager, ITEM, TS)[0]
    assert ev["old_price"] == 60.0 and ev["price"] == 55.0


def test_restock_met_lagere_prijs_geeft_beide_events():
    prev = {"listed": True, "in_stock": False, "price": 60.0}
    cur = {"listed": True, "in_stock": True, "price": 50.0, "url": None}
    assert ev_types(tracker.diff_events(prev, cur, ITEM, TS)) == ["price_drop", "restock"]


# --------------------------------------------------------------------------- #
# Unit: BolRetailerClient (tegen een nep-sessie, geen netwerk)
# --------------------------------------------------------------------------- #
class FakeResponse:
    def __init__(self, status, payload=None, headers=None):
        self.status_code = status
        self._payload = payload if payload is not None else {}
        self.headers = headers or {}
        self.text = json.dumps(self._payload)

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")


class FakeSession:
    """Geeft vooraf klaargezette responses terug en logt alle calls."""

    def __init__(self, post_queue=None, get_queue=None):
        self.post_queue = list(post_queue or [])
        self.get_queue = list(get_queue or [])
        self.post_calls = []
        self.get_calls = []

    def post(self, url, **kw):
        self.post_calls.append((url, kw))
        return self.post_queue.pop(0)

    def get(self, url, **kw):
        self.get_calls.append((url, kw))
        return self.get_queue.pop(0)


TOKEN_OK = FakeResponse(200, {"access_token": "tok", "expires_in": 299})


def make_client(session, monkeypatch):
    monkeypatch.delenv("BOL_API_BASE", raising=False)
    monkeypatch.delenv("BOL_TOKEN_URL", raising=False)
    return bol_api.BolRetailerClient("id", "secret", session=session)


def test_token_wordt_gecachet(monkeypatch):
    s = FakeSession(
        post_queue=[TOKEN_OK],
        get_queue=[FakeResponse(200, {"offers": []}), FakeResponse(200, {"offers": []})],
    )
    c = make_client(s, monkeypatch)
    c.get_offers("111")
    c.get_offers("111")
    assert len(s.post_calls) == 1  # tweede call hergebruikt token


def test_token_request_gebruikt_form_body_en_basic_auth(monkeypatch):
    s = FakeSession(post_queue=[TOKEN_OK], get_queue=[FakeResponse(200, {"offers": []})])
    c = make_client(s, monkeypatch)
    c.get_offers("111")
    url, kw = s.post_calls[0]
    assert kw["data"] == {"grant_type": "client_credentials"}  # form-body, geen query-param
    assert kw["auth"] == ("id", "secret")                       # HTTP Basic
    assert kw["headers"]["Accept"] == "application/json"


def test_offers_request_headers_en_params(monkeypatch):
    s = FakeSession(post_queue=[TOKEN_OK], get_queue=[FakeResponse(200, {"offers": []})])
    c = make_client(s, monkeypatch)
    c.get_offers("111")
    url, kw = s.get_calls[0]
    assert url.endswith("/products/111/offers")
    assert kw["headers"]["Accept"] == "application/vnd.retailer.v10+json"
    assert kw["headers"]["Authorization"] == "Bearer tok"
    assert kw["params"]["country-code"] == "NL"
    # geen best-offer-only meer: we halen álle offers op om bol (retailerId 0) te vinden
    assert "best-offer-only" not in kw["params"]
    assert kw["params"]["condition"] == "NEW"


@pytest.mark.parametrize("status", [401, 403])
def test_ongeldige_bearer_vernieuwt_token_eenmalig(monkeypatch, status):
    s = FakeSession(
        post_queue=[TOKEN_OK, FakeResponse(200, {"access_token": "tok2", "expires_in": 299})],
        get_queue=[FakeResponse(status), FakeResponse(200, {"offers": []})],
    )
    c = make_client(s, monkeypatch)
    result = c.get_offers("111")
    assert result["listed"] is True
    assert len(s.post_calls) == 2


def test_429_wordt_geretried(monkeypatch):
    monkeypatch.setattr(bol_api.time, "sleep", lambda *_: None)
    s = FakeSession(
        post_queue=[TOKEN_OK],
        get_queue=[FakeResponse(429, headers={"Retry-After": "1"}),
                   FakeResponse(200, {"offers": []})],
    )
    c = make_client(s, monkeypatch)
    assert c.get_offers("111")["listed"] is True


def test_404_betekent_niet_listed(monkeypatch):
    s = FakeSession(post_queue=[TOKEN_OK], get_queue=[FakeResponse(404)])
    c = make_client(s, monkeypatch)
    assert c.get_offers("111") == {"listed": False, "in_stock": False, "price": None,
                                   "bol_in_stock": False, "bol_price": None}


def test_lege_offers_is_listed_zonder_voorraad(monkeypatch):
    s = FakeSession(post_queue=[TOKEN_OK], get_queue=[FakeResponse(200, {"offers": []})])
    c = make_client(s, monkeypatch)
    assert c.get_offers("111") == {"listed": True, "in_stock": False, "price": None,
                                   "bol_in_stock": False, "bol_price": None}


def test_best_offer_prijs_wint(monkeypatch):
    offers = {"offers": [
        {"condition": "NEW", "price": 60.0, "bestOffer": False, "retailerId": "999"},
        {"condition": "NEW", "price": 55.0, "bestOffer": True, "retailerId": "999"},
        {"condition": "AS_NEW", "price": 40.0, "bestOffer": False, "retailerId": "999"},
    ]}
    s = FakeSession(post_queue=[TOKEN_OK], get_queue=[FakeResponse(200, offers)])
    c = make_client(s, monkeypatch)
    result = c.get_offers("111")
    assert result == {"listed": True, "in_stock": True, "price": 55.0,
                      "bol_in_stock": False, "bol_price": None}


def test_zonder_best_offer_laagste_new_prijs(monkeypatch):
    offers = {"offers": [
        {"condition": "NEW", "price": 60.0},
        {"condition": "NEW", "price": 52.5},
    ]}
    s = FakeSession(post_queue=[TOKEN_OK], get_queue=[FakeResponse(200, offers)])
    c = make_client(s, monkeypatch)
    assert c.get_offers("111")["price"] == 52.5


def test_bol_eigen_aanbieding_herkend(monkeypatch):
    # retailerId "0" is bol zelf; 12345 is een marketplace-verkoper.
    offers = {"offers": [
        {"condition": "NEW", "price": 49.99, "bestOffer": True, "retailerId": "0"},
        {"condition": "NEW", "price": 46.39, "bestOffer": False, "retailerId": "12345"},
    ]}
    s = FakeSession(post_queue=[TOKEN_OK], get_queue=[FakeResponse(200, offers)])
    c = make_client(s, monkeypatch)
    r = c.get_offers("111")
    assert r["in_stock"] is True                # er is een aanbieding
    assert r["bol_in_stock"] is True            # en bol zelf verkoopt
    assert r["bol_price"] == 49.99


def test_alleen_marketplace_geen_bol(monkeypatch):
    offers = {"offers": [
        {"condition": "NEW", "price": 30.0, "bestOffer": True, "retailerId": "12345"},
    ]}
    s = FakeSession(post_queue=[TOKEN_OK], get_queue=[FakeResponse(200, offers)])
    c = make_client(s, monkeypatch)
    r = c.get_offers("111")
    assert r["in_stock"] is True
    assert r["bol_in_stock"] is False           # bol verkoopt (nog) niet
    assert r["bol_price"] is None


def test_400_geeft_nette_ongeldige_ean_fout(monkeypatch):
    s = FakeSession(post_queue=[TOKEN_OK],
                    get_queue=[FakeResponse(400, {"title": "Bad request"})])
    c = make_client(s, monkeypatch)
    with pytest.raises(bol_api.BolApiError, match="ongeldige EAN"):
        c.get_offers("0000000000000")


def test_ongeldige_credentials_geeft_autherror(monkeypatch):
    s = FakeSession(post_queue=[FakeResponse(401, {"error": "invalid_client"})])
    c = make_client(s, monkeypatch)
    with pytest.raises(bol_api.BolAuthError):
        c.get_offers("111")


# --------------------------------------------------------------------------- #
# End-to-end: echte tracker.py als subprocess tegen de mock-server
# --------------------------------------------------------------------------- #
WATCHLIST = [
    {"name": "Pokémon 151 Bundle", "ean": "111", "retailer": "bol", "product_id": "9300000001"},
    {"name": "Charizard ETB", "ean": "222", "retailer": "bol"},
]


def run_tracker(server, watchlist_path):
    env = {**server.env(), "WATCHLIST": str(watchlist_path), "PATH": "/usr/bin:/bin"}
    return subprocess.run(
        [sys.executable, str(TRACKER_PY)],
        env=env, capture_output=True, text=True, timeout=60,
    )


@pytest.fixture
def watchlist_file(tmp_path):
    p = tmp_path / "watchlist.json"
    p.write_text(json.dumps(WATCHLIST))
    return p


def test_e2e_drie_runs(watchlist_file):
    with MockServer() as server:
        # Run 1: item 111 op voorraad, 222 wel listed maar uitverkocht.
        server.mock.scenario["111"] = {"listed": True, "in_stock": True, "price": 54.99}
        server.mock.scenario["222"] = {"listed": True, "in_stock": False, "price": None}
        r1 = run_tracker(server, watchlist_file)
        assert r1.returncode == 0, r1.stderr
        assert "Retailer API" in r1.stdout
        assert len(server.mock.state) == 2
        assert server.mock.events == []  # eerste waarneming: geen events
        assert server.mock.state[("bol", "111")]["in_stock"] is True
        assert server.mock.state[("bol", "111")]["price"] == 54.99

        # Run 2: prijsdaling op 111, restock van 222.
        server.mock.scenario["111"]["price"] = 49.99
        server.mock.scenario["222"] = {"listed": True, "in_stock": True, "price": 64.99}
        r2 = run_tracker(server, watchlist_file)
        assert r2.returncode == 0, r2.stderr
        types = sorted(e["type"] for e in server.mock.events)
        assert types == ["price_drop", "restock"]
        drop = next(e for e in server.mock.events if e["type"] == "price_drop")
        assert drop["old_price"] == 54.99 and drop["price"] == 49.99
        restock = next(e for e in server.mock.events if e["type"] == "restock")
        assert restock["ean"] == "222" and restock["price"] == 64.99
        assert server.mock.state[("bol", "222")]["in_stock"] is True

        # Run 3: niets veranderd -> geen nieuwe events.
        before = len(server.mock.events)
        r3 = run_tracker(server, watchlist_file)
        assert r3.returncode == 0, r3.stderr
        assert len(server.mock.events) == before


def test_e2e_tijdelijke_fout_slaat_item_over(watchlist_file):
    with MockServer() as server:
        server.mock.scenario["111"] = {"listed": True, "in_stock": True, "price": 54.99}
        server.mock.scenario["222"] = {"listed": True, "in_stock": True, "price": 64.99}
        server.mock.fail_next_offers = 1  # eerste offers-call faalt met 500
        r = run_tracker(server, watchlist_file)
        assert r.returncode == 0, r.stderr
        # item 111 faalde en is overgeslagen; 222 is wel verwerkt
        assert ("bol", "111") not in server.mock.state
        assert ("bol", "222") in server.mock.state
        assert "overgeslagen" in r.stdout


def test_e2e_foute_credentials_stopt_met_duidelijke_fout(watchlist_file):
    with MockServer() as server:
        server.mock.reject_auth = True
        server.mock.scenario["111"] = {"listed": True, "in_stock": True, "price": 54.99}
        r = run_tracker(server, watchlist_file)
        assert r.returncode != 0
        assert "authenticatie" in (r.stdout + r.stderr).lower()


def _drop_watchlist(tmp_path):
    p = tmp_path / "drop.json"
    p.write_text(json.dumps([
        {"name": "Pokémon 151 Booster Bundle", "ean": "111", "retailer": "bol", "drop_watch": True},
    ]))
    return p


def test_e2e_bol_drop_bij_offline_naar_online(tmp_path):
    wl = _drop_watchlist(tmp_path)
    with MockServer() as server:
        # Run 1: product bestaat in catalogus maar bol verkoopt (nog) niet.
        server.mock.scenario["111"] = {"listed": True}
        r1 = run_tracker(server, wl)
        assert r1.returncode == 0, r1.stderr
        assert server.mock.state[("bol", "111")]["in_stock"] is False
        assert server.mock.events == []

        # Run 2: bol zet z'n eigen aanbieding live -> BOL DROP.
        server.mock.scenario["111"] = {"listed": True, "bol": True, "bol_price": 54.99}
        r2 = run_tracker(server, wl)
        assert r2.returncode == 0, r2.stderr
        assert [e["type"] for e in server.mock.events] == ["bol_drop"]
        assert server.mock.events[0]["price"] == 54.99
        assert server.mock.state[("bol", "111")]["in_stock"] is True


def test_e2e_drop_watch_negeert_marketplace(tmp_path):
    wl = _drop_watchlist(tmp_path)
    with MockServer() as server:
        # Run 1: baseline, bol verkoopt niet.
        server.mock.scenario["111"] = {"listed": True}
        run_tracker(server, wl)
        # Run 2: alléén een marketplace-verkoper heeft voorraad, bol niet.
        server.mock.scenario["111"] = {"listed": True, "in_stock": True, "price": 30.0}
        r2 = run_tracker(server, wl)
        assert r2.returncode == 0, r2.stderr
        # Geen bol-drop: bol zelf verkoopt niet, dus voor drop_watch = niet op voorraad.
        assert server.mock.events == []
        assert server.mock.state[("bol", "111")]["in_stock"] is False


def test_e2e_kapotte_watchlist_regel_crasht_niet(tmp_path):
    wl = tmp_path / "wl.json"
    wl.write_text(json.dumps([
        {"name": "Zonder ean", "retailer": "bol"},          # mist ean
        {"ean": "333"},                                      # mist name
        {"name": "Goed product", "ean": "111", "retailer": "bol"},
    ]))
    with MockServer() as server:
        server.mock.scenario["111"] = {"listed": True, "in_stock": True, "price": 20.0}
        r = run_tracker(server, wl)
        assert r.returncode == 0, r.stderr
        assert "ongeldige watchlist-regel" in r.stdout
        # alleen het geldige product belandt in de state
        assert list(server.mock.state.keys()) == [("bol", "111")]


def test_e2e_events_niet_verloren_als_insert_faalt(watchlist_file):
    with MockServer() as server:
        server.mock.scenario["111"] = {"listed": True, "in_stock": False, "price": None}
        server.mock.scenario["222"] = {"listed": True, "in_stock": False, "price": None}
        run_tracker(server, watchlist_file)  # run 1: baseline (beide uitverkocht)

        # Run 2: 111 komt terug op voorraad, maar het events-insert faalt.
        server.mock.scenario["111"] = {"listed": True, "in_stock": True, "price": 30.0}
        server.mock.fail_events = True
        r2 = run_tracker(server, watchlist_file)
        assert r2.returncode != 0  # insert-fout stopt de run met een fout
        assert server.mock.events == []
        # Cruciaal: de state van 111 is NIET naar in_stock=True gezet,
        # want events worden vóór de state weggeschreven.
        assert server.mock.state[("bol", "111")]["in_stock"] is False

        # Run 3: insert werkt weer -> de restock wordt alsnog gemeld.
        server.mock.fail_events = False
        r3 = run_tracker(server, watchlist_file)
        assert r3.returncode == 0, r3.stderr
        assert [e["type"] for e in server.mock.events] == ["restock"]
        assert server.mock.state[("bol", "111")]["in_stock"] is True


def test_e2e_state_paginatie_over_meerdere_paginas(watchlist_file):
    with MockServer() as server:
        server.mock.page_size = 2  # server geeft max 2 rijen per pagina
        # vul de state met 5 bestaande producten, uitverkocht met prijs 10
        for i in range(5):
            server.mock.state[("bol", f"seed{i}")] = {
                "retailer": "bol", "ean": f"seed{i}", "name": f"Seed {i}",
                "in_stock": False, "listed": True, "price": 10.0, "id": i,
            }
        # één seed komt terug op voorraad -> alleen zichtbaar als paginatie klopt
        server.mock.scenario["seed3"] = {"listed": True, "in_stock": True, "price": 12.0}
        server.mock.scenario["111"] = {"listed": True, "in_stock": True, "price": 54.99}
        server.mock.scenario["222"] = {"listed": True, "in_stock": True, "price": 64.99}
        wl = watchlist_file.parent / "wl2.json"
        wl.write_text(json.dumps(WATCHLIST + [{"name": "Seed 3", "ean": "seed3", "retailer": "bol"}]))
        env = {**server.env(), "WATCHLIST": str(wl), "STATE_PAGE_SIZE": "2", "PATH": "/usr/bin:/bin"}
        r = subprocess.run([sys.executable, str(TRACKER_PY)], env=env,
                           capture_output=True, text=True, timeout=60)
        assert r.returncode == 0, r.stderr
        # seed3 stond op pagina 2/3; alleen met correcte paginatie zien we z'n
        # vorige (uitverkochte) status en dus de restock
        assert [e["type"] for e in server.mock.events] == ["restock"]
        assert server.mock.events[0]["ean"] == "seed3"


def test_e2e_drop_signal_wanneer_pagina_verschijnt(watchlist_file):
    with MockServer() as server:
        # Run 1: 222 bestaat nog helemaal niet (404).
        server.mock.scenario["111"] = {"listed": True, "in_stock": True, "price": 54.99}
        r1 = run_tracker(server, watchlist_file)
        assert r1.returncode == 0, r1.stderr
        assert server.mock.state[("bol", "222")]["listed"] is False

        # Run 2: pagina verschijnt zonder voorraad -> drop_signal.
        server.mock.scenario["222"] = {"listed": True, "in_stock": False, "price": None}
        r2 = run_tracker(server, watchlist_file)
        assert r2.returncode == 0, r2.stderr
        assert [e["type"] for e in server.mock.events] == ["drop_signal"]
        assert server.mock.events[0]["ean"] == "222"
