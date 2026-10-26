"""Shared status vocabulary and exit codes.

A successful request that matched no rows is not a failure. Every command
reports one of these, and the exit code follows from it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# -- statuses -------------------------------------------------------------

OK = "OK"
NO_MEETINGS = "NO_MEETINGS"
CONTENT_UNAVAILABLE = "CONTENT_UNAVAILABLE"
MULTIPLE_MEETINGS = "MULTIPLE_MEETINGS"
IDENTITY_REVIEW_REQUIRED = "IDENTITY_REVIEW_REQUIRED"
CONFIGURATION_ERROR = "CONFIGURATION_ERROR"
AUTHENTICATION_ERROR = "AUTHENTICATION_ERROR"
PERMISSION_ERROR = "PERMISSION_ERROR"
PROVIDER_ERROR = "PROVIDER_ERROR"
PARTIAL_RESULT = "PARTIAL_RESULT"
SEND_RESULT_UNKNOWN = "SEND_RESULT_UNKNOWN"
NOT_FOUND = "NOT_FOUND"
USAGE_ERROR = "USAGE_ERROR"

#: Statuses that mean "the request worked, there was simply nothing to return".
EMPTY_OK = frozenset({NO_MEETINGS})

EXIT_CODES = {
    OK: 0,
    NO_MEETINGS: 0,
    MULTIPLE_MEETINGS: 2,
    CONTENT_UNAVAILABLE: 3,
    IDENTITY_REVIEW_REQUIRED: 4,
    USAGE_ERROR: 64,
    NOT_FOUND: 66,
    CONFIGURATION_ERROR: 78,
    AUTHENTICATION_ERROR: 77,
    PERMISSION_ERROR: 77,
    PROVIDER_ERROR: 69,
    PARTIAL_RESULT: 75,
    SEND_RESULT_UNKNOWN: 76,
}

DEFAULT_EXIT_CODE = 70


def exit_code_for(status: str) -> int:
    return EXIT_CODES.get(status, DEFAULT_EXIT_CODE)


@dataclass
class Finding:
    """One doctor result. `ok=False` is a problem, `ok=None` is informational."""

    check: str
    ok: bool | None
    detail: str
    remedy: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "check": self.check,
            "ok": self.ok,
            "detail": self.detail,
            "remedy": self.remedy,
        }


@dataclass
class Result:
    """The outcome of one command."""

    command: str
    status: str
    data: dict[str, Any] = field(default_factory=dict)
    findings: list[Finding] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    incomplete: list[str] = field(default_factory=list)

    def as_dict(self, *, include_findings: bool = True) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "command": self.command,
            "status": self.status,
            "exitCode": exit_code_for(self.status),
        }
        payload.update(self.data)
        if include_findings and self.findings:
            payload["findings"] = [f.as_dict() for f in self.findings]
        if self.notes:
            payload["notes"] = self.notes
        if self.warnings:
            payload["warnings"] = self.warnings
        if self.incomplete:
            payload["incomplete"] = self.incomplete
            payload["complete"] = False
        else:
            payload.setdefault("complete", True)
        return payload