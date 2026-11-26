"""Minimal fleetq dashboard API client. Copy this one file into any Python project.

    from client import FleetQ
    fq = FleetQ("http://coordinator:8787", api_key="…")
    job = fq.crawl("https://example.com", limit=200, rps=2)
    fq.wait(job)                       # blocks until every task finished
    for d in fq.documents(job):
        print(d["url"], d["title"], d["word_count"])
    md = fq.document(url)["markdown"]  # one page as Markdown
    fq.export_zip(job, "pages.zip")    # or everything as a zip

Only the standard library is used (urllib). Raises FleetQError on any non-2xx reply.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request


class FleetQError(RuntimeError):
    def __init__(self, status: int, message: str):
        super().__init__(f"HTTP {status}: {message}")
        self.status = status


class FleetQ:
    def __init__(self, base_url: str, api_key: str | None = None, timeout: float = 30):
        self.base = base_url.rstrip("/")
        self.key = api_key
        self.timeout = timeout

    # -- transport -------------------------------------------------------
    def _req(self, method: str, path: str, body: dict | None = None, raw: bool = False):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method)
        req.add_header("Accept", "application/json")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        if self.key:
            req.add_header("Authorization", f"Bearer {self.key}")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                payload = r.read()
        except urllib.error.HTTPError as e:
            try:
                msg = json.loads(e.read().decode()).get("error", e.reason)
            except Exception:  # noqa: BLE001
                msg = str(e.reason)
            raise FleetQError(e.code, msg) from None
        return payload if raw else json.loads(payload or b"{}")

    # -- jobs ------------------------------------------------------------
    def health(self) -> dict:
        return self._req("GET", "/api/health")

    def crawl(
        self,
        url: str,
        limit: int | None = None,
        rps: float = 2.0,
        name: str | None = None,
        max_age_s: float | None = None,
        force: bool = False,
    ) -> int:
        """Submit a site crawl. Returns the job id."""
        body = {"url": url, "limit": limit, "rps": rps, "name": name, "force": force}
        if max_age_s is not None:
            body["max_age_s"] = max_age_s
        return int(self._req("POST", "/api/queue/crawl", body)["job_id"])

    def job(self, job_id: int) -> dict:
        return self._req("GET", f"/api/queue/job/{job_id}")

    def wait(self, job_id: int, poll: float = 3.0, timeout: float | None = None) -> dict:
        """Poll until no task is ready or running. Returns the final job record."""
        t0 = time.monotonic()
        while True:
            j = self.job(job_id)
            if j.get("total", 0) > 0 and j.get("pending", 0) == 0:
                return j
            if timeout is not None and time.monotonic() - t0 > timeout:
                raise TimeoutError(f"job {job_id} still has {j.get('pending')} pending tasks")
            time.sleep(poll)

    def documents(self, job_id: int) -> list[dict]:
        return self._req("GET", f"/api/queue/job/{job_id}/documents")["documents"]

    def document(self, url: str) -> dict:
        return self._req("GET", "/api/queue/document?" + urllib.parse.urlencode({"url": url}))

    def export_zip(self, job_id: int, path: str) -> str:
        with open(path, "wb") as f:
            f.write(self._req("GET", f"/api/queue/job/{job_id}/export", raw=True))
        return path

    def cancel(self, job_id: int) -> dict:
        return self._req("POST", f"/api/queue/job/{job_id}/cancel", {})

    def retry(self, job_id: int) -> dict:
        return self._req("POST", f"/api/queue/job/{job_id}/retry", {})


if __name__ == "__main__":  # quick manual test: python fleetq_client.py http://host:8787 KEY https://site
    import sys

    fq = FleetQ(sys.argv[1], sys.argv[2] or None)
    jid = fq.crawl(sys.argv[3], limit=10)
    print("job", jid, fq.wait(jid)["done"], "tasks done")
    for d in fq.documents(jid):
        print(d["url"], "-", d["title"])
