# Letterboxd Top 500 API

[![CI](https://github.com/mmdju/letterboxd-top500-api/actions/workflows/ci.yml/badge.svg)](https://github.com/mmdju/letterboxd-top500-api/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Cloudflare Workers](https://img.shields.io/badge/Cloudflare-Workers-F38020?logo=cloudflare&logoColor=white)](https://workers.cloudflare.com/)
[![PRs Welcome](https://img.shields.io/badge/PRs-welcome-brightgreen.svg)](https://github.com/mmdju/letterboxd-top500-api/pulls)

Clean JSON API for the official Letterboxd Top 500 films list, read live from
`https://letterboxd.com/official/list/letterboxds-top-500-films/`
(500 films across 5 pages, poster metadata is server-rendered so no proxy is needed)
and served from Cloudflare Workers with edge caching.

**No deploy needed — use the hosted API right now:**

Base URL: `https://letterboxd-top500.mmdju.workers.dev`

- Full list: [*/top500*](https://letterboxd-top500.mmdju.workers.dev/top500)
- First 10: [*/top500?limit=10*](https://letterboxd-top500.mmdju.workers.dev/top500?limit=10)
- Single film: [*/film/harakiri*](https://letterboxd-top500.mmdju.workers.dev/film/harakiri)
- Random pick: [*/random*](https://letterboxd-top500.mmdju.workers.dev/random)

Just open the links — no key, no setup. (Deploy your own copy only if you want
your own cache, stats and rate limits — see below.)

## Features

- Full Top 500 list (rank, title, year, Letterboxd slug + link)
- Edge cached (refreshed max once a week — the list barely changes), long fallback chain
- Bundled seed data so the API answers even when the live fetch is down
- Search, filter, sort and pagination
- Single-film lookup (`/film/:slug`) and `/random`
- Request counters backed by D1 (`/stats`)
- Facts only — no posters, ratings or images

## Endpoints

| Method | Path | Description |
| :--- | :--- | :--- |
| `GET` | `/` | Help page |
| `GET` | `/top500` | Full Top 500 list (cached, refreshed max once a week) |
| `GET` | `/top500?limit=10` | First N films |
| `GET` | `/film/harakiri` | Single film by Letterboxd slug (`404` if missing) |
| `GET` | `/random` | Random film from the list |

List filters (can be combined):

| Param | Example | Description |
| :--- | :--- | :--- |
| `search` | `?search=godfather` | Title contains, case-insensitive |
| `year` | `?year=1962` | Exact year match |
| `sort` | `?sort=year` | `rank\|year\|title` (default `rank`) |
| `order` | `?order=desc` | `asc\|desc` (default: `rank`/`title` asc, `year` desc) |
| `limit` | `?limit=10` | `1..500`, default `500` |
| `offset` | `?offset=20` | Skip N after filtering/sorting, default `0` |

Combined example: `/top500?search=the&year=1994&sort=title&order=asc&limit=5&offset=0`

Example item:

```json
{
  "rank": 1,
  "title": "Harakiri",
  "year": 1962,
  "slug": "harakiri",
  "url": "https://letterboxd.com/film/harakiri/"
}
```

Only factual fields are served (rank, title, year, link).
No posters, ratings or images — those belong to their copyright holders.

List responses wrap the items with paging info:

```json
{
  "source": "live: letterboxd.com official Top 500 list",
  "updatedAt": "2026-09-11T04:06:35.401Z",
  "stale": false,
  "total": 500,
  "count": 2,
  "offset": 0,
  "limit": 500,
  "filters": { "search": "godfather", "year": null, "sort": "rank", "order": "asc" },
  "data": [ { "rank": 1, "...": "..." }, { "rank": 2, "...": "..." } ]
}
```

## Examples

- [Python](examples/python.py) — standard library only, no install needed:

```bash
# against wrangler dev (PowerShell):
#   $env:BASE_URL="http://localhost:8787"; python examples/python.py
# against wrangler dev (bash):
#   BASE_URL=http://localhost:8787 python examples/python.py
# against the Python server:
#   BASE_URL=http://localhost:8000 python examples/python.py
```

## Python version

Prefer self-hosting with Python? `python/` is the same API on FastAPI:

```bash
cd python
pip install -r requirements.txt
uvicorn app:app        # local test at http://localhost:8000/top500
# Docs (Swagger): http://localhost:8000/docs
```
```bash
# Docker (run from the repo root, not from python/):
docker build -f python/Dockerfile -t letterboxd-top500-api . && docker run -p 8000:8000 letterboxd-top500-api
```

Same endpoints and response shape. Config via environment (see `python/.env.example`):

```bash
ADMIN_KEY=... uvicorn app:app --host 0.0.0.0 --port 8000
```

Use it as a library in your own project (no HTTP needed, run from inside `python/`):

```python
from app import query_list, get_by_slug, get_random

query_list(search="godfather", limit=5)
get_by_slug("harakiri")
get_random()
```

Seed data is shared with the Worker (`../src/seed.json`);
request counters live in a local SQLite file (`hits.db`).

## Test on your PC

```bash
npm install
npx wrangler dev        # local test at http://localhost:8787/top500
```

KV and D1 work locally with emulation, no setup needed for a first test.
(Windows: if `npx` is blocked by the execution policy, run `npx.cmd wrangler dev` instead.)

## Deploy

```bash
npx wrangler kv namespace create CACHE
# put the returned id into wrangler.toml

npx wrangler d1 create letterboxd-top500-db
# put the binding/id into wrangler.toml, then:
npx wrangler d1 migrations apply letterboxd-top500-db --remote

npx wrangler secret put ADMIN_KEY   # optional, protects /refresh
npx wrangler deploy
```

## Admin endpoints

These are not listed on the `/` help page, but they work:

| Method | Path | Description |
| :--- | :--- | :--- |
| `GET` | `/refresh?key=ADMIN_KEY` | Force a fresh fetch of the list (needs the key only if `ADMIN_KEY` is set) |
| `GET` | `/stats` | Total request counts, backed by D1 |

## Notes

- The list pages are read directly — no proxy needed. If the live fetch fails,
  the API serves the last good cached copy with `"stale": true`, otherwise the
  bundled seed (`src/seed.json`, snapshot 2026-09-11).
- Note: the Python version's live fetch can be throttled on some home/office
  networks (Letterboxd sometimes resets Python's default TLS fingerprint
  mid-response — it retries, then falls back to seed). The Worker version is
  unaffected. Either way the API keeps answering.
- The official list updates about once a week, so the cache lives for a week
  (`expirationTtl` is 4 weeks as a safety net).
- List pages carry no ratings — that's why items have rank/title/year/link only.
  Ratings can be added later via a TMDB enrichment step.

## License

MIT — see [LICENSE](LICENSE).

## Keywords

letterboxd top 500 api, letterboxd api, best movies list api, top rated films json, free movies api, cloudflare workers api, fastapi movies api.

## Project structure

```
src/index.js        worker (JS version): routes, live fetch, parsing, cache, stats
src/seed.json       seed data, top 500 films (500 titles, facts only)
python/app.py       same API in Python (FastAPI), self-hostable + importable (query_list/get_by_slug/get_random)
python/requirements.txt  Python dependencies (pinned)
python/Dockerfile   container for the Python version
python/.env.example sample config (ADMIN_KEY)
examples/python.py  tiny client example (stdlib only)
migrations/         D1 schema for the request counters
wrangler.toml       Worker, KV and D1 config (local placeholders until first deploy)
```
