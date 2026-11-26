"""render: load a page in headless Chromium for sites that build their content with JavaScript.

Spawned by `parse` when a fetched page has almost no text but carries scripts.
Stores the rendered HTML as a new blob and queues a parse of it (rendered=true, so it
never escalates again). A bot-protection challenge screen is never stored: the task is
deferred and later fails permanently, exactly as in fetch (see fleetq/botwall.py).

payload: {"url": "...", "rps": 2}
Requires: pip install playwright && playwright install chromium   (worker then gets the "browser" tag)
"""

import asyncio
import os
from urllib.parse import urlsplit

from fleetq import blobs, botwall
from fleetq.urls import extract_links
from fleetq.worker import USER_AGENT, Defer, Permanent

KIND = "render"
VERSION = 4
NEEDS = ["browser"]
TAG = "browser"  # a worker earns this tag when SETUP["check"] passes on it


def _mem_slots() -> int:
    """About one browser context per 1.5 GB of RAM, capped at 8."""
    try:
        total = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    except (ValueError, OSError, AttributeError):
        return 2
    return max(1, min(8, int(total / 1.5e9)))


SLOTS = dict(start=1, floor=1, ceiling=_mem_slots())


def setup_check():
    try:
        import playwright  # noqa: F401
    except ImportError:
        return False, "playwright not installed"
    import glob

    cache = os.path.expanduser("~/Library/Caches/ms-playwright" if os.uname().sysname == "Darwin" else "~/.cache/ms-playwright")
    hits = glob.glob(os.path.join(cache, "chromium*"))
    if not hits:
        return False, "chromium not downloaded"
    # Downloaded is not the same as runnable (Linux needs system libraries): launch it once.
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as pw:
            b = pw.chromium.launch(headless=True, timeout=20000)
            v = b.version
            b.close()
        return True, f"chromium {v} launches"
    except Exception as e:
        first = str(e).strip().splitlines()[0][:120] if str(e).strip() else type(e).__name__
        return False, f"chromium present but cannot launch: {first}"


SETUP = {
    "pip": ["playwright>=1.47"],
    "macos": ["{python} -m playwright install chromium"],
    "linux": ["{python} -m playwright install --with-deps chromium"],  # needs sudo once for system libs
    "check": setup_check,
}

_state: dict = {}
BLOCKED = ("image", "media", "font", "stylesheet")


async def _browser():
    if "browser" not in _state:
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            raise Permanent("playwright is not installed on this worker")
        _state["lock"] = asyncio.Lock()
        async with _state["lock"]:
            if "browser" not in _state:
                pw = await async_playwright().start()
                _state["pw"] = pw
                _state["browser"] = await pw.chromium.launch(headless=True)
    return _state["browser"]


async def run(task, ctx):
    url = task.payload["url"]
    host = urlsplit(url).netloc
    rps = float(task.payload.get("rps", 2))

    await ctx.pace_site(host, rps)
    browser = await _browser()
    context = await browser.new_context(user_agent=USER_AGENT, viewport={"width": 1280, "height": 900}, java_script_enabled=True)
    try:
        page = await context.new_page()
        await page.route("**/*", lambda route: route.abort() if route.request.resource_type in BLOCKED else route.continue_())
        resp = await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        status = resp.status if resp else 0
        headers = resp.headers if resp else {}
        if status == 429:
            raise Defer(15, "HTTP 429 in browser", throttled=True)
        if status >= 400:
            botwall.refuse(task, status, headers, (await page.content()).encode("utf-8", "replace"))
            return {"status": status}
        try:
            await page.wait_for_load_state("networkidle", timeout=10000)
        except Exception:
            pass  # sites that poll never go idle; take what has rendered
        await page.wait_for_timeout(500)
        html = await page.content()
        botwall.refuse(task, status, headers, html.encode("utf-8", "replace"))  # a challenge screen is not the page
    finally:
        await context.close()

    key = f"rendered:{url}"
    async with ctx.db.connection() as conn:
        await blobs.put(conn, key, "text/html; rendered", html.encode("utf-8"), task.id)
    ctx.spawn(
        "parse", {"url": url, "blob_key": key, "rendered": True}, idem_key=f"parse-rendered:{url}", needs=["cpu"], counted=False
    )

    # Links on JS-built pages only exist after rendering, so discovery continues from here.
    found = 0
    if task.payload.get("discover"):
        for u in extract_links(html, url, int(task.payload.get("max_links", 300))):
            if u != url:
                ctx.spawn(
                    "fetch",
                    {"url": u, "rps": rps, "discover": True, "max_links": task.payload.get("max_links", 300)},
                    idem_key=f"fetch:{u}",
                    needs=["net"],
                    priority=max(0, 10 - task.depth),
                )
                found += 1
    return {"status": status, "bytes": len(html), "links": found}
