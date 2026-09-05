# Recipe Swipe

A Tinder-style card interface for approving or rejecting recipes into a
[Mealie](https://mealie.io) instance. Swipe right to import a recipe, left to
reject it, up for a more detailed view before deciding.

Built to sit in front of a **staging directory**: a folder of pending recipe
records (JSON + a hero image each) produced by some upstream ingestion
process, plus `pending/`, `approved/`, `rejected/` subfolders and a couple of
small JSON state files. This repo includes a minimal harvester (`src/`) that
can populate that staging directory itself from a curated list of recipe
blogs, `sources.json`.

## What it does

- **Deck** — every record in `staging/pending/` becomes a swipeable card:
  hero image, title, time, servings, a partial ingredient preview, and a diet
  tag if the record carries one.
- **Swipe right** → `POST /api/recipes/create/html-or-json` against your Mealie
  instance, using the record's stored `schema.org` JSON-LD so the import
  works even if the source page has since changed or disappeared. The file
  moves from `pending/` to `approved/`.
- **Swipe left** → the file moves to `rejected/` and its canonical URL is
  added to a blacklist, so it's never re-staged by a future harvest.
- **Swipe up** → a detail sheet slides in with the full ingredient list, the
  method, description, keywords and source link. From there, right/left still
  approve/reject; down dismisses back to the card, unjudged.
- **Buttons and keys too** — a reject/info/approve bar sits under the deck and
  the arrow keys drive the same paths. A gesture that silently does nothing is
  indistinguishable from a broken app, so nothing is gesture-only.
- **Pull more** — trigger the harvester on demand from inside the app, with
  optional source/course/search filters, rather than only via an external
  cron job.

## Dry run

Set `SWIPE_DRY_RUN=1` on any instance that isn't your real one. Swipes still
file the staged record, so the whole flow is exercised, but the Mealie write is
skipped and the UI shows a **Dry run** badge. A dev server needs real Mealie
credentials to be worth testing against, which is exactly why it needs this —
otherwise a test swipe-right lands in your real recipe library.

```bash
curl -s -u user:pass http://localhost:9926/api/health   # -> "dry_run": true
```

## Stack

FastAPI backend (`webapp/backend/`), no-build-step vanilla JS/CSS frontend
(`webapp/frontend/`) as an installable PWA. [Catppuccin](https://catppuccin.com)
Mocha (dark) / Latte (light) color scheme, following system preference.

## Running it

```bash
cp .env.example .env   # fill in MEALIE_URL, MEALIE_TOKEN, SWIPE_AUTH_USER/PASS
docker compose up -d --build
```

Bind-mount your own staging directory over `/data/staging` in
`docker-compose.yml` (or point `STAGING_ROOT` at wherever it lives). The app
listens on `:9926`, HTTP Basic auth in front of every `/api/*` route.

## Staging record shape

```json
{
  "schema": 1,
  "id": "<sha1 of canonical_url>",
  "canonical_url": "https://example.com/a-recipe/",
  "source": "example.com",
  "source_name": "Example Blog",
  "staged_at": "2026-01-01T10:00:00-07:00",
  "diet": "Vegan",
  "preview": { "title": "...", "description": "...", "total_minutes": 45, "servings": 4,
               "ingredient_count": 12, "instruction_count": 8, "course": ["Dinner"], "keywords": [] },
  "jsonld": { "@type": "Recipe", "...": "..." }
}
```

`jsonld` is the page's own schema.org Recipe block - a few KB, and exactly
what Mealie's own scraper reads on import.
