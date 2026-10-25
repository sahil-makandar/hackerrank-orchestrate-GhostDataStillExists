"""PDF evidence: text layer via pypdf, then a model structures it into facts.
A scanned page without a text layer would use the vision chain (none in this dataset).

Scanned PDFs with no text layer are rendered to images and sent through the vision path instead.
Every identifier the model returns must literally appear in the extracted text, otherwise it is dropped.
"""
import json
import re

import config
from llm import client
from .common import Obs, RESOURCE_RE, extract_ids, norm_phone, system_of

PROMPT = """Below is text extracted from a business PDF (invoice, support record, statement, register or journal extract).
Extract the facts it contains. Copy identifiers EXACTLY as written; never invent or infer any value.
Return ONLY JSON: {"document_type": str, "records": [{"reference": str, "fields": {"<label>": "<value>"}}]}.
"reference" is the main id of the record (resource id such as support_001 / audit_record_003, statement number,
or order number). Put names, emails, phones, user ids, dates, retention dates and statuses in "fields".
One record per distinct reference; a register listing several orders yields one record per row.

TEXT:
"""


def _text(path):
    from pypdf import PdfReader
    return "\n".join((page.extract_text() or "") for page in PdfReader(str(path)).pages)


def _render_first_pages(path, n=2):
    import fitz
    doc = fitz.open(str(path))
    return [(doc[i].get_pixmap(dpi=140).tobytes("png"), "image/png") for i in range(min(n, len(doc)))]


def _line_level(file_id, t, text):
    obs = []
    for line in text.splitlines():
        for rid in set(RESOURCE_RE.findall(line)):
            o = Obs(file_id, t, rid, system_of(rid) or "", "pdf:line", {"line": line.strip()})
            o.absorb(line)
            obs.append(o)
    return obs


def parse(file_id, path, manifest_time):
    text = _text(path)
    note = {"text_chars": len(text), "model": None, "dropped": []}
    if len(text.strip()) < 30:  # no usable text layer: vision on rendered pages
        note["model"] = "vision (no text layer)"
        obs = []
        for img in _render_first_pages(path):
            r = client.chat(config.VISION_CHAIN,
                            [{"role": "user", "content": "Transcribe all text in this page exactly, one line per row."}],
                            image=img, max_tokens=3000, tag=f"pdfvision:{file_id}")
            obs += _line_level(file_id, manifest_time, r["text"])
        return obs, [], manifest_time, note
    low = text.lower()
    low_digits = re.sub(r"\D", "", text)
    obs = []
    try:
        r = client.chat(config.ranked_chain(), [{"role": "user", "content": PROMPT + text}], json_mode=True,
                        max_tokens=4000, tag=f"pdfstruct:{file_id}")
        facts = json.loads(r["text"])
        note["model"] = f'{r["provider"]}/{r["model"]}'
    except (client.LLMError, ValueError) as e:
        note["model"] = f"failed: {str(e)[:80]}"
        facts = {"records": []}
    for rec in facts.get("records", []):
        ref = str(rec.get("reference", "")).strip()
        if not ref or ref.lower() not in low:
            note["dropped"].append(f"reference={ref}")
            continue
        fields = {}
        for label, value in rec.get("fields", {}).items():
            value = "" if value is None else str(value)
            ids = extract_ids(value)
            tokens = list(ids["emails"]) + list(ids["hashes"]) + list(ids["user_ids"]) + list(ids["resources"])
            phones_ok = all(norm_phone(p).lstrip("+") in low_digits for p in ids["phones"])
            if phones_ok and all(tok in low for tok in tokens):
                fields[str(label)] = value
            else:
                note["dropped"].append(f"{label}={value}")
        o = Obs(file_id, manifest_time, ref, system_of(ref) or "", f'pdf:{facts.get("document_type", "")}', fields)
        o.absorb(json.dumps(fields, ensure_ascii=False))
        obs.append(o)
    seen = {o.resource_id for o in obs}
    obs += [o for o in _line_level(file_id, manifest_time, text) if o.resource_id not in seen]
    return obs, [], manifest_time, note
