"""Pydantic schemas for every stage of the pipeline.

Internal models (catalog rows, extracted lines) are richer than the submission
format; ``RFQOutput`` / ``OutputLine`` mirror check_submission.py exactly and
forbid unknown keys so a schema drift fails loudly in our own tests first.
"""

from __future__ import annotations

import re
from enum import Enum
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

RL_PART_RE = re.compile(r"^RL\d{6}$")
RFQ_ID_RE = re.compile(r"^rfq_\d{2}_[a-z0-9_]+$")

MatchMethod = Literal["ridgeline_part_number", "cross_reference", "description", "llm"]
SourceFormat = Literal["email", "pdf", "xlsx", "image"]


class Flag(str, Enum):
    """Closed vocabulary of review flags, so downstream tooling can rely on them."""

    STUB = "stub"                                  # checker requires exactly "stub"
    MISSING_QUANTITY = "missing_quantity"
    MISSING_UNIT = "missing_unit"
    AMBIGUOUS_QUANTITY = "ambiguous_quantity"      # e.g. "300 (we usually get boxes)"
    UNIT_MISMATCH = "unit_mismatch"                # requested UOM differs from catalog UOM
    NO_MATCH = "no_match"
    LOW_CONFIDENCE = "low_confidence"
    AMBIGUOUS_MATCH = "ambiguous_match"            # several plausible catalog items
    SUBSTITUTION = "substitution"                  # cross-ref "close sub" / equivalent offered
    CROSS_REF_NOTE = "cross_ref_note"              # e.g. "pkg qty differs", "check thickness"
    CROSS_REF_CONFLICT = "cross_ref_conflict"      # one competitor part -> several RL parts
    DISCONTINUED_REPLACED = "discontinued_replaced"
    PART_NOT_FOUND = "part_not_found"              # identifier given but unknown to us
    REFERENCES_PRIOR_ORDER = "references_prior_order"  # "same as last order"
    REVISED_IN_THREAD = "revised_in_thread"
    UNRESOLVED_CONFLICT = "unresolved_conflict"
    INHERITED_FROM_LINE = "inherited_from_line"    # "same as above but 1/2 in"
    CUSTOMER_UNIDENTIFIED = "customer_unidentified"
    EXTRACTION_ERROR = "extraction_error"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


# --------------------------------------------------------------------------
# Reference data
# --------------------------------------------------------------------------

class Product(_Strict):
    part_number: str
    description: str
    category: str
    unit: str
    list_price: float = Field(ge=0)  # loaded for completeness; never written to outputs
    status: Literal["active", "discontinued"]
    replaced_by: Optional[str] = None
    primary_warehouse: str

    @field_validator("part_number")
    @classmethod
    def _rl_format(cls, v: str) -> str:
        if not RL_PART_RE.match(v):
            raise ValueError(f"not a Ridgeline part number: {v!r}")
        return v

    @field_validator("replaced_by", mode="before")
    @classmethod
    def _blank_is_none(cls, v: Any) -> Any:
        return v or None


class CrossReference(_Strict):
    competitor: str
    competitor_part_number: str
    ridgeline_part_number: str
    notes: Optional[str] = None
    last_verified: Optional[str] = None

    @field_validator("notes", "last_verified", mode="before")
    @classmethod
    def _blank_is_none(cls, v: Any) -> Any:
        return v or None


class Customer(_Strict):
    customer_id: str
    name: str
    region: str
    pricing_tier: str


# --------------------------------------------------------------------------
# Extraction stage (internal)
# --------------------------------------------------------------------------

class ExtractedLine(_Strict):
    """One requested item exactly as the customer asked for it, before matching."""

    line: int = Field(ge=1)
    as_written: str = Field(min_length=1)
    quantity: Optional[float] = Field(default=None, gt=0)
    unit: Optional[str] = None                # normalised UOM, e.g. "EA", "BX", "CS"
    part_number: Optional[str] = None         # identifier as supplied (RL / competitor)
    description: Optional[str] = None
    source_ref: str = Field(min_length=1)     # e.g. "body line 12", "sheet RFQ row 7"
    flags: list[Flag] = Field(default_factory=list)
    note: Optional[str] = None


class ExtractedRFQ(_Strict):
    rfq_id: str
    source_file: str
    source_format: SourceFormat
    customer_id: Optional[str] = None
    customer_name: Optional[str] = None
    lines: list[ExtractedLine] = Field(default_factory=list)
    non_item_rows: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)

    @field_validator("rfq_id")
    @classmethod
    def _rfq_id(cls, v: str) -> str:
        if not RFQ_ID_RE.match(v):
            raise ValueError(f"rfq_id must look like rfq_01_email, got {v!r}")
        return v

    @model_validator(mode="after")
    def _sequential(self) -> "ExtractedRFQ":
        _check_sequence([ln.line for ln in self.lines])
        return self


# --------------------------------------------------------------------------
# Submission format (mirrors check_submission.py)
# --------------------------------------------------------------------------

class OutputLine(_Strict):
    line: int = Field(ge=1)
    as_written: str = Field(min_length=1)
    quantity: Optional[float] = Field(default=None, gt=0)
    unit: Optional[str] = None
    ridgeline_part_number: Optional[str] = None
    match_method: Optional[MatchMethod] = None
    confidence: Optional[float] = Field(default=None, ge=0, le=1)
    needs_review: bool
    flags: list[Flag] = Field(default_factory=list)
    note: Optional[str] = None

    @field_validator("ridgeline_part_number")
    @classmethod
    def _rl_format(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and not RL_PART_RE.match(v):
            raise ValueError(f"not a Ridgeline part number: {v!r}")
        return v

    @model_validator(mode="after")
    def _consistency(self) -> "OutputLine":
        if self.ridgeline_part_number and self.match_method is None:
            raise ValueError("a matched line needs match_method")
        if self.ridgeline_part_number and self.confidence is None:
            raise ValueError("a matched line needs a confidence score")
        if not self.ridgeline_part_number and self.match_method is not None:
            raise ValueError("match_method set without a ridgeline_part_number")
        if Flag.STUB in self.flags:
            if not self.needs_review:
                raise ValueError("a stub line must have needs_review=true")
            if not (self.note and self.note.strip()):
                raise ValueError("a stub line needs the reason in note")
        if not self.ridgeline_part_number and not self.needs_review:
            raise ValueError("an unmatched line must be sent to human review")
        return self


class RFQOutput(_Strict):
    rfq_id: str
    customer_id: Optional[str]
    customer_name: Optional[str] = None
    lines: list[OutputLine] = Field(min_length=1)
    non_item_rows: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _checks(self) -> "RFQOutput":
        _check_sequence([ln.line for ln in self.lines])
        if any(Flag.STUB in ln.flags for ln in self.lines) and len(self.lines) != 1:
            raise ValueError("a stubbed RFQ has a single line")
        return self

    def to_json_dict(self) -> dict[str, Any]:
        """Plain JSON-ready dict; enum flags become strings."""
        return self.model_dump(mode="json")


class EvaluationResults(_Strict):
    threshold: float = Field(ge=0, le=1)
    extraction_accuracy: float = Field(ge=0, le=1)
    match_precision: float = Field(ge=0, le=1)
    match_recall: float = Field(ge=0, le=1)
    additional_metrics: dict[str, Any]


def _check_sequence(numbers: list[int]) -> None:
    if numbers != list(range(1, len(numbers) + 1)):
        raise ValueError(f"line numbers must run 1, 2, 3...; got {numbers}")
