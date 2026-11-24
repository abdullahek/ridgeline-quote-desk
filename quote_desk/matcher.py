"""Catalog matching with explainable confidence scores.

Order of evidence (strongest first):
1. A Ridgeline part number written by the customer          -> ``ridgeline_part_number``
2. A competitor part number found in cross_reference.csv     -> ``cross_reference``
3. Description fallback: RapidFuzz text similarity, gated by  -> ``description``
   *critical attributes*: every size / thread / grade / seal / rating / material
   the customer states must appear in the candidate, or the candidate is rejected.

Prices and stock are never read or emitted. Discontinued products are replaced
only when the catalog names a replacement, and the substitution is flagged.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

from rapidfuzz import fuzz

from . import config
from .normalize import RL_RE, normalize_text
from .reference import load_cross_reference, load_products
from .schemas import CrossReference, ExtractedLine, Flag, MatchMethod, Product

# --------------------------------------------------------------------------
# Canonical text and critical attributes
# --------------------------------------------------------------------------

_SYNONYMS = {
    "brg": "bearing", "brgs": "bearing", "bolt": "hex cap screw", "bolts": "hex cap screw",
    "hyd": "hydraulic", "ss": "stainless", "fp": "full port", "thk": "thick",
    "push in": "push to connect", "gallon": "gal", "lg": "large",
}
_MATERIALS = ("brass", "stainless", "cellulose", "glass", "nitrile", "buna",
              "iron", "polyurethane", "cobalt", "zirconia")
_UNIT_SUFFIX = r"in|lb|hp|rpm|micron|mil|oz|ft|gal|ml|phase|psi|wog|ton|grit"
_SIZE_WORDS = {"small": "s", "medium": "m", "large": "l", "x large": "xl", "xlarge": "xl"}


def canon(text: str) -> str:
    """Lower-case text with specs rewritten into comparable tokens (150# -> 150lb, 1 1/2 -> 1-1/2)."""
    t = normalize_text(text).lower()
    t = t.replace('"', " in ").replace("gallon", "gal")
    t = re.sub(r"(\d)\s*#(?!\d)", r"\1 lb", t)                       # 150# -> 150 lb
    t = re.sub(r"#\s+(\d)", r"#\1", t)
    t = re.sub(r"\b(\d+)\s+(\d+/\d+)\b", r"\1-\2", t)                 # 1 1/2 -> 1-1/2
    t = re.sub(r"\b(\d{4})[\s-]?(2rs|zz)\b", r"\1-\2", t)            # 6206 zz -> 6206-zz
    t = re.sub(r"\bgr(?:ade)?\.?\s*(\d)\b", r"grade\1", t)            # gr5 -> grade5
    t = re.sub(r"\b(aw|iso|merv|nlgi)\s*#?\s*(\d+)\b", r"\1\2", t)    # MERV 8 -> merv8
    t = re.sub(r"(\d)-phase\b", r"\1 phase", t)
    t = re.sub(rf"(\d+(?:\.\d+)?)\s*({_UNIT_SUFFIX})\b", r"\1\2", t)  # 5 HP -> 5hp
    t = re.sub(r"(?<=[a-z])-(?=[a-z])", " ", t)                       # food-grade -> food grade
    t = re.sub(r"[,;()]", " ", t)
    for word, repl in _SYNONYMS.items():
        t = re.sub(rf"\b{re.escape(word)}\b", repl, t)
    for word, size in _SIZE_WORDS.items():
        t = re.sub(rf"\b{word}\b", f"size-{size}", t)
    t = re.sub(r"\bsize\s+(xs|s|m|l|xl)\b", r"size-\1", t)
    t = re.sub(r"(?<![\w-])(xs|s|m|l|xl)(?![\w-])", r"size-\1", t)   # bare glove size "L"
    words = []
    for w in t.split():
        w = w.strip(".-")
        if len(w) > 3 and w.endswith("s") and not w.endswith("ss") and not any(c.isdigit() for c in w):
            w = w[:-1]                                                   # bearings -> bearing
        if w:
            words.append(w)
    return " ".join(words)


def spec_tokens(canonical: str) -> set[str]:
    """Measurable attributes: anything with a digit, plus glove sizes."""
    return {w for w in canonical.split() if any(c.isdigit() for c in w) or w.startswith("size-")}


def offered_tokens(canonical: str) -> set[str]:
    """Catalog side: also offer the parts of compound specs (6205-2rs -> 6205, 3/8-16 -> 3/8)."""
    out = spec_tokens(canonical)
    for w in list(out):
        if w.endswith("in") and w[:-2] and w[-3].isdigit():
            out.add(w[:-2])                                               # 3/4in also satisfies "3/4"
        if "-" in w and not re.fullmatch(r"\d+-\d+/\d+(?:in)?", w):
            out.update(p for p in w.split("-") if p)
    return out


def materials(canonical: str) -> set[str]:
    words = set(canonical.split())
    return {m for m in _MATERIALS if m in words}


# --------------------------------------------------------------------------
# Results
# --------------------------------------------------------------------------

@dataclass
class MatchResult:
    part_number: Optional[str] = None
    method: Optional[MatchMethod] = None
    confidence: Optional[float] = None
    flags: list[Flag] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)


@dataclass
class _Candidate:
    product: Product
    score: float
    text: float
    reasons: list[str]
    unit_ok: Optional[bool]


class Matcher:
    def __init__(self, products: tuple[Product, ...] | None = None,
                 xrefs: tuple[CrossReference, ...] | None = None):
        self.products = products or load_products()
        self.by_part = {p.part_number: p for p in self.products}
        self.xref: dict[str, list[CrossReference]] = {}
        for x in (xrefs or load_cross_reference()):
            self.xref.setdefault(_key(x.competitor_part_number), []).append(x)
        self._canon = {p.part_number: canon(p.description) for p in self.products}

    # ---- public -----------------------------------------------------------

    def match(self, line: ExtractedLine) -> MatchResult:
        if Flag.STUB in line.flags or Flag.EXTRACTION_ERROR in line.flags:
            return MatchResult(reasons=["not matched: no extracted item"])
        if Flag.REFERENCES_PRIOR_ORDER in line.flags:
            return MatchResult(flags=[Flag.NO_MATCH], reasons=[
                "refers to a previous order; order history is not available, so no product is guessed"])

        result = MatchResult()
        part = (line.part_number or "").upper()
        if part and RL_RE.match(part):
            if part in self.by_part:
                return self._finish(line, self.by_part[part], "ridgeline_part_number", 0.97,
                                    [f"customer quoted Ridgeline part {part}"], result)
            result.flags.append(Flag.PART_NOT_FOUND)
            result.reasons.append(f"{part} is not in the catalog")
        elif part:
            hits = self.xref.get(_key(part), [])
            if hits:
                return self._from_xref(line, part, hits, result)
            result.flags.append(Flag.PART_NOT_FOUND)
            result.reasons.append(f"{part} not found in cross_reference.csv")

        return self._by_description(line, result)

    # ---- cross reference ----------------------------------------------------

    def _from_xref(self, line, part, hits, result: MatchResult) -> MatchResult:
        targets = {h.ridgeline_part_number for h in hits}
        if len(targets) > 1:
            result.flags.append(Flag.CROSS_REF_CONFLICT)
            result.reasons.append(f"{part} maps to several Ridgeline parts: {', '.join(sorted(targets))}")
            ranked = self._rank(line, [self.by_part[t] for t in targets if t in self.by_part])
            if ranked and ranked[0].score > 0 and (len(ranked) == 1 or ranked[0].score - ranked[1].score > config.AMBIGUITY_MARGIN):
                hit = next(h for h in hits if h.ridgeline_part_number == ranked[0].product.part_number)
                return self._finish(line, ranked[0].product, "cross_reference", 0.60,
                                    [f"description selects {hit.ridgeline_part_number}"], result, xref=hit)
            result.flags.append(Flag.NO_MATCH)
            result.reasons.append("description does not settle the conflict; left for review")
            return result
        hit = hits[0]
        product = self.by_part.get(hit.ridgeline_part_number)
        if product is None:
            result.flags.append(Flag.PART_NOT_FOUND)
            result.reasons.append(f"cross-reference target {hit.ridgeline_part_number} missing from catalog")
            return self._by_description(line, result)
        return self._finish(line, product, "cross_reference", 0.95,
                            [f"{hit.competitor} {part} -> {product.part_number} via cross-reference"],
                            result, xref=hit)

    # ---- description ----------------------------------------------------------

    def _rank(self, line: ExtractedLine, pool) -> list[_Candidate]:
        query = canon(" ".join(filter(None, [line.description])))
        q_specs, q_mats = spec_tokens(query), materials(query)
        out = []
        for p in pool:
            c = self._canon[p.part_number]
            missing = q_specs - offered_tokens(c)
            if missing or not q_mats <= materials(c):
                continue
            text = fuzz.token_set_ratio(query, c) / 100
            if q_specs:
                if text < config.MIN_TEXT_SIMILARITY_WITH_SPECS:
                    continue
                score = 0.50 + 0.30 * text + 0.05 * min(len(q_specs), 3)
                why = [f"all {len(q_specs)} stated specs match ({', '.join(sorted(q_specs))})",
                       f"text similarity {text:.2f}"]
            else:
                if text < config.MIN_TEXT_SIMILARITY_NO_SPECS:
                    continue
                score = 0.30 + 0.50 * text
                why = [f"no measurable specs stated; text similarity {text:.2f}"]
            if q_mats:
                why.append(f"material {', '.join(sorted(q_mats))} matches")
            unit_ok = _unit_compatible(line.unit, p)
            if unit_ok is True:
                score += 0.03
            elif unit_ok is False:
                score -= 0.10
            out.append(_Candidate(p, round(min(score, 0.95), 3), text, why, unit_ok))
        return sorted(out, key=lambda c: c.score, reverse=True)

    def _by_description(self, line: ExtractedLine, result: MatchResult) -> MatchResult:
        if not line.description:
            result.flags.append(Flag.NO_MATCH)
            result.reasons.append("no description to match on")
            return result
        ranked = self._rank(line, self.products)
        if not ranked:
            result.flags.append(Flag.NO_MATCH)
            result.reasons.append("no catalog item satisfies the stated attributes")
            return result
        best = ranked[0]
        reasons = list(best.reasons)
        conf = best.score
        if len(ranked) > 1 and best.score - ranked[1].score <= config.AMBIGUITY_MARGIN:
            runner = ranked[1].product
            result.flags.append(Flag.AMBIGUOUS_MATCH)
            reasons.append(f"runner-up {runner.part_number} ({runner.description}) scores "
                           f"{ranked[1].score:.2f} vs {best.score:.2f}")
            conf = min(conf, 0.60)
        elif len(ranked) > 1:
            reasons.append(f"next best {ranked[1].product.part_number} scores {ranked[1].score:.2f}")
        if Flag.PART_NOT_FOUND in result.flags:
            result.flags.append(Flag.SUBSTITUTION)
            reasons.append("customer's part number unknown; offered as an equivalent by description")
        return self._finish(line, best.product, "description", conf, reasons, result)

    # ---- shared ---------------------------------------------------------------

    def _finish(self, line, product: Product, method: MatchMethod, conf: float,
                reasons: list[str], result: MatchResult, xref: CrossReference | None = None) -> MatchResult:
        reasons = list(reasons)
        if xref is not None:
            note = (xref.notes or "").lower()
            if xref.notes:
                reasons.append(f"cross-reference note: '{xref.notes}'")
                if "sub" in note:
                    result.flags.append(Flag.SUBSTITUTION)
                    conf -= 0.15
                elif "dup" in note or "confirm" in note:
                    result.flags.append(Flag.CROSS_REF_CONFLICT)
                    conf -= 0.20
                else:
                    result.flags.append(Flag.CROSS_REF_NOTE)
                    conf -= 0.10
            if not xref.last_verified:
                conf -= 0.05
                reasons.append("cross-reference never verified")

        # Cross-check the customer's own description against the chosen product.
        if method != "description" and line.description:
            q = canon(line.description)
            missing = spec_tokens(q) - offered_tokens(self._canon[product.part_number])
            bad_mats = materials(q) - materials(self._canon[product.part_number])
            if missing or bad_mats:
                conf -= 0.30
                result.flags.append(Flag.LOW_CONFIDENCE)
                reasons.append(f"customer description disagrees with {product.part_number} on "
                               f"{', '.join(sorted(missing | bad_mats))}")
            else:
                reasons.append("customer description consistent with the product")

        if _unit_compatible(line.unit, product) is False:
            result.flags.append(Flag.UNIT_MISMATCH)
            reasons.append(f"requested unit {line.unit} vs catalog unit {product.unit}")
            if method != "description":
                conf -= 0.10  # description candidates were already penalised in _rank

        if product.status == "discontinued":
            replacement = self.by_part.get(product.replaced_by or "")
            if replacement is None:
                result.flags.append(Flag.NO_MATCH)
                result.reasons.extend(reasons + [f"{product.part_number} is discontinued with no replacement"])
                return result
            result.flags += [Flag.DISCONTINUED_REPLACED, Flag.SUBSTITUTION]
            reasons.append(f"{product.part_number} is discontinued; catalog replacement is "
                           f"{replacement.part_number} ({replacement.description})")
            conf -= 0.10
            product = replacement

        result.part_number, result.method = product.part_number, method
        result.confidence = round(max(0.0, min(1.0, conf)), 2)
        result.reasons.extend(reasons)
        return result


def _key(part: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", part.upper())


_UNIT_WORDS = {"PAIL": "pail", "CS": "case", "BX": "box", "PK": "pack", "RL": "roll", "PR": "pair"}


def _unit_compatible(unit: Optional[str], product: Product) -> Optional[bool]:
    """None when the customer gave no unit; True when it equals the catalog UOM or the
    catalog item is sold as that container (e.g. PAIL vs '5 gallon pail' sold EA)."""
    if not unit:
        return None
    if unit == product.unit:
        return True
    word = _UNIT_WORDS.get(unit)
    return bool(word and product.unit == "EA" and word in product.description.lower())
