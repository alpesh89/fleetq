# Configuration

Everything is an environment variable. A `.env` file in the **fleet home** is loaded first and real
environment variables override it. The fleet home is `FLEETQ_HOME` if set, otherwise the repository
root when running from a checkout, otherwise the current directory. `inventory.txt`,
`deploy/roles.txt` and the install logs live there too.

## Database

| Variable | Default | Meaning |
|---|---|---|
| `FLEETQ_DSN` | `postgresql://fleetq:fleetq@localhost:5433/fleetq` | coordinator database |
| `FLEETQ_COORDINATOR` | | host of the coordinator; when set it builds the DSN from the parts below and **wins over `FLEETQ_DSN`**. The installer writes it to each worker's `.env`. |
| `FLEETQ_DB_USER` / `FLEETQ_DB_PASSWORD` / `FLEETQ_DB_NAME` / `FLEETQ_DB_PORT` | `fleetq` / `fleetq` / `fleetq` / `5433` | parts used with `FLEETQ_COORDINATOR` and by docker-compose |
| `FLEETQ_POOL_SIZE` | 8 | task connections per worker (heartbeats use their own) |

## Worker

| Variable | Default | Meaning |
|---|---|---|
| `FLEETQ_HANDLERS` | | extra handler directories, colon-separated |
| `FLEETQ_LEASE` | 300 | seconds a running task is leased before a sweeper may re-ready it |
| `FLEETQ_TASK_TIMEOUT` | 600 | a handler that has not returned by then is abandoned and retried |
| `FLEETQ_MAX_LOAD` | 1.0 | load average per core above which the worker stops claiming |
| `FLEETQ_MAX_CPU` | 0.85 | fraction of CPU busy above which it stops claiming |
| `FLEETQ_MIN_FREE_MEM_GB` | 1.5 | memory floor in GB (the smaller of this and the fraction applies) |
| `FLEETQ_MIN_FREE_MEM` | 0.10 | memory floor as a fraction of RAM |
| `FLEETQ_MAX_INFLIGHT` | 0 | total tasks at once on the host; 0 = per-kind ceilings only |
| `FLEETQ_NICE` | 10 | CPU priority reduction for the worker and its children |
| `FLEETQ_USER_AGENT` | `fleetq/<version> (+repo url; polite crawler)` | User-Agent for HTTP handlers; put a contact URL in it |
| `FLEETQ_WORKER_ID` | hostname | worker id used by the service scripts |

## Sites and crawls

| Variable | Default | Meaning |
|---|---|---|
| `FLEETQ_SERVER_RPS` | 2 | requests per second across all sites that share one server address |
| `FLEETQ_SITE_PENALTY_CAP` | 120 | max seconds a failing site's bucket is debited per failure streak |
| `FLEETQ_SITE_GIVE_UP` | 25 | consecutive failures after which a site's remaining tasks fail |
| `FLEETQ_SITE_DOWN_SECONDS` | 600 | how long the streak must have lasted before giving up |
| `FLEETQ_CRAWL_DEFAULT_LIMIT` | 100 | pages per crawl when no limit is given |
| `FLEETQ_CRAWL_MAX_LIMIT` | 2000 | ceiling no crawl may exceed |
| `FLEETQ_LISTING_DOMAINS` | | extra domains a redirect must not be followed into, comma-separated |

## Dashboard

| Variable | Default | Meaning |
|---|---|---|
| `DASHBOARD_PORT` | 8787 | listen port |
| `DASHBOARD_BIND` | 0.0.0.0 | `127.0.0.1` to keep it local |
| `DASHBOARD_INTERVAL` | 15 | seconds between SSH polls of the inventory |
| `FLEETQ_API_KEYS` | | comma-separated keys; when set every `/api/*` route except `/api/health` needs one |
| `FLEETQ_SSH_KEY` | `~/.ssh/fleetq` | identity file written into `~/.ssh/config` entries the dashboard creates |
