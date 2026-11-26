"""OCR engines for scanned pages, picked per machine. Everything runs offline.

    macOS  -> Apple Vision (VNRecognizeTextRequest, accurate level). Built into the OS, best
              accuracy on printed Latin text, no install beyond the pyobjc bridge.
    Linux  -> Tesseract 5 (the `tesseract` binary, LSTM engine, English). apt-installed where sudo
              allows; otherwise the worker reports the handler as missing and takes no pages.

`render_page` turns one page of a PDF into a 300 DPI grey PNG; `recognize` returns the page text
(lines in reading order, blank line between paragraphs) and a 0..1 confidence. Both are plain
functions so a handler can run them in the worker's process pool.
"""

from __future__ import annotations

import csv
import io
import os
import platform
import shutil
import statistics
import subprocess
import tempfile

DPI = 300


def engine_name() -> str | None:
    if platform.system() == "Darwin":
        try:
            import Vision  # noqa: F401

            return "vision"
        except ImportError:
            return None
    return "tesseract" if shutil.which("tesseract") else None


def check() -> tuple[bool, str]:
    """For `fleetq setup` / `fleetq doctor`."""
    try:
        import pymupdf
    except ImportError:
        return False, "pymupdf not installed"
    eng = engine_name()
    if eng == "vision":
        return True, f"pymupdf {pymupdf.version[0]} + Apple Vision"
    if eng == "tesseract":
        v = subprocess.run(["tesseract", "--version"], capture_output=True, text=True).stdout.split("\n")[0]
        langs = subprocess.run(["tesseract", "--list-langs"], capture_output=True, text=True).stdout
        if "eng" not in langs:
            return False, f"{v} without the eng language pack"
        return True, f"pymupdf {pymupdf.version[0]} + {v}"
    if platform.system() == "Darwin":
        return False, "pyobjc Vision bridge not installed"
    return False, "tesseract not installed (apt-get install tesseract-ocr)"


# -- page image --------------------------------------------------------------------------------


def page_count(pdf: bytes) -> int:
    import pymupdf

    with pymupdf.open(stream=pdf, filetype="pdf") as d:
        return len(d)


def split_page(pdf: bytes, index: int) -> bytes:
    """A single-page PDF holding page `index` (0-based). The scan's image stream is copied as is,
    so the page keeps its native resolution and the piece stays small."""
    import pymupdf

    with pymupdf.open(stream=pdf, filetype="pdf") as src, pymupdf.open() as out:
        out.insert_pdf(src, from_page=index, to_page=index)
        return out.tobytes(garbage=3, deflate=True)


def split_all(pdf: bytes) -> list[bytes]:
    """Every page as its own single-page PDF, in order (one call, so the PDF crosses into the
    process pool once instead of once per page)."""
    import pymupdf

    out = []
    with pymupdf.open(stream=pdf, filetype="pdf") as src:
        for i in range(len(src)):
            with pymupdf.open() as one:
                one.insert_pdf(src, from_page=i, to_page=i)
                out.append(one.tobytes(garbage=3, deflate=True))
    return out


def render_page(pdf: bytes, index: int = 0, dpi: int = DPI) -> bytes:
    """Grey PNG of one page. A scan embedded above `dpi` is rendered at its own resolution
    (never upsampled past 600) so no detail is thrown away; a lower one is rendered at `dpi`."""
    import pymupdf

    with pymupdf.open(stream=pdf, filetype="pdf") as d:
        page = d[index]
        use = dpi
        for info in page.get_images(full=True):
            try:
                w = d.extract_image(info[0])["width"]
            except Exception:  # noqa: BLE001
                continue
            native = w / max(page.rect.width, 1) * 72
            if native > use:
                use = int(min(600, native))
        pix = page.get_pixmap(dpi=use, colorspace=pymupdf.csGRAY, alpha=False)
        return pix.tobytes("png")


def ocr_pdf_page(piece: bytes, dpi: int = DPI) -> dict:
    """Render and recognise one single-page PDF in one go (meant for a process pool)."""
    import pymupdf

    with pymupdf.open(stream=piece, filetype="pdf") as d:
        page = d[0]
        use = dpi
        for info in page.get_images(full=True):
            try:
                w = d.extract_image(info[0])["width"]
            except Exception:  # noqa: BLE001
                continue
            native = w / max(page.rect.width, 1) * 72
            if native > use:
                use = int(min(600, native))
        png = page.get_pixmap(dpi=use, colorspace=pymupdf.csGRAY, alpha=False).tobytes("png")
    r = recognize(png)
    r["dpi"] = use
    return r


# -- recognition -------------------------------------------------------------------------------


def recognize(png: bytes) -> dict:
    """{"text": str, "confidence": float 0..1, "lines": int, "engine": str}"""
    eng = engine_name()
    if eng == "vision":
        return _vision(png)
    if eng == "tesseract":
        return _tesseract(png)
    raise RuntimeError("no OCR engine on this machine")


def _join_lines(lines: list[tuple[float, float, float, str]]) -> str:
    """lines: (top, left, height, text) with top growing downwards, in any order -> text in
    reading order with a blank line where the vertical gap is clearly larger than line spacing."""
    if not lines:
        return ""
    heights = [ln[2] for ln in lines if ln[2] > 0]
    h = statistics.median(heights) if heights else 1.0
    # Rows: lines whose tops are within half a line height of each other (a slightly skewed scan
    # still keeps the halves of one line together); each row is read left to right.
    rows: list[list] = []
    for line in sorted(lines, key=lambda ln: ln[0]):
        if rows and abs(line[0] - rows[-1][0][0]) < 0.5 * h:
            rows[-1].append(line)
        else:
            rows.append([line])
    out, prev_bottom = [], None
    for row in rows:
        row.sort(key=lambda ln: ln[1])
        top = min(ln[0] for ln in row)
        if prev_bottom is not None and top - prev_bottom > 0.9 * h:
            out.append("")
        out.append(" ".join(ln[3] for ln in row))
        prev_bottom = max(ln[0] + ln[2] for ln in row)
    return "\n".join(out).strip()


def _vision(png: bytes) -> dict:
    import Vision
    from Foundation import NSData

    data = NSData.dataWithBytes_length_(png, len(png))
    handler = Vision.VNImageRequestHandler.alloc().initWithData_options_(data, None)
    req = Vision.VNRecognizeTextRequest.alloc().init()
    req.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)
    req.setUsesLanguageCorrection_(True)
    req.setRecognitionLanguages_(["en-US"])
    ok, err = handler.performRequests_error_([req], None)
    if not ok:
        raise RuntimeError(f"Vision: {err}")
    lines, confs = [], []
    for obs in req.results() or []:
        cands = obs.topCandidates_(1)
        if not cands:
            continue
        c = cands[0]
        box = obs.boundingBox()  # normalised, origin bottom-left
        x, y, h = box.origin.x, box.origin.y, box.size.height
        lines.append((1.0 - (y + h), x, h, str(c.string())))
        confs.append(float(c.confidence()))
    text = _join_lines(lines)
    return {
        "text": text,
        "confidence": round(sum(confs) / len(confs), 4) if confs else 0.0,
        "lines": len(lines),
        "engine": "vision",
    }


def _tesseract(png: bytes) -> dict:
    with tempfile.TemporaryDirectory() as td:
        p = os.path.join(td, "page.png")
        with open(p, "wb") as f:
            f.write(png)
        r = subprocess.run(
            ["tesseract", p, "stdout", "--dpi", str(DPI), "--psm", "3", "-l", "eng", "tsv"],
            capture_output=True,
            text=True,
            timeout=300,
        )
    if r.returncode != 0:
        raise RuntimeError(f"tesseract: {r.stderr.strip()[-300:]}")
    rows = list(csv.DictReader(io.StringIO(r.stdout), delimiter="\t", quoting=csv.QUOTE_NONE))
    # level 5 = word rows; group by block/paragraph/line
    groups: dict[tuple, dict] = {}
    confs = []
    for w in rows:
        if w.get("level") != "5" or not (w.get("text") or "").strip():
            continue
        k = (int(w["block_num"]), int(w["par_num"]), int(w["line_num"]))
        g = groups.setdefault(k, {"top": int(w["top"]), "left": int(w["left"]), "h": int(w["height"]), "words": []})
        g["top"] = min(g["top"], int(w["top"]))
        g["h"] = max(g["h"], int(w["height"]))
        g["words"].append(w["text"])
        c = float(w.get("conf") or -1)
        if c >= 0:
            confs.append(c / 100.0)
    lines = [(g["top"], g["left"], g["h"], " ".join(g["words"])) for g in groups.values()]
    text = _join_lines(lines)
    return {
        "text": text,
        "confidence": round(sum(confs) / len(confs), 4) if confs else 0.0,
        "lines": len(lines),
        "engine": "tesseract",
    }
