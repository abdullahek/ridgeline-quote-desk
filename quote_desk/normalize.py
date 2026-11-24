"""Text, unit, quantity and identifier normalisation.

Rules of thumb:
* Never change product specifications (sizes, grades, materials stay verbatim).
* Never invent a quantity or unit: if it is not stated, it stays ``None`` and is flagged.
* Everything that is uncertain is kept as a flag or a note, not resolved silently.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

from .schemas import Flag

# Canonical units follow the catalog's UOM codes where one exists.
UNIT_ALIASES = {
    "ea": "EA", "each": "EA", "pc": "EA", "pcs": "EA", "piece": "EA", "pieces": "EA",
    "bx": "BX", "box": "BX", "boxes": "BX",
    "cs": "CS", "case": "CS", "cases": "CS",
    "pr": "PR", "pair": "PR", "pairs": "PR",
    "pk": "PK", "pack": "PK", "packs": "PK", "pkg": "PK",
    "rl": "RL", "roll": "RL", "rolls": "RL",
    "ft": "FT", "feet": "FT", "foot": "FT",
    "pail": "PAIL", "pails": "PAIL",
}
_UNIT_WORDS = "|".join(sorted(UNIT_ALIASES, key=len, reverse=True))
_VAGUE_QTY = r"a\s+few|few|several|some|a\s+couple(?:\s+of)?|couple(?:\s+of)?"

# Ridgeline numbers, and competitor numbers shaped like LETTERS-<alnum with a digit>.
PART_RE = re.compile(
    r"\b(RL\d{6}|[A-Z]{2,4}-(?=[A-Z0-9-]*\d)[A-Z0-9]+(?:-[A-Z0-9]+)*|MRD\d{6})\b"
)
RL_RE = re.compile(r"^RL\d{6}$")

_BULLET_RE = re.compile(r"^\s*(?:[-*•‣◦]|\d{1,2}[.)])\s+")
_PRIOR_ORDER_RE = re.compile(
    r"\b(?:as|same\s+as|like)\s+(?:the\s+|our\s+)?(?:last|previous|prior)\s+(?:order|time)\b"
    r"|\bthe\s+usual\b",
    re.I,
)


def normalize_text(text: str) -> str:
    """Unify quotes/dashes and whitespace without touching the specification itself."""
    text = (text.replace("“", '"').replace("”", '"').replace("″", '"')
                .replace("‘", "'").replace("’", "'")
                .replace("–", "-").replace("—", "-").replace("\xa0", " "))
    return re.sub(r"\s+", " ", text).strip()


def normalize_unit(raw: Optional[str]) -> Optional[str]:
    if raw is None:
        return None
    key = normalize_text(str(raw)).lower().rstrip(".")
    if not key:
        return None
    return UNIT_ALIASES.get(key, key.upper())


def is_bullet(line: str) -> bool:
    return bool(_BULLET_RE.match(line))


def strip_bullet(line: str) -> str:
    return _BULLET_RE.sub("", line, count=1).strip()


def find_part_numbers(text: str) -> list[str]:
    return PART_RE.findall(text)


def references_prior_order(text: str) -> bool:
    return bool(_PRIOR_ORDER_RE.search(text))


def to_number(value) -> Optional[float]:
    """Spreadsheet/PDF cell -> positive number, or None if absent/unparseable."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) if value > 0 else None
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*", str(value))
    return float(m.group(1)) if m and float(m.group(1)) > 0 else None


@dataclass
class ParsedItem:
    quantity: Optional[float] = None
    unit: Optional[str] = None
    part_number: Optional[str] = None
    description: Optional[str] = None
    flags: list[Flag] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def parse_item_text(raw: str) -> ParsedItem:
    """Pull quantity, unit and identifier out of one free-text request line.

    Patterns are tried from most to least explicit; the first hit wins, so a size
    such as ``1/2-13 x 2"`` is never mistaken for a quantity.
    """
    item = ParsedItem()
    s = normalize_text(strip_bullet(raw))

    # A trailing parenthetical is customer commentary: keep it as a note.
    paren = re.search(r"\(([^()]*)\)\s*$", s)
    if paren:
        item.notes.append(paren.group(1).strip())
        s = s[: paren.start()].rstrip()

    qty: Optional[float] = None
    unit: Optional[str] = None
    vague = False
    patterns = [
        # "... - qty 20", "qty: 24"
        (rf"[-,]?\s*\bqty\.?\s*:?\s*(?P<q>\d+(?:\.\d+)?)\s*(?P<u>{_UNIT_WORDS})?\b", "q"),
        # "..., 2 boxes", "... - 3 cases", "... - a few boxes"
        (rf"[,\-]\s*(?P<q>\d+(?:\.\d+)?|{_VAGUE_QTY})\s+(?P<u>{_UNIT_WORDS})\s*$", "q"),
        # "... - 300"
        (r"\s-\s*(?P<q>\d+(?:\.\d+)?)\s*$", "q"),
        # "RL159429 x 3", "tires 18x7-8 x4"
        (rf"\s[x×]\s*(?P<q>\d+(?:\.\d+)?)\s*(?P<u>{_UNIT_WORDS})?\s*$", "q"),
        # "20 ea 6204-2RS bearings", "10 A42 v-belts"
        (rf"^(?P<q>\d{{1,3}}(?:\.\d+)?)\s+(?:(?P<u>{_UNIT_WORDS})\s+)?(?![\"/x\-]|hp\b|in\b|mm\b)", "q"),
    ]
    for pattern, _ in patterns:
        m = re.search(pattern, s, re.I)
        if not m:
            continue
        q = m.group("q")
        if re.fullmatch(r"\d+(?:\.\d+)?", q):
            qty = float(q)
        else:
            vague = True
        unit = normalize_unit(m.groupdict().get("u"))
        s = (s[: m.start()] + " " + s[m.end():]).strip()
        break

    if qty is None:
        item.flags.append(Flag.MISSING_QUANTITY)
        if vague:
            item.notes.append("quantity stated only vaguely")
    elif unit is None:
        item.flags.append(Flag.MISSING_UNIT)
        if any(re.search(rf"\b(?:{_UNIT_WORDS})\b", n, re.I) for n in item.notes):
            # e.g. "300 (we usually get boxes)": count of pieces or of boxes is unclear.
            item.flags.append(Flag.AMBIGUOUS_QUANTITY)

    parts = find_part_numbers(s)
    if parts:
        item.part_number = parts[0]
        s = s.replace(parts[0], " ", 1)

    if references_prior_order(raw):
        item.flags.append(Flag.REFERENCES_PRIOR_ORDER)

    desc = normalize_text(s).strip(" ,;:-")
    item.quantity, item.unit, item.description = qty, unit, desc or None
    return item
