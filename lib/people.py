"""People and meeting extraction from provider payloads.

Pure functions over already-retrieved data. No database access, no network, no
matching decisions - `identities.py` owns matching, this module owns reading.

`extract_attendees` returns **facts about the stored data**: which addresses
appear in the attendee array, and which are absent. It never concludes that
anyone attended a meeting, and never concludes that an absent address did not
join it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

#: Addresses the team has asked to be reported explicitly, with the exact
#: statement each one supports. The statement is deliberately about the *list*.
WATCHED_ADDRESSES: dict[str, str] = {
    "teams@maestrolabs.com": (
        "absent from the stored attendee list; this list does not establish "
        "whether the bot joined"
    ),
    "vlsi_delivery@siliconpatterns.com": (
        "present in the stored attendee list and/or as the source account; "
        "presence in the list does not establish that this account attended"
    ),
}


@dataclass
class AttendeeExtract:
    entries: list[dict[str, Any]] = field(default_factory=list)
    malformed_count: int = 0
    shapes_seen: list[str] = field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.entries)

    def emails(self) -> list[str]:
        return [e["email"] for e in self.entries if e.get("email")]

    def names(self) -> list[str]:
        return [e["name"] for e in self.entries if e.get("name")]


def extract_attendees(value: Any) -> AttendeeExtract:
    """Read the `meetings.attendees` JSON array without assuming its shape.

    The column is `Json?`, so an object entry (`{name, email, company,
    is_external}`) is the observed case but a bare string is possible. Unknown
    shapes are counted, not silently dropped.
    """
    result = AttendeeExtract()
    if value is None:
        return result
    if not isinstance(value, list):
        result.malformed_count += 1
        result.shapes_seen.append(f"not-a-list:{type(value).__name__}")
        return result

    for entry in value:
        if isinstance(entry, dict):
            result.shapes_seen.append("object")
            result.entries.append(
                {
                    "name": _clean(entry.get("name")),
                    "email": _clean(entry.get("email")),
                    "company": _clean(entry.get("company")),
                    "isExternal": entry.get("is_external")
                    if isinstance(entry.get("is_external"), bool)
                    else None,
                    "rawKeys": sorted(entry.keys()),
                }
            )
        elif isinstance(entry, str):
            result.shapes_seen.append("string")
            result.entries.append(
                {"name": _clean(entry), "email": None, "company": None, "isExternal": None}
            )
        else:
            result.shapes_seen.append(f"other:{type(entry).__name__}")
            result.malformed_count += 1
    return result


def _clean(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def watched_address_report(
    *,
    attendee_emails: Iterable[str],
    organizer_email: str | None = None,
    source_account_email: str | None = None,
) -> list[dict[str, Any]]:
    """Report each watched address as a fact plus the supported reading.

    The reading is fixed per address and is about the attendee list only. This
    function must not be extended to assert attendance.
    """
    present = {str(e).strip().lower() for e in attendee_emails if e}
    report: list[dict[str, Any]] = []
    for address, statement in WATCHED_ADDRESSES.items():
        in_list = address.lower() in present
        report.append(
            {
                "address": address,
                "inStoredAttendeeList": in_list,
                "isOrganizer": (organizer_email or "").strip().lower() == address.lower(),
                "isSourceAccount": (source_account_email or "").strip().lower()
                == address.lower(),
                "statement": statement,
                "doesNotEstablish": "attendance, or the absence of attendance",
            }
        )
    return report


# -- provider meeting id extraction -------------------------------------


@dataclass
class ProviderIdExtraction:
    provider_meeting_id: str | None
    json_path: str | None
    candidates: list[dict[str, Any]] = field(default_factory=list)
    parse_error: str | None = None

    @property
    def found(self) -> bool:
        return self.provider_meeting_id is not None

    def as_dict(self) -> dict[str, Any]:
        return {
            "providerMeetingId": self.provider_meeting_id,
            "jsonPath": self.json_path,
            "candidates": self.candidates,
            "parseError": self.parse_error,
        }


#: Ordered by specificity. The first match wins, and the path is recorded so a
#: reader can see exactly where the value came from rather than trusting it.
_ID_PATHS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("data.meeting.id", ("data", "meeting", "id")),
    ("meeting.id", ("meeting", "id")),
    ("data.meetingId", ("data", "meetingId")),
    ("data.meeting_id", ("data", "meeting_id")),
    ("payload.meeting.id", ("payload", "meeting", "id")),
)


def extract_provider_meeting_id(raw_body: str | bytes | None) -> ProviderIdExtraction:
    """Find the provider meeting id inside a webhook body.

    Only the documented, column-adjacent paths are used - no heuristic search for
    anything that looks like an id. If the body cannot be parsed, that is
    reported rather than guessed at.
    """
    if raw_body is None:
        return ProviderIdExtraction(None, None, parse_error="no body")
    if isinstance(raw_body, bytes):
        try:
            text = raw_body.decode("utf-8")
        except UnicodeDecodeError as exc:
            return ProviderIdExtraction(None, None, parse_error=f"not UTF-8: {exc}")
    else:
        text = raw_body

    import json

    try:
        document = json.loads(text)
    except json.JSONDecodeError as exc:
        return ProviderIdExtraction(None, None, parse_error=f"not JSON: {exc.msg}")

    for path, keys in _ID_PATHS:
        value = _dig(document, keys)
        if isinstance(value, str) and value.strip():
            return ProviderIdExtraction(value.strip(), path)

    # Record what was available, so a missing id is diagnosable.
    candidates: list[dict[str, Any]] = []
    for path, keys in _ID_PATHS:
        value = _dig(document, keys)
        if value is not None:
            candidates.append({"jsonPath": path, "type": type(value).__name__})
    return ProviderIdExtraction(None, None, candidates=candidates)


def _dig(document: Any, keys: tuple[str, ...]) -> Any:
    node = document
    for key in keys:
        if not isinstance(node, dict) or key not in node:
            return None
        node = node[key]
    return node