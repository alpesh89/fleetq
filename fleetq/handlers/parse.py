"""parse: raw HTML blob -> Markdown document (boilerplate removed) in the documents table.

payload: {"url": "...", "blob_key": "html:..."}
"""

import os
import re
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

from psycopg.types.json import Jsonb

from fleetq import blobs, signals
from fleetq.worker import Permanent

KIND = "parse"
VERSION = 4
NEEDS = ["cpu"]
SLOTS = dict(start=2, floor=1, ceiling=max(2, os.cpu_count() or 2))


def setup_check():
    try:
        import trafilatura

        return True, f"trafilatura {trafilatura.__version__}"
    except ImportError:
        return False, "trafilatura not installed (fallback parser only)"


SETUP = {"pip": ["trafilatura>=2.0", "zstandard>=0.22"], "check": setup_check}


class _Anchors(HTMLParser):
    """Every <a href> with its visible text, in document order. Boilerplate removal drops the
    nav and footer from the Markdown; this keeps their links (contact, social, team pages)
    available as structured data instead."""

    def __init__(self, base):
        super().__init__()
        self.base, self.out, self._href, self._text = base, [], None, []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            href = dict(attrs).get("href")
            if href:
                self._href, self._text = href.strip(), []

    def handle_data(self, data):
        if self._href is not None:
            self._text.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self._href is not None:
            h = self._href
            if not h.startswith(("#", "javascript:", "data:")):
                if not h.startswith(("mailto:", "tel:")):
                    h = urljoin(self.base, h)
                self.out.append({"href": h, "text": re.sub(r"\s+", " ", "".join(self._text)).strip()[:200]})
            self._href = None


def page_links(html: str, base: str, limit: int = 500) -> list[dict]:
    p = _Anchors(base)
    try:
        p.feed(html)
    except Exception:
        pass
    seen, out = set(), []
    for a in p.out:
        if a["href"] in seen:
            continue
        seen.add(a["href"])
        out.append(a)
        if len(out) >= limit:
            break
    return out


class _Stripper(HTMLParser):
    """Fallback when trafilatura finds nothing: drop scripts/styles, keep text."""

    def __init__(self):
        super().__init__()
        self.parts, self.skip, self.title = [], 0, None
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "noscript", "svg"):
            self.skip += 1
        elif tag == "title":
            self._in_title = True
        elif tag in ("p", "div", "br", "li", "h1", "h2", "h3", "h4", "tr"):
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript", "svg") and self.skip:
            self.skip -= 1
        elif tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._in_title and self.title is None:
            self.title = data.strip() or None
        if not self.skip:
            self.parts.append(data)


JS_MARKERS = (
    "enable javascript",
    "data-reactroot",
    "ng-version=",
    "__nuxt",
    "data-v-app",
    "__next_data__",
    "ember-application",
    "data-svelte",
    "reactdom",
    "ng-app",
    "data-wf-site",
    "webpackjsonp",
)


def needs_render(html: bytes, words: int) -> str | None:
    """Content-based test, independent of element ids: little text, scripts present.
    Returns the reason or None."""
    if words >= 60:
        return None
    h = html[:300_000].decode("utf-8", "replace").lower()
    scripts = h.count("<script")
    if scripts == 0:
        return None
    for m in JS_MARKERS:
        if m in h:
            return f"{words} words, {scripts} scripts, marker {m!r}"
    if scripts >= 2:
        return f"{words} words, {scripts} scripts"
    return None


def to_markdown(html: bytes, url: str) -> dict:
    """Runs in the CPU process pool."""
    title, text, doc = None, None, None
    try:
        import trafilatura

        # extract() is the call that formats output; bare_extraction() ignores output_format
        # (it returned empty text for markdown in 2.3.x, which silently pushed every page to the
        # plain-text fallback below). Metadata (title) still comes from bare_extraction().
        text = trafilatura.extract(
            html,
            url=url,
            output_format="markdown",
            include_links=True,
            include_tables=True,
            include_formatting=True,
            include_images=False,
            favor_recall=True,
        )
        doc = trafilatura.bare_extraction(html, url=url, only_with_metadata=False)
        if doc is not None:
            title = doc.title
    except Exception:
        pass
    if text:
        text = text.strip()
    parser_ok = bool(text)
    decoded = html.decode("utf-8", errors="replace")
    if not text or not title:
        s = _Stripper()
        s.feed(decoded)
        title = title or s.title
        if not text:
            text = re.sub(r"\n\s*\n+", "\n\n", "".join(s.parts)).strip()
    words = len(text.split()) if text else 0
    title = (title or "").replace("\x00", "") or None
    text = (text or "").replace("\x00", "")  # Postgres text cannot hold NUL bytes
    return {
        "title": title,
        "markdown": text or "",
        "words": words,
        "parser": "trafilatura" if parser_ok else "fallback",
        "links": page_links(decoded, url),
    }


async def run(task, ctx):
    url = task.payload["url"]
    key = task.payload["blob_key"]
    async with ctx.db.connection() as conn:
        raw = await blobs.get(conn, key)
    if raw is None:
        raise Permanent(f"blob {key} missing")

    doc = await ctx.run_cpu(to_markdown, raw, url)

    escalate = None
    if not task.payload.get("rendered"):
        escalate = needs_render(raw, doc["words"])
        if escalate:
            ctx.spawn(
                "render",
                {
                    "url": url,
                    "rps": task.payload.get("rps", 2),
                    "discover": bool(task.payload.get("discover")),
                    "max_links": task.payload.get("max_links", 300),
                },
                idem_key=f"render:{url}",
                needs=["browser"],
                counted=False,
                priority=3,
            )

    async with ctx.db.connection() as conn:
        await conn.execute(
            """insert into documents (url, title, markdown, word_count, parser_version, job_id, links)
               values (%s, %s, %s, %s, %s, %s, %s)
               on conflict (url) do update set title = excluded.title, markdown = excluded.markdown,
                 word_count = excluded.word_count, parser_version = excluded.parser_version,
                 job_id = excluded.job_id, links = excluded.links, updated_at = now()""",
            (url, doc["title"], doc["markdown"], doc["words"], VERSION, task.job_id, Jsonb(doc["links"])),
        )
        # Ad pixels, analytics, call tracking, platform: read from the HTML we already have, kept per site.
        sig = signals.detect(raw)
        host = urlsplit(url).netloc.lower().removeprefix("www.")
        async with conn.transaction():
            cur = await conn.execute("select platform, builder, tags from site_signals where host = %s for update", (host,))
            merged = signals.merge(await cur.fetchone(), sig)
            await conn.execute(
                """insert into site_signals (host, url, platform, builder, tags, pages) values (%s, %s, %s, %s, %s, 1)
                   on conflict (host) do update set url = excluded.url, platform = excluded.platform, builder = excluded.builder,
                     tags = excluded.tags, pages = site_signals.pages + 1, detected_at = now()""",
                (host, url, merged["platform"], merged["builder"], Jsonb(merged["tags"])),
            )
    out = {"words": doc["words"], "title": doc["title"], "parser": doc["parser"], "rendered": bool(task.payload.get("rendered"))}
    if sig["platform"] or sig["tags"]:
        out["signals"] = {"platform": sig["platform"], "tags": sorted(sig["tags"])}
    if escalate:
        out["escalated"] = f"render ({escalate})"
    return out
