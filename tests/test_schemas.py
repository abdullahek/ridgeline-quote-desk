import csv
from pathlib import Path

import pytest
from pydantic import ValidationError

import check_submission as checker  # imported read-only to validate against the official rules
from quote_desk.schemas import (
    CrossReference,
    Customer,
    EvaluationResults,
    ExtractedLine,
    ExtractedRFQ,
    Flag,
    OutputLine,
    Product,
    RFQOutput,
)

ROOT = Path(__file__).resolve().parents[1]


def _rows(name):
    with open(ROOT / name, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


# ---- reference data loads cleanly -------------------------------------------

def test_all_reference_rows_validate():
    products = [Product(**r) for r in _rows("products.csv")]
    xrefs = [CrossReference(**r) for r in _rows("cross_reference.csv")]
    customers = [Customer(**r) for r in _rows("customers.csv")]
    assert len(products) == 200 and len(customers) == 10 and xrefs
    discontinued = [p for p in products if p.status == "discontinued"]
    assert discontinued and all(p.replaced_by for p in discontinued)


def test_blank_csv_fields_become_none():
    x = CrossReference(competitor="A", competitor_part_number="X1",
                       ridgeline_part_number="RL100000", notes="", last_verified="")
    assert x.notes is None and x.last_verified is None


# ---- output lines -----------------------------------------------------------

def _matched(**kw):
    base = dict(line=1, as_written="20 ea 6204-2RS bearings", quantity=20, unit="EA",
                ridgeline_part_number="RL171482", match_method="description",
                confidence=0.93, needs_review=False, flags=[])
    base.update(kw)
    return OutputLine(**base)


def test_valid_matched_line():
    assert _matched().confidence == 0.93


@pytest.mark.parametrize("bad", [
    {"confidence": 1.2},
    {"confidence": -0.1},
    {"ridgeline_part_number": "6204-2RS"},          # not an RL number
    {"match_method": "guess"},
    {"match_method": None},                          # matched but no method
    {"confidence": None},                            # matched but no score
    {"quantity": 0},
    {"as_written": ""},
    {"flags": ["made_up_flag"]},
    {"price": 8.40},                                 # pricing must never be emitted
])
def test_invalid_lines_rejected(bad):
    with pytest.raises(ValidationError):
        _matched(**bad)


def test_unmatched_line_must_be_reviewed():
    with pytest.raises(ValidationError):
        OutputLine(line=1, as_written="forklift tires", needs_review=False)
    ok = OutputLine(line=1, as_written="forklift tires", needs_review=True,
                    flags=[Flag.NO_MATCH])
    assert ok.ridgeline_part_number is None and ok.quantity is None


def test_stub_requires_review_and_note():
    with pytest.raises(ValidationError):
        OutputLine(line=1, as_written="photo", needs_review=True, flags=[Flag.STUB])
    with pytest.raises(ValidationError):
        OutputLine(line=1, as_written="photo", needs_review=False, flags=[Flag.STUB], note="x")


# ---- RFQ-level --------------------------------------------------------------

def test_line_sequence_enforced():
    with pytest.raises(ValidationError):
        RFQOutput(rfq_id="rfq_01_email", customer_id=None,
                  lines=[_matched(line=1), _matched(line=3)])


def test_stubbed_rfq_single_line():
    stub = OutputLine(line=1, as_written="photo", needs_review=True,
                      flags=[Flag.STUB], note="handwritten image; OCR not implemented")
    with pytest.raises(ValidationError):
        RFQOutput(rfq_id="rfq_04_handwritten", customer_id=None,
                  lines=[stub, _matched(line=2)])


def test_empty_lines_rejected():
    with pytest.raises(ValidationError):
        RFQOutput(rfq_id="rfq_01_email", customer_id="C1003", lines=[])


def test_extracted_rfq_validates_id_and_sequence():
    line = ExtractedLine(line=1, as_written="6205 bearings sealed qty 24", quantity=24,
                         description="6205 bearings sealed", source_ref="body line 7")
    ExtractedRFQ(rfq_id="rfq_06_messy", source_file="rfqs/rfq_06_messy.txt",
                 source_format="email", lines=[line])
    with pytest.raises(ValidationError):
        ExtractedRFQ(rfq_id="RFQ-6", source_file="x", source_format="email")


# ---- conformance with the official checker ----------------------------------

def test_serialised_output_passes_official_checker():
    out = RFQOutput(
        rfq_id="rfq_06_messy", customer_id="C1008", customer_name="Granite Peak Mining",
        lines=[
            _matched(),
            OutputLine(line=2, as_written="nitrile gloves L - a few boxes", needs_review=True,
                       flags=[Flag.MISSING_QUANTITY, Flag.AMBIGUOUS_MATCH]),
        ],
        non_item_rows=["sent from my phone"],
    )
    data = out.to_json_dict()
    assert data["lines"][1]["flags"] == ["missing_quantity", "ambiguous_match"]
    fails, warns = checker._validate_output(data, "rfq_06_messy")
    assert fails == [] and warns == []


def test_evaluation_results_schema_matches_checker_numbers():
    r = EvaluationResults(threshold=0.8, extraction_accuracy=0.9, match_precision=1.0,
                          match_recall=0.75, additional_metrics={"review_rate": 0.3})
    assert set(checker.RESULT_NUMBERS) <= set(r.model_dump())
    with pytest.raises(ValidationError):
        EvaluationResults(threshold=80, extraction_accuracy=0.9, match_precision=1,
                          match_recall=1, additional_metrics={})
