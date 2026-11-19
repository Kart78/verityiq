"""Test scope: which KPIs get test cases.

  dashboard    (default) measures shown on visible report pages, incl. implicit aggregations
  requirements KPIs found in an uploaded requirement document or dashboard screenshot (confirmed by the user)
  custom       a hand-picked list
  all          every measure in the model
"""
from __future__ import annotations

import base64
import difflib
import io
import os
import re
import zipfile
from typing import Optional

from .models import KPIDefinition, MeasureSpec, ModelMetadata, Tolerance, is_slicer
from .names import unbracket

MODES = ("dashboard", "requirements", "custom", "all")


# ------------------------------------------------------------------ what the dashboard displays

def _label(v) -> str:
    from ..engine.wiring import visual_label
    return visual_label(v)


def displayed(model: ModelMetadata) -> dict[str, list[dict]]:
    """measure name (or implicit:T[C]) -> [{page, visual, visual_type, title}] for visible pages."""
    out: dict[str, list[dict]] = {}
    for v in model.visuals:
        if v.page_hidden or is_slicer(v.visual_type):
            continue
        for m in v.measures:
            key = m if m.startswith("implicit:") else unbracket(m)
            out.setdefault(key, []).append({"page": v.page, "visual": v.id, "visual_type": v.visual_type,
                                            "title": _label(v)})
    return out


def implicit_kpis(model: ModelMetadata, start: int = 1) -> list[KPIDefinition]:
    """Columns dropped straight onto visuals (Sum of X, Count of Y) become KPIs too."""
    out, seen, i = [], set(), start
    for v in model.visuals:
        for ref, agg in v.implicit_aggs.items():
            if ref in seen:
                continue
            seen.add(ref)
            tbl, _, col = ref[9:].partition("[")
            col = col.rstrip("]")
            dax_fn = {"SUM": "SUM", "AVG": "AVERAGE", "MIN": "MIN", "MAX": "MAX", "COUNT": "COUNT",
                      "COUNT_DISTINCT": "DISTINCTCOUNT"}.get(agg, "SUM")
            label = {"SUM": "Sum", "AVG": "Average", "MIN": "Min", "MAX": "Max", "COUNT": "Count",
                     "COUNT_DISTINCT": "Count (distinct)"}.get(agg, agg)
            out.append(KPIDefinition(
                id=f"IMP-{i:03d}", name=f"{label} of {col}", kind="simple", approval_status="DRAFT", basis="inferred",
                business_definition="(implicit aggregation on a report visual - no DAX measure)",
                measure=MeasureSpec(table=tbl, aggregation=agg, column=f"{tbl}.{col}"),
                tolerance=Tolerance(type="exact") if agg.startswith("COUNT") else Tolerance(type="absolute", value=0.01, rounding=2),
                bi={"measure": ref, "expression": f"={dax_fn}('{tbl.replace(chr(39), chr(39) * 2)}'[{col.replace(']', ']]')}])",
                    "model_table": tbl, "expression_language": "dax"},
                translation_notes=[f"Implicit aggregation from the report ({label}); aggregation code read from the visual - verify."]))
            i += 1
    return out


INCLUDE_KEYS = ("slicers", "charts", "visual_checks", "data_quality")


def includes(scope: Optional[dict]) -> dict:
    """What is tested besides the KPI values themselves. Dashboard / all: everything shown; a requirement
    document, screenshot or hand-picked list: only the selected KPIs unless switched on."""
    scope = scope or {}
    broad = scope.get("mode", "all") in ("dashboard", "all")
    inc = scope.get("include") or {}
    return {k: bool(inc.get(k, broad)) for k in INCLUDE_KEYS}


def in_scope(kpis: list[KPIDefinition], model: ModelMetadata, scope: dict) -> list[KPIDefinition]:
    mode = scope.get("mode", "dashboard")
    if mode == "all":                          # every measure the report needs - dead (unused) DAX is left out
        from ..analyzers.usage import unused_measures
        dead = set() if scope.get("include_unused") else (set(scope["_dead"]) if "_dead" in scope else unused_measures(model))
        return [k for k in kpis if not (k.bi and not k.bi.measure.startswith("implicit:") and unbracket(k.bi.measure).lower() in dead)]
    if mode == "dashboard":
        shown = {k.lower() for k in displayed(model)}
        return [k for k in kpis if k.bi and (unbracket(k.bi.measure).lower() in shown or k.bi.measure.lower() in shown)]
    sel = {s.lower() for s in scope.get("selected", [])}
    return [k for k in kpis if k.bi and (unbracket(k.bi.measure).lower() in sel or k.bi.measure.lower() in sel)]


# ------------------------------------------------------------------ requirement documents & screenshots

def extract_text(filename: str, data: bytes) -> tuple[str, str]:
    """-> (text, how). Never executes content."""
    name = filename.lower()
    if name.endswith((".txt", ".md", ".csv", ".json", ".sql")):
        return data.decode("utf-8-sig", errors="replace"), "text"
    if name.endswith(".docx"):
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            xml = z.read("word/document.xml").decode("utf8", errors="replace")
        xml = re.sub(r"</w:p>|<w:br/>|<w:tab/>", "\n", xml)
        return re.sub(r"<[^>]+>", "", xml), "Word document"
    if name.endswith(".pptx"):
        parts = []
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            for n in sorted(x for x in z.namelist() if re.match(r"ppt/slides/slide\d+\.xml", x)):
                parts += re.findall(r"<a:t>([^<]*)</a:t>", z.read(n).decode("utf8", errors="replace"))
        return "\n".join(parts), "PowerPoint"
    if name.endswith(".xlsx"):
        from openpyxl import load_workbook
        wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        return "\n".join(" ".join(str(c) for c in row if c is not None) for ws in wb.worksheets for row in ws.iter_rows(values_only=True)), "Excel"
    if name.endswith(".pdf"):
        try:
            from pypdf import PdfReader  # type: ignore
        except ImportError:
            raise ValueError("Reading PDF needs the pypdf package: run  .\\.venv\\Scripts\\python.exe -m pip install pypdf")
        return "\n".join((p.extract_text() or "") for p in PdfReader(io.BytesIO(data)).pages), "PDF"
    if name.endswith((".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp")):
        return ocr_image(data, name)
    raise ValueError(f"Unsupported file type: {filename} (use .docx .pdf .xlsx .pptx .txt .md .csv or a .png/.jpg screenshot)")


_RAPID = None


def _rapidocr(data: bytes) -> str:
    """Offline OCR from the rapidocr-onnxruntime pip package (models bundled; nothing else to install)."""
    global _RAPID
    from rapidocr_onnxruntime import RapidOCR  # type: ignore
    import numpy as np
    from PIL import Image
    if _RAPID is None:
        _RAPID = RapidOCR()
    img = Image.open(io.BytesIO(data)).convert("RGB")
    if max(img.size) > 4000:                       # keep huge screenshots fast
        img.thumbnail((4000, 4000))
    res, _ = _RAPID(np.array(img))
    return "\n".join(r[1] for r in (res or []))


def ocr_image(data: bytes, name: str) -> tuple[str, str]:
    try:                                           # 0. bundled offline OCR (pip package rapidocr-onnxruntime)
        txt = _rapidocr(data)
        if txt.strip():
            return txt, "OCR (built-in)"
    except Exception:  # noqa: BLE001
        pass
    try:                                           # 1. local OCR when Tesseract is installed
        import pytesseract  # type: ignore
        from PIL import Image
        return pytesseract.image_to_string(Image.open(io.BytesIO(data))), "OCR (Tesseract)"
    except Exception:  # noqa: BLE001
        pass
    if os.environ.get("ANTHROPIC_API_KEY"):        # 2. Claude vision when an API key is configured
        import requests
        mt = "image/png" if name.endswith(".png") else "image/jpeg"
        r = requests.post("https://api.anthropic.com/v1/messages", timeout=90,
                          headers={"x-api-key": os.environ["ANTHROPIC_API_KEY"], "anthropic-version": "2023-06-01"},
                          json={"model": os.environ.get("LLM_MODEL", "claude-sonnet-5-5"), "max_tokens": 1500, "messages": [{
                              "role": "user", "content": [
                                  {"type": "image", "source": {"type": "base64", "media_type": mt, "data": base64.b64encode(data).decode()}},
                                  {"type": "text", "text": "List every KPI / measure name, card title, chart title and axis label visible "
                                                           "in this dashboard screenshot, one per line. Text only."}]}]})
        r.raise_for_status()
        return r.json()["content"][0]["text"], "Claude vision"
    return "", "no OCR available"


def _norm(s: str) -> str:
    s = s.replace("%", " percent ").replace("#", " number ").replace("&", " and ")
    return re.sub(r"\s+", " ", re.sub(r"[^0-9a-z]+", " ", s.lower())).strip()


def _compact(s: str) -> str:
    return _norm(s).replace(" ", "")


def _fuzzy_find(n: str, words: list[str], lines: list[str]) -> tuple[float, str]:
    """Best similarity of phrase n against word windows and whole OCR lines, ignoring spaces
    (OCR often drops or adds them: 'ExecutiveSalesOverview', 'Leads Cortscted')."""
    cn, k = n.replace(" ", ""), len(n.split())
    best, phrase = 0.0, ""
    cands = [" ".join(words[i:i + w]) for w in {max(1, k - 1), k, k + 1} for i in range(0, max(1, len(words) - w + 1))]
    for w in cands + lines:
        cw = w.replace(" ", "")
        if not cw or abs(len(cw) - len(cn)) > max(2, len(cn) // 6):
            continue
        r = difflib.SequenceMatcher(None, cn, cw).ratio()
        if r > best:
            best, phrase = r, w
    return best, phrase


def match_kpis(text: str, model: ModelMetadata, candidates: list[str], fuzzy: float = 0.86) -> list[dict]:
    """Find measures (by name or by the title of a visual showing them) mentioned in the text.
    fuzzy: similarity needed for an inexact match (lower for OCR'd screenshots)."""
    t = " " + _norm(text) + " "
    words = t.split()
    lines = [_norm(x) for x in text.splitlines() if _norm(x)]
    tc = t.replace(" ", "")
    titles: dict[str, list[str]] = {}
    for v in model.visuals:
        if v.title:
            for m in v.measures:
                titles.setdefault(_norm(v.title), []).append(m if m.startswith("implicit:") else unbracket(m))
    found: dict[str, dict] = {}

    def hit(name: str, score: float, via: str, phrase: str):
        if name not in found or found[name]["score"] < score:
            found[name] = {"measure": name, "score": round(score, 2), "via": via, "phrase": phrase}

    for c in candidates:
        n = _norm(c[9:] if c.startswith("implicit:") else c)
        if len(n) < 3:
            continue
        if f" {n} " in t or (len(n.replace(" ", "")) >= 8 and n.replace(" ", "") in tc and " " in n):
            hit(c, 1.0, "name", c)
            continue
        if len(n.replace(" ", "")) < 5:                # too short to match approximately
            continue
        best, phrase = _fuzzy_find(n, words, lines)
        if best >= fuzzy:
            hit(c, best, "similar wording", phrase)
    for title, ms in titles.items():
        if len(title) < 4:
            continue
        if f" {title} " in t:
            score, via, ph = 0.99, "visual title", title
        else:
            r, ph = _fuzzy_find(title, words, lines) if len(title.replace(" ", "")) >= 6 else (0.0, "")
            if r < fuzzy:
                continue
            score, via = min(r, 0.98), "visual title (similar)"
        for m in ms:
            if m in candidates:
                hit(m, score, via, ph)
    return sorted(found.values(), key=lambda x: -x["score"])
