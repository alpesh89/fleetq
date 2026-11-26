# Contributing to fleetq

Thanks for helping. Every contributor is credited in [CONTRIBUTORS.md](CONTRIBUTORS.md). Most contributions are one of three things: a **new handler** (a task kind the
fleet can run), a **fix or improvement to the core** (queue, worker, dashboard), or **docs**. All are
welcome. Please open an issue first for anything bigger than a small fix so we can agree on the
approach before you spend time on it.

## Set up a development environment

    git clone https://github.com/alpesh89/fleetq && cd fleetq
    docker compose up -d                         # Postgres on :5433
    uv venv .venv && uv pip install --python .venv/bin/python -e '.[dev,ocr]'
    # (or: python3 -m venv .venv && .venv/bin/pip install -e '.[dev,ocr]')
    .venv/bin/fleetq migrate
    .venv/bin/fleetq doctor                      # what this machine can run

Run a worker and the dashboard in two terminals, then submit something:

    .venv/bin/fleetq worker -v
    .venv/bin/fleetq dashboard
    .venv/bin/fleetq crawl https://example.com --limit 20

`scripts/smoke.py` is the end-to-end check CI runs: it starts a worker, submits the example handler
from `examples/handlers/` and waits for the result. Run it before opening a pull request:

    .venv/bin/python scripts/smoke.py

## Writing a handler

Read [docs/handlers.md](docs/handlers.md). The short version: one module, `KIND`, `NEEDS`, an
`async def run(task, ctx)`, optional `SLOTS`, `SETUP` and `TAG`. Put it in `fleetq/handlers/` if it is
generally useful and has no paid or proprietary dependency; otherwise ship it as a separate package
with a `fleetq.handlers` entry point, and open a PR that adds a line to the "Community handlers" list
in the README.

A handler PR is accepted when:

- It does one thing and documents its payload and result in the module docstring.
- Its network use is polite: it goes through `ctx.pace_site` / `ctx.rate_limit`, respects
  `robots.txt` where that applies, and never retries a 429 without `Defer(..., throttled=True)`.
- Its machine setup is declared in `SETUP` and `fleetq doctor` reports correctly on a machine
  without the dependency.
- Anything it stores has a table in `schema.sql` guarded by `create table if not exists`, or goes
  into `tasks.result` / `blobs` / `documents`.
- Secrets come from environment variables, never from the payload or the repository.

## Core changes

- Keep the queue's invariants: children are inserted in the transaction that completes the parent;
  leases are the only lock; everything in `schema.sql` must be re-runnable.
- Behaviour that costs a remote site anything (requests per second, retries, concurrency growth)
  needs a comment that says why the number is what it is.
- The dashboard is dependency-free on purpose (stdlib HTTP server, one static page). Please keep it
  that way.

## Style

- Python 3.11+, `ruff check .` and `ruff format --check .` must pass (settings in `pyproject.toml`).
- Comments explain *why*, not *what*. Module docstrings state the payload and result shape.
- Commit messages: a short imperative subject, a body when the change needs context.
- Add a line to `CHANGELOG.md` under *Unreleased* for anything a user would notice.
- Add yourself to `CONTRIBUTORS.md` with your first contribution; credit is part of the project.

## Pull requests

1. Fork, branch from `main`, make the change.
2. Run `ruff check .` and `scripts/smoke.py`.
3. Open the PR with the template filled in. CI runs lint and the smoke test against a Postgres service.
4. A maintainer reviews; expect questions about rate limits and failure modes, that is where bugs live.

By contributing you agree that your contribution is licensed under the MIT license of this repository.
