"""fetch: download one page politely, store the raw HTML, queue a parse task.
With discover=true, also follow same-site links (used when a site has no sitemap).

Change detection (works for any site):
  1. If we fetched this URL within max_age_s and it was fine, skip without a request.
  2. If the sitemap gave a lastmod and it has not moved since our last fetch, skip.
  3. Otherwise ask the server conditionally (If-None-Match / If-Modified-Since); a 304 is a skip.
  4. On a 200, compare a normalized content fingerprint (comments, scripts, nonces, whitespace
     removed) with the stored copy; identical content is a skip.
  5. Only genuinely new or changed content is stored and re-parsed.
force=true bypasses 1 to 4.

Bot-protection challenges (SiteGround, Cloudflare, Imperva, ...) are recognised: the task is
deferred with a growing wait up to three times (such walls are usually rate-triggered and the page
loads later), then fails permanently with the vendor named. The interstitial is never stored as the
page. A redirect to a different site (parked or sold domain) is recorded and not followed.

payload: {"url": "...", "rps": 2, "discover": false, "max_links": 300,
          "max_age_s": 604800, "force": false, "sitemap_lastmod": "2026-10-01"}
"""

from urllib.parse import urlsplit

import httpx

from fleetq import blobs, botwall, config, redirects
from fleetq.urls import content_fingerprint, extract_links, same_site
from fleetq.worker import Defer

KIND = "fetch"
VERSION = 9
NEEDS = ["net"]
SLOTS = dict(start=8, floor=1, ceiling=64)
DEFAULT_MAX_AGE_S = 7 * 86400


def setup_check():
    import httpx

    return True, f"httpx {httpx.__version__}"


SETUP = {"pip": ["httpx>=0.27", "zstandard>=0.22"], "check": setup_check}


async def _page(conn, url):
    cur = await conn.execute(
        """select url, fetched_at, changed_at, change_count, status, content_hash, etag, last_modified, sitemap_lastmod,
                  extract(epoch from (now() - fetched_at))::float as age_s,
                  exists(select 1 from documents d where d.url = pages.url) as has_doc,
                  exists(select 1 from blobs b where b.key = 'html:' || pages.url) as has_blob
           from pages where url = %s""",
        (url,),
    )
    return await cur.fetchone()


async def _mark(conn, url, status, nbytes, job_id, lastmod, redirect_to=None):
    """Record a fetch that stored no content (error status, non-HTML, blocked, redirected away)."""
    await conn.execute(
        """insert into pages (url, status, bytes, last_job_id, sitemap_lastmod, redirect_to) values (%s, %s, %s, %s, %s, %s)
           on conflict (url) do update set fetched_at = now(), status = excluded.status, bytes = excluded.bytes,
                                           last_job_id = excluded.last_job_id,
                                           redirect_to = coalesce(excluded.redirect_to, pages.redirect_to)""",
        (url, status, nbytes, job_id, lastmod, redirect_to),
    )


async def _touch(conn, url, job_id, **cols):
    """Update bookkeeping columns on an existing row."""
    sets = ", ".join(f"{k} = %s" for k in cols)
    await conn.execute(
        f"update pages set fetched_at = now(), last_job_id = %s{', ' + sets if sets else ''} where url = %s",
        (job_id, *cols.values(), url),
    )


def _spawn_parse(ctx, task, url, final_url, rps, discover, max_links):
    ctx.spawn(
        "parse",
        {"url": url, "final_url": final_url, "blob_key": f"html:{url}", "rps": rps, "discover": discover, "max_links": max_links},
        idem_key=f"parse:{url}",
        needs=["cpu"],
        counted=False,
    )


def _spawn_links(ctx, task, html, base, url, rps, max_links, max_age_s, force):
    found = 0
    for u in extract_links(html, base, max_links):
        if u == url:
            continue
        ctx.spawn(
            "fetch",
            {"url": u, "rps": rps, "discover": True, "max_links": max_links, "max_age_s": max_age_s, "force": force},
            idem_key=f"fetch:{u}",
            needs=["net"],
            priority=max(0, 10 - task.depth),
        )
        found += 1
    return found


async def run(task, ctx):
    p = task.payload
    url = p["url"]
    host = urlsplit(url).netloc
    rps = float(p.get("rps", 2))
    discover = bool(p.get("discover"))
    max_links = int(p.get("max_links", 300))
    max_age_s = float(p.get("max_age_s", DEFAULT_MAX_AGE_S))
    force = bool(p.get("force"))
    lastmod = p.get("sitemap_lastmod")

    async with ctx.db.connection() as conn:
        page = await _page(conn, url)

    async def finish_skip(reason, page):
        """Skipped a download. Still make sure a document exists and discovery continues from the stored copy."""
        out = {"skipped": reason, "age_s": round(page["age_s"]) if page else None, "status": page["status"] if page else None}
        if page and page["has_blob"]:
            if not page["has_doc"]:
                _spawn_parse(ctx, task, url, url, rps, discover, max_links)
                out["reparse"] = True
            if discover:
                async with ctx.db.connection() as conn:
                    stored = await blobs.get(conn, f"html:{url}")
                if stored:
                    out["links"] = _spawn_links(
                        ctx, task, stored.decode("utf-8", "replace"), url, url, rps, max_links, max_age_s, force
                    )
        return out

    # 1. fresh enough: no request at all
    if page and not force and page["status"] and page["status"] < 400 and page["age_s"] < max_age_s and page["has_blob"]:
        return await finish_skip("fresh", page)
    # 2. sitemap says unchanged since our last fetch
    if page and not force and lastmod and page["sitemap_lastmod"] == lastmod and page["status"] == 200 and page["has_blob"]:
        async with ctx.db.connection() as conn:
            await _touch(conn, url, task.job_id)
        return await finish_skip("sitemap unchanged", page)

    if not await ctx.robots_allowed(url):
        return {"skipped": "robots.txt"}

    # 3. conditional request
    headers = {}
    if page and not force and page["has_blob"]:
        if page["etag"]:
            headers["If-None-Match"] = page["etag"]
        if page["last_modified"]:
            headers["If-Modified-Since"] = page["last_modified"]

    site = f"domain:{host}"
    await ctx.pace_site(host, rps, give_up_after=config.SITE_GIVE_UP_FAILURES)
    try:
        r = await ctx.http.get(url, headers=headers)
    except httpx.TimeoutException as e:
        await ctx.site_failed(site, rps)
        raise RuntimeError(f"timeout: {e}")  # retried with backoff

    # Push-back and server errors slow the whole fleet down on this one site (see Context.site_failed).
    if r.status_code == 429:
        await ctx.site_failed(site, rps)
        if addr := await ctx.server_of(host):  # a shared host pushes back for all its sites
            await ctx.site_failed(f"server:{addr}", config.SERVER_RPS)
        raise Defer(float(r.headers.get("retry-after", 10)), "HTTP 429", throttled=True)
    if r.status_code >= 500:
        await ctx.site_failed(site, rps)
        raise RuntimeError(f"HTTP {r.status_code}")
    if botwall.challenge(r.status_code, r.headers, r.content):
        await ctx.site_failed(site, rps)
        async with ctx.db.connection() as conn:
            await _mark(conn, url, r.status_code, len(r.content), task.job_id, lastmod)
        botwall.refuse(task, r.status_code, r.headers, r.content)  # raises Defer or Permanent
    await ctx.site_ok(site)  # any real answer, even a 404, means the site is up

    if r.status_code == 304 and page:
        async with ctx.db.connection() as conn:
            await _touch(conn, url, task.job_id, sitemap_lastmod=lastmod or page["sitemap_lastmod"])
        return await finish_skip("not modified", page)

    final_url = str(r.url)
    if not same_site(final_url, url):
        async with ctx.db.connection() as conn:
            await _mark(conn, url, r.status_code, len(r.content), task.job_id, lastmod, redirects.host(final_url))
        return {
            "status": r.status_code,
            "skipped": f"redirected off-site to {urlsplit(final_url).netloc}",
            "final_url": final_url,
        }

    ctype = r.headers.get("content-type", "")
    etag, last_modified = r.headers.get("etag"), r.headers.get("last-modified")

    if r.status_code >= 400 or ("html" not in ctype and "xml" not in ctype and "text/plain" not in ctype):
        async with ctx.db.connection() as conn:
            await _mark(conn, url, r.status_code, len(r.content), task.job_id, lastmod)
        return {"status": r.status_code, "skipped": None if r.status_code >= 400 else ctype}

    # 4. content comparison
    digest = content_fingerprint(r.content)  # ignores comments, scripts, nonces, whitespace
    if page and not force and page["content_hash"] == digest and page["has_blob"]:
        async with ctx.db.connection() as conn:
            await _touch(
                conn,
                url,
                task.job_id,
                etag=etag,
                last_modified=last_modified,
                sitemap_lastmod=lastmod or page["sitemap_lastmod"],
                status=r.status_code,
            )
        return await finish_skip("unchanged", page)

    # 5. new or changed content
    changed = bool(page and page["content_hash"] and page["content_hash"] != digest)
    async with ctx.db.connection() as conn:
        async with conn.transaction():
            await blobs.put(conn, f"html:{url}", ctype, r.content, task.id)
            await conn.execute(
                """insert into pages (url, fetched_at, changed_at, change_count, status, bytes, content_hash, etag, last_modified,
                                      sitemap_lastmod, last_job_id)
                   values (%s, now(), now(), 0, %s, %s, %s, %s, %s, %s, %s)
                   on conflict (url) do update set
                     fetched_at = now(),
                     changed_at = case when pages.content_hash is distinct from excluded.content_hash then now() else pages.changed_at end,
                     change_count = pages.change_count + case when pages.content_hash is distinct from excluded.content_hash then 1 else 0 end,
                     status = excluded.status, bytes = excluded.bytes, content_hash = excluded.content_hash,
                     etag = excluded.etag, last_modified = excluded.last_modified,
                     sitemap_lastmod = coalesce(excluded.sitemap_lastmod, pages.sitemap_lastmod),
                     last_job_id = excluded.last_job_id""",
                (url, r.status_code, len(r.content), digest, etag, last_modified, lastmod, task.job_id),
            )

    _spawn_parse(ctx, task, url, final_url, rps, discover, max_links)
    found = 0
    if discover and "html" in ctype:
        found = _spawn_links(ctx, task, r.text, final_url, url, rps, max_links, max_age_s, force)
    return {
        "status": r.status_code,
        "bytes": len(r.content),
        "final_url": final_url,
        "links": found,
        "changed": changed,
        "new": page is None,
    }
