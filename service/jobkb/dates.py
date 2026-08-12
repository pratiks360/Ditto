"""Date parsing and re-encoding.

A date widget rejects anything but its own format — "Sept 2025" typed into an
<input type="month"> is silently dropped — so a stored date has to be rewritten
into the encoding the control is asking for before it will stick. The control
advertises that encoding in its placeholder, pattern, title, alt text or a
data-* attribute; the extension collects all of those into `hint`.

Ported from storage.js so both sides agree.
"""

from __future__ import annotations

import re
from typing import Any

MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}

ONGOING = re.compile(r"\b(present|current|currently|now|ongoing|to date|till date)\b", re.I)

MASK_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\bY{2,4}(\s*[/.\-]\s*)M{1,2}\1D{1,2}\b", re.I), "YMD"),
    (re.compile(r"\bD{1,2}(\s*[/.\-]\s*)M{1,2}\1Y{2,4}\b", re.I), "DMY"),
    (re.compile(r"\bM{1,2}(\s*[/.\-]\s*)D{1,2}\1Y{2,4}\b", re.I), "MDY"),
    (re.compile(r"\bY{2,4}(\s*[/.\-]\s*)M{1,2}\b", re.I), "YM"),
    (re.compile(r"\bM{1,2}(\s*[/.\-]\s*)Y{2,4}\b", re.I), "MY"),
    (re.compile(r"(^|\s)Y{4}(\s|$)", re.I), "Y"),
]


def is_ongoing(value: Any) -> bool:
    return bool(ONGOING.search(str(value or "")))


def parse_date(value: Any) -> dict[str, Any] | None:
    s = str(value or "").strip()
    if not s:
        return None
    if ONGOING.search(s):
        return {"ongoing": True}

    m = re.search(r"\b((?:19|20)\d{2})-(\d{1,2})(?:-(\d{1,2}))?", s)         # 2022-08-01
    if m:
        return {"year": int(m[1]), "month": int(m[2]), "day": int(m[3]) if m[3] else None}

    m = re.search(r"\b(\d{1,2})[/.\-](\d{1,2})[/.\-]((?:19|20)\d{2})\b", s)  # 01/06/2019
    if m:
        return {"year": int(m[3]), "month": int(m[2]), "day": int(m[1])}

    m = re.search(r"\b(\d{1,2})[/.\-]((?:19|20)\d{2})\b", s)                 # 06/2019
    if m:
        return {"year": int(m[2]), "month": int(m[1]), "day": None}

    m = re.search(r"\b([A-Za-z]{3,9})\.?\s+((?:19|20)\d{2})\b", s)           # Sept 2025
    if m:
        month = MONTHS.get(m[1][:3].lower())
        if month:
            return {"year": int(m[2]), "month": month, "day": None}

    m = re.search(r"\b((?:19|20)\d{2})\b", s)                                # 2017
    if m:
        return {"year": int(m[1]), "month": None, "day": None}

    return None


def sort_key(value: Any) -> int:
    """Newest-first ordering. An ongoing role sorts above every dated one."""
    if is_ongoing(value):
        return 10**9
    p = parse_date(value)
    if not p or not p.get("year"):
        return 0
    return p["year"] * 100 + (p.get("month") or 0)


def _hint_text(hints: dict[str, Any]) -> str:
    h = hints or {}
    return " ".join(
        str(h.get(k) or "") for k in ("placeholder", "pattern", "hint", "label")
    )


def detect_mask(hints: dict[str, Any]) -> dict[str, str] | None:
    """The layout a text box is asking for, e.g. {"order": "MY", "sep": "/"}."""
    text = _hint_text(hints)
    for pattern, order in MASK_PATTERNS:
        m = pattern.search(text)
        if m:
            # Group 1 is the separator the mask used, captured so "DD-MM-YYYY"
            # comes back out hyphenated rather than slashed.
            sep = (m.group(1) or "").strip() or "/"
            return {"order": order, "sep": sep}
    return None


def is_date_field(hints: dict[str, Any]) -> bool:
    h = hints or {}
    if str(h.get("inputType") or "").lower() in ("month", "date", "week"):
        return True
    return detect_mask(h) is not None


def format_for_field(value: Any, hints: dict[str, Any]) -> str:
    """Re-encode a stored date for the control it is going into.

    Returns "" when the value cannot be represented — an ongoing role has no end
    date, and a month/year value cannot fill a control that demands a day it was
    never told. Non-date fields pass through untouched.
    """
    if not is_date_field(hints):
        return str(value or "")

    parts = parse_date(value)
    if not parts or parts.get("ongoing"):
        return ""

    h = hints or {}
    itype = str(h.get("inputType") or "").lower()
    if itype == "month":
        return f"{parts['year']}-{parts['month']:02d}" if parts.get("month") else ""
    if itype == "date":
        if not parts.get("month"):
            return ""
        return f"{parts['year']}-{parts['month']:02d}-{(parts.get('day') or 1):02d}"

    mask = detect_mask(h)
    if not mask:
        return str(value or "")
    if mask["order"] == "Y":
        return str(parts["year"])
    if not parts.get("month"):
        return ""

    # Resume dates are month + year, so the day is ours to choose: the 1st is
    # the convention every application form expects.
    y, mo = str(parts["year"]), f"{parts['month']:02d}"
    d = f"{(parts.get('day') or 1):02d}"
    sep = mask["sep"]
    order = mask["order"]
    if order == "YMD":
        return sep.join([y, mo, d])
    if order == "DMY":
        return sep.join([d, mo, y])
    if order == "MDY":
        return sep.join([mo, d, y])
    if order == "YM":
        return sep.join([y, mo])
    if order == "MY":
        return sep.join([mo, y])
    return str(value or "")
