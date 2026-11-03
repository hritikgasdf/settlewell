"""urllib timeouts become TimeoutError. No live network."""

import urllib.error
import urllib.request

import pytest

from backend.airwallex.http import HttpRequest, UrllibTransport


def test_a_wrapped_socket_timeout_becomes_timeout_error(monkeypatch):
    def explode(*_args, **_kwargs):
        raise urllib.error.URLError(TimeoutError("timed out"))

    monkeypatch.setattr(urllib.request, "urlopen", explode)
    request = HttpRequest("GET", "https://api-demo.airwallex.com/api/v1/balances/current", {}, None)
    with pytest.raises(TimeoutError):
        UrllibTransport().send(request)


def test_a_read_oserror_becomes_timeout_error(monkeypatch):
    class Body:
        status = 200
        headers = None

        def read(self):
            raise OSError("timed out")

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(urllib.request, "urlopen", lambda *_args, **_kwargs: Body())
    request = HttpRequest("GET", "https://api-demo.airwallex.com/api/v1/balances/current", {}, None)
    with pytest.raises(TimeoutError):
        UrllibTransport().send(request)
