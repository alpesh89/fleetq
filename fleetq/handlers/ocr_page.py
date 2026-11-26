"""ocr_page: one single-page PDF blob -> its text as a document.

payload: {"blob_key": "ocrpage:<doc>:0001", "doc": "<id>", "name": "contract.pdf", "page": 1, "pages": 12}

Renders the page at 300 DPI (or the scan's own resolution when higher), recognises it with the
engine this machine has (Apple Vision on macOS, Tesseract on Linux; see fleetq/ocr.py) and stores
the text at ocr://job<id>/<doc>/page-0001 in `documents`, so the job's Documents tab, the Markdown zip
and the plain-text download all show it. The task result records engine and confidence; a page
under 0.8 is flagged low_confidence so it can be proofread first.
"""

import os

from psycopg.types.json import Jsonb

from fleetq import blobs, ocr
from fleetq.worker import Permanent

KIND = "ocr_page"
VERSION = 1
NEEDS = ["ocr"]
TAG = "ocr"  # a worker earns this tag when SETUP["check"] passes on it
SLOTS = dict(start=2, floor=1, ceiling=max(2, (os.cpu_count() or 2) // 2))
SETUP = {
    "pip": ["pymupdf>=1.24"],
    "macos": ["{home}/.local/bin/uv pip install -q --python {python} 'pyobjc-framework-Vision>=10'"],
    "linux": ["command -v tesseract >/dev/null || sudo -n apt-get install -y tesseract-ocr"],
    "check": ocr.check,
}


async def run(task, ctx):
    p = task.payload
    async with ctx.db.connection() as conn:
        piece = await blobs.get(conn, p["blob_key"])
    if piece is None:
        raise Permanent(f"page blob {p['blob_key']} not found")
    r = await ctx.run_cpu(ocr.ocr_pdf_page, piece)
    page, pages = int(p["page"]), int(p.get("pages") or 0)
    url = f"ocr://job{task.job_id}/{p['doc']}/page-{page:04d}"
    title = f"{p.get('name') or 'document'} · page {page}" + (f" of {pages}" if pages else "")
    words = len(r["text"].split())
    async with ctx.db.connection() as conn:
        await conn.execute(
            """insert into documents (url, title, markdown, word_count, parser_version, job_id, links)
               values (%s, %s, %s, %s, %s, %s, %s)
               on conflict (url) do update set title = excluded.title, markdown = excluded.markdown,
                 word_count = excluded.word_count, parser_version = excluded.parser_version,
                 job_id = excluded.job_id, links = excluded.links, updated_at = now()""",
            (url, title, r["text"], words, VERSION, task.job_id, Jsonb([])),
        )
    out = {
        "page": page,
        "words": words,
        "chars": len(r["text"]),
        "lines": r["lines"],
        "confidence": r["confidence"],
        "engine": r["engine"],
        "dpi": r["dpi"],
    }
    if r["confidence"] < 0.8:
        out["low_confidence"] = True
    return out
