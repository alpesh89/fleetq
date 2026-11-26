"""Recognise bot-protection challenge pages so they are recorded as blocked, not stored as content.

Nothing here tries to pass a challenge. A blocked site is reported with the vendor name so the
owner can allowlist the fleet's address, or the site can be left out of the run.
"""

from __future__ import annotations

import random

MAX_BLOCKS = 3  # challenge responses tolerated per task before it fails permanently
BACKOFF_S = (120, 300, 600)

# header name, required value (None = any), label
_HEADER_RULES = (
    ("sg-captcha", None, "SiteGround captcha"),
    ("cf-mitigated", "challenge", "Cloudflare challenge"),
    ("x-amzn-waf-action", "challenge", "AWS WAF challenge"),
    ("x-sucuri-block", None, "Sucuri firewall"),
)
# Vendor markers that only ever appear on an interstitial, so they count at any status when the body is tiny.
_STRONG_BODY = (
    (b"sgcaptcha", "SiteGround captcha"),
    (b"cf-chl-", "Cloudflare challenge"),
    (b"challenge-platform", "Cloudflare challenge"),
    (b"_incapsula_resource", "Imperva Incapsula"),
    (b"pardon our interruption", "Imperva bot defence"),
    (b"awswaf", "AWS WAF challenge"),
    (b"ddos-guard", "DDoS-Guard challenge"),
    (b"px-captcha", "PerimeterX challenge"),
    (b"datadome", "DataDome challenge"),
)
# Generic captcha widgets: a real contact page can embed one, so these count only on a refusal status.
_WEAK_BODY = ((b"g-recaptcha", "captcha page"), (b"h-captcha", "captcha page"), (b"captcha", "captcha page"))
SUSPECT_STATUS = frozenset({202, 401, 403, 405, 429, 503})
SMALL = 4000


def challenge(status: int, headers, body: bytes) -> str | None:
    """Return the vendor label if this response is a bot-wall challenge instead of the page, else None."""
    h = {str(k).lower(): str(v) for k, v in headers.items()}
    for name, value, label in _HEADER_RULES:
        if name in h and (value is None or h[name].lower() == value):
            return label
    sample = body[:20000].lower()
    if status in SUSPECT_STATUS or len(body) < SMALL:
        for needle, label in _STRONG_BODY:
            if needle in sample:
                return label
    if status in SUSPECT_STATUS:
        for needle, label in _WEAK_BODY:
            if needle in sample:
                return label
    return None


def refuse(task, status: int, headers, body: bytes) -> None:
    """Raise the right exception for a challenge response, or return if it is not one.

    Many walls (SiteGround, Cloudflare) challenge only when a client looks busy, and the same
    URL often loads minutes later, so the task is deferred with a growing wait and the slot
    controller is told to run less (throttled). After MAX_BLOCKS challenges the task fails
    permanently with the vendor named. Nothing here tries to pass a challenge.
    """
    from fleetq.worker import Defer, Permanent  # handlers import botwall; worker is already loaded

    wall = challenge(status, headers, body)
    if not wall:
        return
    n = int((task.payload or {}).get("blocked", 0)) + 1
    msg = f"blocked by {wall} (HTTP {status}); the site owner can allowlist this address"
    if n > MAX_BLOCKS:
        raise Permanent(msg)
    wait = BACKOFF_S[min(n, len(BACKOFF_S)) - 1]
    raise Defer(wait * (0.75 + random.random() * 0.5), f"{msg}; retry {n}/{MAX_BLOCKS}", throttled=True, patch={"blocked": n})
