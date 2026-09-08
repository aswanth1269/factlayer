"""Deterministic normalization. No model calls in this file.

A fact is only comparable to another fact once both have been moved onto the
same coordinate system: same base unit, same period expressed as dates, same
entity key. Everything here is pure Python so the comparison step downstream is
fully explainable.
"""

import calendar
import re
from datetime import date

# --------------------------------------------------------------------------
# Units
# --------------------------------------------------------------------------
# Each entry maps a surface form to (dimension, base unit, multiplier).
# Indian scale words matter here: one crore is ten million, one lakh is
# one hundred thousand.

_UNIT_TABLE = [
    (r"(?:inr|rs\.?|₹|rupee)s?\s*(?:in\s*)?(?:crore|cr)\b", ("currency", "INR", 1e7)),
    (r"(?:inr|rs\.?|₹|rupee)s?\s*(?:in\s*)?lakh", ("currency", "INR", 1e5)),
    (r"(?:inr|rs\.?|₹|rupee)s?\s*(?:in\s*)?(?:million|mn)\b", ("currency", "INR", 1e6)),
    (r"(?:inr|rs\.?|₹|rupee)s?\s*(?:in\s*)?(?:billion|bn)\b", ("currency", "INR", 1e9)),
    (r"(?:usd|us\$|\$|dollar)s?\s*(?:in\s*)?(?:billion|bn)\b", ("currency", "USD", 1e9)),
    (r"(?:usd|us\$|\$|dollar)s?\s*(?:in\s*)?(?:million|mn)\b", ("currency", "USD", 1e6)),
    (r"(?:usd|us\$|\$|dollar)", ("currency", "USD", 1.0)),
    (r"(?:inr|rs\.?|₹|rupee)", ("currency", "INR", 1.0)),
    (r"\bcrore\b|\bcr\b", ("currency", "INR", 1e7)),
    (r"\blakh\b", ("currency", "INR", 1e5)),
    (r"(?:million|mn)\s*(?:metric\s*)?(?:tonne|ton)s?", ("mass", "ton", 1e6)),
    (r"(?:thousand|'000|000s)\s*(?:metric\s*)?(?:tonne|ton)s?", ("mass", "ton", 1e3)),
    (r"(?:metric\s*)?(?:tonne|ton)s?\b", ("mass", "ton", 1.0)),
    (r"(?:percent|per cent|%|pct|percentage points?|bps)", ("percent", "percent", 1.0)),
    (r"(?:million|mn)\s*(?:sq\.?\s*ft|square feet)", ("area", "sqft", 1e6)),
    (r"(?:sq\.?\s*ft|square feet)", ("area", "sqft", 1.0)),
    (r"\b(?:million|mn)\b", ("count", "unit", 1e6)),
    (r"\b(?:billion|bn)\b", ("count", "unit", 1e9)),
    (r"\b(?:thousand|k)\b", ("count", "unit", 1e3)),
    (r"\b(?:days?|years?|months?)\b", ("duration", "day", 1.0)),
]

_BPS = re.compile(r"\bbps\b", re.I)


def normalize_unit(unit: str | None) -> tuple[str, str, float]:
    """Return (dimension, base_unit, multiplier)."""
    if not unit:
        return ("count", "unit", 1.0)
    u = unit.strip().lower()
    if _BPS.search(u):
        return ("percent", "percent", 0.01)
    for pattern, result in _UNIT_TABLE:
        if re.search(pattern, u, re.I):
            return result
    return ("other", u, 1.0)


# --------------------------------------------------------------------------
# Values and rounding tolerance
# --------------------------------------------------------------------------

def parse_number(value_text: str | None) -> float | None:
    if value_text is None:
        return None
    s = str(value_text).strip()
    negative = s.startswith("(") and s.endswith(")")   # (452) means minus 452
    s = s.strip("()")
    s = re.sub(r"[^0-9.\-]", "", s)
    if not s or s in {"-", "."}:
        return None
    try:
        v = float(s)
    except ValueError:
        return None
    return -v if negative else v


def rounding_tolerance(value_text: str | None) -> float:
    """Half of the last stated significant digit, in the unit as written.

    "1.4" was rounded to one decimal, so anything within 0.05 of it is the same
    number. "18,793" was stated exactly, so the tolerance is half a unit. This
    is what lets 1.4 million tons match 1,429 thousand tons without a fuzzy
    similarity score anywhere in the pipeline.
    """
    if value_text is None:
        return 0.0
    s = re.sub(r"[^0-9.]", "", str(value_text))
    if not s:
        return 0.0
    if "." in s:
        decimals = len(s.split(".", 1)[1])
        return 0.5 * (10 ** -decimals)
    trimmed = s.rstrip("0")
    trailing_zeros = len(s) - len(trimmed)
    return 0.5 * (10 ** trailing_zeros) if trailing_zeros else 0.5


# --------------------------------------------------------------------------
# Periods
# --------------------------------------------------------------------------
# Indian financial years run 1 April to 31 March. FY24 means April 2023 to
# March 2024.

_FY = re.compile(r"\bFY\s?'?(\d{2,4})(?:\s?[-/]\s?(\d{2,4}))?\b", re.I)
_QFY = re.compile(r"\bQ([1-4])\s*(?:of\s*)?FY\s?'?(\d{2,4})\b", re.I)
_FYQ = re.compile(r"\bFY\s?'?(\d{2,4})\s*Q([1-4])\b", re.I)
_HFY = re.compile(r"\bH([12])\s*(?:of\s*)?FY\s?'?(\d{2,4})\b", re.I)
_SPAN = re.compile(r"\b(20\d{2})\s*[-/]\s*(\d{2,4})\b")
_CY = re.compile(r"\b(?:CY\s?)?(20\d{2})\b")
_AS_OF = re.compile(
    r"\b(?:as (?:of|at|on)|ending|ended)\s+"
    r"(\d{1,2})?\s*([A-Z][a-z]+)?\s*(20\d{2})\b", re.I
)

_MONTHS = {m: i + 1 for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july",
     "august", "september", "october", "november", "december"])}


def _fy_end_year(raw: str) -> int:
    n = int(raw)
    return 2000 + n if n < 100 else n


def _fy_window(end_year: int) -> tuple[str, str]:
    return (f"{end_year - 1}-04-01", f"{end_year}-03-31")


def _quarter_window(end_year: int, q: int) -> tuple[str, str]:
    # Q1 of FY24 is Apr-Jun 2023, Q4 is Jan-Mar 2024.
    starts = {1: (end_year - 1, 4), 2: (end_year - 1, 7),
              3: (end_year - 1, 10), 4: (end_year, 1)}
    y, m = starts[q]
    start = date(y, m, 1)
    em, ey = (m + 2, y) if m + 2 <= 12 else (m - 10, y + 1)
    last = calendar.monthrange(ey, em)[1]
    return (start.isoformat(), date(ey, em, last).isoformat())


def normalize_period(period: str | None) -> tuple[str | None, str | None]:
    """Return (start_date, end_date) as ISO strings, or (None, None)."""
    if not period:
        return (None, None)
    p = period.strip()

    m = _QFY.search(p) or None
    if m:
        return _quarter_window(_fy_end_year(m.group(2)), int(m.group(1)))
    m = _FYQ.search(p)
    if m:
        return _quarter_window(_fy_end_year(m.group(1)), int(m.group(2)))
    m = _HFY.search(p)
    if m:
        y = _fy_end_year(m.group(2))
        return (f"{y-1}-04-01", f"{y-1}-09-30") if m.group(1) == "1" \
            else (f"{y-1}-10-01", f"{y}-03-31")
    m = _FY.search(p)
    if m:
        end = m.group(2) or m.group(1)
        return _fy_window(_fy_end_year(end))
    m = _AS_OF.search(p)
    if m:
        day, mon, yr = m.group(1), (m.group(2) or "").lower(), int(m.group(3))
        if mon in _MONTHS:
            mm = _MONTHS[mon]
            last = calendar.monthrange(yr, mm)[1]
            d = min(int(day), last) if day else 1
            iso = date(yr, mm, d).isoformat()
            return (iso, iso)
        return (f"{yr}-01-01", f"{yr}-12-31")
    m = _SPAN.search(p)
    if m:
        start = int(m.group(1))
        tail = m.group(2)
        end = int(tail) if len(tail) == 4 else start + 1
        return (f"{start}-04-01", f"{end}-03-31")
    m = _CY.search(p)
    if m:
        y = int(m.group(1))
        return (f"{y}-01-01", f"{y}-12-31")
    return (None, None)


# --------------------------------------------------------------------------
# Entities and metrics
# --------------------------------------------------------------------------

_CORP_SUFFIX = re.compile(
    r"\b(?:limited|ltd|private|pvt|inc|incorporated|corporation|corp|plc|llp|company|co)\b\.?",
    re.I,
)
_STOP = {"the", "of", "for", "in", "a", "an", "total", "and"}


def entity_key(entity: str | None) -> str:
    if not entity:
        return "unknown"
    e = _CORP_SUFFIX.sub(" ", entity.lower())
    e = re.sub(r"[^a-z0-9 ]", " ", e)
    return "_".join(w for w in e.split() if w) or "unknown"


def metric_slug(metric: str | None) -> str:
    if not metric:
        return "unknown"
    m = re.sub(r"[^a-z0-9 ]", " ", metric.lower())
    words = [w for w in m.split() if w and w not in _STOP]
    return "_".join(words) or "unknown"


_BASIS_MAP = [
    (r"pro[\s-]?forma", "pro forma"),
    (r"restat", "restated"),
    (r"adjust|adj\.?\b|normalis|normaliz", "adjusted"),
    (r"project|forecast|estimat|guidance|outlook|\bproj\b|\be\b$", "projected"),
    (r"provisional|preliminary", "provisional"),
    (r"revised", "revised"),
    (r"annualis|annualiz", "annualised"),
]


def normalize_basis(basis: str | None) -> str:
    if not basis:
        return "reported"
    b = basis.strip().lower()
    for pattern, label in _BASIS_MAP:
        if re.search(pattern, b):
            return label
    return b


def normalize_scope(scope: str | None) -> str:
    if not scope:
        return "unspecified"
    s = scope.strip().lower()
    if "consolidat" in s:
        return "consolidated"
    if "standalone" in s or "separate" in s:
        return "standalone"
    return re.sub(r"\s+", " ", s)
