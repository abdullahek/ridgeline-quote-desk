"""Loading of the supplied reference data, plus customer identification."""

from __future__ import annotations

import csv
import re
from functools import lru_cache
from pathlib import Path
from typing import Optional

from .schemas import CrossReference, Customer, Product

DATA_DIR = Path(__file__).resolve().parents[1]
_SUFFIXES = {"co", "company", "inc", "llc", "ltd", "corp", "corporation", "the"}


def _read(path: Path) -> list[dict]:
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


@lru_cache(maxsize=None)
def load_products(data_dir: Path = DATA_DIR) -> tuple[Product, ...]:
    return tuple(Product(**r) for r in _read(data_dir / "products.csv"))


@lru_cache(maxsize=None)
def load_cross_reference(data_dir: Path = DATA_DIR) -> tuple[CrossReference, ...]:
    return tuple(CrossReference(**r) for r in _read(data_dir / "cross_reference.csv"))


@lru_cache(maxsize=None)
def load_customers(data_dir: Path = DATA_DIR) -> tuple[Customer, ...]:
    return tuple(Customer(**r) for r in _read(data_dir / "customers.csv"))


def _name_tokens(name: str) -> list[str]:
    words = re.findall(r"[a-z0-9]+", name.lower())
    return [w for w in words if w not in _SUFFIXES]


def identify_customer(
    text: str, sender_domains: list[str], customers: tuple[Customer, ...]
) -> tuple[Optional[Customer], str]:
    """Return (customer, evidence). Only an unambiguous hit counts.

    1. The customer's name appears in the document (word boundaries, suffixes ignored).
    2. Otherwise a sender e-mail domain equals the squashed name (granitepeakmining).
    """
    low = " ".join(re.findall(r"[a-z0-9]+", text.lower()))
    by_name = [c for c in customers
               if re.search(r"\b" + r"\s+".join(_name_tokens(c.name)) + r"\b", low)]
    if len(by_name) == 1:
        return by_name[0], f"name '{by_name[0].name}' found in document"

    labels = {d.lower().split(".")[0] for d in sender_domains}
    by_domain = [c for c in customers if "".join(_name_tokens(c.name)) in labels]
    if len(by_domain) == 1:
        return by_domain[0], f"sender domain matches '{by_domain[0].name}'"

    if len(by_name) > 1 or len(by_domain) > 1:
        return None, "several customers matched; left for review"
    return None, "no customer name or domain found"
