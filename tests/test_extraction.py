"""Extraction tests. Expectations come from reading the RFQ source files, never answer_key/."""

from pathlib import Path

import pytest

from quote_desk.extract import extract_rfq
from quote_desk.extract.email import extract_email
from quote_desk.normalize import normalize_unit, parse_item_text
from quote_desk.reference import identify_customer, load_customers
from quote_desk.schemas import Flag

RFQS = Path(__file__).resolve().parents[1] / "rfqs"


# ---- line parsing -------------------------------------------------------------

@pytest.mark.parametrize("text, qty, unit, part", [
    ("- 20 ea 6204-2RS bearings", 20, "EA", None),
    ("RL159429 x 3", 3, None, "RL159429"),
    ('1/2-13 x 2" grade 8 hex bolts, 2 boxes', 2, "BX", None),   # size is not a quantity
    ("3 HP TEFC motor, 1800 rpm, 3 phase - qty 2", 2, None, None),
    ("forklift tires 18x7-8 x4", 4, None, None),
    ("NF-778812 x 4 (whatever you have thats equivalent)", 4, None, "NF-778812"),
    ("food grade grease cartridges - 3 cases", 3, "CS", None),
])
def test_parse_item_text(text, qty, unit, part):
    p = parse_item_text(text)
    assert (p.quantity, p.unit, p.part_number) == (qty, unit, part)


def test_specs_are_not_altered():
    assert parse_item_text('1/2-13 x 2" grade 8 hex bolts, 2 boxes').description == '1/2-13 x 2" grade 8 hex bolts'


def test_missing_and_vague_quantities_are_not_invented():
    p = parse_item_text("o-rings 214 buna")
    assert p.quantity is None and Flag.MISSING_QUANTITY in p.flags
    p = parse_item_text("nitrile gloves L - a few boxes")
    assert p.quantity is None and p.unit == "BX" and Flag.MISSING_QUANTITY in p.flags


def test_ambiguous_box_quantity_kept_as_written():
    p = parse_item_text("bolts 3/8 x 1 1/2 gr5 - 300 (we usually get boxes)")
    assert p.quantity == 300 and p.unit is None
    assert {Flag.MISSING_UNIT, Flag.AMBIGUOUS_QUANTITY} <= set(p.flags)
    assert p.notes == ["we usually get boxes"]


def test_prior_order_reference_flagged():
    assert Flag.REFERENCES_PRIOR_ORDER in parse_item_text("Same grease cartridges as last order, 2 cases").flags


@pytest.mark.parametrize("raw, expected", [("ea", "EA"), ("Boxes", "BX"), ("cs", "CS"),
                                           ("PAIL", "PAIL"), ("", None), (None, None)])
def test_normalize_unit(raw, expected):
    assert normalize_unit(raw) == expected


# ---- customers ---------------------------------------------------------------------

def test_customer_by_name_and_by_domain():
    customers = load_customers()
    c, _ = identify_customer("Purchasing, Mesa Fabrication", [], customers)
    assert c.customer_id == "C1003"
    c, _ = identify_customer("need asap", ["granitepeakmining.example"], customers)
    assert c.customer_id == "C1008"
    c, why = identify_customer("hello", ["gmail.com"], customers)
    assert c is None and "no customer" in why


# ---- whole documents ------------------------------------------------------------------

def test_email_items_signature_and_order():
    r = extract_rfq(RFQS / "rfq_01_email.txt")
    assert r.customer_id == "C1003" and len(r.lines) == 5
    assert [ln.source_ref for ln in r.lines] == [f"text line {n}" for n in range(10, 15)]
    assert "(480) 555-0142" in r.non_item_rows and "Luis Ortega" in r.non_item_rows


def test_pdf_table():
    r = extract_rfq(RFQS / "rfq_02_clearwater.pdf")
    assert r.customer_id == "C1007" and len(r.lines) == 6 and not r.errors
    first = r.lines[0]
    assert (first.part_number, first.description, first.quantity, first.unit) == (
        "APX-BB-6206ZZ", "Ball bearing 6206 ZZ shielded", 12, "EA")
    assert all("Ship to" not in ln.as_written for ln in r.lines)


def test_xlsx_table_inheritance_and_non_items():
    r = extract_rfq(RFQS / "rfq_03_tricounty.xlsx")
    assert r.customer_id == "C1010" and len(r.lines) == 8
    inherited = r.lines[2]
    assert inherited.description == '1/2" brass ball valve FP' and inherited.part_number is None
    assert Flag.INHERITED_FROM_LINE in inherited.flags
    assert any(row.startswith("Deliver to Plant 2") for row in r.non_item_rows)


def test_forwarded_chain_revisions():
    r = extract_rfq(RFQS / "rfq_05_forwarded_chain.txt")
    motor, gaskets, grease = r.lines
    assert motor.description.startswith("5 HP") and motor.as_written.startswith("3 HP")
    assert gaskets.quantity == 40 and Flag.REVISED_IN_THREAD in gaskets.flags
    assert grease.quantity == 3 and Flag.REVISED_IN_THREAD not in grease.flags
    assert r.customer_id == "C1009" and not r.errors


def test_unparseable_revision_is_flagged_not_dropped():
    text = ("From: a@x.example\n\nPlease swap the widget for something cheaper.\n\n"
            "---------- Original message ----------\nFrom: b@x.example\n\nNeed:\n- 5 ea widgets\n")
    lines, _, _, unresolved = extract_email(text)
    assert unresolved and Flag.UNRESOLVED_CONFLICT in lines[0].flags


def test_image_is_stubbed():
    r = extract_rfq(RFQS / "rfq_04_handwritten.png")
    assert len(r.lines) == 1 and r.lines[0].flags == [Flag.STUB] and r.lines[0].note


def test_failures_become_flagged_lines(tmp_path):
    bad_pdf = tmp_path / "rfq_90_corrupt.pdf"
    bad_pdf.write_bytes(b"not a pdf")
    unsupported = tmp_path / "rfq_91_fax.doc"
    unsupported.write_text("x")
    empty = tmp_path / "rfq_92_empty.txt"
    empty.write_text("From: someone@nowhere.example\n\nhi, call me\n")
    for f in (bad_pdf, unsupported, empty):
        r = extract_rfq(f)
        assert r.errors and r.lines[0].flags == [Flag.EXTRACTION_ERROR]
