"""Output helpers.

Hard rules:

- progress and diagnostics go to **stderr**;
- `--json` sends exactly one JSON document to **stdout** and nothing else;
- human output is written to stdout only;
- secret-shaped values are redacted before display, unconditionally.
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass
from typing import Any, Iterable, TextIO

from . import status as status_mod

REDACTED = "[redacted]"

#: Patterns whose matches are credentials and must never be printed.
#:
#: These are deliberately conservative. An earlier version also redacted any
#: 40+ character opaque run, which silently destroyed legitimate identifiers
#: (provider meeting ids and content hashes are exactly that shape). Redaction
#: is for credentials, not for looking tidy: a redacted content hash makes a
#: bundle unusable for the agent that has to cite it.
_CREDENTIAL_PATTERNS = (
    re.compile(r"Bearer\s+[A-Za-z0-9._\-]+", re.IGNORECASE),
    re.compile(r"\beyJ[A-Za-z0-9._\-]{16,}"),  # JWT (header.payload.signature)
    re.compile(r"(?i)\b(?:sk|pk|rk|ghp|gho|xox[baprs])[-_][A-Za-z0-9_\-]{16,}"),
    re.compile(r"(?i)(client_secret|secret|access_token|api[_-]?key|apikey)"
               r"\s*[=:]\s*[A-Za-z0-9._\-~]{8,}"),
    re.compile(r"postgres(?:ql)?://[^\s]+", re.IGNORECASE),
    re.compile(r"redis://[^\s]+", re.IGNORECASE),
    re.compile(r"https://[^\s/]*:[^\s/]*@[^\s]+"),  # URL with userinfo
)

#: Kept for callers that import the old name.
_TOKEN_PATTERNS = _CREDENTIAL_PATTERNS


@dataclass
class Output:
    json_mode: bool = False
    stdout: TextIO = sys.stdout
    stderr: TextIO = sys.stderr
    quiet: bool = False

    # -- diagnostics (stderr) ---------------------------------------------

    def progress(self, message: str) -> None:
        if self.quiet or self.json_mode:
            return
        print(redact(message), file=self.stderr)

    def warn(self, message: str) -> None:
        print(redact(f"warning: {message}"), file=self.stderr)

    def error(self, message: str) -> None:
        print(redact(f"error: {message}"), file=self.stderr)

    def note(self, message: str) -> None:
        if self.json_mode:
            return
        print(redact(message), file=self.stderr)

    # -- payload (stdout) --------------------------------------------------

    def emit_json(self, payload: Any) -> None:
        json.dump(_clean(payload), self.stdout, indent=2, sort_keys=False, default=str)
        self.stdout.write("\n")
        self.stdout.flush()

    def emit_lines(self, lines: Iterable[str]) -> None:
        for line in lines:
            self.stdout.write(redact(line) + "\n")
        self.stdout.flush()

    def emit_text(self, text: str) -> None:
        self.stdout.write(redact(text))
        if not text.endswith("\n"):
            self.stdout.write("\n")
        self.stdout.flush()


def redact(text: str) -> str:
    out = text
    for pattern in _TOKEN_PATTERNS:
        out = pattern.sub(REDACTED, out)
    return out


def _clean(value: Any) -> Any:
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {k: _clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_clean(v) for v in value]
    return value


def emit_result(output: Output, result: status_mod.Result) -> int:
    """Print one command result and return the process exit code."""
    code = status_mod.exit_code_for(result.status)

    if output.json_mode:
        output.emit_json(result.as_dict())
        # Warnings go to stderr *after* the document, deliberately. Ordering
        # them first reads better, but a caller that pipes stdout to a parser
        # must never be racing an interleaved write - and if the JSON encode
        # itself fails, no diagnostic claim about the run has been made yet.
        for warning in result.warnings:
            output.warn(warning)
        for item in result.incomplete:
            output.warn(f"incomplete: {item}")
        return code

    for finding in result.findings:
        mark = {True: "OK", False: "FAIL", None: "INFO"}[finding.ok]
        output.emit_lines([f"[{mark}] {finding.check}: {finding.detail}"])
        if finding.ok is False and finding.remedy:
            output.emit_lines([f"        remedy: {finding.remedy}"])

    lines = render_data(result)
    if lines:
        output.emit_lines(lines)

    for note in result.notes:
        output.note(note)
    for warning in result.warnings:
        output.warn(warning)
    for item in result.incomplete:
        output.warn(f"incomplete: {item}")

    output.emit_lines([f"status: {result.status}"])
    return code


def render_data(result: status_mod.Result, prefix: str = "") -> list[str]:
    """Render the command payload as aligned key/value lines."""
    lines: list[str] = []
    for key, value in result.data.items():
        lines.extend(_render_value(key, value, prefix))
    return lines


def _render_value(key: str, value: Any, prefix: str) -> list[str]:
    label = f"{prefix}{key}"
    if isinstance(value, dict):
        return [f"{label}:"] + [
            line for k, v in value.items() for line in _render_value(k, v, prefix + "  ")
        ]
    if isinstance(value, list):
        if not value:
            return [f"{label}: (none)"]
        out = [f"{label}:"]
        for item in value:
            if isinstance(item, dict):
                out.append(f"{prefix}  -")
                for k, v in item.items():
                    out.extend(_render_value(k, v, prefix + "    "))
            else:
                out.append(f"{prefix}  - {item}")
        return out
    return [f"{label}: {value if value is not None else '(null)'}"]