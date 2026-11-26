"""fleetq dashboard: fleet monitor, queue console and HTTP API in one process.

    fleetq dashboard                       # http://localhost:8787
    fleetq dashboard --port 9000 --interval 15 --bind 127.0.0.1

Machines view: polls every host in inventory.txt (fleet home) over SSH using collector.sh, no
agent needed on the hosts; installs or updates workers over SSH. Queue view: submit crawls, custom
tasks and scanned PDFs, watch progress, read documents, download results.

Endpoints
    GET  /api/health                         liveness (never needs a key)
    GET  /api/status                         latest host snapshot
    GET  /api/refresh                        poll now, then return the snapshot
    GET  /api/hosts                          the inventory
    GET  /api/host/<alias>/processes?limit=40
    GET  /api/machines                       inventory + ssh config + roles + install state
    POST /api/machines                       {alias, hostname, user, kinds}
    GET  /api/machines/<alias>/probe
    POST /api/machines/<alias>/install       run deploy/install_worker.sh in the background
    GET  /api/machines/<alias>/install       install state and log
    GET  /api/queue/overview                 counts, throughput, workers, recent jobs
    GET  /api/queue/jobs?limit=50
    GET  /api/queue/kinds                    kinds some live worker can run
    GET  /api/queue/job/<id>                 progress by kind and worker, timing, errors, running tasks
    GET  /api/queue/job/<id>/tasks?status=&kind=&limit=
    GET  /api/queue/job/<id>/documents
    GET  /api/queue/job/<id>/export          zip of Markdown files
    GET  /api/queue/job/<id>/text            OCR jobs: the whole PDF as plain text
    GET  /api/queue/document?url=
    GET  /api/queue/site?host=               marketing/platform signals for one site
    POST /api/queue/crawl                    {url, limit, rps, name, max_age_s, force}
    POST /api/queue/submit                   {kind, payload, needs, name, max_tasks}
    POST /api/queue/ocr                      raw PDF body (+ X-File-Name)
    POST /api/queue/job/<id>/cancel | retry | delete
    POST /api/queue/ctl                      {action: reload|pause|resume|stop|dump, target}

Access control: --api-key KEY (repeatable) or FLEETQ_API_KEYS=key1,key2. With a key configured,
every /api/* route except /api/health requires `Authorization: Bearer <key>` (or `X-API-Key`, or
`?api_key=` for downloads). Without one the API is open: fine on a trusted LAN, never on the internet.
"""

from __future__ import annotations

import argparse
import hmac
import json
import os
import re
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote

from .. import config
from .. import handlers as handler_registry
from .. import machines as fm
from ..admin import Admin

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(HERE, "static")
COLLECTOR = os.path.join(HERE, "collector.sh")
API_KEYS: set[str] = set()  # filled in main(); empty = open access

SSH_OPTS = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=5", "-o", "StrictHostKeyChecking=accept-new", "-o", "LogLevel=ERROR"]
PS_CMD = "ps -A -o pid=,rss=,pcpu=,pmem=,user=,comm="  # works on macOS and Linux


def collect_host(alias: str) -> dict:
    """SSH into one host, run collector.sh, return a result dict (never raises)."""
    started = time.time()
    base = {"alias": alias, "online": False, "latency_ms": None, "error": None, "data": None}
    try:
        with open(COLLECTOR, "rb") as script:
            proc = subprocess.run(["ssh", *SSH_OPTS, alias, "bash -s"], stdin=script, capture_output=True, timeout=25)
        base["latency_ms"] = int((time.time() - started) * 1000)
        if proc.returncode != 0:
            base["error"] = (proc.stderr.decode(errors="replace").strip() or f"ssh exit {proc.returncode}")[:300]
            return base
        base["data"] = json.loads(proc.stdout.decode(errors="replace"))
        base["online"] = True
    except subprocess.TimeoutExpired:
        base["error"] = "timed out"
    except json.JSONDecodeError as e:
        base["error"] = f"bad collector output: {e}"
    except Exception as e:  # noqa: BLE001
        base["error"] = str(e)[:300]
    return base


def collect_processes(alias: str, limit: int = 40) -> dict:
    """Top processes on one host, by memory, by CPU and grouped by name. Never raises."""
    out = {"alias": alias, "ok": False, "error": None, "by_memory": [], "by_cpu": [], "grouped": [], "total_rss": 0, "count": 0}
    try:
        proc = subprocess.run(["ssh", *SSH_OPTS, alias, PS_CMD], capture_output=True, timeout=20)
        if proc.returncode != 0:
            out["error"] = (proc.stderr.decode(errors="replace").strip() or f"ssh exit {proc.returncode}")[:300]
            return out
        rows = []
        for line in proc.stdout.decode(errors="replace").splitlines():
            parts = line.split(None, 5)
            if len(parts) < 6:
                continue
            pid, rss, cpu, mem, user, comm = parts
            try:
                rss_b, cpu_f, mem_f = int(float(rss)) * 1024, float(cpu), float(mem)
            except ValueError:
                continue
            name = os.path.basename(comm.strip()) or comm.strip()
            rows.append(
                {"pid": int(pid), "rss": rss_b, "cpu": cpu_f, "mem": mem_f, "user": user, "name": name, "cmd": comm.strip()}
            )
        groups: dict[str, dict] = {}
        for r in rows:
            g = groups.setdefault(r["name"], {"name": r["name"], "count": 0, "rss": 0, "cpu": 0.0, "users": set()})
            g["count"] += 1
            g["rss"] += r["rss"]
            g["cpu"] += r["cpu"]
            g["users"].add(r["user"])
        grouped = sorted(groups.values(), key=lambda g: g["rss"], reverse=True)[:limit]
        for g in grouped:
            g["users"] = sorted(g["users"])
            g["cpu"] = round(g["cpu"], 1)
        out.update(
            ok=True,
            count=len(rows),
            total_rss=sum(r["rss"] for r in rows),
            by_memory=sorted(rows, key=lambda r: r["rss"], reverse=True)[:limit],
            by_cpu=sorted(rows, key=lambda r: r["cpu"], reverse=True)[:limit],
            grouped=grouped,
            collected_at=time.time(),
        )
    except subprocess.TimeoutExpired:
        out["error"] = "timed out"
    except Exception as e:  # noqa: BLE001
        out["error"] = str(e)[:300]
    return out


class Poller:
    """Polls every host in the inventory every `interval` seconds on a background thread."""

    def __init__(self, interval: int):
        self.interval = interval
        self.lock = threading.Lock()
        self.snapshot = {"hosts": [], "updated_at": None, "interval": interval}
        self._wake = threading.Event()
        threading.Thread(target=self._loop, daemon=True, name="poller").start()

    def _loop(self):
        while True:
            self.poll()
            self._wake.wait(self.interval)
            self._wake.clear()

    def poll(self):
        hosts = fm.read_inventory()
        if hosts:
            with ThreadPoolExecutor(max_workers=len(hosts)) as pool:
                results = list(pool.map(collect_host, hosts))
        else:
            results = []
        with self.lock:
            self.snapshot = {"hosts": results, "updated_at": time.time(), "interval": self.interval}
        online = sum(1 for r in results if r["online"])
        print(f"[{time.strftime('%H:%M:%S')}] polled {len(results)} hosts, {online} online", flush=True)

    def refresh_now(self):
        self.poll()
        self._wake.set()

    def refresh_soon(self):
        self._wake.set()

    def get(self) -> dict:
        with self.lock:
            return self.snapshot


class Handler(BaseHTTPRequestHandler):
    poller: Poller
    admin: Admin | None = None  # set at startup when the database is reachable
    admin_error: str | None = None
    kinds: list[str] = []  # handler kinds this checkout knows, for the Add machine form

    def log_message(self, fmt, *args):  # quieter logs: page loads only
        if "/api/" not in (args[0] if args else ""):
            super().log_message(fmt, *args)

    # -- helpers ------------------------------------------------------------
    def _send(self, code, body: bytes, ctype: str, extra: dict | None = None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, default=str).encode(), "application/json")

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b""
        try:
            return json.loads(raw.decode() or "{}")
        except json.JSONDecodeError:
            return {}

    def _authorized(self, q: dict) -> bool:
        if not API_KEYS:
            return True
        auth = self.headers.get("Authorization", "")
        key = (
            auth[7:].strip() if auth.lower().startswith("bearer ") else (self.headers.get("X-API-Key") or q.get("api_key") or "")
        )
        return any(hmac.compare_digest(key, k) for k in API_KEYS)

    def _guard(self, path: str, q: dict) -> bool:
        """True when the request may proceed; otherwise a 401 has been sent."""
        if not path.startswith("/api/") or path == "/api/health" or self._authorized(q):
            return True
        self._send(
            401,
            json.dumps({"error": "api key required", "hint": "send Authorization: Bearer <key>"}).encode(),
            "application/json",
            {"WWW-Authenticate": 'Bearer realm="fleetq"'},
        )
        return False

    def _need_admin(self) -> bool:
        if self.admin is None:
            self._json(
                {
                    "error": "queue database not available",
                    "detail": self.admin_error,
                    "hint": "check FLEETQ_DSN / FLEETQ_COORDINATOR and that Postgres is up",
                },
                503,
            )
            return False
        return True

    # -- GET ----------------------------------------------------------------
    def do_GET(self):
        path, _, qs = self.path.partition("?")
        q = {k: v[0] for k, v in parse_qs(qs).items()}
        try:
            if path in ("/", "/index.html"):
                with open(os.path.join(STATIC, "index.html"), "rb") as f:
                    return self._send(200, f.read(), "text/html; charset=utf-8")
            if path == "/api/health":
                return self._json({"ok": True, "db": self.admin is not None, "auth": bool(API_KEYS), "time": int(time.time())})
            if not self._guard(path, q):
                return
            if path == "/api/status":
                return self._json(self.poller.get())
            if path == "/api/refresh":
                self.poller.refresh_now()
                return self._json(self.poller.get())
            if path == "/api/hosts":
                return self._json({"hosts": fm.read_inventory()})
            if path.startswith("/api/host/") and path.endswith("/processes"):
                alias = unquote(path[len("/api/host/") : -len("/processes")])
                if alias not in fm.read_inventory():
                    return self._json({"error": "unknown host"}, 404)
                try:
                    limit = max(5, min(200, int(q.get("limit", 40))))
                except ValueError:
                    limit = 40
                return self._json(collect_processes(alias, limit))

            if path == "/api/machines":
                return self._json({"machines": fm.list_machines(), "coordinator": fm.coordinator_ip(), "kinds": self.kinds})
            m = re.match(r"^/api/machines/([^/]+)/(probe|install)$", path)
            if m:
                alias = unquote(m.group(1))
                return self._json(fm.probe(alias) if m.group(2) == "probe" else fm.install_status(alias))

            if path.startswith("/api/queue/"):
                if not self._need_admin():
                    return
                a = self.admin
                m = re.match(r"^/api/queue/job/(\d+)/text$", path)
                if m:
                    t = a.job_text(int(m.group(1)))
                    if not t:
                        return self._json({"error": "not found"}, 404)
                    fname = re.sub(r"[^A-Za-z0-9._-]+", "-", t["name"].removeprefix("ocr ")).strip("-") or "document"
                    return self._send(
                        200,
                        t["text"].encode(),
                        "text/plain; charset=utf-8",
                        {"Content-Disposition": f'attachment; filename="{fname}.txt"'},
                    )
                if path == "/api/queue/overview":
                    return self._json(a.overview())
                if path == "/api/queue/jobs":
                    return self._json({"jobs": a.jobs(int(q.get("limit", 50)))})
                if path == "/api/queue/kinds":
                    return self._json({"kinds": a.kinds()})
                if path == "/api/queue/document":
                    d = a.document(q.get("url", ""))
                    return self._json(d or {"error": "not found"}, 200 if d else 404)
                if path == "/api/queue/site":
                    d = a.site(q.get("host", ""))
                    return self._json(d or {"error": "not found"}, 200 if d else 404)
                m = re.match(r"^/api/queue/job/(\d+)(?:/(tasks|documents|export))?$", path)
                if m:
                    jid, sub = int(m.group(1)), m.group(2)
                    if sub is None:
                        j = a.job(jid)
                        return self._json(j or {"error": "not found"}, 200 if j else 404)
                    if sub == "tasks":
                        return self._json(
                            {"tasks": a.job_tasks(jid, q.get("status") or None, q.get("kind") or None, int(q.get("limit", 100)))}
                        )
                    if sub == "documents":
                        return self._json({"documents": a.job_documents(jid)})
                    return self._send(
                        200,
                        a.export_zip(jid),
                        "application/zip",
                        {"Content-Disposition": f'attachment; filename="fleetq-job{jid}.zip"'},
                    )
            self._json({"error": "not found"}, 404)
        except Exception as e:  # noqa: BLE001
            self._json({"error": f"{type(e).__name__}: {e}"}, 500)

    # -- POST ---------------------------------------------------------------
    def _ocr_upload(self, q: dict):
        if not self._need_admin():
            return
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0:
            return self._json({"error": "empty upload"}, 400)
        if n > 500 * 1024 * 1024:
            return self._json({"error": "PDF larger than 500 MB"}, 413)
        data = self.rfile.read(n)
        name = unquote(self.headers.get("X-File-Name") or q.get("name") or "document.pdf")
        try:
            return self._json(self.admin.submit_ocr(name, data))
        except ValueError as e:
            return self._json({"error": str(e)}, 400)

    def do_POST(self):
        path, _, qs = self.path.partition("?")
        q = {k: v[0] for k, v in parse_qs(qs).items()}
        if not self._guard(path, q):
            return
        if path == "/api/queue/ocr":  # raw PDF body, not JSON
            return self._ocr_upload(q)
        body = self._body()
        try:
            if path == "/api/machines":
                kinds = body.get("kinds")
                if isinstance(kinds, str):
                    kinds = [k.strip() for k in kinds.split(",") if k.strip()]
                try:
                    res = fm.add_machine(body.get("alias", ""), body.get("hostname", ""), body.get("user", ""), kinds)
                except ValueError as e:
                    return self._json({"error": str(e)}, 400)
                self.poller.refresh_soon()
                return self._json(res)
            m = re.match(r"^/api/machines/([^/]+)/install$", path)
            if m:
                alias = unquote(m.group(1))
                if alias not in fm.read_inventory():
                    return self._json({"error": "unknown host"}, 404)
                st = fm.start_install(alias, body.get("coordinator") or None)
                return self._json({k: v for k, v in st.items() if k != "log"})

            if path.startswith("/api/queue/"):
                if not self._need_admin():
                    return
                a = self.admin
                if path == "/api/queue/crawl":
                    url = (body.get("url") or "").strip()
                    if not url:
                        return self._json({"error": "url required"}, 400)
                    limit = body.get("limit")
                    limit = int(limit) if limit not in (None, "", 0, "0") else None
                    ma = body.get("max_age_s")
                    return self._json(
                        a.submit_crawl(
                            url,
                            limit,
                            float(body.get("rps") or 2),
                            body.get("name") or None,
                            float(ma) if ma not in (None, "") else None,
                            bool(body.get("force")),
                        )
                    )
                if path == "/api/queue/submit":
                    kind = (body.get("kind") or "").strip()
                    if not kind:
                        return self._json({"error": "kind required"}, 400)
                    payload = body.get("payload") or {}
                    if isinstance(payload, str):
                        payload = json.loads(payload or "{}")
                    needs = body.get("needs") or []
                    if isinstance(needs, str):
                        needs = [n.strip() for n in needs.split(",") if n.strip()]
                    mt = body.get("max_tasks")
                    return self._json(a.submit_task(kind, payload, needs, body.get("name") or None, int(mt) if mt else None))
                if path == "/api/queue/ctl":
                    return self._json(a.ctl(body.get("action", ""), body.get("target")))
                m = re.match(r"^/api/queue/job/(\d+)/(cancel|retry|delete)$", path)
                if m:
                    jid, act = int(m.group(1)), m.group(2)
                    n = getattr(a, act if act != "delete" else "delete_job")(jid)
                    return self._json({"ok": True, "affected": n})
            self._json({"error": "not found"}, 404)
        except Exception as e:  # noqa: BLE001
            self._json({"error": f"{type(e).__name__}: {e}"}, 500)


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="fleetq dashboard", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--port", type=int, default=int(os.environ.get("DASHBOARD_PORT", 8787)))
    ap.add_argument(
        "--bind",
        default=os.environ.get("DASHBOARD_BIND", "0.0.0.0"),
        help="0.0.0.0 = reachable from the LAN, 127.0.0.1 = this machine only",
    )
    ap.add_argument(
        "--interval", type=int, default=int(os.environ.get("DASHBOARD_INTERVAL", 15)), help="seconds between host polls"
    )
    ap.add_argument(
        "--api-key",
        action="append",
        default=[],
        metavar="KEY",
        help="require this key on /api/* (repeatable; also FLEETQ_API_KEYS=key1,key2)",
    )
    ap.add_argument("--handlers", action="append", default=[], help="extra handler directory, for the kinds list (repeatable)")
    args = ap.parse_args(argv)

    for k in args.api_key + os.environ.get("FLEETQ_API_KEYS", "").split(","):
        if k.strip():
            API_KEYS.add(k.strip())
    if API_KEYS:
        print(f"api auth: {len(API_KEYS)} key(s) accepted", flush=True)
    elif args.bind != "127.0.0.1":
        print("api auth: OPEN (no --api-key / FLEETQ_API_KEYS); anyone on the network can submit work", flush=True)

    Handler.kinds = sorted(handler_registry.discover(args.handlers))
    Handler.poller = Poller(args.interval)
    try:
        adm = Admin()
        adm.open()
        Handler.admin = adm
        print(f"queue database: {adm.dsn}", flush=True)
    except Exception as e:  # noqa: BLE001
        Handler.admin_error = str(e)
        print(f"queue database unavailable: {e}", flush=True)
    srv = ThreadingHTTPServer((args.bind, args.port), Handler)
    print(
        f"dashboard: http://localhost:{args.port}  fleet home: {config.HOME}  "
        f"hosts: {', '.join(fm.read_inventory()) or 'none (add inventory.txt or use Add machine)'}",
        flush=True,
    )
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        sys.exit(0)


if __name__ == "__main__":
    main()
