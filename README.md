# Boltracker

Volgt voorraad en prijzen van producten op bol.com (en later andere winkels) en
toont ze live in een dashboard. Handig om er snel bij te zijn wanneer een schaars
item terug op voorraad komt of in prijs daalt.

```
[ tracker.py ]  →  [ Supabase ]  →  [ index.html ]
 checkt winkels     database         dashboard in de browser
 (GitHub Actions)   (state+events)   (leest met anon key)
```

## Onderdelen

| Bestand | Wat |
|---|---|
| `index.html` | Het dashboard. Draait in demo-modus tot je de Supabase-keys invult. |
| `schema.sql` | Maakt de twee database-tabellen aan in Supabase. |
| `tracker/tracker.py` | Checkt de producten en schrijft naar Supabase. |
| `tracker/watchlist.json` | De producten die je volgt. |
| `.github/workflows/tracker.yml` | Draait de tracker automatisch elke ~10 min. |

## Eenmalige setup

### 1. Database-tabellen aanmaken
Open in Supabase je project → **SQL Editor** → **New query**, plak de inhoud van
[`schema.sql`](schema.sql) en klik **Run**.

### 2. Keys ophalen (Supabase → Project Settings → API)
- **Project URL** en **anon public key** → voor het dashboard.
- **service_role key** → voor de tracker. ⚠️ Deze is geheim: nooit in `index.html`
  of ergens openbaar zetten. Alleen als GitHub-secret (stap 4).

### 3. Dashboard koppelen
Zet bovenaan in [`index.html`](index.html) je waarden:
```js
const SUPABASE_URL = "https://JOUWPROJECT.supabase.co";
const SUPABASE_ANON_KEY = "je-anon-public-key";
```
Zodra deze kloppen verdwijnt de demo-modus en toont het dashboard live data.
Je kunt `index.html` hosten via GitHub Pages of gewoon lokaal openen.

### 4. GitHub-secrets instellen
In je repo → **Settings → Secrets and variables → Actions → New repository secret**,
voeg deze secrets toe:
- `SUPABASE_URL` = je Project URL
- `SUPABASE_SERVICE_KEY` = je **service_role** key
- `BOL_CLIENT_ID` en `BOL_CLIENT_SECRET` = je Retailer API credentials
  (zie "Bol via de officiële Retailer API" hieronder)

### 5. Watchlist vullen
Bewerk [`tracker/watchlist.json`](tracker/watchlist.json). Per product:
```json
{
  "name": "Productnaam",
  "ean": "0820650858079",
  "retailer": "bol"
}
```
- `ean` en `name` zijn verplicht (een regel zonder die twee wordt overgeslagen,
  niet fataal).
- Met de Retailer API is **alleen de `ean` nodig** — die bevraagt bol direct.
- `product_id` en `url` zijn optioneel en worden alleen gebruikt voor de
  "bekijk/koop op bol"-links in het dashboard.
- `retailer`: momenteel wordt **`bol`** ondersteund.

De tracker draait daarna vanzelf. Handmatig starten kan via **Actions → boltracker
→ Run workflow**.

## Lokaal draaien / testen
```bash
pip install -r tracker/requirements.txt
export SUPABASE_URL="https://JOUWPROJECT.supabase.co"
export SUPABASE_SERVICE_KEY="je-service-role-key"
export BOL_CLIENT_ID="je-client-id"          # officiële Retailer API
export BOL_CLIENT_SECRET="je-client-secret"
python tracker/tracker.py
```

## ✅ Bol via de officiële Retailer API (aanbevolen)
De tracker gebruikt de **officiële bol Retailer API** zodra je client credentials
instelt. Dat is dé betrouwbare route: het `competing offers`-endpoint geeft per
EAN de actuele beste aanbieding (prijs, beschikbaarheid) — officieel, stabiel en
met een ruime rate limit (900 requests/minuut).

**Credentials aanvragen:**
1. Je hebt een **zakelijk bol-verkoopaccount** nodig via
   [partnerplatform.bol.com](https://partnerplatform.bol.com) — registratie is
   gratis (vereist KVK-inschrijving, btw-nummer, ID en zakelijke bankrekening;
   je betaalt alleen commissie als je iets verkoopt).
2. In het **Seller Dashboard → Instellingen → Diensten → API Instellingen**:
   vul eerst een technisch contactpersoon in, maak dan onder *"Client
   credentials voor de Retailer API"* een credential aan. ⚠️ Het secret wordt
   maar één keer getoond — sla het direct veilig op.
3. Voeg ze toe als GitHub-secrets: `BOL_CLIENT_ID` en `BOL_CLIENT_SECRET`
   (naast de bestaande `SUPABASE_URL`/`SUPABASE_SERVICE_KEY`).

Extra opties via environment variables: `BOL_DEMO=1` gebruikt bol's
demo-omgeving (handig om je credentials/auth-flow te testen; geeft vaste
voorbeelddata), `BOL_COUNTRY=BE` voor de Belgische winkel.

**Alternatief zonder verkoopaccount:** het bol **Affiliate Program**
(partner.bol.com) geeft toegang tot de Marketing Catalog API met een
`offers/best`-endpoint per EAN — lagere instapdrempel, wel een
goedkeuringsproces. Nog niet ingebouwd; laat het weten als je die route wil.

**Zonder credentials** valt de tracker terug op best-effort HTML-scraping, maar
bol rendert prijs/voorraad client-side waardoor die fallback meestal "onbekend"
logt (en bewust géén valse restocks verzint).

## Tests
```bash
pip install -r tracker/requirements-dev.txt
pytest tracker/ -v
```
De suite bevat unit tests (event-logica, API-client met token-caching en
429/403-afhandeling) en end-to-end tests die de echte `tracker.py` als
subprocess draaien tegen een lokale mock van de bol API + Supabase.

## Andere winkels (MediaMarkt, Coolblue, …)
Het dashboard toont deze winkels al met eigen badges en koop-links. De **scraper**
ondersteunt voorlopig geen enkele winkel volledig headless; MediaMarkt en Coolblue
hebben bovendien nog stevigere botbescherming. De haakjes staan klaar in
`tracker/tracker.py` (`SCRAPERS`): vul een functie in en de winkel doet mee.

## Nette scraping
Dit haalt openbare productpagina's op voor persoonlijk gebruik. Houd het interval
rustig en de watchlist klein, zodat je een winkel niet onnodig belast.
