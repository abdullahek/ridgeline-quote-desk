"""PDF (PyMuPDF) and Excel (OpenPyXL) table extraction.

Both formats are reduced to the same shape - a header row plus data rows of
cells - and then interpreted by one function, so a new column layout only needs
the header vocabulary below to recognise it.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

from ..normalize import find_part_numbers, normalize_text, normalize_unit, parse_item_text, to_number
from ..schemas import ExtractedLine, Flag

COLUMN_ROLES = {
    "line": ("line", "ln", "no", "#"),
    "part": ("item #", "item no", "part", "part #", "part no", "supplier part #", "sku", "mfr #"),
    "description": ("desc", "desc.", "description", "item description"),
    "quantity": ("qty", "qty.", "quantity"),
    "unit": ("uom", "unit", "units", "u/m"),
    "notes": ("notes", "note", "comments", "remarks"),
}
_SAME_AS_RE = re.compile(r"\bsame\s+(?:as\s+above|spec\s+as\s+line\s+\d+)\b", re.I)
_LINE_REF_RE = re.compile(r"\bline\s+(\d+)\b", re.I)
_SIZE_RE = re.compile(r'\d+(?:-\d+)?(?:/\d+)?\s*(?:"|in\b|inch\b)')


def _role(header: str) -> Optional[str]:
    h = normalize_text(header).lower()
    for role, names in COLUMN_ROLES.items():
        if h in names:
            return role
    for role, names in COLUMN_ROLES.items():
        if any(n in h for n in names if len(n) > 2):
            return role
    return None


def _header_roles(cells: list[str]) -> dict[str, int]:
    roles: dict[str, int] = {}
    for i, cell in enumerate(cells):
        role = _role(cell) if cell else None
        if role and role not in roles:
            roles[role] = i
    return roles


def is_header(cells: list[str]) -> bool:
    roles = _header_roles(cells)
    return "quantity" in roles and ("description" in roles or "part" in roles)


def _cell(row: list, i: Optional[int]):
    return row[i] if i is not None and i < len(row) else None


def _text(value) -> str:
    return normalize_text("" if value is None else str(value))


def interpret_rows(
    rows: list[tuple[str, list]], source_label: str
) -> tuple[list[ExtractedLine], list[str], list[str]]:
    """rows: (source_ref, cells). Returns (lines, non_item_rows, errors)."""
    header_at = next((i for i, (_, cells) in enumerate(rows)
                      if is_header([_text(c) for c in cells])), None)
    if header_at is None:
        return [], [_text(" ".join(_text(c) for c in cells)) for _, cells in rows if any(cells)], [
            f"{source_label}: no table header with quantity and description/part columns found"]

    roles = _header_roles([_text(c) for c in rows[header_at][1]])
    non_items = [" | ".join(_text(c) for c in cells if _text(c))
                 for _, cells in rows[: header_at + 1] if any(_text(c) for c in cells)]
    lines: list[ExtractedLine] = []

    for ref, cells in rows[header_at + 1:]:
        values = [_text(c) for c in cells]
        if not any(values):
            continue
        part = _text(_cell(cells, roles.get("part"))) or None
        desc = _text(_cell(cells, roles.get("description"))) or None
        qty_raw = _cell(cells, roles.get("quantity"))
        unit_raw = _cell(cells, roles.get("unit"))
        notes = _text(_cell(cells, roles.get("notes"))) or None
        quantity = to_number(qty_raw)

        # A row is an item only if it carries a quantity or a recognisable identifier;
        # when the table numbers its lines, the line cell must be a number too.
        line_label = _text(_cell(cells, roles.get("line")))
        looks_like_item = (quantity is not None or bool(_text(qty_raw))
                           or bool(part and find_part_numbers(part)))
        if "line" in roles and not re.fullmatch(r"\d+", line_label):
            looks_like_item = False
        if not looks_like_item:
            non_items.append(" | ".join(v for v in values if v))
            continue

        flags: list[Flag] = []
        note_parts = [f"customer note: {notes}"] if notes else []
        if quantity is None:
            flags.append(Flag.MISSING_QUANTITY)
            if _text(qty_raw):
                note_parts.append(f"unparsed quantity '{_text(qty_raw)}'")
        unit = normalize_unit(_text(unit_raw)) if _text(unit_raw) else None
        if unit is None and quantity is not None:
            flags.append(Flag.MISSING_UNIT)
        if part and not find_part_numbers(part):
            note_parts.append(f"unrecognised identifier format '{part}'")

        # "same as above but 1/2"": inherit the referenced line, substitute the stated size.
        if desc and _SAME_AS_RE.search(" ".join(filter(None, [desc, notes]))):
            ref_no = _LINE_REF_RE.search(notes or "")
            base = (lines[int(ref_no.group(1)) - 1] if ref_no and 0 < int(ref_no.group(1)) <= len(lines)
                    else (lines[-1] if lines else None))
            new_size = _SIZE_RE.search(desc.split("but", 1)[-1]) if "but" in desc.lower() else None
            old_size = _SIZE_RE.search(base.description or "") if base else None
            flags.append(Flag.INHERITED_FROM_LINE)
            if base and new_size and old_size:
                desc = (base.description[: old_size.start()] + new_size.group(0).replace(" ", "")
                        + base.description[old_size.end():])
                note_parts.append(f"inherits line {base.line} ('{base.description}') with size "
                                  f"{new_size.group(0)}; its part number is not reused")
            else:
                flags.append(Flag.UNRESOLVED_CONFLICT)
                note_parts.append("refers to another line but the change could not be resolved")

        as_written = " | ".join(v for i, v in enumerate(values)
                                if v and i != roles.get("line"))
        line = ExtractedLine(
            line=len(lines) + 1, as_written=as_written, quantity=quantity, unit=unit,
            part_number=part, description=desc, source_ref=ref, flags=flags,
            note="; ".join(note_parts) or None,
        )
        lines.append(line)
    return lines, non_items, []


# --------------------------------------------------------------------------
# Excel
# --------------------------------------------------------------------------

def extract_xlsx(path: Path) -> tuple[list[ExtractedLine], list[str], list[str], str]:
    import openpyxl

    wb = openpyxl.load_workbook(path, data_only=True, read_only=True)
    all_lines, non_items, errors, texts = [], [], [], []
    for ws in wb.worksheets:
        rows = [(f"sheet '{ws.title}' row {r}", list(cells))
                for r, cells in enumerate(ws.iter_rows(values_only=True), 1)]
        texts.extend(" ".join(_text(c) for c in cells) for _, cells in rows)
        lines, nis, errs = interpret_rows(rows, f"sheet '{ws.title}'")
        for ln in lines:
            all_lines.append(ln.model_copy(update={"line": len(all_lines) + 1}))
        non_items.extend(nis)
        errors.extend(errs)
    wb.close()
    return all_lines, non_items, errors, "\n".join(texts)


# --------------------------------------------------------------------------
# PDF
# --------------------------------------------------------------------------

def _pdf_rows(page) -> list[tuple[str, list[str]]]:
    """Group words into visual rows; split cells at header column boundaries."""
    words = sorted(page.get_text("words"), key=lambda w: (round(w[1]), w[0]))
    visual: list[list] = []
    for w in words:
        if visual and abs(visual[-1][0][1] - w[1]) <= 3:
            visual[-1].append(w)
        else:
            visual.append([w])

    def cells_by_gap(row, gap=15.0):
        cells, starts = [], []
        for w in sorted(row, key=lambda w: w[0]):
            if cells and w[0] - cells[-1][-1][2] <= gap:
                cells[-1].append(w)
            else:
                cells.append([w])
                starts.append(w[0])
        return [" ".join(x[4] for x in c) for c in cells], starts

    out: list[tuple[str, list[str]]] = []
    column_starts: Optional[list[float]] = None
    for row in visual:
        y = round(row[0][1])
        texts, starts = cells_by_gap(row)
        if column_starts is None and is_header(texts):
            column_starts = starts
            out.append((f"page {page.number + 1} y={y}", texts))
            continue
        if column_starts is None:
            out.append((f"page {page.number + 1} y={y}", [" ".join(texts)]))
            continue
        cells = [""] * len(column_starts)
        for w in sorted(row, key=lambda w: w[0]):
            idx = max([i for i, s in enumerate(column_starts) if w[0] >= s - 4] or [0])
            cells[idx] = (cells[idx] + " " + w[4]).strip()
        out.append((f"page {page.number + 1} y={y}", cells))
    return out


def extract_pdf(path: Path) -> tuple[list[ExtractedLine], list[str], list[str], str]:
    import pymupdf

    lines, non_items, errors, texts = [], [], [], []
    with pymupdf.open(path) as doc:
        for page in doc:
            texts.append(page.get_text())
            rows = _pdf_rows(page)
            if not rows:
                errors.append(f"page {page.number + 1}: no text layer (scanned PDF needs OCR)")
                continue
            page_lines, nis, errs = interpret_rows(rows, f"page {page.number + 1}")
            if errs and not page_lines:
                # No table: fall back to free-text parsing of the page.
                for ref, cells in rows:
                    t = _text(" ".join(cells))
                    p = parse_item_text(t)
                    if p.quantity is not None and (p.part_number or p.description):
                        page_lines.append(ExtractedLine(
                            line=1, as_written=t, quantity=p.quantity, unit=p.unit,
                            part_number=p.part_number, description=p.description,
                            source_ref=ref, flags=p.flags, note="; ".join(p.notes) or None))
                    else:
                        nis.append(t)
                errs = [e + "; used free-text fallback" for e in errs]
            for ln in page_lines:
                lines.append(ln.model_copy(update={"line": len(lines) + 1}))
            non_items.extend(nis)
            errors.extend(errs)
    return lines, non_items, errors, "\n".join(texts)
