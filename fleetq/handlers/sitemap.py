"""sitemap: find a site's sitemap and fan out one fetch task per URL.

Looks in robots.txt, then common paths. If no sitemap exists, falls back to
crawling from the homepage by following same-site links (fetch with discover=true).
Entries pointing at other sites are ignored, and a bot-protection challenge fails the
task permanently with the vendor named (see fleetq/botwall.py).

A start URL that permanently moved to a new domain (a rebrand) is followed and the new domain
is crawled; one that redirects to a listing, directory, parking or social site is not scraped
and the task fails with the reason (policy in fleetq/redirects.py).

payload: {"url": "https://site" or ".../sitemap.xml", "limit": 100, "rps": 2}
"""

import gzip
import xml.etree.ElementTree as ET
from urllib.parse import urlsplit

from fleetq import botwall, redirects
from fleetq.urls import canon, same_site
from fleetq.worker import Defer, Permanent

KIND = "sitemap"
VERSION = 7
NEEDS = ["net"]
SLOTS = dict(start=2, floor=1, ceiling=4)


def setup_check():
    import httpx

    return True, f"httpx {httpx.__version__}"


SETUP = {"pip": ["httpx>=0.27"], "check": setup_check}

NS = "{http://www.sitemaps.org/schemas/sitemap/0.9}"
COMMON = ["/sitemap.xml", "/sitemap_index.xml", "/sitemap-index.xml", "/wp-sitemap.xml", "/sitemap.txt"]


def _parse(body: bytes, content_type: str) -> tuple[bool, list[tuple[str, str | None]]] | None:
    """Returns (is_index, locs) or None if this is not a sitemap."""
    if body[:2] == b"\x1f\x8b":
        body = gzip.decompress(body)
    text = body.lstrip()
    if text.startswith(b"<"):
        try:
            root = ET.fromstring(text)
        except ET.ParseError:
            return None
        tag = root.tag.replace(NS, "")
        if tag not in ("sitemapindex", "urlset"):
            return None
        locs = []
        for entry in root:  # <url> or <sitemap> children
            loc = entry.find(f"{NS}loc")
            if loc is None:
                loc = entry.find("loc")
            if loc is None or not loc.text:
                continue
            lm = entry.find(f"{NS}lastmod")
            if lm is None:
                lm = entry.find("lastmod")
            locs.append((loc.text.strip(), lm.text.strip() if lm is not None and lm.text else None))
        if not locs:
            locs = [(el.text.strip(), None) for el in root.iter("loc") if el.text]
        return tag == "sitemapindex", locs
    if "text/plain" in content_type or b"http" in text[:10]:
        locs = [(ln.strip(), None) for ln in text.decode("utf-8", "replace").splitlines() if ln.strip().startswith("http")]
        return (False, locs) if locs else None
    return None


async def _candidates(ctx, task, origin: str, given: str | None) -> list[str]:
    urls = [given] if given else []
    try:
        r = await ctx.http.get(origin + "/robots.txt", timeout=10)
        _refuse_if_blocked(task, r)
        if r.status_code == 200:
            for ln in r.text.splitlines():
                k, _, v = ln.partition(":")
                if k.strip().lower() == "sitemap" and v.strip():
                    urls.append(v.strip())
    except (Defer, Permanent):
        raise
    except Exception:
        pass
    urls += [origin + p for p in COMMON]
    seen, out = set(), []
    for u in urls:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def _refuse_if_blocked(task, r):
    botwall.refuse(task, r.status_code, r.headers, r.content)  # raises Defer or Permanent on a challenge page


def _lastmod_key(v) -> float:
    """Sortable value for a sitemap lastmod (ISO date or datetime); unknown sorts oldest."""
    if not v:
        return 0.0
    from datetime import datetime

    try:
        return datetime.fromisoformat(str(v).strip().replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


async def run(task, ctx):
    p = task.payload
    start = p["url"]
    parts = urlsplit(start)
    origin = f"{parts.scheme}://{parts.netloc}"
    limit = p.get("limit")
    rps = float(p.get("rps", 2))
    passthru = {"rps": rps, "max_age_s": p.get("max_age_s"), "force": bool(p.get("force")), "max_links": p.get("max_links", 300)}
    passthru = {k: v for k, v in passthru.items() if v is not None}
    path = parts.path.lower()
    given = start if (path.endswith((".xml", ".xml.gz", ".txt")) or "sitemap" in path) else None

    # Where does the site really live? Follow a rebrand; refuse a listing, parked or shared destination.
    moved = None
    await ctx.rate_limit(f"domain:{parts.netloc}", rps)
    try:
        r0 = await ctx.http.get(origin + "/")
    except Exception as e:
        raise RuntimeError(f"start URL unreachable: {type(e).__name__}")  # retried with backoff
    _refuse_if_blocked(task, r0)
    final = str(r0.url)
    if not same_site(final, origin):
        fh = redirects.host(final)
        async with ctx.db.connection() as conn:
            cur = await conn.execute(
                "select count(distinct split_part(url, '/', 3)) as n from pages where redirect_to = %s and split_part(url, '/', 3) <> %s",
                (fh, parts.netloc),
            )
            others = (await cur.fetchone())["n"]
            await conn.execute(
                """insert into pages (url, status, bytes, last_job_id, redirect_to) values (%s, %s, %s, %s, %s)
                   on conflict (url) do update set fetched_at = now(), status = excluded.status, bytes = excluded.bytes,
                                                   last_job_id = excluded.last_job_id, redirect_to = excluded.redirect_to""",
                (origin + "/", r0.status_code, len(r0.content), task.job_id, fh),
            )
        verdict, why = redirects.classify(origin + "/", final, [h.status_code for h in r0.history], others)
        if verdict == "skip":
            raise Permanent(f"not scraped: {why}")
        moved = why
        parts = urlsplit(final)
        origin = f"{parts.scheme}://{parts.netloc}"
        start = origin + "/"
        if given and not same_site(given, origin):
            given = None

    tried, offsite = [], 0
    for url in await _candidates(ctx, task, origin, given):
        await ctx.rate_limit(f"domain:{parts.netloc}", rps)
        try:
            r = await ctx.http.get(url)
        except Exception as e:
            tried.append(f"{url} ({type(e).__name__})")
            continue
        tried.append(f"{url} ({r.status_code})")
        _refuse_if_blocked(task, r)
        if r.status_code != 200:
            continue
        parsed = _parse(r.content, r.headers.get("content-type", ""))
        if not parsed:
            continue
        is_index, locs = parsed
        if is_index:
            for loc, _ in locs:
                if not same_site(loc, origin):
                    offsite += 1
                    continue
                ctx.spawn(
                    "sitemap", {"url": loc, "limit": limit, **passthru}, idem_key=f"sitemap:{loc}", needs=["net"], counted=False
                )
        else:
            wanted = []
            for loc, lastmod in locs:
                u = canon(loc)
                if not u:
                    continue
                if not same_site(u, origin):
                    offsite += 1
                    continue
                wanted.append((u, lastmod))
            # Pages we have never seen come first, then the most recently modified: a page limit then
            # still catches every new page and the freshest changes (what tracking needs).
            async with ctx.db.connection() as conn:
                cur = await conn.execute("select url from pages where url = any(%s)", ([u for u, _ in wanted],))
                known = {r["url"] for r in await cur.fetchall()}
            wanted.sort(key=lambda x: (x[0] in known, -_lastmod_key(x[1])))
            n = 0
            for u, lastmod in wanted:
                payload = {"url": u, **passthru}
                if lastmod:
                    payload["sitemap_lastmod"] = lastmod
                ctx.spawn("fetch", payload, idem_key=f"fetch:{u}", needs=["net"], priority=5)
                n += 1
                if limit and n >= limit:
                    break
        out = {"sitemap": url, "index": is_index, "urls": len(locs)}
        if offsite:
            out["offsite_ignored"] = offsite
        if moved:
            out["moved"] = moved
        return out

    # No sitemap anywhere: crawl from the front door by following links.
    home = canon(start if not given else origin) or origin
    ctx.spawn("fetch", {"url": home, "discover": True, **passthru}, idem_key=f"fetch:{home}", needs=["net"], priority=10)
    out = {"sitemap": None, "fallback": "discover", "start": home, "tried": tried}
    if moved:
        out["moved"] = moved
    return out
