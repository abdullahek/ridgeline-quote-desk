"""Plain-text e-mail extraction, including forwarded/replied chains.

Item lines are recognised structurally (bullets, or the block that follows an
introductory line ending in ':'), not by matching known products. Every other
non-empty body line is kept in ``non_item_rows`` so nothing disappears silently.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

from ..normalize import is_bullet, normalize_text, parse_item_text, strip_bullet
from ..schemas import ExtractedLine, Flag

_HEADER_RE = re.compile(r"^(From|To|Cc|Date|Sent|Subject):\s*(.*)$", re.I)
_SEPARATOR_RE = re.compile(
    r"^\s*(?:-{3,}\s*(?:Forwarded|Original)\s+message\s*-{3,}|-{3,}\s*Original Message\s*-{3,}"
    r"|On .+ wrote:)\s*$",
    re.I,
)
_EMAIL_RE = re.compile(r"[\w.+-]+@([\w-]+(?:\.[\w-]+)+)")
_NON_ITEM_RE = re.compile(
    r"^(?:hi|hello|hey|thanks|thank you|thx|regards|best|cheers|sent from my)\b", re.I
)
_CHANGE_VERB_RE = re.compile(
    r"\b(make|change|bump|add|remove|drop|cancel|increase|decrease|reduce|swap|replace|switch)\b",
    re.I,
)
_SPEC_REV_RE = re.compile(
    r"\b(?:make|change|switch)\s+the\s+(?P<item>[a-z][a-z -]*?)\s+(?P<new>\d+(?:\.\d+)?)\s*"
    r"(?P<unit>[a-z]+)\s+(?:not|instead\s+of)\s+(?P<old>\d+(?:\.\d+)?)\b",
    re.I,
)
_QTY_REV_RE = re.compile(
    r"\b(?:bump|change|increase|decrease|reduce|raise|make)\s+the\s+(?P<item>[a-z][a-z -]*?)\s+"
    r"(?:qty\s+|quantity\s+)?(?:up\s+|down\s+)?to\s+(?P<qty>\d+(?:\.\d+)?)\b",
    re.I,
)
_KEEP_RE = re.compile(
    r"^(?:the\s+)?(?P<item>[a-z][a-z -]*?)\s+(?:is|are)\s+(?:fine|ok|okay|good)\b", re.I
)


@dataclass
class Message:
    headers: dict[str, str] = field(default_factory=dict)
    lines: list[tuple[int, str]] = field(default_factory=list)  # (1-based file line, text)

    @property
    def sender(self) -> str:
        return self.headers.get("from", "unknown sender")


def split_messages(text: str) -> list[Message]:
    """Split a raw e-mail into messages, newest first (as they appear)."""
    messages = [Message()]
    in_headers = True
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.rstrip()
        if _SEPARATOR_RE.match(line):
            messages.append(Message())
            in_headers = True
            continue
        header = _HEADER_RE.match(line)
        if in_headers and header:
            messages[-1].headers[header.group(1).lower()] = header.group(2).strip()
            continue
        if in_headers and not line.strip():
            continue
        in_headers = False
        messages[-1].lines.append((number, line))
    return [m for m in messages if m.headers or any(t.strip() for _, t in m.lines)]


def sender_domains(messages: list[Message]) -> list[str]:
    domains = []
    for m in messages:
        found = _EMAIL_RE.search(m.headers.get("from", ""))
        if found:
            domains.append(found.group(1))
    return domains


def _item_line_numbers(msg: Message) -> set[int]:
    """Bullets if there are any; otherwise the block after a line ending with ':'."""
    bullets = {n for n, t in msg.lines if is_bullet(t)}
    if bullets:
        return bullets
    block, collecting = set(), False
    for n, t in msg.lines:
        stripped = t.strip()
        if collecting:
            if not stripped or _NON_ITEM_RE.match(stripped):
                if block:
                    break
                continue
            block.add(n)
        elif stripped.endswith(":"):
            collecting = True
    return block


def _singular(word: str) -> str:
    return word[:-1] if word.endswith("s") and not word.endswith("ss") else word


def _find_target(lines: list[ExtractedLine], phrase: str) -> Optional[ExtractedLine]:
    key = _singular(phrase.strip().split()[-1].lower())
    hits = [ln for ln in lines
            if re.search(rf"\b{re.escape(key)}s?\b", (ln.description or ln.as_written), re.I)]
    return hits[0] if len(hits) == 1 else None


def _apply_revisions(lines: list[ExtractedLine], later: list[Message]) -> list[str]:
    """Apply explicit revisions from later messages (oldest first). Returns unresolved text."""
    unresolved = []
    for msg in later:
        who = msg.sender.split("<")[0].strip() or msg.sender
        prose = " ".join(t.strip() for _, t in msg.lines if t.strip())
        for clause in re.split(r"[.;!?]\s+|,\s+|\s+and\s+(?=[a-z]+\s+the\b)", prose, flags=re.I):
            clause = re.sub(r"^\s*(?:and|also|but)\s+", "", clause.strip(), flags=re.I)
            clause = re.sub(r"^[A-Z][a-z]+\s+-\s+", "", clause)  # "Nina - make the ..."
            if not clause:
                continue
            spec, qty, keep = (_SPEC_REV_RE.search(clause), _QTY_REV_RE.search(clause),
                               _KEEP_RE.search(clause))
            match = spec or qty or keep
            if not match:
                if _CHANGE_VERB_RE.search(clause):
                    unresolved.append(f"{who}: '{clause}'")
                continue
            target = _find_target(lines, match.group("item"))
            if target is None:
                unresolved.append(f"{who}: '{clause}' (no single matching line)")
                continue
            if keep and not (spec or qty):
                target.note = _join(target.note, f"Confirmed unchanged by {who}: '{clause}'")
                continue
            if spec:
                old, new, unit = spec.group("old"), spec.group("new"), spec.group("unit")
                pattern = rf"\b{re.escape(old)}\s*{re.escape(unit)}\b"
                desc = target.description or ""
                if not re.search(pattern, desc, re.I):
                    target.flags.append(Flag.UNRESOLVED_CONFLICT)
                    target.note = _join(target.note, f"Unapplied revision by {who}: '{clause}'")
                    continue
                target.description = re.sub(pattern, f"{new} {unit.upper()}", desc, count=1, flags=re.I)
            else:
                new_qty = float(qty.group("qty"))
                target.note = _join(target.note, f"quantity revised from {_fmt(target.quantity)} to {_fmt(new_qty)}")
                target.quantity = new_qty
                if Flag.MISSING_QUANTITY in target.flags:
                    target.flags.remove(Flag.MISSING_QUANTITY)
            if Flag.REVISED_IN_THREAD not in target.flags:
                target.flags.append(Flag.REVISED_IN_THREAD)
            target.note = _join(target.note, f"Revised by {who}: '{clause}'")
    return unresolved


def _fmt(q: Optional[float]) -> str:
    return "none" if q is None else (str(int(q)) if q == int(q) else str(q))


def _join(a: Optional[str], b: str) -> str:
    return f"{a}; {b}" if a else b


def extract_email(text: str) -> tuple[list[ExtractedLine], list[str], list[str], list[str]]:
    """Return (lines, non_item_rows, sender_domains, unresolved_revisions)."""
    messages = split_messages(text)
    chronological = list(reversed(messages))
    lines: list[ExtractedLine] = []
    non_items: list[tuple[int, str]] = []
    item_msg_index = None
    for idx, msg in enumerate(chronological):
        item_numbers = _item_line_numbers(msg)
        if item_numbers:
            item_msg_index = idx
        for number, raw in msg.lines:
            text_line = normalize_text(raw)
            if not text_line:
                continue
            if number not in item_numbers:
                non_items.append((number, text_line))
                continue
            parsed = parse_item_text(raw)
            lines.append(ExtractedLine(
                line=len(lines) + 1,
                as_written=strip_bullet(text_line),
                quantity=parsed.quantity,
                unit=parsed.unit,
                part_number=parsed.part_number,
                description=parsed.description,
                source_ref=f"text line {number}",
                flags=parsed.flags,
                note="; ".join(parsed.notes) or None,
            ))

    unresolved: list[str] = []
    if lines and item_msg_index is not None:
        unresolved = _apply_revisions(lines, chronological[item_msg_index + 1:])
        if unresolved:
            for ln in lines:
                if Flag.UNRESOLVED_CONFLICT not in ln.flags:
                    ln.flags.append(Flag.UNRESOLVED_CONFLICT)
    ordered_non_items = [t for _, t in sorted(non_items)]  # file order, not message order
    return lines, ordered_non_items, sender_domains(messages), unresolved
