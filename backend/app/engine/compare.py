"""Platform-independent comparison of an independently computed expected value with the
BI-side actual value. Knows nothing about where either number came from."""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Optional

from ..core.models import Status, Tolerance


@dataclass
class Comparison:
    status: Status
    expected: Any
    actual: Any
    difference: Optional[float]
    pct_difference: Optional[float]
    message: str


def _num(v: Any) -> Optional[float]:
    if v is None:
        return None
    if isinstance(v, (Decimal, int, float)) and not isinstance(v, bool):
        f = float(v)
        return None if math.isnan(f) else f
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _f(v: float) -> str:
    if float(v).is_integer():
        return f"{int(v):,}"
    return f"{v:,.4f}".rstrip("0").rstrip(".")


def compare_values(expected: Any, actual: Any, tol: Tolerance, unit: str = "number",
                   expected_unit: Optional[str] = None, actual_unit: Optional[str] = None) -> Comparison:
    if expected_unit and actual_unit and expected_unit != actual_unit:
        return Comparison(Status.BLOCKED, expected, actual, None, None,
                          f"Units differ (expected {expected_unit}, actual {actual_unit}); values are not comparable.")
    e, a = _num(expected), _num(actual)
    if e is None or a is None:
        if tol.null_handling == "null_is_zero":
            e, a = e or 0.0, a or 0.0
        elif tol.null_handling == "null_equals_null" and e is None and a is None:
            return Comparison(Status.PASS, expected, actual, None, None, "Both values are null (null_equals_null).")
        else:
            return Comparison(Status.FAIL, expected, actual, None, None,
                              f"Null value ({'expected' if e is None else 'actual'} side) and null_handling = {tol.null_handling}.")
    if tol.rounding is not None:
        e, a = round(e, tol.rounding), round(a, tol.rounding)
    diff = a - e
    pct = (diff / e * 100) if e else (0.0 if diff == 0 else math.inf)
    if tol.type == "exact":
        ok, rule = diff == 0, "exact match"
    elif tol.type == "absolute":
        ok, rule = abs(diff) <= tol.value + 1e-12, f"|diff| <= {tol.value}"
    else:
        ok, rule = abs(pct) <= tol.value + 1e-12, f"|diff%| <= {tol.value}%"
    msg = (f"Within tolerance ({rule})." if ok else
           f"Outside tolerance ({rule}): actual {_f(a)} vs expected {_f(e)} (diff {'+' if diff > 0 else ''}{_f(diff)}, {pct:+.2f}%).")
    return Comparison(Status.PASS if ok else Status.FAIL, e, a, round(diff, 10), None if math.isinf(pct) else round(pct, 6), msg)


def _key(v: Any) -> str:
    if isinstance(v, (datetime, date)):
        return v.isoformat()[:10]
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v).strip() if v is not None else "∅"


def compare_grain(expected_rows: list[dict], actual_rows: list[dict], tol: Tolerance) -> tuple[Status, list[dict], str]:
    exp = {_key(r["grain"]): r.get("expected_value") for r in expected_rows}
    act = {_key(r["grain"]): r.get("value") for r in actual_rows}
    detail, fails = [], 0
    for k in sorted(set(exp) | set(act)):
        if k not in act:
            detail.append({"grain": k, "expected": exp[k], "actual": None, "status": "FAIL", "note": "Missing in BI"})
            fails += 1
            continue
        if k not in exp:
            if _num(act[k]) in (None, 0.0):
                continue
            detail.append({"grain": k, "expected": None, "actual": act[k], "status": "FAIL", "note": "Not in source"})
            fails += 1
            continue
        c = compare_values(exp[k], act[k], tol)
        detail.append({"grain": k, "expected": c.expected, "actual": c.actual, "difference": c.difference,
                       "status": c.status.value, "note": "" if c.status == Status.PASS else c.message})
        fails += c.status != Status.PASS
    if not detail:
        return Status.BLOCKED, detail, "No rows returned on either side; nothing to compare."
    st = Status.PASS if fails == 0 else Status.FAIL
    return st, detail, f"{len(detail) - fails}/{len(detail)} grain rows within tolerance."
