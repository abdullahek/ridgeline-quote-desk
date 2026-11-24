"""End-to-end pipeline: extract -> match -> score -> review decision -> outputs/*.json.

    python -m quote_desk.pipeline [--rfqs rfqs] [--out outputs] [--threshold 0.80]
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from . import config
from .extract import FORMATS, extract_rfq
from .matcher import Matcher
from .schemas import ExtractedLine, Flag, OutputLine, RFQOutput

# Any of these sends a line to review regardless of its score.
REVIEW_FLAGS = frozenset({
    Flag.STUB, Flag.EXTRACTION_ERROR, Flag.NO_MATCH, Flag.AMBIGUOUS_MATCH,
    Flag.AMBIGUOUS_QUANTITY, Flag.MISSING_QUANTITY, Flag.UNIT_MISMATCH, Flag.SUBSTITUTION,
    Flag.DISCONTINUED_REPLACED, Flag.CROSS_REF_NOTE, Flag.CROSS_REF_CONFLICT, Flag.PART_NOT_FOUND,
    Flag.REFERENCES_PRIOR_ORDER, Flag.UNRESOLVED_CONFLICT, Flag.LOW_CONFIDENCE,
})


def build_line(line: ExtractedLine, matcher: Matcher, threshold: float) -> OutputLine:
    m = matcher.match(line)
    flags = list(dict.fromkeys(line.flags + m.flags))  # de-duplicate, keep order
    if m.confidence is not None and m.confidence < threshold and Flag.LOW_CONFIDENCE not in flags:
        flags.append(Flag.LOW_CONFIDENCE)
    needs_review = m.part_number is None or any(f in REVIEW_FLAGS for f in flags)
    note_parts = [p for p in (line.note, "; ".join(m.reasons)) if p]
    return OutputLine(
        line=line.line, as_written=line.as_written,
        quantity=_clean_number(line.quantity), unit=line.unit,
        ridgeline_part_number=m.part_number, match_method=m.method, confidence=m.confidence,
        needs_review=needs_review, flags=flags, note=" | ".join(note_parts) or None,
    )


def process_file(path: Path, matcher: Matcher, threshold: float) -> RFQOutput:
    extracted = extract_rfq(path)
    return RFQOutput(
        rfq_id=extracted.rfq_id, customer_id=extracted.customer_id,
        customer_name=extracted.customer_name,
        lines=[build_line(ln, matcher, threshold) for ln in extracted.lines],
        non_item_rows=extracted.non_item_rows,
    )


def write_output(output: RFQOutput, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / f"{output.rfq_id}.json"
    data = output.to_json_dict()
    for ln in data["lines"]:
        ln["quantity"] = _clean_number(ln["quantity"])
    target.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return target


def _clean_number(q):
    return int(q) if isinstance(q, float) and q.is_integer() else q


def run(rfq_dir: Path, out_dir: Path, threshold: float, quiet: bool = False) -> list[dict]:
    matcher = Matcher()
    summary = []
    files = sorted(p for p in rfq_dir.iterdir() if p.suffix.lower() in FORMATS)
    for path in files:
        start = time.perf_counter()
        output = process_file(path, matcher, threshold)
        write_output(output, out_dir)
        ms = (time.perf_counter() - start) * 1000
        lines = output.lines
        summary.append({
            "rfq_id": output.rfq_id, "customer_id": output.customer_id, "lines": len(lines),
            "matched": sum(1 for ln in lines if ln.ridgeline_part_number),
            "auto_ok": sum(1 for ln in lines if not ln.needs_review),
            "review": sum(1 for ln in lines if ln.needs_review), "ms": round(ms, 1),
        })
        if not quiet:
            _print_rfq(output, ms)
    if not quiet:
        _print_summary(summary, threshold, out_dir)
    return summary


def _print_rfq(output: RFQOutput, ms: float) -> None:
    print(f"\n{output.rfq_id}  customer={output.customer_id or '-'} ({output.customer_name or 'unidentified'})  {ms:.0f} ms")
    for ln in output.lines:
        part = ln.ridgeline_part_number or "-- no match --"
        conf = f"{ln.confidence:.2f}" if ln.confidence is not None else "  - "
        status = "REVIEW" if ln.needs_review else "ok"
        flags = ",".join(f.value for f in ln.flags)
        print(f"  {ln.line:>2}. {part:<15} {(ln.match_method or ''):<22} {conf}  {status:<6} "
              f"{ln.as_written[:48]:<48} {flags}")


def _print_summary(summary: list[dict], threshold: float, out_dir: Path) -> None:
    tot = {k: sum(s[k] for s in summary) for k in ("lines", "matched", "auto_ok", "review", "ms")}
    print(f"\nthreshold={threshold:.2f}  RFQs={len(summary)}  lines={tot['lines']}  matched={tot['matched']}  "
          f"unmatched={tot['lines'] - tot['matched']}  auto-ok={tot['auto_ok']}  needs-review={tot['review']}  "
          f"total={tot['ms']:.0f} ms  avg={tot['ms'] / max(len(summary), 1):.0f} ms/RFQ")
    print(f"outputs written to {out_dir}/")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Ridgeline Quote Desk RFQ pipeline")
    ap.add_argument("--rfqs", type=Path, default=Path("rfqs"))
    ap.add_argument("--out", type=Path, default=Path("outputs"))
    ap.add_argument("--threshold", type=float, default=config.REVIEW_THRESHOLD)
    args = ap.parse_args(argv)
    if not 0 <= args.threshold <= 1:
        ap.error("--threshold must be between 0 and 1")
    run(args.rfqs, args.out, args.threshold)
    return 0


if __name__ == "__main__":
    sys.exit(main())
