# How fleetq works

One Postgres database is the queue, the result store and the control channel. Every machine runs
the same worker program; task kinds are plug-in handlers. Workers pull, so faster machines take
more work and a machine that dies simply stops pulling.

    coordinator: Postgres  <-- one pool each --  worker on every Mac and Linux box
                                                 tags: net, cpu, arm64/x86_64, macos/linux, + earned tags

## Queue

- **Pull, not push.** A worker claims tasks with `SELECT ... FOR UPDATE SKIP LOCKED`, ordered by
  priority then age, only kinds it has a handler for and only tasks whose `needs` its tags cover.
  Twenty workers can claim at once and never collide.
- **Leases, not locks.** A running task holds a lease (`FLEETQ_LEASE`, 300 s) that the worker
  renews at half-life. If the worker dies, any worker's sweeper re-readies the task. Failures
  retry with exponential backoff and jitter until `max_attempts`, then go to `dead`.
- **Fan-out from any node.** `ctx.spawn` children are inserted in the transaction that marks
  the parent done, after the job row lock, with idempotency keys filtered in one query, so a crash
  never leaves half a fan-out and duplicates are harmless. Jobs carry a task budget and
  `FLEETQ_MAX_DEPTH` stops runaway recursion.
- **Instant wake-up.** A trigger sends `NOTIFY fleetq_tasks` on insert or re-ready; workers
  `LISTEN` and claim within milliseconds, with a 2 s poll as fallback. Control events
  (`reload`, `pause`, `resume`, `stop`, `dump`) go through the `events` table and `fleetq_control`.
- **Keepalives.** Every connection uses TCP keepalives, so a query on a socket the network silently
  dropped fails within about a minute instead of holding a slot forever. As a backstop a handler
  that has not returned after `FLEETQ_TASK_TIMEOUT` (600 s) is abandoned and the task retried.

## Rate limiting and pacing

- **Fleet-wide token buckets** live in `rate_limits`. `ctx.rate_limit("domain:example.com", 2)`
  takes a token shared by every machine. A long wait defers the task instead of parking a slot.
- **Pacing at enqueue time.** When children are inserted, each site's requests are spread along
  the site's own timeline: `rate_limits.scheduled_until` is a cursor per domain, and n fetches at
  `rps` get `run_after` values `base, base + 1/rps, ...`. Only about `rps` tasks per site are
  claimable in any second, so the claim query naturally interleaves every site with a slot due. Ten
  sites take about as long as one. A partial index on `(kind, run_after) where status = 'ready'`
  keeps a claim cheap however many paced rows wait behind it.
- **Shared servers.** After the site's token a fetch takes one from a bucket keyed by the address
  the site resolves to (`FLEETQ_SERVER_RPS`, 2/s), so many small sites on one shared host do not
  add up to a flood.
- **Failing sites back off.** A timeout, 5xx, 429 or bot wall debits the site's bucket by 5 s per
  consecutive failure up to `FLEETQ_SITE_PENALTY_CAP` (120 s); one real answer clears the streak.
  A site that failed `FLEETQ_SITE_GIVE_UP` (25) times in a row for `FLEETQ_SITE_DOWN_SECONDS`
  (600) has its remaining tasks failed with `site down`.

## Worker

- **Adaptive concurrency.** Each kind has a slot controller tuned every 5 s from the machine's own
  signals: halve on host pressure or load, grow while the box is cool and work is waiting (back to
  the start value after a cut, then a quarter at a time). Remote errors and push-back are shown but
  never cut slots: a sick site is slowed through its own bucket, not by starving unrelated sites.
  A controller idle for 60 s snaps back to its start value.
- **Resource guard.** Every 5 s the worker reads load, CPU busy and available memory for the whole
  machine. Past `FLEETQ_MAX_LOAD`, `FLEETQ_MAX_CPU` or the memory floor it stops claiming and halves
  its slots until readings clear; the reason shows in `fleetq status`. Workers run at reduced
  priority (`FLEETQ_NICE`, 10).
- **Process pool.** `ctx.run_cpu` runs CPU work in a pool of `cores / 2` processes. If a child dies
  (memory, a crash in a C extension) the pool is replaced and the call retried once.
- **Code identity.** Each worker hashes the `fleetq` package plus handler directories and reports
  it in its heartbeat; the dashboard flags machines whose hash differs from the coordinator's.
- **Hot reload.** `fleetq ctl reload` re-imports handlers in every running worker.

## Built-in handlers

| Kind | Needs | What it does |
|---|---|---|
| `sitemap` | net | loads the site root (redirect policy: rebrand vs listing/parking page), finds the sitemap or falls back to link discovery from the homepage, orders never-seen and recently modified URLs first, spawns `fetch` within the job budget |
| `fetch` | net | polite download with robots.txt, conditional requests, content fingerprint, change bookkeeping in `pages`, raw HTML into `blobs`; bot-wall detection; spawns `parse` |
| `parse` | cpu | Trafilatura HTML to Markdown into `documents`, every anchor as structured links, marketing/platform signals into `site_signals`; escalates JavaScript-built pages to `render` |
| `render` | browser | headless Chromium, assets blocked, waits for network idle, stores the rendered HTML and queues a parse of it |
| `ocr_split` | ocr | cuts an uploaded PDF into single-page blobs and spawns one `ocr_page` per page |
| `ocr_page` | ocr | renders a page at 300 DPI (up to 600 if the scan is sharper), recognises it offline (Apple Vision on macOS, Tesseract on Linux), stores the text as a document with a confidence score |

### Change detection

`pages` remembers each URL: when it was fetched, when its content last changed, ETag and
Last-Modified, a content hash, the sitemap `lastmod`. On every fetch: inside the re-check window
(`max_age_s`, default 7 days) and stored fine → skipped without a request; sitemap `lastmod`
unchanged → skipped; else a conditional request (304 → skip); on 200 an identical fingerprint →
skip; only new or changed content is stored and re-parsed. `force=true` bypasses all of it.

### Blocked and redirected sites

`fleetq/botwall.py` recognises challenge pages (Cloudflare, SiteGround, Imperva, AWS WAF and
others) by header and body markers. The task is deferred with growing waits and the slot
controller told to ease off; after three challenges it fails permanently naming the vendor.
Nothing attempts to pass a challenge. `fleetq/redirects.py` decides whether a start URL that
lands on another domain is a rebrand (crawl the new domain) or a listing, marketplace, parking or
social page (do not scrape); `fleetq/listing_domains.txt` and `FLEETQ_LISTING_DOMAINS` list those.

## Tables

| Table | Holds |
|---|---|
| `workers` | who is alive, tags, kinds, current slots with reasons, code hash |
| `jobs` | a submission with a task budget |
| `tasks` | the queue: status, lease, attempts, payload, result, parent, depends_on |
| `rate_limits` | token buckets, pacing cursors, failure streaks |
| `events` | control broadcasts |
| `blobs` | fetched and rendered HTML, uploaded PDFs, page images; zstd-compressed |
| `documents` | parsed Markdown per URL (and OCR text per page) |
| `pages` | per-URL change-detection memory |
| `site_signals` | marketing and platform signals per site |

Measured storage: about 25 KB per page of compressed HTML, 5-11 KB Markdown, 4-12 KB links,
5 KB rows and indexes; roughly 45 KB per page all in.
