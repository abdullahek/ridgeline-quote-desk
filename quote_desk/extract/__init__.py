"""Format dispatch for RFQ extraction. Never raises: failures become flagged lines."""

from __future__ import annotations

import logging
from pathlib import Path

from ..reference import identify_customer, load_customers
from ..schemas import ExtractedLine, ExtractedRFQ, Flag

log = logging.getLogger(__name__)

FORMATS = {
    ".txt": "email", ".eml": "email",
    ".pdf": "pdf",
    ".xlsx": "xlsx", ".xlsm": "xlsx",
    ".png": "image", ".jpg": "image", ".jpeg": "image", ".tif": "image", ".tiff": "image",
}

IMAGE_STUB_NOTE = (
    "Handwritten/photographed RFQ: handwriting recognition is not implemented in this "
    "deterministic prototype, so no line items, quantities or customer were extracted. "
    "A person must transcribe this request before it can be quoted."
)


def _placeholder(as_written: str, flag: Flag, note: str, ref: str) -> ExtractedLine:
    return ExtractedLine(line=1, as_written=as_written, source_ref=ref, flags=[flag], note=note)


def extract_rfq(path: str | Path) -> ExtractedRFQ:
    path = Path(path)
    rfq_id = path.stem
    fmt = FORMATS.get(path.suffix.lower())
    base = dict(rfq_id=rfq_id, source_file=path.as_posix(), source_format=fmt or "email")

    if fmt == "image":
        return ExtractedRFQ(**base, lines=[_placeholder(
            f"[image file {path.name}: handwritten request, not transcribed]",
            Flag.STUB, IMAGE_STUB_NOTE, "whole image")])

    try:
        if fmt is None:
            raise ValueError(f"unsupported file type '{path.suffix}'")
        if fmt == "email":
            from .email import extract_email
            text = path.read_text(encoding="utf-8", errors="replace")
            lines, non_items, domains, unresolved = extract_email(text)
            errors = [f"unresolved revision: {u}" for u in unresolved]
        elif fmt == "pdf":
            from .tabular import extract_pdf
            lines, non_items, errors, text = extract_pdf(path)
            domains = []
        else:
            from .tabular import extract_xlsx
            lines, non_items, errors, text = extract_xlsx(path)
            domains = []
    except Exception as exc:  # corrupt/unsupported file: keep the RFQ visible for review
        log.exception("extraction failed for %s", path)
        return ExtractedRFQ(**base, errors=[f"{type(exc).__name__}: {exc}"], lines=[_placeholder(
            f"[extraction failed for {path.name}]", Flag.EXTRACTION_ERROR,
            f"Automatic extraction failed ({type(exc).__name__}: {exc}); manual entry required.",
            "whole file")])

    if not lines:
        errors.append("no line items recognised")
        lines = [_placeholder(f"[no line items recognised in {path.name}]", Flag.EXTRACTION_ERROR,
                              "No item rows were recognised; manual review required.", "whole file")]

    customer, evidence = identify_customer(text, domains, load_customers())
    if customer is None:
        errors.append(f"customer not identified: {evidence}")
    return ExtractedRFQ(
        **base, lines=lines, non_item_rows=non_items, errors=errors,
        customer_id=customer.customer_id if customer else None,
        customer_name=customer.name if customer else None,
    )
