"""Score the first 15 incidents. SETTLEWELL_MOCK=1. No network and no secrets."""

from __future__ import annotations

import sys

from backend.eval_harness import main

if __name__ == "__main__":
    sys.exit(main())
