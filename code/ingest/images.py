"""Application screenshots (PNG): Gemini vision -> structured facts, cross-checked against local OCR.
Local OCR is both the independent check and the fallback when the vision model is out of quota.

Chain: Gemini vision, then local OCR only. Cross-check: rapidocr text of the same image. Every identifier the
vision model returns must appear in that OCR text, otherwise it is dropped. If vision fails, only OCR row-level
facts are kept.
"""
import json
import re

import config
from llm import client
from . import localocr
from .common import Obs, RESOURCE_RE, extract_ids, norm_phone, system_of

PROMPT = """You are reading a screenshot of an internal business application. Extract every record shown, including the
detail side panel (key it by the id in the panel header). Copy text EXACTLY as displayed; never guess or infer values.
Return ONLY JSON: {"page_title": str, "records": [{"resource_id": str, "fields": {"<column or label>": "<value>"}}]}.
resource_id is the record/archive/ticket/export id shown for the row (for example account_123, backup_9287)."""


def _present(token, ocr_norm, ocr_digits):
    if token.lower() in ocr_norm:
        return True
    digits = norm_phone(token).lstrip("+")
    return len(digits) > 7 and digits.isdigit() and digits in ocr_digits  # phone formatting may differ


def _check_fields(fields, ocr_norm, ocr_digits, dropped):
    kept = {}
    for label, value in fields.items():
        value = "" if value is None else str(value)
        ids = extract_ids(value)
        tokens = ids["emails"] | ids["hashes"] | ids["user_ids"] | ids["resources"] | ids["phones"]
        if all(_present(t, ocr_norm, ocr_digits) for t in tokens):
            kept[str(label)] = value
        else:
            dropped.append(f"{label}={value}")
    return kept


def _ocr_only(file_id, t, ocr_text):
    obs = []
    for line in ocr_text.splitlines():
        for rid in set(RESOURCE_RE.findall(line)):
            o = Obs(file_id, t, rid, system_of(rid) or "", "image:ocr_row", {"ocr_row": line.strip()})
            o.absorb(line)
            obs.append(o)
    return obs


def parse(file_id, path, manifest_time):
    data = path.read_bytes()
    ocr_text = localocr.read(data)
    ocr_norm = ocr_text.lower()
    ocr_digits = re.sub(r"\D", "", ocr_text)
    note = {"vision": None, "ocr_rows": len(ocr_text.splitlines()), "dropped": []}
    try:
        r = client.chat(config.VISION_CHAIN, [{"role": "user", "content": PROMPT}], image=(data, "image/png"),
                        json_mode=True, max_tokens=4000, tag=f"vision:{file_id}")
        facts = json.loads(r["text"])
        note["vision"] = f'{r["provider"]}/{r["model"]}'
    except (client.LLMError, ValueError) as e:
        note["vision"] = "none; OCR only (" + str(e)[:70] + ")"
        return _ocr_only(file_id, manifest_time, ocr_text), [], manifest_time, note

    title = str(facts.get("page_title", ""))
    obs = []
    for rec in facts.get("records", []):
        rid = str(rec.get("resource_id", "")).strip()
        if not RESOURCE_RE.fullmatch(rid) or rid.lower() not in ocr_norm:
            note["dropped"].append(f"resource_id={rid}")
            continue
        fields = _check_fields(rec.get("fields", {}), ocr_norm, ocr_digits, note["dropped"])
        o = Obs(file_id, manifest_time, rid, system_of(rid) or "", f"image:{title}", fields)
        o.absorb(json.dumps(fields, ensure_ascii=False))
        obs.append(o)
    # resource ids visible in the OCR text that vision missed still count (row-level facts only)
    seen = {o.resource_id for o in obs}
    obs += [o for o in _ocr_only(file_id, manifest_time, ocr_text) if o.resource_id not in seen]
    return obs, [], manifest_time, note
