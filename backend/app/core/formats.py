"""Detect how a date stored as text is formatted, from sample values (Spark/Databricks pattern letters)."""
from __future__ import annotations

from datetime import datetime
from typing import Iterable, Optional

# (Spark pattern, Python strptime) - order matters for ambiguous values: US month-first before day-first
CANDIDATES = [
    ("yyyy-MM-dd", "%Y-%m-%d"), ("yyyy-MM-dd HH:mm:ss", "%Y-%m-%d %H:%M:%S"), ("yyyy-MM-dd'T'HH:mm:ss", "%Y-%m-%dT%H:%M:%S"),
    ("MM/dd/yyyy", "%m/%d/%Y"), ("MM/dd/yyyy HH:mm:ss", "%m/%d/%Y %H:%M:%S"), ("M/d/yyyy", "%m/%d/%Y"),
    ("dd/MM/yyyy", "%d/%m/%Y"), ("dd-MM-yyyy", "%d-%m-%Y"), ("MM-dd-yyyy", "%m-%d-%Y"), ("yyyyMMdd", "%Y%m%d"),
    ("yyyy/MM/dd", "%Y/%m/%d"), ("dd.MM.yyyy", "%d.%m.%Y"), ("dd-MMM-yyyy", "%d-%b-%Y"), ("MMM d, yyyy", "%b %d, %Y"),
]


def _ok(values: list[str], fmt: str) -> bool:
    for v in values:
        try:
            datetime.strptime(v, fmt)
        except ValueError:
            return False
    return True


def detect_date_format(samples: Iterable) -> Optional[str]:
    vals = [str(v).strip() for v in samples if v is not None and str(v).strip()]
    if not vals:
        return None
    for spark, py in CANDIDATES:
        if _ok(vals, py):
            if spark == "MM/dd/yyyy" and any(len(v.split("/")[0]) == 1 or len(v.split("/")[1]) == 1 for v in vals):
                return "M/d/yyyy"
            return spark
    return None
