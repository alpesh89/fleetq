"""Marketing and platform signals read from a page's HTML: ad pixels, analytics, tag managers,
call tracking, chat widgets and the site platform. No extra requests: the parse step runs this on
the HTML the crawl already stored, and the result is kept per site (host) as a union over pages.

A Meta pixel means "set up for Meta ads", not "running ads this week"; same for the others. For
tens of thousands of sites it is the only automatic signal, and it is free.
"""

from __future__ import annotations

import re

MAX_BYTES = 1_500_000

# key, label, presence patterns (any), id pattern (first non-empty group)
TAGS = [
    (
        "meta_pixel",
        "Meta pixel (Facebook/Instagram ads)",
        [r"connect\.facebook\.net/[a-z_]+/fbevents\.js", r"fbq\(\s*['\"]init['\"]", r"facebook\.com/tr\?"],
        r"fbq\(\s*['\"]init['\"]\s*,\s*['\"](\d{6,20})['\"]|facebook\.com/tr\?id=(\d{6,20})",
    ),
    (
        "google_ads",
        "Google Ads conversion / remarketing",
        [r"\bAW-\d{6,}\b", r"googleadservices\.com/pagead/conversion", r"google_conversion_id"],
        r"\b(AW-\d{6,})\b",
    ),
    (
        "ga4",
        "Google Analytics 4",
        [r"(?<![A-Z0-9-])G-[A-Z0-9]{8,12}(?![A-Z0-9-])"],
        r"(?<![A-Z0-9-])(G-[A-Z0-9]{8,12})(?![A-Z0-9-])",
    ),
    (
        "universal_analytics",
        "Google Analytics Universal (retired 2024; left behind)",
        [r"\bUA-\d{4,}-\d+\b"],
        r"\b(UA-\d{4,}-\d+)\b",
    ),
    ("gtm", "Google Tag Manager", [r"googletagmanager\.com/gtm\.js", r"\bGTM-[A-Z0-9]{4,}\b"], r"\b(GTM-[A-Z0-9]{4,})\b"),
    (
        "tiktok_pixel",
        "TikTok pixel",
        [r"analytics\.tiktok\.com/i18n/pixel", r"\bttq\.load\("],
        r"ttq\.load\(\s*['\"]([A-Z0-9]{8,})['\"]",
    ),
    ("bing_uet", "Microsoft Ads UET tag", [r"bat\.bing\.com/bat\.js", r"\buetq\b"], r"\"ti\"\s*:\s*\"?(\d{5,})"),
    (
        "linkedin_insight",
        "LinkedIn Insight tag",
        [r"snap\.licdn\.com", r"_linkedin_partner_id"],
        r"_linkedin_partner_id\s*=\s*['\"](\d+)['\"]",
    ),
    (
        "pinterest_tag",
        "Pinterest tag",
        [r"s\.pinimg\.com/ct/core\.js", r"\bpintrk\("],
        r"pintrk\(\s*['\"]load['\"]\s*,\s*['\"](\d+)['\"]",
    ),
    ("callrail", "CallRail call tracking", [r"callrail\.com", r"calltrk\.com"], r"callrail\.com/companies/(\d+)"),
    ("calltrackingmetrics", "CallTrackingMetrics", [r"calltrackingmetrics\.com", r"\btctm\.co\b"], None),
    ("whatconverts", "WhatConverts", [r"whatconverts\.com"], None),
    ("hotjar", "Hotjar", [r"static\.hotjar\.com"], r"hjid\s*:\s*(\d+)"),
    ("clarity", "Microsoft Clarity", [r"clarity\.ms/tag/"], r"clarity\.ms/tag/([a-z0-9]+)"),
    ("hubspot", "HubSpot tracking", [r"js\.hs-scripts\.com", r"js\.hsforms\.net"], r"hs-scripts\.com/(\d+)\.js"),
    ("podium", "Podium", [r"connect\.podium\.com"], None),
    ("birdeye", "Birdeye", [r"birdeye\.com/embed", r"cdn\.birdeye\.com"], None),
    ("tawk", "tawk.to chat", [r"embed\.tawk\.to"], None),
    ("intercom", "Intercom", [r"widget\.intercom\.io"], None),
    ("drift", "Drift chat", [r"js\.driftt\.com"], None),
    ("adsense", "Google AdSense (publisher ads)", [r"pagead2\.googlesyndication\.com"], r"(ca-pub-\d+)"),
]
# First match wins, so the unambiguous markers come first.
PLATFORMS = [
    ("WordPress", [r"/wp-content/", r"/wp-includes/", r"content=\"WordPress", r"/wp-json/", r"wp-emoji-release", r"xmlrpc\.php"]),
    ("Shopify", [r"cdn\.shopify\.com", r"Shopify\.theme"]),
    ("Squarespace", [r"static1\.squarespace\.com", r"This is Squarespace"]),
    ("Wix", [r"static\.parastorage\.com", r"wixstatic\.com", r"wix\.com/"]),
    ("Webflow", [r"data-wf-site", r"assets\.website-files\.com", r"webflow\.js"]),
    ("GoDaddy Website Builder", [r"img1\.wsimg\.com", r"\.wsimg\.com/"]),
    ("Duda", [r"irp\.cdn-website\.com", r"cdn-website\.com/", r"dudamobile\.com"]),
    ("Weebly", [r"editmysite\.com", r"weebly\.com"]),
    ("HubSpot CMS", [r"hs-sites\.com", r"hubspotusercontent"]),
    ("Framer", [r"framerusercontent\.com"]),
    ("Drupal", [r"Drupal\.settings", r"content=\"Drupal", r"/sites/default/files/"]),
    ("Joomla", [r"content=\"Joomla", r"/media/jui/"]),
    ("Jimdo", [r"jimdo\.com"]),
    ("Site123", [r"site123\.com"]),
    ("Strikingly", [r"strikingly\.com"]),
    ("Houzz Site Designer", [r"st\.hzcdn\.com/sites"]),
    ("B12", [r"b12\.io", r"b12-website", r"b12cdn"]),
    ("Hostinger Website Builder", [r"content=\"Hostinger", r"hostinger\.com/builder", r"zyrosite\.com"]),
    ("Next.js (custom build)", [r"/_next/static/", r"__NEXT_DATA__"]),
    ("Gatsby (custom build)", [r"content=\"Gatsby", r"/page-data/app-data\.json"]),
    ("Nuxt (custom build)", [r"/_nuxt/", r"__NUXT__"]),
    ("Blogger", [r"content=\"blogger"]),
]
_GENERATOR = re.compile(r'<meta\s+name=["\']generator["\']\s+content=["\']([^"\']{2,60})', re.I)
BUILDERS = [  # WordPress page builders; informative for "how was this site made"
    ("Elementor", [r"/elementor/"]),
    ("Divi", [r"/Divi/", r"et-divi", r"et_pb_"]),
    ("WPBakery", [r"js_composer", r"\bvc_row\b"]),
    ("Beaver Builder", [r"fl-builder"]),
    ("Oxygen", [r"/oxygen/"]),
    ("Bricks", [r"/bricks/"]),
    ("Avada", [r"/Avada/", r"fusion-builder"]),
    ("Gutenberg blocks", [r"wp-block-"]),
]

_TAGS = [(k, lbl, [re.compile(p, re.I) for p in pres], re.compile(idp) if idp else None) for k, lbl, pres, idp in TAGS]
_PLAT = [(n, [re.compile(p) for p in pats]) for n, pats in PLATFORMS]
_BUILD = [(n, [re.compile(p) for p in pats]) for n, pats in BUILDERS]
LABELS = {k: lbl for k, lbl, _, _ in TAGS}
ADS_KEYS = ("meta_pixel", "google_ads", "tiktok_pixel", "bing_uet", "linkedin_insight", "pinterest_tag")


def detect(html: bytes | str) -> dict:
    """{'platform': str|None, 'builder': str|None, 'tags': {key: [ids]}} for one page."""
    text = html.decode("utf-8", "replace") if isinstance(html, bytes) else html
    text = text[:MAX_BYTES]
    tags: dict[str, list[str]] = {}
    for key, _label, pres, idp in _TAGS:
        if not any(p.search(text) for p in pres):
            continue
        ids: list[str] = []
        if idp:
            for m in idp.finditer(text):
                v = next((g for g in m.groups() if g), None)
                if v and v not in ids:
                    ids.append(v)
                if len(ids) >= 5:
                    break
        tags[key] = ids
    platform = next((n for n, pats in _PLAT if any(p.search(text) for p in pats)), None)
    if platform is None:
        m = _GENERATOR.search(text)
        if m:
            platform = m.group(1).split(" - ")[0].strip()  # e.g. "Hostinger Horizons", "Joomla! 4.3"
    builder = None
    if platform == "WordPress":
        builder = next((n for n, pats in _BUILD if any(p.search(text) for p in pats)), None)
    return {"platform": platform, "builder": builder, "tags": tags}


_RANK = {n: i for i, (n, _) in enumerate(PLATFORMS)}


def rank(platform: str | None) -> int:
    """Lower is more trustworthy: listed markers in list order, then a generator meta tag, then nothing."""
    if platform is None:
        return 10_000
    return _RANK.get(platform, 1_000)  # unknown name = came from the generator fallback


def merge(old: dict | None, new: dict) -> dict:
    """Union per site: a pixel seen on any page counts; ids accumulate; the platform with the strongest
    evidence wins whatever order the pages were seen in (a page linking to blogspot must not beat /wp-content/)."""
    if not old:
        return {"platform": new["platform"], "builder": new["builder"], "tags": dict(new["tags"])}
    tags = {k: list(v) for k, v in (old.get("tags") or {}).items()}
    for k, ids in new["tags"].items():
        cur = tags.setdefault(k, [])
        for i in ids:
            if i not in cur:
                cur.append(i)
    platform = min((old.get("platform"), new["platform"]), key=rank)
    return {"platform": platform, "builder": old.get("builder") or new["builder"], "tags": tags}


def summary(row: dict) -> dict:
    """API view of a site_signals row: adds labels and the one-line verdicts a CRM wants."""
    tags = row.get("tags") or {}
    return {
        "host": row["host"],
        "platform": row.get("platform"),
        "builder": row.get("builder"),
        "pages_scanned": row.get("pages"),
        "detected_at": row.get("detected_at"),
        "ads_ready": any(k in tags for k in ADS_KEYS),
        "meta_ads": "meta_pixel" in tags,
        "google_ads": "google_ads" in tags,
        "tiktok_ads": "tiktok_pixel" in tags,
        "analytics": [k for k in ("ga4", "universal_analytics", "gtm", "hotjar", "clarity") if k in tags],
        "call_tracking": [k for k in ("callrail", "calltrackingmetrics", "whatconverts") if k in tags],
        "chat": [k for k in ("podium", "birdeye", "tawk", "intercom", "drift", "hubspot") if k in tags],
        "tags": {k: {"label": LABELS.get(k, k), "ids": v} for k, v in tags.items()},
    }
