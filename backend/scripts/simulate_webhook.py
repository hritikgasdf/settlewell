"""Send one signed synthetic webhook to the local MOCK API. Loopback only; prints a sanitized summary.

The secret comes from AIRWALLEX_WEBHOOK_SECRET. The signature, secret and raw response are never printed."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from urllib.parse import urlparse

LOOPBACK = {"127.0.0.1", "localhost", "::1"}


def build(secret: str, *, transfer_id: str, status: str, event_id: str, now_ms: int) -> tuple[bytes, dict]:
    body = {
        "id": event_id,
        "name": f"payout.transfer.{status.lower()}",
        "created_at": datetime.fromtimestamp(now_ms / 1000, UTC).isoformat(),
        "data": {"id": transfer_id, "status": status.upper()},
    }
    raw = json.dumps(body, separators=(",", ":")).encode()
    signature = hmac.new(secret.encode(), str(now_ms).encode() + raw, hashlib.sha256).hexdigest()
    return raw, {"x-timestamp": str(now_ms), "x-signature": signature, "content-type": "application/json"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8765/api/webhooks/airwallex")
    parser.add_argument("--transfer-id", default="synthetic-inv-2291")
    parser.add_argument("--status", default="PAID", choices=["SCHEDULED", "PROCESSING", "SENT", "PAID", "FAILED", "CANCELLED"])
    parser.add_argument("--event-id", default=None)
    args = parser.parse_args(argv)
    if urlparse(args.url).hostname not in LOOPBACK:
        print("refused: loopback URLs only")
        return 2
    secret = os.environ.get("AIRWALLEX_WEBHOOK_SECRET", "")
    if not secret.strip():
        print("refused: AIRWALLEX_WEBHOOK_SECRET is not set")
        return 2
    now_ms = int(time.time() * 1000)
    event_id = args.event_id or f"evt-sim-{now_ms}"
    raw, headers = build(secret, transfer_id=args.transfer_id, status=args.status, event_id=event_id, now_ms=now_ms)
    request = urllib.request.Request(args.url, data=raw, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            status, payload = response.status, response.read()
    except urllib.error.HTTPError as error:
        status, payload = error.code, error.read()
    except OSError:
        print("http=unreachable event_id=" + event_id)
        return 1
    try:
        outcome = str(json.loads(payload).get("outcome") or "rejected")
    except (ValueError, AttributeError):
        outcome = "rejected"
    print(f"http={status} event_id={event_id} outcome={outcome}")
    return 0 if status == 200 else 1


if __name__ == "__main__":
    sys.exit(main())
