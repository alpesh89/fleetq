"""Blob storage helpers: zstd-compressed page bodies in the `blobs` table.

Raw HTML compresses about 6:1 with zstd (Postgres' built-in pglz manages ~3.5:1), and the
column is marked STORAGE EXTERNAL so Postgres does not spend CPU re-compressing our output.
`encoding` is null for legacy raw rows, so old and new blobs read back transparently.
"""

from __future__ import annotations

import zstandard

LEVEL = 6
ENCODING = "zstd"
MAGIC = b"\x28\xb5\x2f\xfd"  # zstd frame header
_c = zstandard.ZstdCompressor(level=LEVEL)
_d = zstandard.ZstdDecompressor()


def pack(data: bytes) -> tuple[bytes, str | None]:
    """Compress unless it does not pay (tiny or incompressible bodies stay raw)."""
    if len(data) < 256:
        return data, None
    out = _c.compress(data)
    return (out, ENCODING) if len(out) < len(data) else (data, None)


def unpack(data: bytes | memoryview, encoding: str | None) -> bytes:
    data = bytes(data)
    if encoding == ENCODING:
        if not data.startswith(MAGIC):
            return data  # mislabelled raw row (one was found on 2026-11-03); the bytes are still the page
        return _d.decompress(data)
    if encoding and encoding != "raw":
        raise ValueError(f"unknown blob encoding {encoding!r}")
    return data


async def put(conn, key: str, content_type: str | None, data: bytes, task_id: int | None) -> int:
    """Upsert one blob. Returns stored (compressed) size."""
    body, enc = pack(data)
    await conn.execute(
        """insert into blobs (key, content_type, data, encoding, task_id) values (%s, %s, %s, %s, %s)
           on conflict (key) do update set data = excluded.data, encoding = excluded.encoding,
             content_type = excluded.content_type, task_id = excluded.task_id, created_at = now()""",
        (key, content_type, body, enc, task_id),
    )
    return len(body)


async def get(conn, key: str) -> bytes | None:
    cur = await conn.execute("select data, encoding from blobs where key = %s", (key,))
    row = await cur.fetchone()
    if not row:
        return None
    return unpack(row["data"], row["encoding"])
