"""Dev entrypoint. The console lands with checklist item 35."""

from __future__ import annotations

import sys

from settlewell.flags import mock_mode


def main() -> int:
    if mock_mode():
        print("MOCK")
        return 0
    print("Settlewell console is not built yet.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
