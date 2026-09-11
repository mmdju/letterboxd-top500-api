// Letterboxd Top 500 as clean JSON, served from Cloudflare Workers.
//
// The official Top 500 list (500 films across 5 pages) is read directly
// from letterboxd.com — the poster grid is server-rendered with all
// metadata in data attributes, so no proxy is needed. Only facts are
// served (rank, title, year, link) — no posters, ratings or images.
//
//   GET /               help page
//   GET /top500         full list, cached up to a week
//   List query params:
//     limit=500       1..500, how many to return
//     offset=0        skip N after filtering/sorting
//     search=         case-insensitive substring on title
//     year=1962       exact year match
//     sort=rank       rank|year|title
//     order=asc       asc|desc (default: rank/title asc, year desc)
//   GET /film/harakiri    single film by Letterboxd slug
//   GET /random           random film from the list
//   GET /refresh          force a fresh fetch (admin)
//   GET /stats            request counters (admin)

import SEED from "./seed.json";

const LIST = {
  base: "https://letterboxd.com/official/list/letterboxds-top-500-films/",
  pages: 5,
  perPage: 100,
  cacheKey: "top500:v1",
  seed: SEED,
  // Snapshot captured 2026-09-11 with the prototype parser (500/500 clean).
  seedUpdatedAt: "2026-09-11T00:00:00.000Z",
  seedSource: "seed: Letterboxd Top 500 snapshot 2026-09-11 (rank, title, year, link)",
  liveSource: "live: letterboxd.com official Top 500 list",
};
const WEEK = 7 * 24 * 3600;
const UA =
  "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36";
const VALID_SORTS = ["rank", "year", "title"];
// data-item-name="Harakiri (1962)" ... data-item-slug="harakiri" ... data-target-link="/film/harakiri/"
const POSTER_RE =
  /data-component-class="LazyPoster"[\s\S]*?data-item-name="([^"]+)"[\s\S]*?data-item-slug="([^"]+)"[\s\S]*?data-target-link="([^"]+)"/g;
const YEAR_RE = /\((\d{4})\)\s*$/;

export default {
  async fetch(request, env) {
    const url = new URL(request.url);

    // CORS preflight for browser apps.
    if (request.method === "OPTIONS") {
      return new Response(null, {
        status: 204,
        headers: {
          "access-control-allow-origin": "*",
          "access-control-allow-methods": "GET, OPTIONS",
          "access-control-allow-headers": "*",
          "access-control-max-age": "86400",
        },
      });
    }

    if (url.pathname === "/stats") {
      await recordHit(env, "/stats");
      const stats = await readStats(env);
      return json({
        total_requests: stats.total,
        by_path: stats.by_path,
        d1_connected: !!env.DB,
        updatedAt: new Date().toISOString(),
      });
    }

    if (url.pathname === "/top500") {
      await recordHit(env, "/top500");
      try {
        const params = parseListParams(url.searchParams);
        const result = await getList(env, false);
        return json(listResponse(result, params));
      } catch (err) {
        return json({ error: "Failed to fetch list", detail: String((err && err.message) || err) }, 503);
      }
    }

    if (url.pathname.startsWith("/film/")) {
      await recordHit(env, "/film");
      const slug = url.pathname.split("/")[2] || "";
      if (!/^[a-z0-9-]{1,100}$/.test(slug)) {
        return json({ error: "Invalid film slug. Use like /film/harakiri" }, 400);
      }
      try {
        const result = await getList(env, false);
        const item = result.data.find((e) => e.slug === slug) || null;
        if (!item) return json({ error: "Not found", slug }, 404);
        return json({
          source: result.source,
          updatedAt: result.updatedAt,
          stale: result.stale,
          ...(result.fallback ? { fallback: true, note: result.note } : {}),
          type: "movie",
          data: item,
        });
      } catch (err) {
        return json({ error: "Failed to fetch list", detail: String((err && err.message) || err) }, 503);
      }
    }

    if (url.pathname === "/random") {
      await recordHit(env, "/random");
      try {
        const result = await getList(env, false);
        if (!result.data.length) return json({ error: "Empty list" }, 503);
        const item = result.data[Math.floor(Math.random() * result.data.length)];
        return json({
          source: result.source,
          updatedAt: result.updatedAt,
          stale: result.stale,
          ...(result.fallback ? { fallback: true, note: result.note } : {}),
          type: "movie",
          data: item,
        });
      } catch (err) {
        return json({ error: "Failed to fetch list", detail: String((err && err.message) || err) }, 503);
      }
    }

    if (url.pathname === "/refresh") {
      await recordHit(env, "/refresh");
      if (env.ADMIN_KEY) {
        const key = url.searchParams.get("key") || "";
        if (key !== env.ADMIN_KEY) return json({ error: "Unauthorized" }, 401);
      }
      try {
        const result = await getList(env, true);
        return json(
          result.fallback
            ? {
                error: "Refresh failed",
                detail: result.liveError || "Live fetch failed",
                fallback: true,
                note: result.note,
                count: result.data.length,
              }
            : { refreshed: true, count: result.data.length, updatedAt: result.updatedAt }
        );
      } catch (err) {
        return json({ error: "Refresh failed", detail: String((err && err.message) || err) }, 503);
      }
    }

    await recordHit(env, url.pathname === "/" ? "/" : "other");
    return new Response(helpHtml(), {
      headers: {
        "content-type": "text/html; charset=utf-8",
        "access-control-allow-origin": "*",
      },
    });
  },
};

// ---- list querying (shared logic with python/app.py) ----

function parseListParams(sp) {
  const limit = Math.min(Math.max(parseInt(sp.get("limit") || "500", 10) || 500, 1), 500);
  const offset = Math.max(parseInt(sp.get("offset") || "0", 10) || 0, 0);
  const search = (sp.get("search") || "").trim();
  const yearRaw = (sp.get("year") || "").trim();
  const year = /^\d{4}$/.test(yearRaw) ? parseInt(yearRaw, 10) : null;
  const sortRaw = (sp.get("sort") || "rank").trim().toLowerCase();
  const sort = VALID_SORTS.includes(sortRaw) ? sortRaw : "rank";
  const orderRaw = (sp.get("order") || "").trim().toLowerCase();
  // Sensible defaults: rank/title ascending, year descending.
  const defaultOrder = sort === "year" ? "desc" : "asc";
  const order = orderRaw === "asc" || orderRaw === "desc" ? orderRaw : defaultOrder;
  return { limit, offset, search, year, sort, order };
}

function listResponse(listResult, params) {
  const total = listResult.data.length;
  let filtered = listResult.data;
  if (params.search) {
    const q = params.search.toLowerCase();
    filtered = filtered.filter((e) => (e.title || "").toLowerCase().includes(q));
  }
  if (params.year != null) {
    filtered = filtered.filter((e) => e.year === params.year);
  }
  const count = filtered.length;
  const sorted = [...filtered].sort((a, b) => {
    let cmp = 0;
    if (params.sort === "title") {
      cmp = String(a.title || "").localeCompare(String(b.title || ""));
    } else {
      const av = a[params.sort] ?? null;
      const bv = b[params.sort] ?? null;
      if (av == null && bv == null) cmp = 0;
      else if (av == null) cmp = 1; // nulls last
      else if (bv == null) cmp = -1;
      else cmp = av - bv;
    }
    return params.order === "desc" ? -cmp : cmp;
  });
  const data = sorted.slice(params.offset, params.offset + params.limit);
  return {
    ...listResult,
    total,
    count,
    offset: params.offset,
    limit: params.limit,
    filters: {
      search: params.search || null,
      year: params.year,
      sort: params.sort,
      order: params.order,
    },
    data,
  };
}

// Cached list when fresh, otherwise fetch live and cache. Falls back to
// the last good copy, then to the bundled seed.
async function getList(env, force) {
  if (!force && env.CACHE) {
    const cached = await env.CACHE.get(LIST.cacheKey, "json");
    if (cached && Date.now() - cached.fetchedAt < WEEK * 1000) {
      return { ...cached, stale: false };
    }
  }

  try {
    const data = await fetchLiveList();
    const payload = {
      source: LIST.liveSource,
      updatedAt: new Date().toISOString(),
      fetchedAt: Date.now(),
      count: data.length,
      total: data.length,
      stale: false,
      data,
    };
    if (env.CACHE) {
      await env.CACHE.put(LIST.cacheKey, JSON.stringify(payload), {
        expirationTtl: WEEK * 4,
      });
    }
    return payload;
  } catch (err) {
    const liveError = String((err && err.message) || err);
    if (env.CACHE) {
      const cached = await env.CACHE.get(LIST.cacheKey, "json");
      if (cached) return { ...cached, stale: true };
    }
    return {
      source: LIST.seedSource,
      updatedAt: LIST.seedUpdatedAt,
      fetchedAt: Date.now(),
      count: LIST.seed.length,
      total: LIST.seed.length,
      stale: true,
      fallback: true,
      liveError,
      note: "Live fetch failed. Serving bundled seed data.",
      data: LIST.seed,
    };
  }
}

// All 5 list pages, parsed and ranked 1..500. Throws when unusable.
async function fetchLiveList() {
  const items = [];
  for (let page = 1; page <= LIST.pages; page++) {
    const pageUrl = page === 1 ? LIST.base : `${LIST.base}page/${page}/`;
    const res = await fetch(pageUrl, { headers: { "User-Agent": UA } });
    if (!res.ok) {
      throw new Error(`List page ${page} responded with HTTP ${res.status}`);
    }
    const html = await res.text();
    const parsed = parseListPage(html);
    if (parsed.length < 50) {
      throw new Error(`Only parsed ${parsed.length} films from list page ${page} (need 50+)`);
    }
    for (const e of parsed) {
      e.rank = items.length + 1;
      items.push(e);
    }
  }
  if (items.length < 400) {
    throw new Error(`Only parsed ${items.length} films total (need 400+)`);
  }
  return items;
}

function parseListPage(html) {
  const entries = [];
  POSTER_RE.lastIndex = 0;
  let m;
  while ((m = POSTER_RE.exec(html)) !== null) {
    const name = decodeEntities(m[1]).trim();
    const ym = name.match(YEAR_RE);
    entries.push({
      title: (ym ? name.slice(0, ym.index) : name).trim(),
      year: ym ? parseInt(ym[1], 10) : null,
      slug: m[2],
      url: "https://letterboxd.com" + m[3],
    });
  }
  return entries;
}

function decodeEntities(s) {
  return s
    .replace(/&amp;/g, "&")
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">")
    .replace(/&quot;/g, '"')
    .replace(/&apos;|&#0*39;|&#x0*27;/gi, "'")
    .replace(/&#0*(\d+);/g, (_, n) => String.fromCharCode(parseInt(n, 10)))
    .replace(/&#x([0-9a-f]+);/gi, (_, h) => String.fromCharCode(parseInt(h, 16)));
}

// Request counters in D1. Stats failures never break the API.
async function recordHit(env, path) {
  try {
    if (!env.DB) return;
    const known = ["/", "/top500", "/film", "/random", "/refresh", "/stats"].includes(path);
    const batch = [
      env.DB.prepare(
        'INSERT INTO hits("key", count) VALUES (\'total\', 1) ON CONFLICT("key") DO UPDATE SET count = count + 1'
      ),
      env.DB.prepare(
        'INSERT INTO hits("key", count) VALUES (?1, 1) ON CONFLICT("key") DO UPDATE SET count = count + 1'
      ).bind(known ? path : "other"),
    ];
    await env.DB.batch(batch);
  } catch {
    // ignore
  }
}

async function readStats(env) {
  const out = { total: 0, by_path: {} };
  try {
    if (!env.DB) return out;
    const rows = await env.DB.prepare('SELECT "key", count FROM hits').all();
    for (const r of rows.results || []) {
      if (r.key === "total") out.total = r.count;
      else out.by_path[r.key] = r.count;
    }
  } catch {
    // ignore, return zeros
  }
  return out;
}

function json(obj, status = 200) {
  return new Response(JSON.stringify(obj, null, 2), {
    status,
    headers: {
      "content-type": "application/json; charset=utf-8",
      "access-control-allow-origin": "*",
      "cache-control": "public, max-age=3600",
    },
  });
}

function helpHtml() {
  return `<!doctype html><html><head><meta charset="utf-8"><title>Letterboxd Top 500 API</title></head>
<body style="font-family:sans-serif;max-width:680px;margin:40px auto;line-height:1.7">
<h1>Letterboxd Top 500 API</h1>
<p>Clean JSON served from the edge. The official Letterboxd Top 500 list (facts only: rank, title, year, link).</p>
<ul>
<li><code>GET /top500</code> — full Top 500 list</li>
<li><code>GET /film/harakiri</code> — single film by Letterboxd slug</li>
<li><code>GET /random</code> — random film from the list</li>
</ul>
<p><b>List filters</b> (work on /top500):</p>
<ul>
<li><code>?search=godfather</code> — title contains (case-insensitive)</li>
<li><code>?year=1972</code> — exact year</li>
<li><code>?sort=year&amp;order=desc</code> — sort by <code>rank|year|title</code></li>
<li><code>?limit=10&amp;offset=20</code> — pagination</li>
</ul>
<p>Examples:<br>
<code>/top500?search=godfather</code><br>
<code>/top500?year=1994&amp;limit=5</code><br>
<code>/top500?sort=title&amp;limit=5</code></p>
</body></html>`;
}
