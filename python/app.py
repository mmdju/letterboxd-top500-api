"""Letterboxd Top 500 API - Python version (FastAPI).

Same API as the Cloudflare Worker in ../src, for those who'd rather
self-host with Python:

    pip install -r requirements.txt
    uvicorn app:app --host 0.0.0.0 --port 8000
    # or: docker build -t letterboxd-top500-api . && docker run -p 8000:8000 letterboxd-top500-api

Endpoints: /top500 (with search/filter/sort/pagination),
/film/{slug}, /random, /refresh (admin), /stats, / (help).

Use as a library in your own project:

    from app import query_list, get_by_slug, get_random

    top = query_list(search="godfather", limit=5)
    one = get_by_slug("harakiri")
    lucky = get_random()

Config via environment (.env supported if python-dotenv is installed):
    ADMIN_KEY=...     # required to enable /refresh (fail-closed without it)
"""

import hmac
import html
import http.client
import json
import os
import random
import re
import sqlite3
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Optional

from fastapi import FastAPI, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse

HERE = Path(__file__).parent
SEED_PATH = HERE.parent / "src" / "seed.json"
WEEK = 7 * 24 * 3600

try:  # optional: load python/.env for local dev
    from dotenv import load_dotenv

    load_dotenv(HERE / ".env")
except ImportError:
    pass

ADMIN_KEY = os.environ.get("ADMIN_KEY", "")


def admin_key_from(request: Request, key: str) -> str:
    """Key via `Authorization: Bearer <key>` (preferred) or `?key=` fallback."""
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return key or ""


def check_admin(key: str):
    """Fail closed: /refresh is unavailable until ADMIN_KEY is configured."""
    if not ADMIN_KEY:
        return JSONResponse(
            {"error": "Refresh unavailable (ADMIN_KEY not configured)"}, status_code=503
        )
    if not hmac.compare_digest(key or "", ADMIN_KEY):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    return None

LIST_BASE = "https://letterboxd.com/official/list/letterboxds-top-500-films/"
LIST_PAGES = 5
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)
POSTER_RE = re.compile(
    r'data-component-class="LazyPoster".*?data-item-name="([^"]+)"'
    r'.*?data-item-slug="([^"]+)".*?data-target-link="([^"]+)"',
    re.S,
)
YEAR_RE = re.compile(r"\((\d{4})\)\s*$")

VALID_SORTS = ("rank", "year", "title")


def load_seed():
    with open(SEED_PATH, encoding="utf-8") as f:
        return json.load(f)


CHART = {
    "seed": load_seed(),
    "seed_updated_at": "2026-09-11T00:00:00.000Z",
    "seed_source": "seed: Letterboxd Top 500 snapshot 2026-09-11 (rank, title, year, link)",
    "live_source": "live: letterboxd.com official Top 500 list",
}

# In-memory cache. Lost on restart, rebuilt on demand.
cache = {}

DB_PATH = HERE / "hits.db"


def now_iso():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def db():
    con = sqlite3.connect(DB_PATH)
    con.execute("CREATE TABLE IF NOT EXISTS hits (key TEXT PRIMARY KEY, count INTEGER)")
    return con


def record_hit(path):
    try:
        known = path in ("/", "/top500", "/film", "/random", "/refresh", "/stats")
        key = path if known else "other"
        con = db()
        con.execute(
            "INSERT INTO hits(key, count) VALUES ('total', 1) "
            "ON CONFLICT(key) DO UPDATE SET count = count + 1"
        )
        con.execute(
            "INSERT INTO hits(key, count) VALUES (?, 1) "
            "ON CONFLICT(key) DO UPDATE SET count = count + 1",
            (key,),
        )
        con.commit()
        con.close()
    except Exception:
        pass  # stats never break the API


def read_stats():
    out = {"total": 0, "by_path": {}}
    try:
        con = db()
        for key, count in con.execute("SELECT key, count FROM hits"):
            if key == "total":
                out["total"] = count
            else:
                out["by_path"][key] = count
        con.close()
    except Exception:
        pass
    return out


def get_list(force=False):
    """Cached list when fresh, else live, else last good copy, else seed."""
    if not force and cache.get("payload"):
        item = cache["payload"]
        if time.time() - item["fetched_at"] < WEEK:
            return {**item, "stale": False}
    try:
        data = fetch_live_list()
        payload = {
            "source": CHART["live_source"],
            "updatedAt": now_iso(),
            "fetched_at": time.time(),
            "count": len(data),
            "total": len(data),
            "stale": False,
            "data": data,
        }
        cache["payload"] = payload
        return payload
    except Exception as err:
        if cache.get("payload"):
            return {**cache["payload"], "stale": True}
        return {
            "source": CHART["seed_source"],
            "updatedAt": CHART["seed_updated_at"],
            "fetched_at": time.time(),
            "count": len(CHART["seed"]),
            "total": len(CHART["seed"]),
            "stale": True,
            "fallback": True,
            "liveError": str(err),
            "note": "Live fetch failed. Serving bundled seed data.",
            "data": CHART["seed"],
        }


def default_order(sort: str) -> str:
    return "desc" if sort == "year" else "asc"


def query_list(
    search: str = "",
    year: Optional[int] = None,
    sort: str = "rank",
    order: Optional[str] = None,
    offset: int = 0,
    limit: int = 500,
    force: bool = False,
) -> dict:
    """Library-friendly query: filter + sort + paginate the list.

    Mirrors the JS `listResponse()` in ../src/index.js so both versions
    behave identically. Returns the public payload dict.
    """
    sort = (sort or "rank").lower()
    if sort not in VALID_SORTS:
        sort = "rank"
    order = (order or default_order(sort)).lower()
    if order not in ("asc", "desc"):
        order = default_order(sort)
    limit = min(max(int(limit or 500), 1), 500)
    offset = max(int(offset or 0), 0)
    search = (search or "").strip()

    result = get_list(force=force)
    total = len(result["data"])
    filtered = result["data"]
    if search:
        q = search.lower()
        filtered = [e for e in filtered if q in (e.get("title") or "").lower()]
    if year is not None:
        filtered = [e for e in filtered if e.get("year") == year]
    count = len(filtered)

    if sort == "title":
        ordered = sorted(filtered, key=lambda e: (e.get("title") or "").lower(),
                         reverse=(order == "desc"))
    else:
        not_none = sorted([e for e in filtered if e.get(sort) is not None],
                          key=lambda e: e.get(sort), reverse=(order == "desc"))
        nones = [e for e in filtered if e.get(sort) is None]
        ordered = not_none + nones

    page = ordered[offset : offset + limit]
    out = {k: v for k, v in result.items() if k != "fetched_at"}
    out.update(
        {
            "total": total,
            "count": count,
            "offset": offset,
            "limit": limit,
            "filters": {
                "search": search or None,
                "year": year,
                "sort": sort,
                "order": order,
            },
            "data": page,
        }
    )
    return out


def get_by_slug(slug: str, force: bool = False) -> Optional[dict]:
    """Library-friendly single lookup. Returns {meta, data} or None."""
    result = get_list(force=force)
    for e in result["data"]:
        if e.get("slug") == slug:
            return {"meta": {k: v for k, v in result.items() if k != "data"}, "data": e}
    return None


def get_random(force: bool = False) -> dict:
    """Library-friendly random pick. Returns {type, meta, data}."""
    result = get_list(force=force)
    if not result["data"]:
        raise RuntimeError("Empty list")
    return {
        "type": "movie",
        "meta": {k: v for k, v in result.items() if k != "data"},
        "data": random.choice(result["data"]),
    }


def http_get(url):
    """GET with a browser UA. Tolerates flaky chunked responses."""
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=60) as res:
            if res.status != 200:
                raise RuntimeError(f"List page responded with HTTP {res.status}")
            raw = res.read()
    except http.client.IncompleteRead as err:
        raw = err.partial  # use what arrived; parser validates the count
    return raw.decode("utf-8", "replace")


def fetch_live_list():
    items = []
    for page in range(1, LIST_PAGES + 1):
        url = LIST_BASE if page == 1 else f"{LIST_BASE}page/{page}/"
        parsed = []
        # urllib sometimes drops chunked responses midway (partial HTML).
        # Retry a few times - a good attempt parses 100 films.
        for attempt in range(5):
            parsed = parse_list_page(http_get(url))
            if len(parsed) >= 50:
                break
            time.sleep(3)
        if len(parsed) < 50:
            raise RuntimeError(f"Only parsed {len(parsed)} films from page {page} (need 50+)")
        for e in parsed:
            e["rank"] = len(items) + 1
            items.append(e)
        time.sleep(2)
    if len(items) < 400:
        raise RuntimeError(f"Only parsed {len(items)} films total (need 400+)")
    return items


def parse_list_page(page_html):
    entries = []
    for m in POSTER_RE.finditer(page_html):
        name = html.unescape(m.group(1)).strip()
        ym = YEAR_RE.search(name)
        entries.append(
            {
                "title": (name[: ym.start()] if ym else name).strip(),
                "year": int(ym.group(1)) if ym else None,
                "slug": m.group(2),
                "url": "https://letterboxd.com" + m.group(3),
            }
        )
    return entries


app = FastAPI(
    title="Letterboxd Top 500 API",
    description="Clean JSON API for the official Letterboxd Top 500 list (facts only). Same shape as the Cloudflare Worker version.",
    version="0.1.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "OPTIONS"],
    allow_headers=["*"],
)


ListSort = Literal["rank", "year", "title"]
ListOrder = Literal["asc", "desc"]


@app.get("/top500", summary="Letterboxd Top 500")
def top500(
    limit: int = Query(500, ge=1, le=500, description="How many to return"),
    offset: int = Query(0, ge=0, description="Skip N after filtering/sorting"),
    search: str = Query("", description="Case-insensitive substring on title"),
    year: Optional[int] = Query(None, ge=1800, le=2100, description="Exact year match"),
    sort: ListSort = Query("rank", description="Sort field"),
    order: Optional[ListOrder] = Query(None, description="asc|desc (default smart)"),
):
    record_hit("/top500")
    return query_list(search=search, year=year, sort=sort, order=order,
                      offset=offset, limit=limit)


@app.get("/film/{slug}", summary="Single film by Letterboxd slug")
def film_by_slug(slug: str):
    record_hit("/film")
    if not re.fullmatch(r"[a-z0-9-]{1,100}", slug or ""):
        return JSONResponse({"error": "Invalid film slug. Use like /film/harakiri"},
                            status_code=400)
    found = get_by_slug(slug)
    if not found:
        return JSONResponse({"error": "Not found", "slug": slug}, status_code=404)
    meta, item = found["meta"], found["data"]
    return {
        "source": meta.get("source"),
        "updatedAt": meta.get("updatedAt"),
        "stale": meta.get("stale", True),
        **({"fallback": True, "note": meta.get("note")} if meta.get("fallback") else {}),
        "type": "movie",
        "data": item,
    }


@app.get("/random", summary="Random film")
def random_title():
    record_hit("/random")
    try:
        picked = get_random()
    except RuntimeError as err:
        return JSONResponse({"error": str(err)}, status_code=503)
    meta = picked["meta"]
    return {
        "source": meta.get("source"),
        "updatedAt": meta.get("updatedAt"),
        "stale": meta.get("stale", True),
        **({"fallback": True, "note": meta.get("note")} if meta.get("fallback") else {}),
        "type": picked["type"],
        "data": picked["data"],
    }


@app.get("/refresh")
def refresh(request: Request, key: str = ""):
    record_hit("/refresh")
    denied = check_admin(admin_key_from(request, key))
    if denied is not None:
        return denied
    result = get_list(force=True)
    if result.get("fallback"):
        return {
            "error": "Refresh failed",
            "detail": result.get("liveError", "Live fetch failed"),
            "fallback": True,
            "note": result.get("note"),
            "count": len(result["data"]),
        }
    return {"refreshed": True, "count": len(result["data"]), "updatedAt": result["updatedAt"]}


@app.get("/stats")
def stats():
    record_hit("/stats")
    s = read_stats()
    return {
        "total_requests": s["total"],
        "by_path": s["by_path"],
        "updatedAt": now_iso(),
    }


@app.get("/", response_class=HTMLResponse)
def help_page():
    record_hit("/")
    return """<!doctype html><html><head><meta charset="utf-8"><title>Letterboxd Top 500 API</title></head>
<body style="font-family:sans-serif;max-width:680px;margin:40px auto;line-height:1.7">
<h1>Letterboxd Top 500 API</h1>
<p>Clean JSON served from Python. The official Letterboxd Top 500 list (facts only).</p>
<ul>
<li><code>GET /top500</code> - full Top 500 list</li>
<li><code>GET /film/harakiri</code> - single film</li>
<li><code>GET /random</code> - random film</li>
</ul>
<p><b>Filters:</b> <code>?search=godfather&year=1972&sort=title&order=asc&limit=10&offset=0</code></p>
<p>Docs: <a href="/docs">/docs</a> - OpenAPI auto-generated.</p>
</body></html>"""
