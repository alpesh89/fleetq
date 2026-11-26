"""Policy for a crawl whose start URL redirects to another domain.

A business that rebranded answers with a permanent redirect from the old domain to the new one,
and the new site is the one to crawl. A dead or sold domain usually redirects to a directory,
marketplace, parking page or social profile, which must not be crawled as if it were the business.
Signals, in order: a listing-domain list, a destination shared by other crawled sites, the redirect
kind, and whether the landing page is the destination's front page or a page deep inside it.
"""

from __future__ import annotations

import os
from importlib import resources
from urllib.parse import urlsplit

from fleetq.urls import same_site

PERMANENT = frozenset({301, 308})


def _load_listing() -> frozenset[str]:
    names = set()
    try:
        text = resources.files("fleetq").joinpath("listing_domains.txt").read_text()
    except OSError:
        text = ""
    for ln in text.splitlines():
        ln = ln.split("#", 1)[0].strip().lower()
        if ln:
            names.add(ln)
    for ln in os.environ.get("FLEETQ_LISTING_DOMAINS", "").split(","):
        if ln.strip():
            names.add(ln.strip().lower())
    return frozenset(names)


LISTING = _load_listing()


def host(url: str) -> str:
    return urlsplit(url).netloc.lower().removeprefix("www.")


def is_listing(h: str) -> bool:
    parts = h.split(".")
    return any(".".join(parts[i:]) in LISTING for i in range(len(parts) - 1))


def classify(start_url: str, final_url: str, statuses: list[int], shared_sources: int = 0) -> tuple[str, str]:
    """Returns ('same' | 'follow' | 'skip', reason)."""
    if same_site(final_url, start_url):
        return "same", ""
    h = host(final_url)
    if is_listing(h):
        return "skip", f"redirects to listing site {h}"
    if shared_sources >= 2:
        return "skip", f"redirects to {h}, already the destination of {shared_sources} other sites"
    if not statuses or any(s not in PERMANENT for s in statuses):
        return "skip", f"temporary redirect to {h}"
    depth = len([seg for seg in urlsplit(final_url).path.split("/") if seg])
    if depth >= 2:
        return "skip", f"redirects to a page deep inside {h}"
    return "follow", f"permanently moved to {h}"
