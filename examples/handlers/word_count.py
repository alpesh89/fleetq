"""word_count: an example handler that shows the whole contract in twenty lines.

payload: {"text": "..."}            result: {"words": N, "longest": "..."}

Run it without touching the built-ins:

    fleetq worker --handlers examples/handlers
    fleetq submit word_count --payload '{"text": "the quick brown fox"}'
    fleetq tasks --kind word_count

See docs/handlers.md for everything `ctx` offers (HTTP client, database pool, rate limits,
fan-out, CPU pool).
"""

from fleetq.worker import Permanent

KIND = "word_count"
VERSION = 1
NEEDS = ["cpu"]  # any worker with the cpu tag (every worker has it)
SLOTS = dict(start=4, floor=1, ceiling=16)  # concurrent tasks of this kind per worker


def _count(text: str) -> dict:
    words = text.split()
    return {"words": len(words), "longest": max(words, key=len) if words else ""}


async def run(task, ctx):
    text = task.payload.get("text")
    if not isinstance(text, str):
        raise Permanent("payload.text must be a string")  # never retried
    ctx.log.info("counting %d characters", len(text))
    return await ctx.run_cpu(_count, text)  # CPU work goes to the process pool
