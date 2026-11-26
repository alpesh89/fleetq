"""ocr_split: one uploaded PDF -> one ocr_page task per page.

payload: {"blob_key": "pdf:<sha256>", "doc": "<sha256[:12]>", "name": "contract.pdf"}

The dashboard upload stored the whole PDF in `blobs`. This cuts it into single-page PDFs (the
scan image inside is copied, not re-encoded, so a piece is small and keeps its resolution),
stores each piece as its own blob and spawns an ocr_page task per page, so every worker with an
OCR engine takes pages in parallel.
"""

from fleetq import blobs, ocr
from fleetq.worker import Permanent

KIND = "ocr_split"
VERSION = 1
NEEDS = ["ocr"]
TAG = "ocr"  # a worker earns this tag when SETUP["check"] passes on it
SLOTS = dict(start=1, floor=1, ceiling=2)
SETUP = {"pip": ["pymupdf>=1.24"], "check": ocr.check}


async def run(task, ctx):
    p = task.payload
    key, doc, name = p["blob_key"], p["doc"], p.get("name") or "document"
    async with ctx.db.connection() as conn:
        pdf = await blobs.get(conn, key)
    if pdf is None:
        raise Permanent(f"uploaded file {key} is no longer in the blob store")
    try:
        pieces = await ctx.run_cpu(ocr.split_all, pdf)
    except Exception as e:  # noqa: BLE001
        raise Permanent(f"cannot read the PDF: {type(e).__name__}: {e}")
    if not pieces:
        raise Permanent("the PDF has no pages")
    n = len(pieces)
    async with ctx.db.connection() as conn:
        for i, piece in enumerate(pieces, 1):
            await blobs.put(conn, f"ocrpage:{doc}:{i:04d}", "application/pdf", piece, task.id)
    for i in range(1, n + 1):
        ctx.spawn(
            "ocr_page",
            {"blob_key": f"ocrpage:{doc}:{i:04d}", "doc": doc, "name": name, "page": i, "pages": n},
            idem_key=f"job{task.job_id}:ocr:{doc}:{i}",
            needs=["ocr"],
            priority=5,
        )
    ctx.log.info("ocr %s: %d pages queued", name, n)
    return {"pages": n, "bytes": len(pdf)}
