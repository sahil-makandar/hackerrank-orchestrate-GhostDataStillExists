"""Local OCR (rapidocr-onnxruntime): independent of every LLM provider, used to cross-check vision output.
It reads the text rows of a screenshot without any model call.

Detected text boxes are grouped into visual rows so that a table row ("backup_9287 | areed1433@gmail.com | ...")
stays on one line; this keeps identifiers attached to the record they sit next to.
"""
import hashlib
from pathlib import Path

CACHE = Path(__file__).resolve().parents[1] / "cache" / "ocr"
_engine = None


def _rows(items):
    """items: [(x, y_center, height, text)] -> list of row strings, top to bottom, left to right."""
    rows = []
    for x, y, h, text in sorted(items, key=lambda i: (i[1], i[0])):
        if rows and abs(rows[-1]["y"] - y) <= 0.6 * max(h, rows[-1]["h"]):
            rows[-1]["items"].append((x, text))
        else:
            rows.append({"y": y, "h": h, "items": [(x, text)]})
    return [" | ".join(t for _, t in sorted(r["items"])) for r in rows]


def read(image_bytes):
    """Return the OCR text of a PNG/JPEG, one visual row per line (cached by image hash)."""
    global _engine
    CACHE.mkdir(parents=True, exist_ok=True)
    cp = CACHE / (hashlib.sha256(image_bytes).hexdigest() + ".txt")
    if cp.exists():
        return cp.read_text(encoding="utf-8")
    import cv2
    import numpy as np
    from rapidocr_onnxruntime import RapidOCR
    if _engine is None:
        _engine = RapidOCR()
    img = cv2.imdecode(np.frombuffer(image_bytes, np.uint8), cv2.IMREAD_COLOR)
    result, _ = _engine(img)
    items = []
    for box, text, _score in result or []:
        ys = [p[1] for p in box]
        items.append((box[0][0], sum(ys) / 4, max(ys) - min(ys), text.strip()))
    text = "\n".join(_rows(items))
    cp.write_text(text, encoding="utf-8", newline="\n")
    return text
