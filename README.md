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
voeg twee secrets toe:
- `SUPABASE_URL` = je Project URL
- `SUPABASE_SERVICE_KEY` = je **service_role** key

### 5. Watchlist vullen
Bewerk [`tracker/watchlist.json`](tracker/watchlist.json). Per product:
```json
{
  "name": "Productnaam",
  "ean": "0820650858079",
  "retailer": "bol",
  "product_id": "9300000135741181",
  "url": "https://www.bol.com/nl/nl/p/-/9300000135741181/"
}
```
- `retailer`: momenteel wordt **`bol`** ondersteund door de scraper.
- Geef `product_id` **of** `url` op (een directe productpagina werkt het
  betrouwbaarst). `ean` is verplicht: het koppelt status aan gebeurtenissen.

De tracker draait daarna vanzelf. Handmatig starten kan via **Actions → boltracker
→ Run workflow**.

## Lokaal draaien / testen
```bash
pip install -r tracker/requirements.txt
export SUPABASE_URL="https://JOUWPROJECT.supabase.co"
export SUPABASE_SERVICE_KEY="je-service-role-key"
python tracker/tracker.py
```

## ⚠️ Belangrijk: bol rendert prijs/voorraad client-side
Uit testen blijkt dat bol.com de **prijs en voorraad pas via JavaScript laadt** —
de gewone HTML-response bevat ze niet. Deze requests-gebaseerde tracker kan ze
daarom vaak niet betrouwbaar uitlezen; hij logt dan "onbekend" en verzint géén
valse restocks (de laatst bekende waarden blijven staan).

Om bol écht betrouwbaar te volgen zijn er twee routes:
1. **Headless browser** (Playwright) die de pagina volledig rendert en daarna
   prijs/voorraad uit de DOM leest. Werkt, maar is zwaarder en valt eerder op bij
   botbescherming. Kan op GitHub Actions met een extra install-stap.
2. **Scraping-dienst/API** (bijv. een betaalde proxy die JS rendert).

De rest van de pijplijn (Supabase, gebeurtenis-logica, dashboard, workflow) staat
en werkt; alleen `scrape_bol` moet je opwaarderen naar route 1 of 2 voor live data.

## Andere winkels (MediaMarkt, Coolblue, …)
Het dashboard toont deze winkels al met eigen badges en koop-links. De **scraper**
ondersteunt voorlopig geen enkele winkel volledig headless; MediaMarkt en Coolblue
hebben bovendien nog stevigere botbescherming. De haakjes staan klaar in
`tracker/tracker.py` (`SCRAPERS`): vul een functie in en de winkel doet mee.

## Nette scraping
Dit haalt openbare productpagina's op voor persoonlijk gebruik. Houd het interval
rustig en de watchlist klein, zodat je een winkel niet onnodig belast.
