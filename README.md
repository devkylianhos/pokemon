# PocketPop

Een restock- en drop-alert-tracker voor TCG en verzamelitems — "wees er vóór de
scalpers". Vormgegeven in de **Sunny Pixel Pop**-stijl: 16-bit SNES-energie op warm
crème papier, candy-kleuren, dikke ink-borders en harde sticker-schaduwen. Origineel
fan-brand (eigen mascotte "Blip"); géén officiële Pokémon-assets. "PocketPop" is een
werknaam.

## Frontend

| Bestand | Wat |
|---|---|
| [`index.html`](index.html) | **Landing** — hero, live-feed-voorbeeld, features, roadmap, prijzen, community |
| [`dashboard.html`](dashboard.html) | **Live dashboard** — watchlist, restocks/prijsdalingen, koop-links, meldingen |
| [`pocketpop.css`](pocketpop.css) | Design system: tokens + componentklassen (Sunny Pixel Pop) |
| [`assets/`](assets/) | Mascotte "Blip" (pixel-SVG) |

Het dashboard leest uit **Supabase** en draait in **demo-modus** zolang de keys niet
zijn ingevuld (dan zie je voorbeelddata met een gele banner). Features: zoeken,
filters (status + winkel), sorteren, prijs-sparklines, "hot" items (sneller pollen +
luider alarm), browser-/geluids-/telefoonmeldingen (ntfy) en per-winkel koop-links.

Vul bovenaan `dashboard.html` je `SUPABASE_URL` en `SUPABASE_ANON_KEY` in voor live data.

### Design system
Crème papier `#FDF4E3`, navy ink `#1E2245` voor tekst/borders/schaduwen, vier
candy-accenten (coral, teal, lime, amber). Fonts: Jersey 15 (pixel-display), Sora
(UI/body), Silkscreen (mini-labels), Space Mono (cijfers) — via Google Fonts.
Dikke ink-borders, harde sticker-schaduwen, chunky radii, snappy arcade-animatie.

## Backend — tracker

De map [`tracker/`](tracker/) bevat de Python-tracker die voorraad en prijzen volgt
via de officiële **bol Retailer API** en naar Supabase schrijft. Draait automatisch
via GitHub Actions ([`.github/workflows/tracker.yml`](.github/workflows/tracker.yml))
en heeft een testsuite (`pytest tracker/`).

**Setup:** voer [`schema.sql`](schema.sql) uit in Supabase, zet de GitHub-secrets
(`SUPABASE_URL`, `SUPABASE_SERVICE_KEY`, `BOL_CLIENT_ID`, `BOL_CLIENT_SECRET`) en
vul [`tracker/watchlist.json`](tracker/watchlist.json). Zie de commentaarblokken in
`tracker/tracker.py` voor details.

## Lokaal bekijken
Geen build nodig:
```bash
python3 -m http.server 4173
# open http://localhost:4173/index.html
```
