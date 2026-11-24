"""Matcher and pipeline tests. Products below are synthetic or taken from the supplied catalog
rows; nothing comes from answer_key/."""

import json

import pytest

import check_submission as checker
from quote_desk.matcher import Matcher, canon, offered_tokens, spec_tokens
from quote_desk.pipeline import build_line, run
from quote_desk.schemas import CrossReference, ExtractedLine, Flag, Product


def _p(part, desc, unit="EA", status="active", replaced_by=None):
    return Product(part_number=part, description=desc, category="X", unit=unit, list_price=1,
                   status=status, replaced_by=replaced_by, primary_warehouse="DAL")


def _x(comp, rl, notes=None, verified="2025-01-01"):
    return CrossReference(competitor="Comp", competitor_part_number=comp,
                          ridgeline_part_number=rl, notes=notes, last_verified=verified)


def _line(desc=None, part=None, unit=None, qty=1, flags=()):
    return ExtractedLine(line=1, as_written=desc or part or "x", description=desc, part_number=part,
                         unit=unit, quantity=qty, source_ref="t", flags=list(flags))


CATALOG = (
    _p("RL000001", "Deep groove ball bearing 6205-2RS, rubber sealed both sides"),
    _p("RL000002", "Deep groove ball bearing 6205-ZZ, metal shielded both sides"),
    _p("RL000003", "Hex cap screw 3/8-16 x 1-1/2 in, grade 5, zinc plated, box of 100", unit="BX"),
    _p("RL000004", "Hex cap screw 3/8-16 x 1-1/2 in, grade 8, yellow zinc, box of 100", unit="BX"),
    _p("RL000005", "Ball valve 1/2 in, lead-free brass, full port, NPT, 600 WOG"),
    _p("RL000006", "Ball valve 1/2 in, 316 stainless steel, full port, NPT, 1000 WOG"),
    _p("RL000007", "Old fitting", status="discontinued", replaced_by="RL000008"),
    _p("RL000008", "New fitting, Gen 2"),
    _p("RL000009", "Hydraulic oil AW 46, 5 gallon pail"),
)
XREFS = (
    _x("CMP-1", "RL000005"),
    _x("CMP-SUB", "RL000006", notes="close sub"),
    _x("CMP-OLD", "RL000007"),
    _x("CMP-DUP", "RL000001"), _x("CMP-DUP", "RL000002", notes="dup? confirm"),
)


@pytest.fixture(scope="module")
def m():
    return Matcher(CATALOG, XREFS)


def test_canonical_specs():
    assert spec_tokens(canon('bolts 3/8 x 1 1/2 gr5')) == {"3/8", "1-1/2", "grade5"}
    assert {"6205", "2rs", "3/8"} <= offered_tokens(canon("6205-2RS 3/8-16"))
    assert "150lb" in canon('2" flange gasket 150#') and "2in" in canon('2" flange gasket 150#')


def test_exact_ridgeline_part(m):
    r = m.match(_line(part="RL000003"))
    assert (r.part_number, r.method, r.confidence) == ("RL000003", "ridgeline_part_number", 0.97)


def test_cross_reference_preferred_over_description(m):
    r = m.match(_line(desc="1/2 brass ball valve", part="CMP-1"))
    assert (r.part_number, r.method) == ("RL000005", "cross_reference") and r.confidence >= 0.9


def test_cross_reference_contradicted_by_description(m):
    r = m.match(_line(desc="1/2 stainless ball valve", part="CMP-1"))
    assert Flag.LOW_CONFIDENCE in r.flags and r.confidence < 0.8


def test_close_sub_flagged(m):
    r = m.match(_line(part="CMP-SUB"))
    assert Flag.SUBSTITUTION in r.flags and r.confidence < 0.95


def test_discontinued_uses_catalog_replacement(m):
    r = m.match(_line(part="CMP-OLD"))
    assert r.part_number == "RL000008" and Flag.DISCONTINUED_REPLACED in r.flags


def test_duplicate_cross_reference_not_forced(m):
    r = m.match(_line(part="CMP-DUP"))
    assert Flag.CROSS_REF_CONFLICT in r.flags and r.part_number is None


def test_critical_attribute_selects_grade_and_rejects_conflicts(m):
    assert m.match(_line(desc="bolts 3/8 x 1 1/2 gr5", unit="BX")).part_number == "RL000003"
    assert m.match(_line(desc="bolts 3/8 x 1 1/2 gr8")).part_number == "RL000004"
    r = m.match(_line(desc="bolts 3/8 x 3 gr5"))           # no 3 in length in catalog
    assert r.part_number is None and Flag.NO_MATCH in r.flags


def test_material_is_critical(m):
    assert m.match(_line(desc='1/2" brass ball valve FP')).part_number == "RL000005"
    assert m.match(_line(desc='1/2" ball valve 316 SS')).part_number == "RL000006"


def test_ambiguous_description_is_flagged(m):
    r = m.match(_line(desc="6205 bearing"))                 # 2RS or ZZ?
    assert Flag.AMBIGUOUS_MATCH in r.flags and r.confidence <= 0.6


def test_unknown_items_are_not_forced(m):
    for desc in ("forklift tires 18x7-8", "Chlorine residual test kit"):
        r = m.match(_line(desc=desc))
        assert r.part_number is None and Flag.NO_MATCH in r.flags


def test_unit_container_compatibility(m):
    r = m.match(_line(desc="AW46 hyd oil 5 gal", unit="PAIL"))
    assert r.part_number == "RL000009" and Flag.UNIT_MISMATCH not in r.flags


def test_prior_order_reference_never_matched(m):
    r = m.match(_line(desc="grease cartridges", flags=[Flag.REFERENCES_PRIOR_ORDER]))
    assert r.part_number is None


def test_review_decision_respects_threshold(m):
    line = _line(desc="bolts 3/8 x 1 1/2 gr5", unit="BX")
    assert build_line(line, m, 0.50).needs_review is False
    strict = build_line(line, m, 0.99)
    assert strict.needs_review and Flag.LOW_CONFIDENCE in strict.flags


def test_pipeline_outputs_pass_official_checker(tmp_path):
    summary = run(__import__("pathlib").Path("rfqs"), tmp_path, 0.80, quiet=True)
    assert len(summary) == 6
    for f in sorted(tmp_path.glob("*.json")):
        data = json.loads(f.read_text(encoding="utf-8"))
        fails, warns = checker._validate_output(data, f.stem)
        assert fails == [] and warns == [], (f.name, fails, warns)
        assert "price" not in f.read_text(encoding="utf-8").lower()
    stub = json.loads((tmp_path / "rfq_04_handwritten.json").read_text(encoding="utf-8"))
    assert stub["lines"][0]["flags"] == ["stub"] and stub["lines"][0]["needs_review"] is True
