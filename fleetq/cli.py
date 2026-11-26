"""fleetq command line.

fleetq migrate                         create/upgrade the schema
fleetq worker [--id X] [--tags a,b]    run a worker on this machine
fleetq dashboard [--port 8787]         web dashboard + HTTP API (see fleetq/dashboard)
fleetq crawl URL [--limit N] [--rps R] sitemap -> fetch -> parse for one site
fleetq submit KIND --payload JSON      queue any task
fleetq status                          queue depth, throughput, workers, jobs
fleetq tasks [--kind K] [--status S]   list recent tasks
fleetq retry [--status dead]           re-queue dead/failed tasks
fleetq cancel JOB_ID                   cancel a job's pending tasks
fleetq export [--job ID] --out DIR     write documents as .md files
fleetq ctl reload|pause|resume         broadcast a control event to all workers
fleetq setup [--kinds a,b] [--force]   install what each handler needs on this machine
fleetq doctor                          report which handlers this machine can run
fleetq compress-blobs [--batch 200]    zstd-compress stored page bodies that are still raw
fleetq detect-signals [--batch 200]    (re)scan stored HTML for ad pixels, analytics, platform -> site_signals
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import warnings
from urllib.parse import urlsplit

from . import config
from .queue import Queue


def _table(rows: list[dict], cols: list[str]):
    if not rows:
        print("  (none)")
        return
    widths = {c: max(len(c), *(len(str(r.get(c, ""))) for r in rows)) for c in cols}
    print("  " + "  ".join(c.ljust(widths[c]) for c in cols))
    for r in rows:
        print("  " + "  ".join(str(r.get(c, "")).ljust(widths[c]) for c in cols))


async def cmd_migrate(q, a):
    await q.migrate()
    print("schema ready at", q.dsn)


def _age_seconds(v) -> float:
    """'7d', '12h', '30m', '3600' -> seconds."""
    v = str(v).strip().lower()
    mult = {"d": 86400, "h": 3600, "m": 60, "s": 1}
    return float(v[:-1]) * mult[v[-1]] if v and v[-1] in mult else float(v)


async def cmd_crawl(q, a):
    host = urlsplit(a.url).netloc or a.url
    limit = config.crawl_budget(a.limit)  # budget = pages; sitemap and parse tasks are not counted
    job = await q.create_job(f"crawl {host}", limit)
    payload = {"url": a.url, "limit": limit, "rps": a.rps, "max_age_s": _age_seconds(a.max_age), "force": a.force}
    tid = await q.submit("sitemap", payload, job_id=job, idem_key=f"sitemap:{a.url}", needs=["net"], priority=10, counted=False)
    print(f"job {job}: sitemap task #{tid} queued for {a.url} (budget {limit} pages)")


async def cmd_submit(q, a):
    payload = json.loads(a.payload) if a.payload else {}
    job = await q.create_job(a.job, a.max_tasks) if a.job else None
    tid = await q.submit(
        a.kind, payload, job_id=job, idem_key=a.idem_key, needs=a.needs.split(",") if a.needs else None, priority=a.priority
    )
    print(f"task #{tid} ({a.kind}) queued" if tid else "duplicate idempotency key; nothing queued")


async def cmd_status(q, a):
    s = await q.status()
    print("Tasks")
    _table(s["counts"], ["kind", "status", "n"])
    print("\nCompleted in the last minute")
    _table(s["per_minute"], ["kind", "n", "avg_seconds"])
    print("\nWorkers")
    rows = []
    for w in s["workers"]:
        slots_all = w["slots"] or {}
        host = slots_all.get("_host", {})
        slots = ", ".join(f"{k}:{v['inflight']}/{v['cur']}" for k, v in slots_all.items() if not k.startswith("_"))
        res = ""
        if host:
            cpu = f"{host['cpu_busy']:.0%}" if host.get("cpu_busy") is not None else "?"
            mem = f"{host['mem_avail_gb']}/{host['mem_total_gb']}G" if host.get("mem_avail_gb") is not None else "?"
            res = f"load {host.get('load', '?')} cpu {cpu} free {mem}" + (
                f" PRESSURE: {host['pressure']}" if host.get("pressure") else ""
            )
        rows.append(
            {
                "id": w["id"],
                "alive": "yes" if w["alive"] else "NO",
                "tags": ",".join(t for t in w["tags"] if t not in ("net", "cpu")),
                "slots (inflight/cur)": slots,
                "host": res,
            }
        )
    _table(rows, ["id", "alive", "tags", "slots (inflight/cur)", "host"])
    print("\nJobs")
    _table(s["jobs"], ["id", "name", "task_count", "max_tasks", "done", "pending", "failed"])


async def cmd_tasks(q, a):
    rows = await q.tasks(a.kind, a.status, a.job, a.limit)
    for r in rows:
        r["payload"] = json.dumps(r["payload"])[:60]
        r["result"] = (json.dumps(r["result"]) if r["result"] else "")[:50]
        r["error"] = (r["error"] or "")[:60]
        r["by"] = r.pop("claimed_by") or ""
    _table(rows, ["id", "job_id", "kind", "status", "attempts", "by", "payload", "result", "error"])


async def cmd_retry(q, a):
    n = await q.retry(a.status, a.kind, a.job)
    print(f"re-queued {n} task(s)")


async def cmd_cancel(q, a):
    n = await q.cancel_job(a.job_id)
    print(f"cancelled {n} pending task(s) in job {a.job_id}")


async def cmd_export(q, a):
    docs = await q.documents(a.job)
    os.makedirs(a.out, exist_ok=True)
    n = 0
    for d in docs:
        if not d["markdown"]:
            continue
        parts = urlsplit(d["url"])
        name = re.sub(r"[^a-zA-Z0-9]+", "-", (parts.netloc + parts.path).strip("/")).strip("-") or "index"
        path = os.path.join(a.out, name[:150] + ".md")
        with open(path, "w", encoding="utf-8") as f:
            f.write(f"# {d['title'] or d['url']}\n\nSource: {d['url']}\n\n{d['markdown']}\n")
        n += 1
    print(f"wrote {n} file(s) to {a.out}")


async def cmd_ctl(q, a):
    topic = {"reload": "reload_handlers"}.get(a.action, a.action)
    await q.publish(topic, {"target": a.target or "all"})
    print(f"sent {topic}")


def compress_blobs(dsn: str, batch: int) -> int:
    """Rewrite raw (encoding is null) blobs as zstd frames, a batch at a time, reporting savings."""
    import psycopg

    from fleetq import blobs

    done = before = after = 0
    with psycopg.connect(dsn) as conn:
        total = conn.execute("select count(*), coalesce(sum(octet_length(data)),0) from blobs where encoding is null").fetchone()
        print(f"{total[0]} raw blobs, {total[1] / 1e6:.1f} MB logical")
        while True:
            rows = conn.execute("select key, data from blobs where encoding is null order by key limit %s", (batch,)).fetchall()
            if not rows:
                break
            with conn.transaction():
                for key, data in rows:
                    data = bytes(data)
                    body, enc = blobs.pack(data)
                    before += len(data)
                    after += len(body)
                    # incompressible rows are marked 'raw' so they are not re-read on the next pass
                    conn.execute("update blobs set data = %s, encoding = %s where key = %s", (body, enc or "raw", key))
                    done += 1
            print(f"  {done} done, {before / 1e6:.1f} MB -> {after / 1e6:.1f} MB", flush=True)
    print(
        f"compressed {done} blobs: {before / 1e6:.1f} MB -> {after / 1e6:.1f} MB ({before / max(after, 1):.1f}x). Run VACUUM FULL blobs to reclaim disk."
    )
    return 0


def detect_signals(dsn: str, batch: int) -> int:
    """Backfill site_signals from every stored html: blob (pages parsed before this feature, or after a rule change)."""
    from urllib.parse import urlsplit

    import psycopg
    from psycopg.rows import dict_row
    from psycopg.types.json import Jsonb

    from fleetq import blobs, signals

    sites: dict[str, dict] = {}
    pages = 0
    last = ""
    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        while True:
            # Both the fetched HTML and the browser-rendered copy: Wix, Squarespace and tag managers often inject
            # pixels only after JavaScript runs, so a rendered page can carry tags the raw HTML lacks.
            rows = conn.execute(
                """select key, data, encoding from blobs
                                   where (key like 'html:%%' or key like 'rendered:%%') and key > %s order by key limit %s""",
                (last, batch),
            ).fetchall()
            if not rows:
                break
            for r in rows:
                last = r["key"]
                url = r["key"].split(":", 1)[1]
                host = urlsplit(url).netloc.lower().removeprefix("www.")
                if not host:
                    continue
                try:
                    html = blobs.unpack(r["data"], r["encoding"])
                except Exception as e:  # noqa: BLE001
                    print(f"  skip {url}: {type(e).__name__}: {e}", flush=True)
                    continue
                sig = signals.detect(html)
                cur = sites.get(host)
                sites[host] = signals.merge(cur, sig) if cur else signals.merge(None, sig)
                sites[host]["url"] = url
                sites[host]["pages"] = (cur or {}).get("pages", 0) + 1
                pages += 1
            print(f"  {pages} pages, {len(sites)} sites", flush=True)
        with conn.transaction():
            conn.execute("delete from site_signals")
            for host, s in sites.items():
                conn.execute(
                    """insert into site_signals (host, url, platform, builder, tags, pages) values (%s, %s, %s, %s, %s, %s)""",
                    (host, s["url"], s["platform"], s["builder"], Jsonb(s["tags"]), s["pages"]),
                )
    with_pixel = sum(1 for s in sites.values() if "meta_pixel" in s["tags"])
    print(
        f"scanned {pages} pages across {len(sites)} sites; Meta pixel on {with_pixel}, "
        f"platform known for {sum(1 for s in sites.values() if s['platform'])}"
    )
    return 0


def main(argv=None):
    warnings.filterwarnings("ignore", message="SOCKS support in urllib3")  # trafilatura pulls urllib3, which nags about PySocks
    argv = sys.argv[1:] if argv is None else list(argv)
    if argv and argv[0] == "dashboard":  # has its own options; see fleetq/dashboard
        from .dashboard import main as dashboard_main

        return dashboard_main(argv[1:])
    p = argparse.ArgumentParser(prog="fleetq", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--dsn", default=config.DSN, help="Postgres DSN (env FLEETQ_DSN)")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("migrate")

    sub.add_parser("dashboard", help="web dashboard and HTTP API (fleetq dashboard --help)")

    w = sub.add_parser("worker")
    w.add_argument("--id", help="worker id (default host-pid)")
    w.add_argument("--tags", help="comma list; default auto (net,cpu,arch,os)")
    w.add_argument("--handlers", action="append", default=[], help="extra handler directory (repeatable)")
    w.add_argument("-v", "--verbose", action="store_true")

    c = sub.add_parser("crawl")
    c.add_argument("url")
    c.add_argument("--limit", type=int, help=f"page budget (default {config.CRAWL_DEFAULT_LIMIT}, max {config.CRAWL_MAX_LIMIT})")
    c.add_argument("--rps", type=float, default=2.0, help="requests per second for this domain, fleet-wide")
    c.add_argument("--max-age", default="7d", help="skip pages fetched more recently than this (7d, 12h, 0 = always re-check)")
    c.add_argument("--force", action="store_true", help="ignore stored copies; fetch and parse everything again")

    s = sub.add_parser("submit")
    s.add_argument("kind")
    s.add_argument("--payload", help="JSON object")
    s.add_argument("--job", help="create a job with this name")
    s.add_argument("--max-tasks", type=int)
    s.add_argument("--idem-key")
    s.add_argument("--needs", help="comma list of worker tags")
    s.add_argument("--priority", type=int, default=0)

    sub.add_parser("status")

    t = sub.add_parser("tasks")
    t.add_argument("--kind")
    t.add_argument("--status")
    t.add_argument("--job", type=int)
    t.add_argument("--limit", type=int, default=30)

    r = sub.add_parser("retry")
    r.add_argument("--status", default="dead", choices=["dead", "failed", "cancelled"])
    r.add_argument("--kind")
    r.add_argument("--job", type=int)

    k = sub.add_parser("cancel")
    k.add_argument("job_id", type=int)

    e = sub.add_parser("export")
    e.add_argument("--job", type=int)
    e.add_argument("--out", default="./out")

    ctl = sub.add_parser("ctl")
    ctl.add_argument("action", choices=["reload", "pause", "resume", "stop", "dump"])
    ctl.add_argument("--target", help="worker id, default all")

    st = sub.add_parser("setup")
    st.add_argument("--kinds", help="comma list of handler kinds (default all)")
    st.add_argument("--handlers", action="append", default=[], help="extra handler directory (repeatable)")
    st.add_argument("--force", action="store_true", help="re-run setup even when the check passes")

    dr = sub.add_parser("doctor")
    dr.add_argument("--handlers", action="append", default=[])
    cb = sub.add_parser("compress-blobs", help="zstd-compress legacy raw blobs in place")
    cb.add_argument("--batch", type=int, default=200)
    cb.add_argument("--dsn", default=config.DSN)
    ds = sub.add_parser("detect-signals", help="rescan stored HTML for marketing/platform signals")
    ds.add_argument("--batch", type=int, default=200)
    ds.add_argument("--dsn", default=config.DSN)

    a = p.parse_args(argv)

    if a.cmd == "compress-blobs":
        sys.exit(compress_blobs(a.dsn, a.batch))
    if a.cmd == "detect-signals":
        sys.exit(detect_signals(a.dsn, a.batch))
    if a.cmd in ("setup", "doctor"):
        from .setup import run_setup

        kinds = a.kinds.split(",") if getattr(a, "kinds", None) else None
        sys.exit(run_setup(kinds, a.handlers, check_only=(a.cmd == "doctor"), force=getattr(a, "force", False)))

    if a.cmd == "worker":
        from .worker import main as worker_main

        worker_main(a.id, a.tags.split(",") if a.tags else None, a.handlers, a.dsn, a.verbose)
        return

    async def go():
        q = Queue(a.dsn, min_size=1, max_size=2)
        await q.open()
        try:
            await globals()[f"cmd_{a.cmd}"](q, a)
        finally:
            await q.close()

    asyncio.run(go())


if __name__ == "__main__":
    main()
