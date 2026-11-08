"""Reset the hero run. Scoped wrapper: never account-wide.

Delegates to `hero_run --reset`, which touches only transfers recorded in this run's manifest.
The old behaviour (wipe the ledger and release every remote SENT transfer) is refused.
Sandbox reset needs `--sandbox --confirm-sandbox`; without it only an empty/mock manifest is removed.
"""

from __future__ import annotations

import sys

from backend.scripts import hero_run


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if "--all" in args or "--account-wide" in args:
        print("refused: unscoped account-wide reset is not supported")
        return 2
    return hero_run.main(["--reset", *args])


if __name__ == "__main__":
    sys.exit(main())
