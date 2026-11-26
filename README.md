# fleetq

**Turn the computers you already have into one big machine.**

Most offices and home labs have a few Macs, a couple of Linux boxes, maybe an old server, all
sitting mostly idle. fleetq joins them into a cluster on your own private network. You give it a
large job, it cuts the job into many small tasks, and every machine takes tasks as fast as it can
finish them. Ten machines work on the job at once, so it finishes many times faster, and you never
had to buy one expensive computer to do it.

fleetq is just the name. Think of it as "a work queue for the fleet of machines you own".

## What it does, in plain words

1. **You install one small program (a worker) on each machine.** Mac or Linux, old or new, it does
   not matter. The worker knows what its machine can do: has a browser, has an OCR engine, has
   lots of memory, and so on.
2. **One machine keeps the list of work.** That list lives in an ordinary Postgres database.
   There is nothing else to run: no message broker, no control server, no cloud account.
3. **You submit a job.** Crawl a website and turn it into clean text, read a 500-page scanned PDF,
   or anything you write a handler for.
4. **The job splits itself into tasks and the machines pull them.** A fast machine takes more
   tasks, a slow machine takes fewer, a machine that is switched off takes none. If a machine
   crashes in the middle of a task, another one picks that task up.
5. **You watch it on a dashboard** and collect the results as files or through a small HTTP API.

The machines stay usable for the people sitting at them: each worker watches its own CPU and
memory and backs off before the machine feels slow.

## What comes in the box

- **Website crawling** that is polite by design: it respects robots.txt, keeps every machine
  together under one request rate per site, notices when a site is struggling and slows down,
  recognises "are you a robot" pages instead of hammering them, and only re-downloads pages that
  actually changed. Pages come out as Markdown.
- **Scanned PDF to text (OCR)** done entirely on your own machines, nothing uploaded anywhere:
  Apple Vision on Macs, Tesseract on Linux, one page per task so a long document is read in parallel.
- **Headless browser rendering** for pages that only show their content after JavaScript runs.
- **A dashboard** showing every machine (CPU, memory, disk, what it is working on), the queue, each
  job's progress, the documents produced, and buttons to install or update workers over SSH.
- **A handler system** so you can add your own kind of task in one Python file (see below).

## Why not a cloud service or a bigger computer?

- The hardware is already paid for and already on your desk.
- Confidential documents and internal data never leave your network.
- Adding capacity is plugging in another machine and clicking **Install worker**.
- When nothing is queued, the machines are just normal computers again.

Compared with job queues like Celery or RQ: fleetq needs only Postgres, routes tasks by what each
machine can do, and comes with the crawling, rendering and OCR pieces ready to use.

## Quick start (one machine)

```bash
git clone https://github.com/alpesh89/fleetq && cd fleetq
docker compose up -d                                        # Postgres on :5433
uv venv .venv && uv pip install --python .venv/bin/python -e '.[ocr]'
#   or: python3 -m venv .venv && .venv/bin/pip install -e '.[ocr]'
.venv/bin/fleetq migrate
.venv/bin/fleetq doctor                                     # which handlers this machine can run
.venv/bin/fleetq worker &                                   # one worker, tags detected automatically
.venv/bin/fleetq dashboard &                                # http://localhost:8787
.venv/bin/fleetq crawl https://example.com --limit 20 --rps 2
.venv/bin/fleetq status
.venv/bin/fleetq export --job 1 --out ./out                 # Markdown files
```

Then open the dashboard, drop a scanned PDF on **Scan a PDF (OCR)**, or try a custom task:

```bash
.venv/bin/fleetq worker --handlers examples/handlers &
.venv/bin/fleetq submit word_count --payload '{"text": "the quick brown fox"}'
.venv/bin/fleetq tasks --kind word_count
```

Python 3.11 or newer. Workers run on macOS (Apple silicon or Intel) and Linux.

## Add your own kind of task (a handler)

A handler is one Python file. It says what the task is called, what a machine needs to run it, and
what to do with one task.

```python
# my_handlers/shout.py
from fleetq.worker import Permanent

KIND = "shout"
NEEDS = ["cpu"]
SLOTS = dict(start=4, floor=1, ceiling=16)

async def run(task, ctx):
    text = task.payload.get("text")
    if not text:
        raise Permanent("payload.text required")        # fail without retry
    ctx.spawn("word_count", {"text": text}, idem_key=f"job{task.job_id}:count:{hash(text)}")
    return {"shout": text.upper()}                       # stored in tasks.result
```

`fleetq worker --handlers my_handlers` and the kind is live. `ctx` gives you a shared HTTP client,
the database pool, fleet-wide rate limits (`ctx.pace_site`, `ctx.rate_limit`), a process pool
(`ctx.run_cpu`), fan-out (`ctx.spawn`) and logging. `Defer(seconds)` releases a task without
counting an attempt; any other exception retries with backoff. A handler can declare what a
machine needs (`SETUP`) and a capability tag it grants (`TAG`), so `fleetq setup` prepares hosts
and tasks only land where they can run. The full contract is in **[docs/handlers.md](docs/handlers.md)**.

## Built-in handlers

| Kind | What it does |
|---|---|
| `sitemap` | find the sitemap or discover links from the homepage; redirect policy (rebrand vs. parking/listing page); spawns `fetch` within the job budget |
| `fetch` | polite download: robots.txt, conditional requests, content fingerprinting, change history, bot-wall detection; raw HTML into `blobs` |
| `parse` | Trafilatura HTML → Markdown into `documents`, every link as data, marketing/platform signals per site; escalates JavaScript-built pages to `render` |
| `render` | headless Chromium (Playwright) for pages that build their content in JavaScript |
| `ocr_split`, `ocr_page` | scanned PDF → one task per page → text, offline (Apple Vision on macOS, Tesseract on Linux), with per-page confidence |

Community handlers: open a pull request to add yours here (see [CONTRIBUTING.md](CONTRIBUTING.md)).

## Dashboard

`fleetq dashboard` serves one page and a JSON API on port 8787:

- **Machines**: every host in `inventory.txt` polled over SSH (nothing installed on the hosts),
  CPU, memory, disk, the worker's state and slots, code version drift, top processes. **Add machine**
  writes the SSH config entry and installs the worker; **Install / Update worker** re-syncs code.
- **Queue**: start a crawl, queue a custom task, upload a PDF; watch progress per stage and per
  machine; read documents; download Markdown zips or OCR text; retry or cancel.
- **API**: everything above over HTTP, with optional API keys. See [docs/api.md](docs/api.md) and
  the dependency-free client in `examples/client.py`.

## Run a fleet

Postgres on one always-on box, the dashboard next to it, SSH keys to the workers, then:

```bash
deploy/install_worker.sh mini1 192.168.1.50                  # rsync, venv, fleetq setup, launchd/systemd service
deploy/install_fleet.sh 192.168.1.50                         # everyone in inventory.txt, in parallel
```

Hosts configure themselves for the handlers they are assigned (`deploy/roles.txt`), restart on
failure and on boot, and show up in `fleetq status` within seconds. macOS specifics (Local
Network privacy, LaunchDaemons) are handled by the scripts. Details in
**[docs/deploy.md](docs/deploy.md)**.

## Documentation

- [docs/handlers.md](docs/handlers.md): the handler contract, routing, fan-out, politeness, storage
- [docs/architecture.md](docs/architecture.md): how the queue, pacing, adaptive concurrency and the built-in handlers work
- [docs/configuration.md](docs/configuration.md): every environment variable
- [docs/deploy.md](docs/deploy.md): coordinator, SSH, installing workers, services, day-to-day commands
- [docs/api.md](docs/api.md): the dashboard's HTTP API
- [CONTRIBUTING.md](CONTRIBUTING.md), [SECURITY.md](SECURITY.md), [CHANGELOG.md](CHANGELOG.md)

## Commands

```
fleetq migrate                           create or upgrade the schema (idempotent)
fleetq worker [--id X] [--tags a,b] [--handlers DIR]
fleetq dashboard [--port 8787] [--api-key KEY]
fleetq crawl URL [--limit N] [--rps R] [--max-age 7d] [--force]
fleetq submit KIND --payload '{...}' [--needs net] [--job NAME --max-tasks N]
fleetq status | tasks | retry | cancel JOB | export --job ID --out DIR
fleetq ctl reload | pause | resume | stop | dump [--target WORKER]
fleetq setup [--kinds a,b] [--force]     install what each handler needs on this machine
fleetq doctor                            report which handlers this machine can run
```

## Status

fleetq runs a production fleet of Mac minis and Ubuntu servers and has crawled hundreds of
thousands of pages. The API and the handler contract are stable for the 0.x series; schema
changes are always additive. Not yet: handler subprocess isolation, per-handler virtual
environments, a web UI for editing roles. Ideas and handlers welcome.

## Contributors

fleetq is maintained by [Alpesh](https://github.com/alpesh89) at Aveosoft Private Limited. Everyone
who has contributed is listed in [CONTRIBUTORS.md](CONTRIBUTORS.md) and on the
[contributors graph](https://github.com/alpesh89/fleetq/graphs/contributors). Add your handler,
fix or doc improvement and add yourself to the list in the same pull request.

## License

[MIT](LICENSE). Copyright (c) 2026 Aveosoft Private Limited.
