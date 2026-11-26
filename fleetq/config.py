"""Runtime settings. Everything comes from environment variables (or the .env file in FLEETQ_HOME);
the defaults match docker-compose.yml. See docs/configuration.md for the full list."""

import os

from fleetq import envfile

envfile.load()  # .env in the fleet home, if present; real environment variables take precedence

HOME = envfile.home()  # .env, inventory.txt, deploy/roles.txt and install logs live here

DB_USER = os.environ.get("FLEETQ_DB_USER", "fleetq")
DB_PASSWORD = os.environ.get("FLEETQ_DB_PASSWORD", "fleetq")
DB_NAME = os.environ.get("FLEETQ_DB_NAME", "fleetq")
DB_PORT = int(os.environ.get("FLEETQ_DB_PORT", "5433"))
DSN = os.environ.get("FLEETQ_DSN", f"postgresql://{DB_USER}:{DB_PASSWORD}@localhost:{DB_PORT}/{DB_NAME}")
# A worker's installer writes FLEETQ_COORDINATOR=<host> to the worker's .env on every run, and it
# wins over FLEETQ_DSN: service files may still carry a DSN with an old fixed address, and editing
# those needs sudo on every machine. Changing the coordinator's address then needs no sudo anywhere.
COORDINATOR = os.environ.get("FLEETQ_COORDINATOR", "").strip()
if COORDINATOR:
    DSN = f"postgresql://{DB_USER}:{DB_PASSWORD}@{COORDINATOR}:{DB_PORT}/{DB_NAME}"
LEASE_SECONDS = int(os.environ.get("FLEETQ_LEASE", "300"))
HEARTBEAT_SECONDS = 10
SWEEP_SECONDS = 30
CONTROL_TICK_SECONDS = 5
IDLE_POLL_SECONDS = 2.0
CLAIM_BATCH = 10
MAX_DEPTH = 8  # link hops from the start page
POOL_SIZE = int(os.environ.get("FLEETQ_POOL_SIZE", "8"))  # task-traffic connections per worker (heartbeats use their own)
# A connection the network silently drops (a Docker port proxy, a Wi-Fi blip) leaves a query waiting for a
# reply that never comes; keepalives turn that into an error within about a minute.
PG_KEEPALIVES = {"keepalives": 1, "keepalives_idle": 30, "keepalives_interval": 10, "keepalives_count": 3}
# Failing sites. Each failed request (timeout, 5xx, 429, bot wall) debits the site's token bucket by
# 5 s per consecutive failure, up to SITE_PENALTY_CAP, so the fleet probes a sick site less and less
# often instead of burning a slot per page on it. A site that has failed SITE_GIVE_UP_FAILURES times in
# a row for SITE_DOWN_SECONDS has its remaining pages failed fast; resubmit the site when it is back.
SITE_PENALTY_CAP = float(os.environ.get("FLEETQ_SITE_PENALTY_CAP", "120"))
SITE_GIVE_UP_FAILURES = int(os.environ.get("FLEETQ_SITE_GIVE_UP", "25"))
SITE_DOWN_SECONDS = float(os.environ.get("FLEETQ_SITE_DOWN_SECONDS", "600"))
# Sites that share a server share a second bucket keyed by its address, so twenty small sites on one
# shared host do not add up to twenty requests a second against it. A site's own rps still applies underneath.
SERVER_RPS = float(os.environ.get("FLEETQ_SERVER_RPS", "2"))
TASK_TIMEOUT = float(os.environ.get("FLEETQ_TASK_TIMEOUT", "600"))  # hard cap on one task run; past it the task fails and retries

# Crawl budgets. A crawl request that gives no page limit gets the default; no request may exceed the ceiling.
CRAWL_DEFAULT_LIMIT = int(os.environ.get("FLEETQ_CRAWL_DEFAULT_LIMIT", "100"))
CRAWL_MAX_LIMIT = int(os.environ.get("FLEETQ_CRAWL_MAX_LIMIT", "2000"))


def crawl_budget(limit) -> int:
    """Pages a crawl may fetch: the default when none is given, never above the ceiling."""
    try:
        n = int(limit) if limit not in (None, "") else 0
    except (TypeError, ValueError):
        n = 0
    return CRAWL_DEFAULT_LIMIT if n <= 0 else min(n, CRAWL_MAX_LIMIT)


# Resource guard: the worker never grows past these and sheds load when crossed.
MAX_LOAD = float(os.environ.get("FLEETQ_MAX_LOAD", "1.0"))  # load avg / cores (Linux load includes I/O wait)
MAX_CPU_BUSY = float(os.environ.get("FLEETQ_MAX_CPU", "0.85"))  # fraction of CPU busy (all processes)
# Memory floor = the smaller of these two, so big machines keep 1.5 GB and an 8 GB Mac keeps ~0.8 GB.
MIN_FREE_MEM_FRACTION = float(os.environ.get("FLEETQ_MIN_FREE_MEM", "0.10"))  # of total RAM
MIN_FREE_MEM_BYTES = int(float(os.environ.get("FLEETQ_MIN_FREE_MEM_GB", "1.5")) * 1e9)
MAX_INFLIGHT = int(os.environ.get("FLEETQ_MAX_INFLIGHT", "0"))  # total tasks at once on this host; 0 = per-kind ceilings only
NICE = int(os.environ.get("FLEETQ_NICE", "10"))  # lower our CPU priority below interactive and server work
