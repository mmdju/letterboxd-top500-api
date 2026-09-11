"""Minimal example: fetch the Letterboxd Top 500 list.

Needs nothing but the Python standard library:

    python python.py
    BASE_URL=http://localhost:8000 python python.py   # against local server
"""

import json
import os
import urllib.parse
import urllib.request

# Local dev servers. After deploy, point at your public Worker URL.
BASE = os.environ.get("BASE_URL", "http://localhost:8787")


def get(path):
    req = urllib.request.Request(
        BASE + path,
        headers={"User-Agent": "Mozilla/5.0 (letterboxd-top500-api example)"},
    )
    with urllib.request.urlopen(req, timeout=30) as res:
        return json.load(res)


def show(title, payload, n=5):
    total = payload.get("total", payload.get("count"))
    print(f"\n{title} (showing {len(payload['data'])} of {payload['count']}, total {total}):")
    for item in payload["data"][:n]:
        print(f"  #{item['rank']:>3}  {item['title']} ({item['year']})  {item['slug']}")


def show_one(title, payload):
    item = payload["data"]
    print(f"\n{title}: #{item['rank']} {item['title']} ({item['year']}) {item['url']}")


if __name__ == "__main__":
    # 1. Plain list (old way still works)
    show("Top films", get("/top500?limit=5"))

    # 2. Search + filter + sort + pagination (new)
    q = urllib.parse.urlencode({"search": "godfather", "sort": "year", "order": "asc"})
    show("Search 'godfather'", get(f"/top500?{q}"))

    q = urllib.parse.urlencode({"year": 1994, "limit": 5})
    show("Films from 1994", get(f"/top500?{q}"))

    q = urllib.parse.urlencode({"sort": "title", "order": "asc", "limit": 5})
    show("First alphabetically", get(f"/top500?{q}"))

    # 3. Single film + random (new)
    show_one("Single film", get("/film/harakiri"))
    show_one("Random pick", get("/random"))

    # 4. Use as a library (no HTTP needed, same logic as the server).
    #     Run from inside python/:
    #     from app import query_list, get_by_slug, get_random
    #     query_list(search="godfather", limit=3)
