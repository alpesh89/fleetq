"""End-to-end smoke test: migrate, start a worker, submit a task to the example handler, wait.

    FLEETQ_DSN=postgresql://fleetq:fleetq@localhost:5433/fleetq python scripts/smoke.py

Exits 0 when the task finished with the expected result, 1 otherwise. Used by CI; safe to run
against a development database (it adds one job and one task).
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
HANDLERS = os.path.join(ROOT, "examples", "handlers")


async def main() -> int:
    from fleetq import config
    from fleetq.queue import Queue

    q = Queue(config.DSN, min_size=1, max_size=2)
    await q.open()
    await q.migrate()
    print("schema ok at", config.DSN)
    worker = subprocess.Popen(
        [sys.executable, "-m", "fleetq.cli", "worker", "--id", "smoke", "--handlers", HANDLERS, "-v"],
        stdout=sys.stdout,
        stderr=subprocess.STDOUT,
    )
    try:
        job = await q.create_job("smoke", None)
        tid = await q.submit("word_count", {"text": "fleetq smoke test passes"}, job_id=job, idem_key=f"smoke:{time.time()}")
        print(f"job {job}: task #{tid} queued")
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            rows = await q.tasks("word_count", None, job, 1)
            if rows and rows[0]["status"] in ("done", "failed", "dead"):
                r = rows[0]
                print("task", r["status"], r["result"], r["error"] or "")
                return 0 if r["status"] == "done" and r["result"] == {"words": 4, "longest": "fleetq"} else 1
            if worker.poll() is not None:
                print("worker exited early with", worker.returncode)
                return 1
            await asyncio.sleep(1)
        print("timed out waiting for the task")
        return 1
    finally:
        worker.terminate()
        try:
            worker.wait(timeout=20)
        except subprocess.TimeoutExpired:
            worker.kill()
        await q.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
