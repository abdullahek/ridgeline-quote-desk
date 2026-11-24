"""Evaluation-logic tests on synthetic data (they never read answer_key/)."""

import importlib.util
from pathlib import Path

from quote_desk.schemas import Flag, OutputLine, RFQOutput

_spec = importlib.util.spec_from_file_location(
    "evaluate", Path(__file__).resolve().parents[1] / "evaluation" / "evaluate.py")
ev = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ev)


def _out(*lines):
    return RFQOutput(rfq_id="rfq_99_test", customer_id="C1", lines=list(lines))


def _ln(n, text, part=None, conf=None, qty=1, unit="EA", flags=()):
    return OutputLine(line=n, as_written=text, quantity=qty, unit=unit, ridgeline_part_number=part,
                      match_method="description" if part else None, confidence=conf,
                      needs_review=part is None or bool(flags), flags=list(flags))


KEY = {"rfq_99_test": {"customer_id": "C1", "lines": [
    {"line": 1, "as_written": "6204-2RS bearings", "quantity": 1, "unit": "EA", "expected_part_number": "RL000001"},
    {"line": 2, "as_written": "A42 belts", "quantity": 1, "unit": "EA", "expected_part_number": "RL000002"},
    {"line": 3, "as_written": "test kit", "quantity": 1, "unit": "EA", "expected_part_number": None},
    {"line": 4, "as_written": "grease", "quantity": 1, "unit": "EA", "expected_part_number": "RL000004"},
]}}


def test_metrics_count_wrong_missing_extra_and_abstentions():
    preds = {"rfq_99_test": _out(
        _ln(1, "6204-2RS bearings", "RL000001", 0.9),          # correct
        _ln(2, "A42 belts", "RL000009", 0.9),                  # wrong part
        _ln(3, "test kit", flags=[Flag.NO_MATCH]),             # correct abstention
        _ln(4, "unrelated extra row", "RL000005", 0.95),       # extra line; 'grease' is missing
    )}
    r = ev.evaluate(KEY, preds)
    assert (r["ext"]["missing"], r["ext"]["extra"]) == (1, 1)
    assert r["denom"] == 5 and r["ext"]["both_ok"] == 3
    m = ev.match_metrics(r["rows"], 0.8)
    assert (m["tp"], m["fp"], m["fn"]) == (1, 2, 2)
    assert m["correct_abstentions"] == 1 and m["abstention_opportunities"] == 1
    assert m["precision"] == round(1 / 3, 4) and m["recall"] == round(1 / 3, 4)


def test_threshold_turns_low_confidence_into_abstention():
    preds = {"rfq_99_test": _out(_ln(1, "6204-2RS bearings", "RL000001", 0.9),
                                 _ln(2, "A42 belts", "RL000009", 0.6),
                                 _ln(3, "test kit"), _ln(4, "grease", "RL000004", 0.85))}
    rows = ev.evaluate(KEY, preds)["rows"]
    low, high = ev.match_metrics(rows, 0.5), ev.match_metrics(rows, 0.8)
    assert low["precision"] < 1.0 and high["precision"] == 1.0 and high["recall"] < 1.0


def test_threshold_selection_prefers_precision_then_configured_value():
    sweep = [dict(threshold=0.5, precision=0.9, recall=1.0), dict(threshold=0.8, precision=1.0, recall=0.9),
             dict(threshold=0.85, precision=1.0, recall=0.9), dict(threshold=0.9, precision=1.0, recall=0.5)]
    assert ev.select_threshold(sweep, 0.80)["threshold"] == 0.8
    assert ev.select_threshold(sweep, 0.70)["threshold"] == 0.85
