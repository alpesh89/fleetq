"""The worker: one program on every machine. Task kinds are plug-in handlers."""

from __future__ import annotations

import asyncio
import io
import logging
import os
import platform
import signal
import socket
import time
import traceback
import urllib.robotparser
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from urllib.parse import urlsplit

import httpx
import psycopg

from . import __version__, config, resources
from . import handlers as handler_registry
from .adaptive import SlotController
from .queue import Queue, Task

log = logging.getLogger("fleetq")

USER_AGENT = os.environ.get("FLEETQ_USER_AGENT") or f"fleetq/{__version__} (+https://github.com/alpesh89/fleetq; polite crawler)"


class Defer(Exception):
    """Release the task without counting an attempt; retry after `seconds`.

    `throttled=True` means the remote side pushed back (HTTP 429, captcha, ...), which the
    slot controller treats as a reason to run fewer tasks. Our own pacing (the fleet-wide
    rate limit) is not a health signal and must leave throttled=False."""

    def __init__(self, seconds: float, why: str = "deferred", throttled: bool = False, patch: dict | None = None):
        super().__init__(why)
        self.seconds = max(0.5, float(seconds))
        self.throttled = throttled
        self.patch = patch  # merged into the task payload, so the next attempt remembers (e.g. a block count)


class Permanent(Exception):
    """The task can never succeed; mark it failed without retrying."""


class Context:
    """What a handler gets besides the task."""

    def __init__(self, worker: Worker, task: Task):
        self._w = worker
        self.task = task
        self.children: list[dict] = []
        self.dropped = 0
        self.http: httpx.AsyncClient = worker.http
        self.db = worker.q.pool
        self.log = logging.getLogger(f"fleetq.{task.kind}")
        self.rate_wait = 0.0  # seconds spent waiting for a token: pacing, not the site's latency
        self.site_streak = 0  # consecutive failures of the site this task last took a token for

    def spawn(
        self,
        kind: str,
        payload: dict,
        *,
        idem_key: str | None = None,
        needs: list[str] | None = None,
        priority: int = 0,
        depends_on: list[int] | None = None,
        max_attempts: int = 5,
        version: int = 1,
        counted: bool = True,
    ):
        # Depth counts page hops only: derived follow-ups (counted=False) stay at the parent's depth.
        if counted and self.task.depth + 1 > config.MAX_DEPTH:
            self.dropped += 1  # the task itself still succeeds; we just stop fanning out
            return
        self.children.append(
            {
                "kind": kind,
                "payload": payload,
                "idem_key": idem_key,
                "needs": needs or [],
                "priority": priority,
                "depends_on": depends_on or [],
                "max_attempts": max_attempts,
                "version": version,
                "counted": counted,
            }
        )

    async def rate_limit(
        self, name: str, per_second: float, burst: float | None = None, max_wait: float = 1.0, give_up_after: int | None = None
    ):
        """Take one token from a bucket shared by every machine. Defers the task if the wait is long.

        Requests are normally spread out at enqueue time (see Queue._reserve_pace), so a token is
        usually there already; a wait past max_wait hands the slot back instead of parking on it,
        and the task is re-spaced on the site's timeline (after the wait) so a backlog that has
        piled up behind a slow or failing site drains at the site's rps instead of every slot in the
        fleet polling it. With give_up_after, a task that finds the site failed that many times in a
        row for config.SITE_DOWN_SECONDS fails permanently; the bucket still lets one probe through
        every config.SITE_PENALTY_CAP seconds, and a success clears the streak.
        """
        burst = burst if burst is not None else max(1.0, per_second * 2)
        waited = 0.0
        while True:
            ok, wait, streak, down = await self._w.q.take_token(name, per_second, burst)
            self.site_streak = streak
            if ok:
                return
            if wait > max_wait or waited + wait > max_wait:
                if give_up_after and streak >= give_up_after and down >= config.SITE_DOWN_SECONDS:
                    raise Permanent(f"site down: {streak} requests in a row failed over {down / 60:.0f} min")
                offset = await self._w.q.reserve_pace(name, per_second, 1, not_before=wait)
                raise Defer(max(wait, offset), f"rate limit {name}" + (f", {streak} failures in a row" if streak else ""))
            await asyncio.sleep(wait)
            waited += wait
            self.rate_wait += wait

    async def site_failed(self, name: str, per_second: float, burst: float | None = None) -> int:
        """Record a failed request to a site (timeout, 5xx, 429, bot wall); see Queue.site_failed."""
        burst = burst if burst is not None else max(1.0, per_second * 2)
        self.site_streak = await self._w.q.site_failed(name, per_second, burst, config.SITE_PENALTY_CAP)
        return self.site_streak

    async def site_ok(self, name: str):
        """A request to the site got a real answer: clear its failure streak (no query when there is none)."""
        if self.site_streak:
            self.site_streak = 0
            await self._w.q.site_ok(name)

    async def server_of(self, host: str) -> str | None:
        """The address a site resolves to (cached per worker), or None if it does not resolve."""
        if host not in self._w.addrs:
            try:
                infos = await asyncio.get_running_loop().getaddrinfo(host.split(":")[0], None, type=socket.SOCK_STREAM)
                self._w.addrs[host] = infos[0][4][0] if infos else None
            except (socket.gaierror, OSError, UnicodeError):
                self._w.addrs[host] = None
        return self._w.addrs[host]

    async def pace_site(self, host: str, rps: float, give_up_after: int | None = None):
        """Take the site's token and then its server's: a site never exceeds its own rps, and sites
        that share a server never exceed config.SERVER_RPS between them."""
        await self.rate_limit(f"domain:{host}", rps, give_up_after=give_up_after)
        addr = await self.server_of(host)
        if addr:
            await self.rate_limit(f"server:{addr}", config.SERVER_RPS)

    async def run_cpu(self, fn, *args):
        loop = asyncio.get_running_loop()
        pool = self._w.cpu_pool
        try:
            return await loop.run_in_executor(pool, fn, *args)
        except BrokenProcessPool:
            # A child died (killed for memory, or a crash in a C extension). The pool never recovers on its
            # own and every later parse on this worker would fail, so start a fresh one and run once more.
            if self._w.cpu_pool is pool:
                log.warning("cpu pool broken (a child process died); starting a new one")
                self._w.cpu_pool = self._w.new_cpu_pool()
                pool.shutdown(wait=False)
            return await loop.run_in_executor(self._w.cpu_pool, fn, *args)

    async def robots_allowed(self, url: str) -> bool:
        parts = urlsplit(url)
        base = f"{parts.scheme}://{parts.netloc}"
        rp = self._w.robots.get(base)
        if rp is None:
            rp = urllib.robotparser.RobotFileParser()
            try:
                r = await self.http.get(base + "/robots.txt", timeout=10)
                rp.parse(r.text.splitlines() if r.status_code == 200 else [])
            except httpx.HTTPError:
                rp.parse([])
            self._w.robots[base] = rp
        return rp.can_fetch(USER_AGENT, url)


def default_tags(handler_dirs: list[str] | None = None) -> list[str]:
    """What this machine is: net, cpu, its architecture and OS, plus every capability tag a
    handler declares (`TAG = "browser"`) and whose setup check passes here."""
    arch = platform.machine().lower()
    arch = "arm64" if arch in ("arm64", "aarch64") else "x86_64" if arch in ("x86_64", "amd64") else arch
    osname = {"Darwin": "macos", "Linux": "linux"}.get(platform.system(), platform.system().lower())
    tags = ["net", "cpu", arch, osname]
    for t in handler_registry.capability_tags(handler_registry.discover(handler_dirs)):
        if t not in tags:
            tags.append(t)
    return tags


class Worker:
    def __init__(self, wid: str, tags: list[str], handler_dirs: list[str], dsn: str = config.DSN):
        self.id = wid
        self.tags = tags
        self.handler_dirs = handler_dirs
        self.code_hash = "unknown"
        self.refresh_code_hash()
        self.q = Queue(dsn, min_size=1, max_size=config.POOL_SIZE)
        self.handlers: dict[str, object] = {}
        self.controllers: dict[str, SlotController] = {}
        self.wake = asyncio.Event()
        self.stopping = False
        self.paused = False
        self.inflight: set[asyncio.Task] = set()
        self._started: dict[asyncio.Task, float] = {}
        self.robots: dict[str, urllib.robotparser.RobotFileParser] = {}
        self.addrs: dict[str, str | None] = {}  # host -> resolved address, for the per-server bucket
        self.http = httpx.AsyncClient(
            headers={"User-Agent": USER_AGENT},
            timeout=httpx.Timeout(30.0, connect=10.0),  # slow sites take >15 s per page
            follow_redirects=True,
            limits=httpx.Limits(max_connections=200, max_keepalive_connections=50),
        )
        self.cpu_pool = self.new_cpu_pool()
        self.pressure: str | None = None  # set by the host guard when CPU or memory is short
        self.res: dict = {}
        try:
            if config.NICE:
                os.nice(config.NICE)  # children (process pool, browsers) inherit it
        except OSError:
            pass

    # -- handlers ------------------------------------------------------------
    @staticmethod
    def new_cpu_pool() -> ProcessPoolExecutor:
        return ProcessPoolExecutor(max_workers=max(1, (os.cpu_count() or 2) // 2))

    def load_handlers(self) -> list[str]:
        found: dict[str, object] = {}
        for kind, mod in handler_registry.discover(self.handler_dirs, reload=True).items():
            needs = set(getattr(mod, "NEEDS", []))
            if not needs <= set(self.tags):
                log.info("handler %s needs %s, this worker has %s; skipping", kind, sorted(needs), self.tags)
                continue
            found[kind] = mod
        self.handlers = found
        for kind, mod in found.items():
            if kind not in self.controllers:
                s = dict(getattr(mod, "SLOTS", {}))
                self.controllers[kind] = SlotController(kind, **s)
        for kind in list(self.controllers):
            if kind not in found:
                del self.controllers[kind]
        return sorted(found)

    def slots_snapshot(self) -> dict:
        snap = {k: c.snapshot() for k, c in self.controllers.items()}
        snap["_host"] = {**self.res, "pressure": self.pressure, "max_inflight": config.MAX_INFLIGHT or None}
        snap["_code"] = {"hash": self.code_hash, "python": platform.python_version()}
        return snap

    def refresh_code_hash(self):
        from fleetq.version import code_hash

        try:
            self.code_hash = code_hash(self.handler_dirs)
        except Exception as e:  # noqa: BLE001
            log.debug("code hash failed: %s", e)
            self.code_hash = "unknown"

    @property
    def total_inflight(self) -> int:
        return sum(c.inflight for c in self.controllers.values())

    def host_pressure(self) -> str | None:
        """Why this host should not take more work right now, or None."""
        r = self.res
        if r.get("load", 0) > config.MAX_LOAD:
            return f"load {r['load']:.2f} > {config.MAX_LOAD}"
        if r.get("cpu_busy") is not None and r["cpu_busy"] > config.MAX_CPU_BUSY:
            return f"cpu {r['cpu_busy']:.0%} busy"
        avail = r.get("mem_avail_gb")
        if avail is not None:
            total = r.get("mem_total_gb") or 0
            floor_gb = config.MIN_FREE_MEM_BYTES / 1e9
            if total:
                floor_gb = min(floor_gb, total * config.MIN_FREE_MEM_FRACTION)
            if avail < floor_gb:
                return f"free memory {avail:.1f} GB < {floor_gb:.1f} GB floor"
        return None

    # -- lifecycle -----------------------------------------------------------
    async def run(self):
        await self.q.open()
        await self.q.migrate()
        kinds = self.load_handlers()
        log.info("worker %s on %s tags=%s kinds=%s", self.id, socket.gethostname(), self.tags, kinds)
        await self.q.register_worker(self.id, self.tags, kinds, self.slots_snapshot(), os.getpid(), None)

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, self._request_stop)

        bg = [asyncio.create_task(c) for c in (self._listen(), self._heartbeat(), self._sweeper(), self._controller())]
        try:
            await self._main_loop()
        finally:
            for t in bg:
                t.cancel()
            if self.inflight:
                log.info("draining %d running task(s), up to 30s", len(self.inflight))
                await asyncio.wait(self.inflight, timeout=30)
            await self.q.unregister_worker(self.id)
            await self.http.aclose()
            self.cpu_pool.shutdown(wait=False, cancel_futures=True)
            await self.q.close()
            log.info("worker %s stopped", self.id)

    def _request_stop(self):
        if not self.stopping:
            log.info("stop requested; finishing in-flight tasks")
        self.stopping = True
        self.wake.set()

    async def _main_loop(self):
        while not self.stopping:
            claimed = 0
            if not self.paused and not self.pressure:
                for kind, ctl in list(self.controllers.items()):
                    n = min(ctl.free, config.CLAIM_BATCH)
                    if config.MAX_INFLIGHT:
                        n = min(n, config.MAX_INFLIGHT - self.total_inflight)
                    if n <= 0:
                        continue
                    try:
                        tasks = await self.q.claim(self.id, kind, self.tags, n, config.LEASE_SECONDS)
                    except psycopg.Error as e:
                        log.warning("claim failed (%s); retrying shortly", e)
                        await asyncio.sleep(2)
                        continue
                    for task in tasks:
                        ctl.inflight += 1
                        t = asyncio.create_task(self._run_task(task, ctl), name=str(task))
                        self.inflight.add(t)
                        self._started[t] = time.monotonic()
                        t.add_done_callback(self.inflight.discard)
                        t.add_done_callback(self._started.pop)
                    claimed += len(tasks)
            if claimed == 0:
                try:
                    await asyncio.wait_for(self.wake.wait(), timeout=config.IDLE_POLL_SECONDS)
                except TimeoutError:
                    pass
                self.wake.clear()

    async def _run_task(self, task: Task, ctl: SlotController):
        ctx = Context(self, task)
        started = time.monotonic()
        lease = asyncio.create_task(self._lease_loop(task))

        def took() -> float:  # what the site made us wait, minus our own pacing
            return max(0.0, time.monotonic() - started - ctx.rate_wait)

        try:
            # A handler that never returns (a query on a dead connection, a drip-feeding site) would otherwise
            # hold its slot and renew its lease forever; past the cap it is abandoned and the task retries.
            result = await asyncio.wait_for(self.handlers[task.kind].run(task, ctx), timeout=config.TASK_TIMEOUT)
            n = await self.q.complete(task, self.id, result, ctx.children)
            ctl.record(True, took())
            log.info(
                "%s done in %.1fs%s%s",
                task,
                time.monotonic() - started,
                f", spawned {n}" if n else "",
                f", dropped {ctx.dropped} past depth limit" if ctx.dropped else "",
            )
        except Defer as d:
            await self.q.defer(task, self.id, d.seconds, d.patch)
            if d.throttled:  # remote push-back counts against us
                ctl.record(True, took(), throttled=True)
            # a rate-limit wait is just pacing: don't record it, or idle waits would read as a slow or throttled site
            log.info("%s deferred %.1fs (%s)", task, d.seconds, d)
        except Permanent as e:
            await self.q.fail(task, self.id, f"permanent: {e}", permanent=True)
            ctl.record(False, took())
            log.warning("%s failed permanently: %s", task, e)
        except Exception as e:
            msg = f"{type(e).__name__}: {e}"
            if isinstance(e, TimeoutError) and time.monotonic() - started >= config.TASK_TIMEOUT:
                msg = f"abandoned after {config.TASK_TIMEOUT:.0f}s: handler never returned"
            await self.q.fail(task, self.id, msg)
            ctl.record(False, took())
            log.warning("%s error: %s", task, msg)
            log.debug(traceback.format_exc())
        finally:
            lease.cancel()
            ctl.inflight -= 1
            self.wake.set()

    async def _lease_loop(self, task: Task):
        while True:
            await asyncio.sleep(config.LEASE_SECONDS / 2)
            try:
                if not await self.q.extend_lease(task.id, self.id, config.LEASE_SECONDS):
                    log.warning("%s: lease lost", task)
            except psycopg.Error as e:
                log.warning("%s: lease extend failed: %s", task, e)

    # -- background loops ----------------------------------------------------
    async def _listen(self):
        while not self.stopping:
            try:
                async with await psycopg.AsyncConnection.connect(self.q.dsn, autocommit=True, **config.PG_KEEPALIVES) as conn:
                    await conn.execute("listen fleetq_tasks")
                    await conn.execute("listen fleetq_control")
                    async for n in conn.notifies():
                        if n.channel == "fleetq_tasks":
                            self.wake.set()
                        else:
                            await self._control(n.payload)
            except (psycopg.Error, OSError) as e:
                log.warning("listener disconnected (%s); reconnecting in 3s", e)
                await asyncio.sleep(3)

    async def _control(self, payload: str):
        topic, _, body = payload.partition(" ")
        if topic == "reload_handlers":
            kinds = self.load_handlers()
            self.refresh_code_hash()
            log.info("handlers reloaded: %s (code %s)", kinds, self.code_hash)
            await self.q.heartbeat(self.id, kinds, self.slots_snapshot(), self.tags)
        elif topic == "pause":
            self.paused = True
            log.info("paused by control event")
        elif topic == "resume":
            self.paused = False
            self.wake.set()
            log.info("resumed by control event")
        elif topic == "dump":  # where every in-flight task is right now, for diagnosing a hang
            for t in list(self.inflight):
                buf = io.StringIO()
                t.print_stack(limit=6, file=buf)
                log.warning("in flight %s (%s):\n%s", t.get_name(), self._task_age(t), buf.getvalue().rstrip())
            log.warning("%d task(s) in flight", len(self.inflight))
        elif topic == "stop" and (self.id in body or '"all"' in body):
            self._request_stop()

    def _task_age(self, t: asyncio.Task) -> str:
        started = self._started.get(t)
        return f"{time.monotonic() - started:.0f}s" if started else "age unknown"

    async def _heartbeat(self):
        while True:
            await asyncio.sleep(config.HEARTBEAT_SECONDS)
            try:
                await self.q.heartbeat(self.id, sorted(self.handlers), self.slots_snapshot(), self.tags)
            except psycopg.Error as e:
                log.warning("heartbeat failed: %s", e)

    async def _sweeper(self):
        while True:
            await asyncio.sleep(config.SWEEP_SECONDS + (hash(self.id) % 7))
            try:
                n = await self.q.sweep()
                if n:
                    log.info("swept %d expired lease(s)", n)
            except psycopg.Error as e:
                log.warning("sweep failed: %s", e)

    async def _controller(self):
        loop = asyncio.get_running_loop()
        while True:
            await asyncio.sleep(config.CONTROL_TICK_SECONDS)
            # Host guard first: readings come from vm_stat/top/proc, so take them off the event loop.
            try:
                self.res = await loop.run_in_executor(None, resources.snapshot)
            except Exception as e:
                log.debug("resource snapshot failed: %s", e)
            was, self.pressure = self.pressure, self.host_pressure()
            if self.pressure and not was:
                log.warning("host under pressure (%s): not claiming new work, cutting slots", self.pressure)
            elif was and not self.pressure:
                log.info("host pressure cleared (was: %s)", was)
            try:
                demand = await self.q.ready_counts()
            except psycopg.Error:
                demand = {}
            load = self.res.get("load", 0.0)
            for kind, ctl in self.controllers.items():
                reason = ctl.tick(load, demand.get(kind, 0), pressure=self.pressure)
                if reason:
                    log.info("slots[%s] -> %d (%s)", kind, ctl.cur, reason)
            self.wake.set()


def main(wid: str | None, tags: list[str] | None, handler_dirs: list[str], dsn: str, verbose: bool):
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    wid = wid or f"{socket.gethostname().split('.')[0]}-{os.getpid()}"
    w = Worker(wid, tags or default_tags(handler_dirs), handler_dirs, dsn)
    asyncio.run(w.run())
