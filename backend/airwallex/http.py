"""HTTP types for the Airwallex adapter. No credentials are logged."""

from __future__ import annotations

import socket
import urllib.error
import urllib.request
from dataclasses import dataclass


@dataclass(frozen=True)
class HttpRequest:
    method: str
    url: str
    headers: dict[str, str]
    body: bytes | None


@dataclass(frozen=True)
class HttpResponse:
    status: int
    headers: dict[str, str]
    body: bytes


class UrllibTransport:
    """Blocking transport used for real sandbox calls."""

    def __init__(self, timeout: float = 30) -> None:
        self._timeout = timeout

    def send(self, request: HttpRequest) -> HttpResponse:
        outgoing = urllib.request.Request(
            request.url,
            data=request.body,
            method=request.method,
            headers=request.headers,
        )
        try:
            with urllib.request.urlopen(outgoing, timeout=self._timeout) as response:
                try:
                    body = response.read()
                except OSError as error:
                    raise TimeoutError("read timed out") from error
                return HttpResponse(response.status, _header_map(response.headers), body)
        except urllib.error.HTTPError as error:
            return HttpResponse(error.code, _header_map(error.headers), error.read())
        except urllib.error.URLError as error:
            if _is_timeout(error.reason):
                raise TimeoutError("request timed out") from error
            raise


def _is_timeout(reason: object) -> bool:
    if isinstance(reason, (TimeoutError, socket.timeout)):
        return True
    return isinstance(reason, OSError) and "timed out" in str(reason).lower()


def _header_map(headers) -> dict[str, str]:
    if headers is None:
        return {}
    return {key: value for key, value in headers.items()}
