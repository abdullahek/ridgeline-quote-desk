"""Pipeline configuration. Values can be overridden from the CLI or environment."""

import os

# Lines whose confidence is below this go to human review (every quote is still
# approved by a person; this decides which lines get a closer look).
REVIEW_THRESHOLD = float(os.environ.get("QUOTE_DESK_REVIEW_THRESHOLD", "0.80"))

# Description matching: below these a candidate is not proposed at all.
MIN_TEXT_SIMILARITY_WITH_SPECS = 0.45   # all stated specs matched, text only has to agree loosely
MIN_TEXT_SIMILARITY_NO_SPECS = 0.75     # nothing measurable to check, so the text must agree strongly
AMBIGUITY_MARGIN = 0.05                 # runner-up this close to the best -> ambiguous
