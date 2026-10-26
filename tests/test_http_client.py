"""HTTP helper tests.

Uses a local `http.server` on 127.0.0.1 so no external network is touched. The
properties under test are the ones the toolkit depends on: explicit timeouts,
bounded retries, no retries for non-idempotent submissions, and no credential
material in any error.
"""

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

TOOLKIT_ROOT = Path(__file__).resolve().parent.parent
if str(TOOLKIT_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOLKIT_ROOT))

from lib.http_client import (  # noqa: E402
    DecodeError,
    HttpClient,
    HttpError,
    TransportError,
    sanitize_url,
)

STATE: dict[str, int] = {}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # silence
        return

    def do_GET(self):  # noqa: N802
        if self.path.startswith("/ok"):
            self._send(200, {"hello": "world"})
        elif self.path.startswith("/flaky"):
            STATE["flaky"] = STATE.get("flaky", 0) + 1
            if STATE["flaky"] < 3:
                self._send(503, {"error": "not ready"})
            else:
                self._send(200, {"attempts": STATE["flaky"]})
        elif self.path.startswith("/boom"):
            self._send(500, {"error": "server exploded"})
        elif self.path.startswith("/missing"):
            self._send(404, {"error": "nope"})
        elif self.path.startswith("/unauthorized"):
            self._send(401, {"error": "bad token"})
        elif self.path.startswith("/throttled"):
            self._send(429, {"error": "slow down"})
        elif self.path.startswith("/leaky"):
            self._send(400, {"error": "Authorization=Bearer leaky-secret-value"})
        elif self.path.startswith("/notjson"):
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"definitely not json")
        else:
            self._send(404, {"error": "unknown"})

    def do_POST(self):  # noqa: N802
        STATE["posts"] = STATE.get("posts", 0) + 1
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        self._send(202, {"received": len(body), "posts": STATE["posts"]})

    def _send(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("request-id", "req-123")
        self.end_headers()
        self.wfile.write(body)


def _serve():
    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{server.server_port}"


def test_successful_get() -> None:
    server, base = _serve()
    try:
        client = HttpClient(max_attempts=1)
        response = client.get(f"{base}/ok")
        assert response.status == 200
        assert response.json() == {"hello": "world"}
        assert response.header("request-id") == "req-123"
        assert response.elapsed_ms >= 0
    finally:
        server.shutdown()


def test_header_lookup_is_case_insensitive() -> None:
    server, base = _serve()
    try:
        response = HttpClient(max_attempts=1).get(f"{base}/ok")
        assert response.header("Request-Id") == "req-123"
        assert response.header("absent") is None
    finally:
        server.shutdown()


def test_retryable_status_is_retried_until_success() -> None:
    server, base = _serve()
    try:
        STATE["flaky"] = 0
        client = HttpClient(max_attempts=5, backoff_seconds=0.0, sleep=lambda _s: None)
        response = client.get(f"{base}/flaky")
        assert response.status == 200
        assert response.json()["attempts"] == 3
    finally:
        server.shutdown()


def test_retries_are_bounded() -> None:
    server, base = _serve()
    try:
        calls = {"n": 0}

        def counting_sleep(_seconds):
            calls["n"] += 1

        client = HttpClient(max_attempts=2, backoff_seconds=0.0, sleep=counting_sleep)
        try:
            client.get(f"{base}/boom")
        except HttpError as exc:
            assert exc.status == 500
            assert exc.retryable is True
        else:
            raise AssertionError("expected HttpError")
        assert calls["n"] == 1, "two attempts means exactly one sleep"
    finally:
        server.shutdown()


def test_client_error_is_not_retried() -> None:
    server, base = _serve()
    try:
        sleeps = []
        client = HttpClient(max_attempts=4, backoff_seconds=0.0, sleep=sleeps.append)
        try:
            client.get(f"{base}/missing")
        except HttpError as exc:
            assert exc.status == 404
            assert exc.retryable is False
        else:
            raise AssertionError("expected HttpError")
        assert sleeps == [], "a 404 must not be retried"
    finally:
        server.shutdown()


def test_retry_false_sends_exactly_one_request() -> None:
    server, base = _serve()
    try:
        STATE["posts"] = 0
        client = HttpClient(max_attempts=5, backoff_seconds=0.0, sleep=lambda _s: None)
        response = client.post(f"{base}/send", data=b"{}", retry=False)
        assert response.status == 202
        assert STATE["posts"] == 1
    finally:
        server.shutdown()


def test_http_error_message_carries_no_query_string() -> None:
    server, base = _serve()
    try:
        client = HttpClient(max_attempts=1)
        try:
            client.get(f"{base}/missing?apikey=secret-value-123")
        except HttpError as exc:
            assert "apikey" not in exc.message
            assert "secret-value-123" not in exc.message
        else:
            raise AssertionError("expected HttpError")
    finally:
        server.shutdown()


def test_sanitize_url_strips_query_and_userinfo() -> None:
    assert sanitize_url("https://host/path?a=1&b=2") == "https://host/path"
    assert sanitize_url("https://u:p@host/path") == "https://host/path"
    assert sanitize_url("https://host/path") == "https://host/path"


def test_sanitize_url_never_returns_free_text() -> None:
    """Anything that is not a URL must become a fixed placeholder.

    A colon in free text can be parsed as a scheme, which once produced
    `://not a url` - a string with no host in it that still looked like a URL.
    The value must not leak through in any form.
    """
    for value in ("not a url", "12:30:45", "C:\\temp", "http://", ""):
        rendered = sanitize_url(value)
        assert rendered in ("(unparseable url)",), (
            f"{value!r} produced {rendered!r}"
        )


def test_transport_failure_is_classified_separately() -> None:
    client = HttpClient(timeout_seconds=0.5, max_attempts=1)
    try:
        client.get("http://127.0.0.1:1/nowhere")
    except TransportError as exc:
        assert "response" in exc.message or "timed out" in exc.message
    except HttpError as exc:
        raise AssertionError(f"expected TransportError, got HttpError {exc.status}")
    else:
        raise AssertionError("expected TransportError")


def test_invalid_json_body_raises_decode_style_error() -> None:
    server, base = _serve()
    try:
        response = HttpClient(max_attempts=1).get(f"{base}/notjson")
        assert response.text() == "definitely not json"
        try:
            response.json()
        except json.JSONDecodeError:
            pass
        else:
            raise AssertionError("expected a JSON decode failure")
    finally:
        server.shutdown()


def test_timeout_must_be_positive() -> None:
    try:
        HttpClient(timeout_seconds=0)
    except ValueError:
        return
    raise AssertionError("expected ValueError for a zero timeout")


def test_max_attempts_must_be_at_least_one() -> None:
    try:
        HttpClient(max_attempts=0)
    except ValueError:
        return
    raise AssertionError("expected ValueError for max_attempts=0")