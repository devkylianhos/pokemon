# PocketPop

Een Pokémon-fan-app in de **Sunny Pixel Pop**-stijl: 16-bit SNES-energie op warm
crème papier — candy-kleuren, dikke ink-borders en harde sticker-schaduwen.
Origineel fan-brand (eigen mascotte "Blip", verzonnen creature-namen); géén
officiële Pokémon-assets. "PocketPop" is een werknaam.

Geïmplementeerd vanuit een Claude Design-handoff: de React-prototypes zijn omgezet
naar zelfstandige HTML/CSS/JS zonder build-stap, zodat het meteen op GitHub Pages
draait.

## Schermen

| Bestand | Scherm |
|---|---|
| [`index.html`](index.html) | **Web-dashboard** — sidebar, stats, collectie-voortgang, activiteit |
| [`card-tracker.html`](card-tracker.html) | **Card tracker** — kaartenbinder met grid, filters, waarde en wishlist |
| [`app.html`](app.html) | **Mobiele dex-app** — browsen, zoeken, filteren, detail met stats en "vangen" |
| [`pocketpop.css`](pocketpop.css) | Gedeeld design system: tokens + alle componentklassen |
| [`assets/`](assets/) | Mascotte "Blip" (pixel-SVG, meerdere kleurvarianten) |

De drie schermen zijn onderling gelinkt via de sidebar-navigatie op het dashboard.

## Design system

- **Kleur:** crème papier `#FDF4E3`, wit voor kaarten, navy ink `#1E2245` voor tekst,
  borders én schaduwen. Vier candy-accenten (coral, teal, lime, amber) + 18 type-kleuren.
- **Type:** Jersey 15 (pixel-display, groot), Sora (UI/body), Silkscreen (mini-labels),
  Space Mono (cijfers/ids) — via Google Fonts.
- **Borders:** 3px ink op kaarten/knoppen, 2px op kleine controls.
- **Schaduwen:** harde offset zonder blur (sticker), altijd ink-kleurig.
- **Radii:** chunky (6/10/14/20/pill). **Animatie:** snappy arcade — hover lift, press-squish.

Alle 17 componenten uit de handoff (Button, Badge, Card, Input, Select, Checkbox,
Switch, Tabs, TypeChip, StatBar, ProgressBar, Dialog, Toast, IconButton …) zijn als
CSS-klassen in `pocketpop.css` beschikbaar.

## Lokaal bekijken

Geen build nodig — open de bestanden direct of serveer de map:

```bash
python3 -m http.server 4173
# open http://localhost:4173/index.html
```

## Tracker-backend (los onderdeel)

De map [`tracker/`](tracker/) bevat een aparte Python-tracker die voorraad en prijzen
op bol.com volgt via de officiële **bol Retailer API** en naar Supabase schrijft
(met GitHub Actions-workflow en testsuite). Dit is een onafhankelijk backend-project
dat losstaat van de PocketPop-frontend hierboven; zie de bestanden in `tracker/` en
[`schema.sql`](schema.sql).
