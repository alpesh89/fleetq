# Writing a handler

A handler is one Python module that teaches every worker in the fleet a new task kind. The
worker imports it, checks the machine has what it needs, and from then on claims tasks of that
kind, runs them with a context object, and records the result.

## The contract

```python
"""translate: send a stored document to a local model and store the translation.

payload: {"url": "https://...", "lang": "fr"}         result: {"chars": 1234}
"""
from fleetq.worker import Defer, Permanent

KIND = "translate"                       # the task kind this module handles (required)
VERSION = 1                              # bump when the output changes; stored on each task
NEEDS = ["net", "gpu"]                   # worker tags required; the task is only claimed where all are present
SLOTS = dict(start=2, floor=1, ceiling=8)   # concurrent tasks of this kind on one worker (adaptive between floor and ceiling)
TAG = "gpu"                              # optional: workers earn this tag when SETUP["check"] passes on them

SETUP = {                                # optional: how to prepare a machine (`fleetq setup`), and how to tell it is ready
    "pip": ["httpx>=0.27"],
    "macos": ["brew install something"],
    "linux": ["sudo -n apt-get install -y something"],
    "any": [],
    "check": lambda: (True, "ready"),    # () -> (ok, detail); used by `fleetq doctor` and for TAG
}


async def run(task, ctx):                # required
    ...
    return {"chars": n}                  # any JSON-serialisable value, stored in tasks.result
```

`run` must be `async`. Blocking work (parsing, OCR, image processing) goes through
`ctx.run_cpu`, blocking I/O through `asyncio.to_thread`, HTTP through `ctx.http`.

### What `task` holds

| Attribute | Meaning |
|---|---|
| `task.id`, `task.job_id`, `task.parent_id` | identifiers; `job_id` groups a submission |
| `task.kind`, `task.version` | the kind, and the handler version the task was created for |
| `task.payload` | the dict the submitter (or a parent handler) passed |
| `task.attempts`, `task.max_attempts` | this is attempt N of M |
| `task.depth` | page hops from the root task; `ctx.spawn` stops at `FLEETQ_MAX_DEPTH` |

### What `ctx` offers

| Member | Use |
|---|---|
| `ctx.http` | shared `httpx.AsyncClient` with the fleet User-Agent, redirects on, 30 s timeout |
| `ctx.db` | `psycopg_pool.AsyncConnectionPool` on the coordinator database |
| `ctx.log` | logger named `fleetq.<kind>` |
| `ctx.spawn(kind, payload, idem_key=, needs=, priority=, depends_on=, max_attempts=, version=, counted=)` | queue a child task. Children are inserted in the same transaction that marks this task done, so a crash never leaves half a fan-out. `counted=False` children do not use the job's task budget. |
| `await ctx.pace_site(host, rps, give_up_after=)` | take one token for the site **and** one for the server it resolves to; defers the task if the wait is long; fails it permanently after `give_up_after` consecutive failures |
| `await ctx.rate_limit(name, per_second, burst=, max_wait=)` | a named fleet-wide token bucket (`"llm:local"`, `"api:vendor"`) |
| `await ctx.site_failed(host, rps)` / `await ctx.site_ok(host)` | record a failed or successful request to a site so the whole fleet slows down on sick sites |
| `await ctx.run_cpu(fn, *args)` | run `fn` in the worker's process pool (picklable args and result) |
| `await ctx.robots_allowed(url)` | robots.txt check with a per-worker cache |
| `await ctx.server_of(host)` | the address a host resolves to (cached) |

### Outcomes

| Do this | Effect |
|---|---|
| `return value` | task done; `value` stored in `tasks.result`; children from `ctx.spawn` inserted |
| `raise Defer(seconds, why, throttled=False, patch=None)` | task released without counting an attempt and re-queued after `seconds`. `throttled=True` tells the slot controller the remote side pushed back (HTTP 429, captcha). `patch` is merged into the payload so the next attempt remembers something (a block count, a cursor). |
| `raise Permanent(msg)` | task failed, no retry, `msg` stored in `tasks.error` |
| any other exception | task retried with exponential backoff until `max_attempts`, then marked `dead` |
| not returning for `FLEETQ_TASK_TIMEOUT` seconds (600) | abandoned and retried like an error |

## Where handlers live

1. **Built-in**: `fleetq/handlers/<kind>.py` in this repository. For handlers that are generally
   useful and have no paid or proprietary dependency.
2. **A directory**: `fleetq worker --handlers /path/to/dir` (repeatable) or
   `FLEETQ_HANDLERS=/path/a:/path/b`. Every `*.py` file there is imported by file name. Good for
   project-specific handlers kept next to the project.
3. **A package**: any installed distribution that registers an entry point in the
   `fleetq.handlers` group:

   ```toml
   [project.entry-points."fleetq.handlers"]
   translate = "my_package.translate"
   ```

   `pip install my-package` on each worker and the kind appears after `fleetq ctl reload`.

The last definition of a KIND wins, so a directory or package can override a built-in handler.
`fleetq doctor` lists every kind found, where each is importable, and what its check says.

## Tags and routing

Every worker starts with `net`, `cpu`, its architecture (`arm64`, `x86_64`) and OS (`macos`,
`linux`). A handler that declares `TAG` adds that tag to every worker where its `SETUP["check"]`
passes: `render` adds `browser` where Playwright and Chromium work, `ocr_page` adds `ocr` where an
OCR engine exists. A task's `needs` list (from the handler's `NEEDS`, or set explicitly at submit
time) must be a subset of the worker's tags for the worker to claim it. Pass `--tags` to a worker
to set its tags by hand (for example `--tags net,cpu,arm64,macos,gpu` on the machine with the GPU).

## Fan-out and idempotency

Give every child an `idem_key` that encodes what it is (`f"job{task.job_id}:fetch:{url}"`).
A duplicate key is silently dropped, so re-running a parent after a crash, or two parents
discovering the same URL, never creates duplicate work. Prefix keys with the job id when the same
thing should be done again in a later job.

A job's `max_tasks` budget counts tasks spawned with `counted=True` (the default). Follow-up
tasks that always accompany another (the `parse` after every `fetch`) are spawned `counted=False`
so a budget of 50 means 50 pages.

## Being polite to remote systems

- Call `await ctx.pace_site(host, rps)` before every request to a website. This is what keeps
  the whole fleet at one site's agreed rate, however many machines run the handler.
- On a timeout, 5xx, 429 or a bot wall call `await ctx.site_failed(host, rps)`; on a real answer
  `await ctx.site_ok(host)`. The site's bucket then slows the fleet down on its own.
- A 429 or a challenge page is `raise Defer(seconds, why, throttled=True)`, never a plain retry.
  `fleetq.botwall.refuse(ctx, task, response)` does the right thing for common vendors.
- For paid APIs use `await ctx.rate_limit("api:vendor", per_second)` and keep credentials in
  environment variables read at run time; a missing credential is a `Defer`, not a crash.

## Storing results

- Small results: return them; they land in `tasks.result` (JSONB) and show in the dashboard.
- Page bodies and other binary data: `fleetq.blobs.put(conn, key, content_type, data, task_id)`,
  compressed transparently; read back with `blobs.get`.
- Text documents: the `documents` table (`url` unique, `title`, `markdown`, `word_count`,
  `job_id`). Anything in it appears in the dashboard's Documents tab and the Markdown export.
- Your own tables: add them to `fleetq/schema.sql` with `create table if not exists`; the file
  runs on every worker start and on `fleetq migrate`.

## Machine setup

`SETUP["pip"]` packages are installed into the worker's virtualenv; `SETUP["macos"]`,
`SETUP["linux"]` and `SETUP["any"]` are shell steps (with `{python}` and `{home}` substituted).
`fleetq setup` runs the check first and skips handlers that already pass, so it is safe on every
deploy; the installer runs it after syncing the code. Keep the check honest: it should fail on a
machine that cannot actually run the handler (a downloaded browser that cannot launch is a classic).

## Testing a handler

```
fleetq worker --handlers ./my_handlers -v              # terminal 1
fleetq submit my_kind --payload '{"x": 1}' --job test  # terminal 2
fleetq tasks --kind my_kind                            # status, result, error
```

`fleetq ctl reload` makes running workers re-import handlers without a restart (rsync the file
first on remote workers; `deploy/install_worker.sh <alias> <coordinator>` does both).
