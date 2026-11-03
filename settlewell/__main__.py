"""One-command local console. Mock is the default. Sandbox is opt-in."""

from __future__ import annotations

import argparse
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from backend.console import ConsoleError, render, render_ledger
from backend.reset import open_ledger
from settlewell.flags import sandbox_enabled

HOST = "127.0.0.1"
PORT = 8765


def local_mode() -> str:
    """Mock unless sandbox mode is explicitly enabled and mock is not."""
    if sandbox_enabled() and os.environ.get("SETTLEWELL_MOCK") != "1":
        return "sandbox"
    return "mock"


def prepare() -> str:
    """Apply the default mock flag. Does not read API credentials."""
    mode = local_mode()
    if mode == "mock":
        os.environ["SETTLEWELL_MOCK"] = "1"
    return mode


def handle(path: str) -> tuple[int, bytes]:
    """Return a console page. Page handlers do not call Airwallex."""
    prepare()
    route = path.split("?", 1)[0] or "/"
    if local_mode() == "sandbox":
        conn = open_ledger(os.environ.get("SETTLEWELL_DB", "var/settlewell.db"))
        try:
            page = render_ledger(route, conn)
        finally:
            conn.close()
        return 200, page.encode()
    try:
        page = render(route)
    except ConsoleError:
        page = _layout("Unavailable", "<p>The console did not render.</p>")
        return 503, page.encode()
    return 200, page.encode()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    previous_mock = os.environ.get("SETTLEWELL_MOCK")
    mode = prepare()
    print("MOCK" if mode == "mock" else "sandbox")
    if args.check:
        if previous_mock is None:
            os.environ.pop("SETTLEWELL_MOCK", None)
        else:
            os.environ["SETTLEWELL_MOCK"] = previous_mock
        return 0
    print(f"http://{HOST}:{PORT}/")
    server = ThreadingHTTPServer((HOST, PORT), _Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    finally:
        server.server_close()
    return 0


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        status, body = handle(self.path)
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *_args) -> None:
        return


def _layout(title: str, body: str) -> str:
    return f"<!DOCTYPE html><html lang=\"en\"><head><meta charset=\"utf-8\"><title>{title}</title></head><body><h1>{title}</h1>{body}</body></html>"


if __name__ == "__main__":
    sys.exit(main())
