"""URL canonicalisation and same-site link extraction shared by the crawl handlers."""

from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit, urlunsplit

SKIP_EXT = (
    ".pdf",
    ".jpg",
    ".jpeg",
    ".png",
    ".gif",
    ".webp",
    ".svg",
    ".ico",
    ".css",
    ".js",
    ".zip",
    ".gz",
    ".mp4",
    ".mp3",
    ".mov",
    ".avi",
    ".woff",
    ".woff2",
    ".ttf",
    ".xml",
    ".rss",
    ".atom",
    ".json",
)


def canon(url: str) -> str | None:
    """Lowercase scheme and host, drop fragment and query, strip trailing slash (except root)."""
    try:
        p = urlsplit(url.strip())
    except ValueError:
        return None
    if p.scheme not in ("http", "https") or not p.netloc:
        return None
    path = p.path or "/"
    if len(path) > 1 and path.endswith("/"):
        path = path[:-1]
    return urlunsplit((p.scheme.lower(), p.netloc.lower(), path, "", ""))


def same_site(a: str, b: str) -> bool:
    ha, hb = urlsplit(a).netloc.lower(), urlsplit(b).netloc.lower()
    return ha.removeprefix("www.") == hb.removeprefix("www.")


class _Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.hrefs: list[str] = []
        self.canonical: str | None = None

    def handle_starttag(self, tag, attrs):
        d = dict(attrs)
        if tag == "a" and d.get("href"):
            self.hrefs.append(d["href"])
        elif tag == "link" and d.get("rel", "").lower() == "canonical" and d.get("href"):
            self.canonical = d["href"]


def extract_links(html: str, base_url: str, max_links: int = 300) -> list[str]:
    """Same-site, canonical, de-duplicated page links in document order."""
    parser = _Links()
    try:
        parser.feed(html)
    except Exception:
        pass
    seen, out = set(), []
    for href in parser.hrefs:
        h = href.strip()
        if not h or h.startswith(("#", "mailto:", "tel:", "javascript:", "data:")):
            continue
        u = canon(urljoin(base_url, h))
        if not u or u in seen or not same_site(u, base_url):
            continue
        if urlsplit(u).path.lower().endswith(SKIP_EXT):
            continue
        seen.add(u)
        out.append(u)
        if len(out) >= max_links:
            break
    return out


_RE_COMMENT = __import__("re").compile(rb"<!--.*?-->", __import__("re").S)
_RE_SCRIPT = __import__("re").compile(rb"<(script|style|noscript)\b[^>]*>.*?</\1\s*>", __import__("re").S | __import__("re").I)
_RE_NONCE = __import__("re").compile(
    rb"""\s(?:nonce|data-nonce|csrf[-_]?token|_token|data-timestamp|data-build|data-reactroot)=["'][^"']*["']""",
    __import__("re").I,
)
_RE_META_DYNAMIC = __import__("re").compile(
    rb"""<meta\b[^>]*(?:csrf|nonce|build|generated|timestamp)[^>]*>""", __import__("re").I
)
_RE_WS = __import__("re").compile(rb"\s+")


def content_fingerprint(html: bytes) -> str:
    """sha256 of the page with per-request noise removed: comments (server stamps), script/style
    bodies (nonces, bundles), nonce/csrf attributes, dynamic meta tags, and whitespace runs."""
    import hashlib

    h = _RE_COMMENT.sub(b"", html)
    h = _RE_SCRIPT.sub(b"", h)
    h = _RE_NONCE.sub(b"", h)
    h = _RE_META_DYNAMIC.sub(b"", h)
    h = _RE_WS.sub(b" ", h).strip()
    return hashlib.sha256(h).hexdigest()
