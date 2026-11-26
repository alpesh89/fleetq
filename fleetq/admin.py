"""Synchronous queue admin used by the dashboard (threaded HTTP server, so sync psycopg)."""

from __future__ import annotations

import hashlib
import io
import re
import zipfile
from datetime import datetime
from urllib.parse import urlsplit

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from . import blobs, config, signals


def _ts(v):
    return v.timestamp() if isinstance(v, datetime) else v


def _clean(row):
    return {k: (_ts(v) if isinstance(v, datetime) else v) for k, v in row.items()}


class Admin:
    def __init__(self, dsn: str = config.DSN):
        self.dsn = dsn
        self.pool = ConnectionPool(
            dsn, min_size=1, max_size=4, open=False, kwargs={"row_factory": dict_row, **config.PG_KEEPALIVES}
        )

    def open(self):
        self.pool.open()
        self.pool.wait(timeout=10)

    def _q(self, sql, params=None, one=False):
        with self.pool.connection() as conn:
            cur = conn.execute(sql, params)
            if cur.description is None:
                return cur.rowcount
            rows = [_clean(r) for r in cur.fetchall()]
            return (rows[0] if rows else None) if one else rows

    # -- overview ----------------------------------------------------------
    def overview(self) -> dict:
        counts = self._q("select kind, status, count(*)::int as n from tasks group by 1,2 order by 1,2")
        per_min = self._q("""select kind, count(*)::int as n,
                                    round(avg(extract(epoch from (finished_at - started_at)))::numeric, 2)::float as avg_seconds
                             from tasks where status='done' and finished_at > now() - interval '1 minute' group by kind""")
        workers = self._q("""select id, host, tags, kinds, slots, started_at, last_seen,
                                    (now() - last_seen) < interval '30 seconds' as alive
                             from workers order by id""")
        ready = sum(c["n"] for c in counts if c["status"] == "ready")
        running = sum(c["n"] for c in counts if c["status"] == "running")
        from fleetq.version import code_hash

        return {
            "counts": counts,
            "per_minute": per_min,
            "workers": workers,
            "ready": ready,
            "running": running,
            "coordinator_code": code_hash(),
            "jobs": self.jobs(limit=50),
        }

    # -- jobs ----------------------------------------------------------------
    def jobs(self, limit: int = 50) -> list[dict]:
        return self._q(
            """
            select j.id, j.name, j.max_tasks, j.task_count, j.created_at,
                   count(t.id)::int as total,
                   count(t.id) filter (where t.status='done')::int as done,
                   count(t.id) filter (where t.status in ('ready','running'))::int as pending,
                   count(t.id) filter (where t.status='running')::int as running,
                   count(t.id) filter (where t.status in ('dead','failed'))::int as failed,
                   count(t.id) filter (where t.status='cancelled')::int as cancelled,
                   min(t.started_at) as started_at,
                   case when count(t.id) filter (where t.status in ('ready','running')) = 0 then max(t.finished_at) end as finished_at,
                   count(t.id) filter (where t.kind='fetch' and t.status='done')::int as pages,
                   count(t.id) filter (where t.kind='fetch' and t.status='done' and t.result->>'skipped' is not null)::int as skipped,
                   count(t.id) filter (where t.kind='fetch' and t.status='done' and (t.result->>'changed')::boolean)::int as changed,
                   count(t.id) filter (where t.kind='fetch' and t.status='done' and (t.result->>'new')::boolean)::int as new_pages,
                   (select count(*) from documents d where d.job_id = j.id
                       or d.url in (select payload->>'url' from tasks x where x.job_id = j.id and x.kind = 'fetch'))::int as documents
            from jobs j left join tasks t on t.job_id = j.id
            group by j.id order by j.id desc limit %s""",
            (limit,),
        )

    def job(self, job_id: int) -> dict | None:
        j = self._q(
            """
            select j.*, count(t.id)::int as total,
                   count(t.id) filter (where t.status='done')::int as done,
                   count(t.id) filter (where t.status in ('ready','running'))::int as pending,
                   count(t.id) filter (where t.status='running')::int as running,
                   count(t.id) filter (where t.status in ('dead','failed'))::int as failed,
                   count(t.id) filter (where t.status='cancelled')::int as cancelled,
                   min(t.started_at) as started_at,
                   case when count(t.id) filter (where t.status in ('ready','running')) = 0 then max(t.finished_at) end as finished_at,
                   (select count(*) from documents d where d.job_id = j.id
                       or d.url in (select payload->>'url' from tasks x where x.job_id = j.id and x.kind = 'fetch'))::int as documents
            from jobs j left join tasks t on t.job_id = j.id where j.id = %s group by j.id""",
            (job_id,),
            one=True,
        )
        if not j:
            return None
        j["fetch_outcomes"] = self._q(
            """
            select coalesce(result->>'skipped',
                            case when (result->>'new')::boolean then 'new' when (result->>'changed')::boolean then 'changed'
                                 when status='done' then 'fetched' end) as outcome, count(*)::int as n
            from tasks where job_id = %s and kind = 'fetch' and status = 'done' group by 1 order by 2 desc""",
            (job_id,),
        )
        j["by_kind"] = self._q(
            """
            select kind,
                   count(*)::int as total,
                   count(*) filter (where status='done')::int as done,
                   count(*) filter (where status='ready')::int as ready,
                   count(*) filter (where status='running')::int as running,
                   count(*) filter (where status in ('dead','failed'))::int as failed,
                   count(*) filter (where status='cancelled')::int as cancelled,
                   round(avg(extract(epoch from (finished_at - started_at))) filter (where status='done')::numeric, 2)::float as avg_seconds,
                   round((percentile_cont(0.5) within group (order by extract(epoch from (finished_at - started_at))) filter (where status='done'))::numeric, 2)::float as p50_seconds,
                   round(max(extract(epoch from (finished_at - started_at))) filter (where status='done')::numeric, 2)::float as max_seconds
            from tasks where job_id = %s group by kind order by kind""",
            (job_id,),
        )
        j["by_worker"] = self._q(
            """
            select claimed_by as worker, kind, count(*)::int as n,
                   round(sum(extract(epoch from (finished_at - started_at)))::numeric, 1)::float as busy_seconds
            from tasks where job_id = %s and status='done' and claimed_by is not null
            group by 1,2 order by 1,2""",
            (job_id,),
        )
        j["errors"] = self._q(
            """
            select id, kind, status, attempts, claimed_by, left(error, 300) as error, payload->>'url' as url, finished_at
            from tasks where job_id = %s and (status in ('dead','failed') or error is not null)
            order by id desc limit 30""",
            (job_id,),
        )
        j["sites"] = [
            signals.summary(r)
            for r in self._q(
                """
            select s.* from site_signals s
            where s.host in (select distinct regexp_replace(lower(split_part(payload->>'url', '/', 3)), '^www\\.', '')
                             from tasks where job_id = %s and kind in ('fetch', 'sitemap'))
            order by s.host""",
                (job_id,),
            )
        ]
        j["running_tasks"] = self._q(
            """
            select id, kind, claimed_by, payload->>'url' as url, started_at,
                   extract(epoch from (now() - started_at))::int as running_seconds
            from tasks where job_id = %s and status='running' order by started_at limit 30""",
            (job_id,),
        )
        j["timeline"] = self._q(
            """
            select date_trunc('second', finished_at) as t, count(*)::int as n
            from tasks where job_id = %s and status='done' and finished_at is not null
            group by 1 order by 1""",
            (job_id,),
        )
        return j

    def job_tasks(self, job_id: int, status: str | None, kind: str | None, limit: int = 100) -> list[dict]:
        return self._q(
            """
            select id, kind, status, attempts, claimed_by, priority, depth, payload, result, left(error, 300) as error,
                   created_at, started_at, finished_at,
                   extract(epoch from (coalesce(finished_at, now()) - started_at))::float as seconds
            from tasks where job_id = %s and (%s::text is null or status = %s) and (%s::text is null or kind = %s)
            order by id desc limit %s""",
            (job_id, status, status, kind, kind, limit),
        )

    def job_documents(self, job_id: int) -> list[dict]:
        return self._q(
            """select d.id, d.url, d.title, d.word_count, d.updated_at,
                                 coalesce(jsonb_array_length(d.links), 0) as link_count,
                                 p.first_seen, p.fetched_at, p.changed_at, p.change_count
                          from documents d left join pages p on p.url = d.url
                          where d.job_id = %s or d.url in (select payload->>'url' from tasks where job_id = %s and kind = 'fetch')
                          order by case when d.url like 'ocr://%%' then d.url end, d.id""",
            (job_id, job_id),
        )

    def site(self, host: str) -> dict | None:
        """Marketing/platform signals for one site (host with or without www., or a URL)."""
        h = host.strip().lower()
        if "://" in h:
            h = urlsplit(h).netloc
        h = h.removeprefix("www.").strip("/")
        row = self._q("select * from site_signals where host = %s", (h,), one=True)
        return signals.summary(row) if row else None

    def document(self, url: str) -> dict | None:
        return self._q(
            "select url, title, markdown, word_count, links, parser_version, updated_at from documents where url = %s",
            (url,),
            one=True,
        )

    def export_zip(self, job_id: int) -> bytes:
        docs = self._q(
            """select url, title, markdown from documents where job_id = %s
                          or url in (select payload->>'url' from tasks where job_id = %s and kind = 'fetch') order by id""",
            (job_id, job_id),
        )
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for d in docs:
                if not d["markdown"]:
                    continue
                p = urlsplit(d["url"])
                name = re.sub(r"[^a-zA-Z0-9]+", "-", (p.netloc + p.path).strip("/")).strip("-") or "index"
                z.writestr(
                    f"job{job_id}/{name[:150]}.md", f"# {d['title'] or d['url']}\n\nSource: {d['url']}\n\n{d['markdown']}\n"
                )
        return buf.getvalue()

    def job_text(self, job_id: int) -> dict | None:
        """Every OCR page of a job in order, as one plain-text copy of the PDF."""
        j = self._q("select name from jobs where id = %s", (job_id,), one=True)
        if not j:
            return None
        docs = self._q(
            "select url, markdown, word_count from documents where job_id = %s and url like 'ocr://%%' order by url", (job_id,)
        )
        parts = []
        for d in docs:
            n = int(d["url"].rsplit("page-", 1)[-1])
            parts.append(f"===== Page {n} =====\n\n{(d['markdown'] or '').rstrip()}\n")
        return {"name": j["name"], "pages": len(docs), "words": sum(d["word_count"] or 0 for d in docs), "text": "\n".join(parts)}

    # -- submission and control --------------------------------------------
    def submit_ocr(self, name: str, pdf: bytes) -> dict:
        """Store an uploaded PDF and queue its OCR: one ocr_split task that fans out a page task per page."""
        if not pdf.lstrip()[:5].startswith(b"%PDF"):
            raise ValueError("not a PDF file")
        name = re.sub(r"[\\/]+", "-", (name or "document.pdf").strip())[:200] or "document.pdf"
        sha = hashlib.sha256(pdf).hexdigest()
        doc, key = sha[:12], f"pdf:{sha}"
        body, enc = blobs.pack(pdf)
        with self.pool.connection() as conn:
            with conn.transaction():
                conn.execute(
                    """insert into blobs (key, content_type, data, encoding) values (%s, 'application/pdf', %s, %s)
                                on conflict (key) do update set data = excluded.data, encoding = excluded.encoding, created_at = now()""",
                    (key, body, enc),
                )
                job = conn.execute(
                    "insert into jobs (name, max_tasks) values (%s, null) returning id", (f"ocr {name}",)
                ).fetchone()["id"]
                conn.execute(
                    """insert into tasks (job_id, kind, payload, idem_key, needs, priority)
                                values (%s, 'ocr_split', %s, %s, '{ocr}', 5)""",
                    (job, Jsonb({"blob_key": key, "doc": doc, "name": name, "bytes": len(pdf)}), f"job{job}:ocr_split:{doc}"),
                )
                conn.execute("update jobs set task_count = 1 where id = %s", (job,))
        return {"job_id": job, "doc": doc, "bytes": len(pdf)}

    def submit_crawl(
        self,
        url: str,
        limit: int | None,
        rps: float,
        name: str | None = None,
        max_age_s: float | None = None,
        force: bool = False,
    ) -> dict:
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
        host = urlsplit(url).netloc
        limit = config.crawl_budget(limit)  # no limit given -> default; never above the ceiling
        payload = {"url": url, "limit": limit, "rps": rps, "force": bool(force)}
        if max_age_s is not None:
            payload["max_age_s"] = float(max_age_s)
        with self.pool.connection() as conn:
            with conn.transaction():
                job = conn.execute(
                    "insert into jobs (name, max_tasks) values (%s, %s) returning id", (name or f"crawl {host}", limit)
                ).fetchone()["id"]
                conn.execute(
                    """insert into tasks (job_id, kind, payload, idem_key, needs, priority)
                                values (%s, 'sitemap', %s, %s, '{net}', 10)""",
                    (job, Jsonb(payload), f"job{job}:sitemap:{url}"),
                )
        return {"job_id": job}

    def submit_task(self, kind: str, payload: dict, needs: list[str], name: str | None, max_tasks: int | None) -> dict:
        with self.pool.connection() as conn:
            with conn.transaction():
                job = conn.execute(
                    "insert into jobs (name, max_tasks) values (%s, %s) returning id", (name or kind, max_tasks)
                ).fetchone()["id"]
                tid = conn.execute(
                    """insert into tasks (job_id, kind, payload, needs) values (%s, %s, %s, %s) returning id""",
                    (job, kind, Jsonb(payload), needs),
                ).fetchone()["id"]
                conn.execute("update jobs set task_count = 1 where id = %s", (job,))
        return {"job_id": job, "task_id": tid}

    def cancel(self, job_id: int) -> int:
        return self._q(
            "update tasks set status='cancelled', finished_at=now() where job_id=%s and status in ('ready','running')", (job_id,)
        )

    def retry(self, job_id: int) -> int:
        return self._q(
            """update tasks set status='ready', attempts=0, error=null, run_after=now(), finished_at=null
                          where job_id=%s and status in ('dead','failed','cancelled')""",
            (job_id,),
        )

    def delete_job(self, job_id: int) -> int:
        return self._q("delete from jobs where id=%s", (job_id,))

    def ctl(self, action: str, target: str | None = None):
        topic = {"reload": "reload_handlers"}.get(action, action)
        self._q("insert into events (topic, body) values (%s, %s)", (topic, Jsonb({"target": target or "all"})))
        return {"sent": topic}

    def kinds(self) -> list[str]:
        rows = self._q("select distinct unnest(kinds) as k from workers order by 1")
        return [r["k"] for r in rows]
