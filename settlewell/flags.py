"""Process flags for the dev entrypoint and the unit suite.

Not the Airwallex adapter. Live HTTP stays behind the sandbox marker.
"""

from __future__ import annotations

import os

PINNED_API_VERSION = "2025-11-11"
LOGIN_PATH = "/api/v1/authentication/login"


def mock_mode() -> bool:
    return os.environ.get("SETTLEWELL_MOCK") == "1"


def sandbox_enabled() -> bool:
    return os.environ.get("SETTLEWELL_SANDBOX") == "1"


def flat_failure_reason_persists(api_version: str | None) -> bool:
    """Flat failure_reason on a CANCELLED transfer survives GET only with our pin.

    Omitting x-api-version clears the field. Sending 2025-11-11 keeps it.
    """
    return api_version == PINNED_API_VERSION
