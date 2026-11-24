"""Evaluation of the pipeline against answer_key/ (the only code allowed to read it).

    python evaluation/evaluate.py                    # metrics, threshold sweep, evaluation/results.json
    python evaluation/evaluate.py --describe-schema  # key names / types only, no values

The pipeline is run in memory on the labelled RFQs, so the numbers always reflect
the current code. Nothing here feeds back into the pipeline: the answer key is
used for scoring only.

Definitions
-----------
* Lines are aligned by text similarity of ``as_written`` (plus a small bonus for the
  same position); unaligned expected lines are *missing*, unaligned predicted lines *extra*.
* extraction_accuracy = aligned lines with correct quantity AND unit
                        / (expected lines + extra predicted lines)
* At threshold t a line *asserts* a part only if it has one and confidence >= t;
  otherwise it abstains (goes to a human).
  precision = correct assertions / all assertions
  recall    = correct assertions / expected lines that have a part number
  correct abstention = expected part is null and the line asserts nothing.
* review rate uses the pipeline's own rule (unmatched, below t, or a review flag).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from rapidfuzz import fuzz

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from quote_desk import config  # noqa: E402
from quote_desk.matcher import Matcher  # noqa: E402
from quote_desk.pipeline import REVIEW_FLAGS, process_file  # noqa: E402
from quote_desk.schemas import EvaluationResults, RFQOutput  # noqa: E402

ANSWER_DIR = ROOT / "answer_key"
RFQ_DIR = ROOT / "rfqs"
OUTPUT_DIR = ROOT / "outputs"
RESULTS_PATH = ROOT / "evaluation" / "results.json"
THRESHOLDS = (0.50, 0.60, 0.70, 0.75, 0.80, 0.85, 0.90, 0.95)
PRECISION_TARGET = 0.95
ALIGN_MIN = 0.50


def load_answer_keys() -> dict[str, dict]:
    return {p.stem: json.loads(p.read_text(encoding="utf-8")) for p in sorted(ANSWER_DIR.glob("*.json"))}


# --------------------------------------------------------------------------
# Schema description (structure only)
# --------------------------------------------------------------------------

def _shape(value):
    if isinstance(value, dict):
        return {k: _shape(v) for k, v in value.items()}
    if isinstance(value, list):
        inner = _shape(value[0]) if value and isinstance(value[0], dict) else sorted({type(v).__name__ for v in value})
        return {"list_len": len(value), "item": inner}
    return type(value).__name__


def describe_schema() -> None:
    for stem, key in load_answer_keys().items():
        print(stem, json.dumps(_shape(key), indent=1))


# --------------------------------------------------------------------------
# Alignment and scoring
# --------------------------------------------------------------------------

def _norm(s: str) -> str:
    return " ".join(str(s or "").lower().replace("|", " ").split())


def align(expected: list[dict], predicted: list) -> tuple[list[tuple[int, int]], list[int], list[int]]:
    pairs = []
    for i, e in enumerate(expected):
        for j, p in enumerate(predicted):
            sim = fuzz.token_set_ratio(_norm(e.get("as_written")), _norm(p.as_written)) / 100
            score = 0.85 * sim + (0.15 if i == j else 0.0)
            if score >= ALIGN_MIN:
                pairs.append((score, i, j))
    used_e, used_p, matched = set(), set(), []
    for _, i, j in sorted(pairs, reverse=True):
        if i not in used_e and j not in used_p:
            used_e.add(i), used_p.add(j)
            matched.append((i, j))
    missing = [i for i in range(len(expected)) if i not in used_e]
    extra = [j for j in range(len(predicted)) if j not in used_p]
    return sorted(matched), missing, extra


def _same_qty(a, b) -> bool:
    return a is not None and b is not None and float(a) == float(b)


def _same_unit(a, b) -> bool:
    return bool(a) and bool(b) and str(a).upper() == str(b).upper()


def _asserted(line, t: float):
    return line.ridgeline_part_number if (line.ridgeline_part_number and line.confidence is not None
                                          and line.confidence >= t) else None


def _needs_review(line, t: float) -> bool:
    return (line.ridgeline_part_number is None or line.confidence is None or line.confidence < t
            or any(f in REVIEW_FLAGS for f in line.flags if f.value != "low_confidence"))


def evaluate(keys: dict[str, dict], preds: dict[str, RFQOutput]) -> dict:
    ext = dict(expected=0, predicted=0, aligned=0, missing=0, extra=0, qty_ok=0, unit_ok=0, both_ok=0)
    customers_ok, per_rfq, rows = 0, {}, []
    for stem, key in keys.items():
        exp_lines, out = key["lines"], preds[stem]
        matched, missing, extra = align(exp_lines, out.lines)
        ext["expected"] += len(exp_lines)
        ext["predicted"] += len(out.lines)
        ext["aligned"] += len(matched)
        ext["missing"] += len(missing)
        ext["extra"] += len(extra)
        customers_ok += int(key.get("customer_id") == out.customer_id)
        both = 0
        for i, j in matched:
            e, p = exp_lines[i], out.lines[j]
            q, u = _same_qty(e.get("quantity"), p.quantity), _same_unit(e.get("unit"), p.unit)
            ext["qty_ok"] += q
            ext["unit_ok"] += u
            ext["both_ok"] += q and u
            both += q and u
            rows.append(dict(rfq=stem, exp_line=e.get("line"), line=p, expected=e.get("expected_part_number"),
                             exp_method=e.get("match_method"), qty_ok=q, unit_ok=u))
        for i in missing:
            e = exp_lines[i]
            rows.append(dict(rfq=stem, exp_line=e.get("line"), line=None, expected=e.get("expected_part_number"),
                             exp_method=e.get("match_method"), qty_ok=False, unit_ok=False))
        for j in extra:
            rows.append(dict(rfq=stem, exp_line=None, line=out.lines[j], expected=None, exp_method=None,
                             qty_ok=False, unit_ok=False, extra=True))
        per_rfq[stem] = dict(expected=len(exp_lines), predicted=len(out.lines), missing=len(missing),
                             extra=len(extra), qty_and_unit_correct=both)
    denom = ext["expected"] + ext["extra"]
    return dict(ext=ext, denom=denom, customers_ok=customers_ok, per_rfq=per_rfq, rows=rows)


def match_metrics(rows: list[dict], t: float) -> dict:
    tp = fp = fn = abst_ok = abst_total = review = predicted_lines = suggestion_ok = suggestions = 0
    expected_parts = sum(1 for r in rows if r["expected"])
    for r in rows:
        line, exp = r["line"], r["expected"]
        if line is not None:
            predicted_lines += 1
            review += _needs_review(line, t)
            if line.ridgeline_part_number:
                suggestions += 1
                suggestion_ok += line.ridgeline_part_number == exp
        got = _asserted(line, t) if line is not None else None
        if got:
            tp += got == exp
            fp += got != exp
        if exp and got != exp:
            fn += 1
        if not exp and not r.get("extra"):
            abst_total += 1
            abst_ok += got is None
    return dict(
        threshold=t, tp=tp, fp=fp, fn=fn,
        precision=round(tp / (tp + fp), 4) if tp + fp else 1.0,
        recall=round(tp / expected_parts, 4) if expected_parts else 1.0,
        correct_abstentions=abst_ok, abstention_opportunities=abst_total,
        review_rate=round(review / predicted_lines, 4) if predicted_lines else 0.0,
        suggestion_accuracy=round(suggestion_ok / suggestions, 4) if suggestions else 1.0,
    )


def select_threshold(sweep: list[dict], current: float) -> dict:
    """Highest recall with precision >= target; ties -> keep the configured value, else the stricter one."""
    ok = [s for s in sweep if s["precision"] >= PRECISION_TARGET] or sweep
    best_recall = max(s["recall"] for s in ok)
    tied = [s for s in ok if s["recall"] == best_recall]
    return next((s for s in tied if s["threshold"] == current), max(tied, key=lambda s: s["threshold"]))


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------

def _outputs_consistent(matcher: Matcher, threshold: float) -> tuple[bool, list[str]]:
    stale = []
    for path in sorted(RFQ_DIR.iterdir()):
        stored = OUTPUT_DIR / f"{path.stem}.json"
        if not stored.exists():
            stale.append(f"{stored.name} missing")
            continue
        fresh = process_file(path, matcher, threshold).to_json_dict()
        for ln in fresh["lines"]:
            if isinstance(ln["quantity"], float) and ln["quantity"].is_integer():
                ln["quantity"] = int(ln["quantity"])
        if json.loads(stored.read_text(encoding="utf-8")) != fresh:
            stale.append(f"{stored.name} differs from a fresh run at threshold {threshold}")
    return not stale, stale


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Evaluate the Ridgeline Quote Desk pipeline")
    ap.add_argument("--describe-schema", action="store_true")
    args = ap.parse_args(argv)
    if args.describe_schema:
        describe_schema()
        return 0

    keys = load_answer_keys()
    matcher = Matcher()
    configured = config.REVIEW_THRESHOLD
    preds = {stem: process_file(next(RFQ_DIR.glob(f"{stem}.*")), matcher, configured) for stem in keys}
    ev = evaluate(keys, preds)
    ext, denom = ev["ext"], ev["denom"]
    sweep = [match_metrics(ev["rows"], t) for t in THRESHOLDS]
    chosen = select_threshold(sweep, configured)
    at = chosen

    print(f"Labelled RFQs: {', '.join(keys)}  (expected lines {ext['expected']}, predicted {ext['predicted']})")
    print("\nExtraction")
    print(f"  aligned {ext['aligned']}  missing {ext['missing']}  extra {ext['extra']}  customers {ev['customers_ok']}/{len(keys)}")
    print(f"  quantity correct {ext['qty_ok']}/{denom}  unit correct {ext['unit_ok']}/{denom}  "
          f"both (extraction_accuracy) {ext['both_ok']}/{denom} = {ext['both_ok'] / denom:.3f}")

    print("\nThreshold sweep (assert a part only when confidence >= t)")
    print("  t     precision  recall  TP  FP  FN  abstain-ok  review-rate  suggestion-acc")
    for s in sweep:
        mark = "  <- selected" if s is chosen else ""
        print(f"  {s['threshold']:.2f}  {s['precision']:.3f}      {s['recall']:.3f}   {s['tp']:>2}  {s['fp']:>2}  "
              f"{s['fn']:>2}  {s['correct_abstentions']}/{s['abstention_opportunities']}         "
              f"{s['review_rate']:.3f}        {s['suggestion_accuracy']:.3f}{mark}")

    print("\nLine-level detail at the selected threshold")
    for r in ev["rows"]:
        line = r["line"]
        got = _asserted(line, at["threshold"]) if line else None
        status = ("correct" if got and got == r["expected"] else "WRONG" if got
                  else "abstain-ok" if not r["expected"] else "missed")
        conf = f"{line.confidence:.2f}" if line and line.confidence is not None else "  - "
        print(f"  {r['rfq']:<18} line {str(r['exp_line'] or '-'):>2}  {status:<10} conf {conf}  "
              f"qty {'ok' if r['qty_ok'] else 'x '}  unit {'ok' if r['unit_ok'] else 'x '}  "
              f"{(line.as_written[:46] if line else '(not extracted)')}")

    consistent, stale = _outputs_consistent(matcher, configured)
    print(f"\nSelected threshold {at['threshold']:.2f} (configured {configured:.2f}; precision target {PRECISION_TARGET})")
    if at["threshold"] != configured:
        print("  NOTE: selected threshold differs from quote_desk/config.py REVIEW_THRESHOLD")
    print(f"  outputs/ consistent with configured threshold: {consistent}" + ("" if consistent else f" {stale}"))

    results = EvaluationResults(
        threshold=at["threshold"],
        extraction_accuracy=round(ext["both_ok"] / denom, 4),
        match_precision=at["precision"],
        match_recall=at["recall"],
        additional_metrics={
            "labelled_rfqs": list(keys),
            "expected_lines": ext["expected"], "predicted_lines": ext["predicted"],
            "missing_lines": ext["missing"], "extra_lines": ext["extra"],
            "quantity_accuracy": round(ext["qty_ok"] / denom, 4),
            "unit_accuracy": round(ext["unit_ok"] / denom, 4),
            "customer_id_accuracy": round(ev["customers_ok"] / len(keys), 4),
            "true_positives": at["tp"], "false_positives": at["fp"], "false_negatives": at["fn"],
            "correct_abstentions": at["correct_abstentions"],
            "abstention_opportunities": at["abstention_opportunities"],
            "review_rate": at["review_rate"],
            "suggestion_accuracy_all_emitted": at["suggestion_accuracy"],
            "configured_threshold": configured,
            "outputs_consistent_with_config": consistent,
            "precision_target": PRECISION_TARGET,
            "threshold_sweep": sweep,
            "per_rfq_extraction": ev["per_rfq"],
            "definitions": {
                "extraction_accuracy": "aligned lines with correct quantity and unit / (expected + extra lines)",
                "asserted_match": "ridgeline_part_number present and confidence >= threshold",
                "match_precision": "correct asserted matches / asserted matches",
                "match_recall": "correct asserted matches / expected lines with a part number",
                "review_rate": "share of predicted lines the pipeline sends to human review",
            },
        },
    )
    RESULTS_PATH.write_text(json.dumps(results.model_dump(mode="json"), indent=2) + "\n", encoding="utf-8")
    print(f"\nwrote {RESULTS_PATH.relative_to(ROOT).as_posix()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
