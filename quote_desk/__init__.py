"""Ridgeline Quote Desk: deterministic RFQ extraction and catalog matching."""

import os

# pymupdf 2.x may lazily load numpy/OpenBLAS; one BLAS thread avoids the
# "Memory allocation still failed" crash seen on Windows. We do no numeric work.
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
