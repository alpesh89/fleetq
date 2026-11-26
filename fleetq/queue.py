"""All database operations. One place, so the backend can be swapped later."""

from __future__ import annotations

import os
import random
import socket
from importlib import resources
from typing import Any
from urllib.parse import urlsplit

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from . import config


def pace_key(payload: dict) -> tuple[str, float] | None:
    """Tasks that hit a site (payload carries url and rps) are paced per domain at enqueue.
    The name matches the token bucket the handlers take from, so both share one rate_limits row."""
    url, rps = payload.get("url"), payload.get("rps")
    if not url or rps in (None, ""):
        return None
    try:
        rate = float(rps)
    except (TypeError, ValueError):
        return None
    host = urlsplit(str(url)).netloc
    return (f"domain:{host}", rate) if host and rate > 0 else None


def _scoped(job_id, idem_key):
    """Idempotency keys dedupe within a job, so a later job can re-crawl the same URLs."""
    if idem_key is None:
        return None
    return f"job{job_id}:{idem_key}" if job_id is not None else idem_key


class Task:
    __slots__ = ("id", "job_id", "parent_id", "depth", "kind", "version", "payload", "attempts", "max_attempts")

    def __init__(self, row: dict):
        for k in self.__slots__:
            setattr(self, k, row.get(k))

    def __repr__(self):
        return f"Task(#{self.id} {self.kind} v{self.version} attempt {self.attempts}/{self.max_attempts})"


class Queue:
    def __init__(self, dsn: str = config.DSN, min_size: int = 1, max_size: int = 4):
        self.dsn = dsn
        self.pool = AsyncConnectionPool(
            dsn,
            min_size=min_size,
            max_size=max_size,
            open=False,
            kwargs={"row_factory": dict_row, **config.PG_KEEPALIVES},
        )
        # Liveness traffic (register, heartbeat) never queues behind task traffic, so a worker
        # whose task connections are waiting on locks still shows as alive.
        self.ctl_pool = AsyncConnectionPool(
            dsn,
            min_size=1,
            max_size=1,
            open=False,
            kwargs={"row_factory": dict_row, **config.PG_KEEPALIVES},
        )

    async def open(self):
        await self.pool.open()
        await self.ctl_pool.open()
        await self.pool.wait()
        await self.ctl_pool.wait()

    async def close(self):
        await self.ctl_pool.close()
        await self.pool.close()

    # -- schema ------------------------------------------------------------
    async def migrate(self):
        sql = resources.files("fleetq").joinpath("schema.sql").read_text()
        async with self.pool.connection() as conn:
            async with conn.transaction():
                # Workers all migrate on start; serialize them so trigger recreation cannot deadlock.
                await conn.execute("select pg_advisory_xact_lock(7311982)")
                await conn.execute(sql)

    # -- workers -----------------------------------------------------------
    async def register_worker(self, wid: str, tags: list[str], kinds: list[str], slots: dict, pid: int, addr: str | None):
        async with self.ctl_pool.connection() as conn:
            await conn.execute(
                """insert into workers (id, host, pid, tags, kinds, addr, slots, started_at, last_seen)
                   values (%s, %s, %s, %s, %s, %s, %s, now(), now())
                   on conflict (id) do update set host = excluded.host, pid = excluded.pid, tags = excluded.tags,
                     kinds = excluded.kinds, addr = excluded.addr, slots = excluded.slots,
                     started_at = now(), last_seen = now()""",
                (wid, socket.gethostname(), pid, tags, kinds, addr, Jsonb(slots)),
            )

    async def heartbeat(self, wid: str, kinds: list[str], slots: dict, tags: list[str] | None = None, pid: int | None = None):
        """Upsert, so a worker the sweeper dropped while it could not reach the database re-registers itself."""
        async with self.ctl_pool.connection() as conn:
            await conn.execute(
                """insert into workers (id, host, pid, tags, kinds, slots, started_at, last_seen)
                   values (%s, %s, %s, %s, %s, %s, now(), now())
                   on conflict (id) do update set last_seen = now(), kinds = excluded.kinds, slots = excluded.slots""",
                (wid, socket.gethostname(), pid or os.getpid(), tags or [], kinds, Jsonb(slots)),
            )

    async def unregister_worker(self, wid: str):
        async with self.ctl_pool.connection() as conn:
            await conn.execute("delete from workers where id = %s", (wid,))

    # -- claiming ----------------------------------------------------------
    async def claim(self, wid: str, kind: str, tags: list[str], n: int, lease_seconds: int) -> list[Task]:
        if n <= 0:
            return []
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                """with due as (
                     -- Longest-due first, through the (kind, run_after) index: bounded work per claim
                     -- however many paced rows sit behind these.
                     select id, priority from tasks
                     where status = 'ready' and kind = %(kind)s and run_after <= now()
                       and needs <@ %(tags)s::text[]
                     order by run_after, id
                     limit 200),
                   pick as (
                     select t.id from tasks t join due on due.id = t.id
                     where t.status = 'ready'
                       and (cardinality(t.depends_on) = 0 or not exists (
                             select 1 from unnest(t.depends_on) d join tasks t2 on t2.id = d
                             where t2.status <> 'done'))
                     order by due.priority desc, t.id
                     for update of t skip locked
                     limit %(n)s)
                   update tasks t
                      set status = 'running', claimed_by = %(wid)s,
                          lease_until = now() + make_interval(secs => %(lease)s),
                          attempts = attempts + 1, started_at = now()
                     from pick where t.id = pick.id
                   returning t.id, t.job_id, t.parent_id, t.depth, t.kind, t.version, t.payload, t.attempts, t.max_attempts""",
                {"kind": kind, "tags": tags, "n": n, "wid": wid, "lease": lease_seconds},
            )
            return [Task(r) for r in await cur.fetchall()]

    async def extend_lease(self, task_id: int, wid: str, lease_seconds: int) -> bool:
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                """update tasks set lease_until = now() + make_interval(secs => %s)
                   where id = %s and claimed_by = %s and status = 'running'""",
                (lease_seconds, task_id, wid),
            )
            return cur.rowcount == 1

    # -- finishing ---------------------------------------------------------
    async def complete(self, task: Task, wid: str, result: Any, children: list[dict]) -> int:
        """Mark done and insert children atomically. Returns number of children inserted."""
        async with self.pool.connection() as conn:
            async with conn.transaction():
                cur = await conn.execute(
                    """update tasks set status = 'done', result = %s, error = null, finished_at = now(),
                              lease_until = null
                       where id = %s and claimed_by = %s and status = 'running'""",
                    (Jsonb(result), task.id, wid),
                )
                if cur.rowcount != 1:
                    return 0  # lost the lease; another worker owns it now
                return await self._insert_children(conn, task, children)

    async def reserve_pace(self, name: str, rate: float, n: int = 1, not_before: float = 0.0) -> float:
        async with self.pool.connection() as conn:
            return await self._reserve_pace(conn, name, rate, n, not_before)

    async def _reserve_pace(self, conn, name: str, rate: float, n: int, not_before: float = 0.0) -> float:
        """Reserve n request slots on a site's timeline; returns seconds from now until the first one.

        Each site's queued requests are spread out at its rps *when they are enqueued*, so only about
        rps tasks per site become claimable each second. Workers then pick from every site that has
        a slot due instead of parking all their slots on the oldest job while its token bucket
        refills; the bucket stays as the safety net for anything that arrives unpaced.
        """
        burst = max(1.0, rate * 2)
        span = n / rate
        await conn.execute(
            """insert into rate_limits (name, capacity, tokens, refill_per_sec)
               values (%s, %s, %s, %s) on conflict (name) do nothing""",
            (name, burst, burst, rate),
        )
        cur = await conn.execute(
            """update rate_limits
                  set scheduled_until = greatest(coalesce(scheduled_until, now()), now() + make_interval(secs => %s))
                                        + make_interval(secs => %s)
                where name = %s
            returning extract(epoch from (scheduled_until - now())) - %s as base""",
            (max(0.0, float(not_before)), span, name, span),
        )
        row = await cur.fetchone()
        return max(0.0, float(row["base"])) if row and row["base"] is not None else 0.0

    async def _insert_children(self, conn, parent: Task, children: list[dict]) -> int:
        """Insert a finished task's children inside its transaction.

        Lock order is always the job row first, then task rows, and keys that already exist are
        filtered out before inserting. Without the filter, inserting a URL a sibling task is
        finishing right now waits on that sibling's uncommitted row while the sibling waits for
        the job lock: a deadlock Postgres resolved by killing one completion per second under load.
        The job budget counts units of work (counted=True), not derived follow-ups such as
        parse-after-fetch (counted=False), so a page budget means pages.
        """
        if not children:
            return 0
        rows, seen = [], set()
        for c in children:
            key = _scoped(parent.job_id, c.get("idem_key"))
            if key is not None:
                if key in seen:
                    continue
                seen.add(key)
            rows.append((key, c))
        allowed = None
        if parent.job_id is not None:
            cur = await conn.execute("select max_tasks, task_count from jobs where id = %s for update", (parent.job_id,))
            job = await cur.fetchone()
            if job and job["max_tasks"] is not None:
                allowed = max(0, job["max_tasks"] - job["task_count"])
        if seen:
            cur = await conn.execute("select idem_key from tasks where idem_key = any(%s)", (list(seen),))
            known = {r["idem_key"] for r in await cur.fetchall()}
            rows = [(k, c) for k, c in rows if k not in known]
        kept = []
        for key, c in rows:
            is_counted = bool(c.get("counted", True))
            if is_counted and allowed is not None:
                if allowed <= 0:
                    continue
                allowed -= 1
            kept.append((key, c, is_counted))
        if not kept:
            return 0
        # Spread each site's requests along its own timeline (rate_limits rows locked in name order).
        offsets = [0.0] * len(kept)
        paced: dict[str, tuple[float, list[int]]] = {}
        for i, (_, c, _) in enumerate(kept):
            # Only tasks that talk to the site are paced; a parse that merely carries the url along
            # (and the rps for the renders it may spawn) runs as soon as a CPU is free.
            needs = c.get("needs") or []
            pk = pace_key(c.get("payload", {})) if ("net" in needs or "browser" in needs) else None
            if pk:
                paced.setdefault(pk[0], (pk[1], []))[1].append(i)
        for name in sorted(paced):
            rate, idx = paced[name]
            base = await self._reserve_pace(conn, name, rate, len(idx))
            for j, i in enumerate(idx):
                offsets[i] = base + j / rate
        params = []
        for (key, c, is_counted), delay in zip(kept, offsets, strict=True):
            params.append(
                (
                    parent.job_id,
                    parent.id,
                    parent.depth + (1 if is_counted else 0),
                    c["kind"],
                    c.get("version", 1),
                    Jsonb(c.get("payload", {})),
                    key,
                    c.get("needs", []),
                    c.get("priority", 0),
                    c.get("depends_on", []),
                    c.get("max_attempts", 5),
                    delay,
                    is_counted,
                )
            )
        inserted, counted = 0, 0
        async with conn.cursor() as cur:
            # One pipelined round trip for the whole batch; RETURNING tells us which rows were new.
            await cur.executemany(
                """insert into tasks (job_id, parent_id, depth, kind, version, payload, idem_key, needs, priority,
                                      depends_on, max_attempts, run_after)
                   values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now() + make_interval(secs => %s))
                   on conflict (idem_key) do nothing
                   returning %s::boolean as counted""",
                params,
                returning=True,
            )
            while True:
                row = await cur.fetchone()
                if row:
                    inserted += 1
                    counted += 1 if row["counted"] else 0
                if not cur.nextset():
                    break
        if parent.job_id is not None and counted:
            await conn.execute("update jobs set task_count = task_count + %s where id = %s", (counted, parent.job_id))
        return inserted

    async def fail(self, task: Task, wid: str, error: str, permanent: bool = False):
        if permanent or task.attempts >= task.max_attempts:
            status, delay = ("failed" if permanent else "dead"), 0
        else:
            status, delay = "ready", min(300, 2**task.attempts) + random.random() * 2
        async with self.pool.connection() as conn:
            await conn.execute(
                """update tasks set status = %s, error = %s, claimed_by = null, lease_until = null,
                          run_after = now() + make_interval(secs => %s),
                          finished_at = case when %s in ('failed','dead') then now() else finished_at end
                   where id = %s and claimed_by = %s and status = 'running'""",
                (status, error[:2000], delay, status, task.id, wid),
            )

    async def defer(self, task: Task, wid: str, seconds: float, patch: dict | None = None):
        """Release a task without counting the attempt (rate limit, not a failure).
        `patch` is merged into the payload so the next attempt can see e.g. how often it was blocked."""
        async with self.pool.connection() as conn:
            await conn.execute(
                """update tasks set status = 'ready', claimed_by = null, lease_until = null,
                          attempts = greatest(0, attempts - 1),
                          run_after = now() + make_interval(secs => %s),
                          payload = payload || %s
                   where id = %s and claimed_by = %s and status = 'running'""",
                (seconds, Jsonb(patch or {}), task.id, wid),
            )

    async def sweep(self) -> int:
        """Re-ready tasks whose worker died. Any worker may run this."""
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                """update tasks
                      set status = case when attempts >= max_attempts then 'dead' else 'ready' end,
                          error = coalesce(error, '') || ' [lease expired on ' || coalesce(claimed_by, '?') || ']',
                          claimed_by = null, lease_until = null
                    where status = 'running' and lease_until < now()"""
            )
            swept = cur.rowcount
            await conn.execute("delete from workers where last_seen < now() - interval '2 minutes'")
            return swept

    # -- rate limits (shared token bucket) ---------------------------------
    async def take_token(self, name: str, rate: float, burst: float) -> tuple[bool, float, int, float]:
        """Try to take one token. Returns (ok, seconds_until_next_token, fail_streak, seconds_down)."""
        async with self.pool.connection() as conn:
            async with conn.transaction():
                await conn.execute(
                    """insert into rate_limits (name, capacity, tokens, refill_per_sec)
                       values (%s, %s, %s, %s) on conflict (name) do nothing""",
                    (name, burst, burst, rate),
                )
                cur = await conn.execute(
                    """update rate_limits
                          set tokens = least(capacity, tokens + extract(epoch from (now() - updated_at)) * refill_per_sec) - 1,
                              updated_at = now()
                        where name = %s
                          and least(capacity, tokens + extract(epoch from (now() - updated_at)) * refill_per_sec) >= 1
                       returning tokens, fail_streak, extract(epoch from (now() - down_since)) as down""",
                    (name,),
                )
                row = await cur.fetchone()
                if row:
                    return True, 0.0, int(row["fail_streak"]), float(row["down"] or 0.0)
                cur = await conn.execute(
                    """select greatest(0, (1 - (tokens + extract(epoch from (now() - updated_at)) * refill_per_sec))
                                           / nullif(refill_per_sec, 0)) as wait,
                              fail_streak, extract(epoch from (now() - down_since)) as down
                       from rate_limits where name = %s""",
                    (name,),
                )
                row = await cur.fetchone()
                if not row:
                    return False, 1.0, 0, 0.0
                return (
                    False,
                    float(row["wait"] if row["wait"] is not None else 1.0),
                    int(row["fail_streak"]),
                    float(row["down"] or 0.0),
                )

    async def site_failed(self, name: str, rate: float, burst: float, cap: float) -> int:
        """One more consecutive failed request to a site: lengthen its streak and debit its bucket by
        5 s per failure in the streak (never deeper than `cap` seconds), so every machine backs off
        from it together. Returns the streak length."""
        async with self.pool.connection() as conn:
            async with conn.transaction():
                await conn.execute(
                    """insert into rate_limits (name, capacity, tokens, refill_per_sec)
                       values (%s, %s, %s, %s) on conflict (name) do nothing""",
                    (name, burst, burst, rate),
                )
                cur = await conn.execute(
                    """update rate_limits
                          set fail_streak = fail_streak + 1,
                              down_since = coalesce(down_since, now()),
                              tokens = greatest(-%(cap)s * refill_per_sec,
                                                least(capacity, tokens + extract(epoch from (now() - updated_at)) * refill_per_sec)
                                                - least(%(cap)s, 5 * (fail_streak + 1)) * refill_per_sec),
                              updated_at = now()
                        where name = %(name)s
                       returning fail_streak""",
                    {"cap": cap, "name": name},
                )
                row = await cur.fetchone()
                return int(row["fail_streak"]) if row else 1

    async def site_ok(self, name: str):
        async with self.pool.connection() as conn:
            # A site that answers again resumes at its normal rps at once: the debt its failures built is forgiven.
            await conn.execute(
                """update rate_limits set fail_streak = 0, down_since = null, tokens = greatest(tokens, 0)
                                   where name = %s and fail_streak > 0""",
                (name,),
            )

    # -- submission and control --------------------------------------------
    async def create_job(self, name: str, max_tasks: int | None) -> int:
        async with self.pool.connection() as conn:
            cur = await conn.execute("insert into jobs (name, max_tasks) values (%s, %s) returning id", (name, max_tasks))
            return (await cur.fetchone())["id"]

    async def submit(
        self,
        kind: str,
        payload: dict,
        *,
        job_id: int | None = None,
        idem_key: str | None = None,
        needs: list[str] | None = None,
        priority: int = 0,
        depends_on: list[int] | None = None,
        max_attempts: int = 5,
        version: int = 1,
        counted: bool = True,
    ) -> int | None:
        async with self.pool.connection() as conn:
            async with conn.transaction():
                if job_id is not None:  # same lock order as _insert_children: job row before task rows
                    await conn.execute("select 1 from jobs where id = %s for update", (job_id,))
                cur = await conn.execute(
                    """insert into tasks (job_id, kind, version, payload, idem_key, needs, priority, depends_on, max_attempts)
                       values (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                       on conflict (idem_key) do nothing returning id""",
                    (
                        job_id,
                        kind,
                        version,
                        Jsonb(payload),
                        _scoped(job_id, idem_key),
                        needs or [],
                        priority,
                        depends_on or [],
                        max_attempts,
                    ),
                )
                row = await cur.fetchone()
                if row and job_id is not None and counted:
                    await conn.execute("update jobs set task_count = task_count + 1 where id = %s", (job_id,))
                return row["id"] if row else None

    async def publish(self, topic: str, body: dict | None = None):
        async with self.pool.connection() as conn:
            await conn.execute("insert into events (topic, body) values (%s, %s)", (topic, Jsonb(body or {})))

    async def retry(self, status: str = "dead", kind: str | None = None, job_id: int | None = None) -> int:
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                """update tasks set status = 'ready', attempts = 0, error = null, run_after = now(), finished_at = null
                   where status = %s and (%s::text is null or kind = %s) and (%s::bigint is null or job_id = %s)""",
                (status, kind, kind, job_id, job_id),
            )
            return cur.rowcount

    async def cancel_job(self, job_id: int) -> int:
        async with self.pool.connection() as conn:
            async with conn.transaction():
                cur = await conn.execute(
                    "update tasks set status = 'cancelled', finished_at = now() where job_id = %s and status in ('ready','running')",
                    (job_id,),
                )
                n = cur.rowcount
                # The cancelled requests no longer occupy their sites' timelines.
                await conn.execute(
                    """update rate_limits set scheduled_until = now()
                        where scheduled_until > now() and name in (
                          select distinct 'domain:' || substring(payload->>'url' from '^[A-Za-z][A-Za-z0-9+.-]*://([^/?#]+)')
                            from tasks where job_id = %s and status = 'cancelled' and payload ? 'rps')""",
                    (job_id,),
                )
                return n

    # -- observation -------------------------------------------------------
    async def ready_counts(self) -> dict[str, int]:
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                "select kind, count(*) as n from tasks where status = 'ready' and run_after <= now() group by kind"
            )
            return {r["kind"]: r["n"] for r in await cur.fetchall()}

    async def status(self) -> dict:
        async with self.pool.connection() as conn:
            cur = await conn.execute("select kind, status, count(*) as n from tasks group by 1, 2 order by 1, 2")
            counts = await cur.fetchall()
            cur = await conn.execute(
                """select kind, count(*) as n,
                          round(avg(extract(epoch from (finished_at - started_at)))::numeric, 2) as avg_seconds
                   from tasks where status = 'done' and finished_at > now() - interval '1 minute' group by kind"""
            )
            rate = await cur.fetchall()
            cur = await conn.execute(
                """select id, host, tags, kinds, slots, started_at, last_seen,
                          (now() - last_seen) < interval '30 seconds' as alive
                   from workers order by id"""
            )
            workers = await cur.fetchall()
            cur = await conn.execute(
                """select j.id, j.name, j.max_tasks, j.task_count,
                          count(t.id) filter (where t.status = 'done') as done,
                          count(t.id) filter (where t.status in ('ready','running')) as pending,
                          count(t.id) filter (where t.status in ('dead','failed')) as failed
                   from jobs j left join tasks t on t.job_id = j.id
                   group by j.id order by j.id desc limit 10"""
            )
            jobs = await cur.fetchall()
        return {"counts": counts, "per_minute": rate, "workers": workers, "jobs": jobs}

    async def tasks(self, kind: str | None, status: str | None, job_id: int | None, limit: int) -> list[dict]:
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                """select id, job_id, kind, status, attempts, claimed_by, priority, depth, payload, result, error,
                          created_at, finished_at
                   from tasks
                   where (%s::text is null or kind = %s) and (%s::text is null or status = %s)
                     and (%s::bigint is null or job_id = %s)
                   order by id desc limit %s""",
                (kind, kind, status, status, job_id, job_id, limit),
            )
            return await cur.fetchall()

    async def documents(self, job_id: int | None) -> list[dict]:
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                """select url, title, markdown, word_count from documents
                   where (%s::bigint is null or job_id = %s) order by id""",
                (job_id, job_id),
            )
            return await cur.fetchall()
