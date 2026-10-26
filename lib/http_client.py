"""Bounded HTTP helper built on urllib. No third-party dependency.

Rules this module exists to enforce:

- every request has an explicit timeout;
- read requests may retry a bounded number of times on transport errors and
  5xx/429, but never on 4xx;
- authorization headers, tokens and body contents never appear in errors;
- a transport failure and an HTTP error are different outcomes.
"""

from __future__ import annotations

import json
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Mapping

DEFAULT_TIMEOUT_SECONDS = 30.0
DEFAULT_MAX_ATTEMPTS = 3
RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})
_USER_AGENT = "Kik-toolkit/0.1 (+read-only operational helper)"


@dataclass
class HttpResponse:
    status: int
    headers: dict[str, str]
    body: bytes
    url: str
    elapsed_ms: int

    def json(self) -> Any:
        if not self.body:
            return None
        return json.loads(self.body.decode("utf-8", "replace"))

    def text(self) -> str:
        return self.body.decode("utf-8", "replace")

    def header(self, name: str) -> str | None:
        target = name.lower()
        for key, value in self.headers.items():
            if key.lower() == target:
                return value
        return None


class HttpError(Exception):
    """HTTP-level failure with a sanitized message."""

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        url: str = "",
        retryable: bool = False,
        detail: str | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status = status
        self.url = url
        self.retryable = retryable
        self.detail = detail

    def as_dict(self) -> dict[str, Any]:
        return {
            "message": self.message,
            "status": self.status,
            "url": sanitize_url(self.url),
            "retryable": self.retryable,
            "detail": self.detail,
        }


class TransportError(Exception):
    """The request never produced an HTTP status (DNS, TLS, timeout, reset)."""

    def __init__(self, message: str, *, url: str = "", attempts: int = 1) -> None:
        super().__init__(message)
        self.message = message
        self.url = url
        self.attempts = attempts

    def as_dict(self) -> dict[str, Any]:
        return {
            "message": self.message,
            "url": sanitize_url(self.url),
            "attempts": self.attempts,
        }


class DecodeError(Exception):
    """The response arrived but was not the shape the caller required."""

    def __init__(self, message: str, *, url: str = "", status: int | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.url = url
        self.status = status

    def as_dict(self) -> dict[str, Any]:
        return {"message": self.message, "url": sanitize_url(self.url), "status": self.status}


_PLACEHOLDER = "(unparseable url)"


def sanitize_url(url: str) -> str:
    """Strip query strings and userinfo so no key can ride along in an error.

    Returns a fixed placeholder - never the input - when the value is not a URL
    with a scheme and a host. Free text containing a colon (a clock time, a
    Windows path) otherwise parses as scheme-plus-path and would be echoed back
    looking harmless while leaking the value.
    """
    if not isinstance(url, str) or not url.strip():
        return _PLACEHOLDER
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return _PLACEHOLDER
    host = parts.hostname
    if not parts.scheme or not host:
        return _PLACEHOLDER
    port = f":{parts.port}" if parts.port else ""
    path = parts.path or ""
    return f"{parts.scheme}://{host}{port}{path}"


def _sanitize_detail(body: bytes, limit: int = 300) -> str:
    text = body.decode("utf-8", "replace")
    # Never echo anything that looks like a credential back to a terminal.
    lowered = text.lower()
    for marker in ("authorization", "bearer ", "apikey", "access_token", "client_secret"):
        index = lowered.find(marker)
        if index != -1:
            text = text[:index] + "[redacted]"
            break
    text = " ".join(text.split())
    return text[:limit]


class HttpClient:
    def __init__(
        self,
        *,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        backoff_seconds: float = 0.6,
        user_agent: str = _USER_AGENT,
        ssl_context: ssl.SSLContext | None = None,
        sleep=time.sleep,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        self.timeout_seconds = timeout_seconds
        self.max_attempts = max_attempts
        self.backoff_seconds = backoff_seconds
        self.user_agent = user_agent
        self.ssl_context = ssl_context
        self._sleep = sleep

    # -- public ------------------------------------------------------------

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        json_body: Any | None = None,
        data: bytes | None = None,
        content_type: str | None = None,
        timeout_seconds: float | None = None,
        max_attempts: int | None = None,
        retry: bool = True,
    ) -> HttpResponse:
        """Perform one logical request, with bounded retries for safe reads.

        `retry=False` is required for anything that could have been received by
        the other side - a mail submission, for instance.
        """
        attempts_allowed = max_attempts if max_attempts is not None else self.max_attempts
        if not retry:
            attempts_allowed = 1

        request_headers = {"User-Agent": self.user_agent, "Accept": "application/json"}
        if headers:
            request_headers.update(headers)

        payload = data
        if json_body is not None:
            payload = json.dumps(json_body).encode("utf-8")
            request_headers.setdefault("Content-Type", "application/json")
        if content_type:
            request_headers["Content-Type"] = content_type

        last_transport: TransportError | None = None
        for attempt in range(1, attempts_allowed + 1):
            started = time.monotonic()
            request = urllib.request.Request(
                url, data=payload, headers=request_headers, method=method.upper()
            )
            try:
                with urllib.request.urlopen(
                    request,
                    timeout=timeout_seconds or self.timeout_seconds,
                    context=self.ssl_context,
                ) as response:
                    body = response.read()
                    return HttpResponse(
                        status=response.status,
                        headers={k: v for k, v in response.headers.items()},
                        body=body,
                        url=url,
                        elapsed_ms=int((time.monotonic() - started) * 1000),
                    )
            except urllib.error.HTTPError as exc:
                body = exc.read() if hasattr(exc, "read") else b""
                status = exc.code
                retryable = status in RETRYABLE_STATUS
                if retryable and attempt < attempts_allowed:
                    self._sleep(self.backoff_seconds * (2 ** (attempt - 1)))
                    continue
                raise HttpError(
                    f"HTTP {status} from {sanitize_url(url)}",
                    status=status,
                    url=url,
                    retryable=retryable,
                    detail=_sanitize_detail(body),
                ) from None
            except urllib.error.URLError as exc:
                last_transport = TransportError(
                    f"transport error before a response: {type(exc.reason).__name__}",
                    url=url,
                    attempts=attempt,
                )
            except TimeoutError:
                last_transport = TransportError(
                    "request timed out before a response", url=url, attempts=attempt
                )
            except ssl.SSLError as exc:
                raise TransportError(
                    f"TLS failure: {exc.__class__.__name__}", url=url, attempts=attempt
                ) from None
            except Exception as exc:  # noqa: BLE001 - last-resort classification
                raise TransportError(
                    f"unexpected transport failure: {exc.__class__.__name__}",
                    url=url,
                    attempts=attempt,
                ) from None

            if attempt < attempts_allowed:
                self._sleep(self.backoff_seconds * (2 ** (attempt - 1)))

        assert last_transport is not None
        raise last_transport

    def get(self, url: str, **kwargs: Any) -> HttpResponse:
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> HttpResponse:
        return self.request("POST", url, **kwargs)