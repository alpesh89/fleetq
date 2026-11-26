# Dashboard HTTP API

The dashboard (`fleetq dashboard`, port 8787) is how other systems submit work and collect
results. Every route returns JSON unless noted. When `FLEETQ_API_KEYS` is set, send
`Authorization: Bearer <key>` (or `X-API-Key: <key>`; `?api_key=` is accepted on downloads).
`examples/client.py` is a dependency-free Python client for the queue routes.

## Queue

| Route | Body / query | Returns |
|---|---|---|
| `POST /api/queue/crawl` | `{url, limit, rps, name, max_age_s, force}` | `{job_id}` |
| `POST /api/queue/submit` | `{kind, payload, needs, name, max_tasks}` | `{job_id, task_id}` |
| `POST /api/queue/ocr` | raw PDF bytes; `X-File-Name` header or `?name=` | `{job_id, doc, bytes}` |
| `GET /api/queue/overview` | | counts by kind and status, last-minute throughput, workers, recent jobs |
| `GET /api/queue/jobs?limit=50` | | jobs with progress, pages fetched / unchanged, documents |
| `GET /api/queue/job/<id>` | | the job: progress by kind and worker, timing, errors, running tasks, site signals |
| `GET /api/queue/job/<id>/tasks?status=&kind=&limit=` | | tasks with payload, result, error |
| `GET /api/queue/job/<id>/documents` | | documents produced by the job with change history |
| `GET /api/queue/job/<id>/export` | | zip of Markdown files (download) |
| `GET /api/queue/job/<id>/text` | | OCR jobs: the whole PDF as plain text (download) |
| `GET /api/queue/document?url=` | | one document's Markdown and links |
| `GET /api/queue/site?host=` | | marketing and platform signals for one site |
| `GET /api/queue/kinds` | | kinds some live worker can run |
| `POST /api/queue/job/<id>/cancel` \| `retry` \| `delete` | `{}` | `{ok, affected}` |
| `POST /api/queue/ctl` | `{action: reload\|pause\|resume\|stop\|dump, target}` | `{sent}` |

A job is finished when `pending` is 0. `fleetq` paces each site at its own `rps` fleet-wide, so
several crawls submitted together finish together.

## Machines

| Route | Body | Returns |
|---|---|---|
| `GET /api/status` | | latest SSH snapshot of every host (CPU, memory, disk, OS, worker) |
| `GET /api/refresh` | | poll now, then the snapshot |
| `GET /api/hosts` | | the inventory |
| `GET /api/host/<alias>/processes?limit=40` | | top processes by memory, by CPU, grouped |
| `GET /api/machines` | | inventory + ssh config + roles + install state + known kinds |
| `POST /api/machines` | `{alias, hostname, user, kinds}` | writes ssh config, inventory, roles |
| `GET /api/machines/<alias>/probe` | | ssh reachability and facts |
| `POST /api/machines/<alias>/install` | `{coordinator?}` | starts `deploy/install_worker.sh` in the background |
| `GET /api/machines/<alias>/install` | | install state and log |
| `GET /api/health` | | `{ok, db, auth, time}`; never needs a key |

## Example

```
curl -X POST -H 'Authorization: Bearer KEY' -H 'Content-Type: application/json' \
     -d '{"url": "https://example.com", "limit": 50, "rps": 2}' http://coordinator:8787/api/queue/crawl
curl -H 'Authorization: Bearer KEY' http://coordinator:8787/api/queue/job/12
curl -H 'Authorization: Bearer KEY' -o pages.zip http://coordinator:8787/api/queue/job/12/export
```
