"""Timezone resolution with a fallback for hosts without tzdata.

This host (an Android/Alpine container running CPython from Termux) has **no
tz database**: every `zoneinfo.TZPATH` entry is missing, so
`ZoneInfo("Asia/Kolkata")` raises `ZoneInfoNotFoundError` and every
midnight-to-midnight calculation fails.

Resolution order:

1. `zoneinfo`, when the tz database is present. Authoritative, handles DST.
2. An explicit table of zones whose offset is **constant year-round**, used only
   when the host has no tzdata. A fixed offset for such a zone is not an
   approximation - it *is* the zone. The result is tagged
   `source="fixed-offset-fallback"` and every command that used it warns.

Two things are refused rather than guessed:

- a zone that observes DST, because its standard offset would be wrong for part
  of the year;
- any zone not in the table, because inventing an offset would corrupt the day
  boundaries every query depends on.

The fallback exists for contemporary meeting dates on this host. It is not a
general timezone implementation.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta, timezone, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

SOURCE_TZDATA = "tzdata"
SOURCE_FALLBACK = "fixed-offset-fallback"

TZDATA_AVAILABLE = False
try:
    ZoneInfo("UTC")
    TZDATA_AVAILABLE = True
except (ZoneInfoNotFoundError, ValueError):  # pragma: no cover - host dependent
    TZDATA_AVAILABLE = False

#: Zones the fallback is **explicitly supported** for.
#:
#: Support requires that the offset be constant year-round, so a fixed offset is
#: not an approximation of the zone - it *is* the zone. A zone that observes DST
#: is deliberately absent, even though its standard offset is easy to write down,
#: because using it would be silently wrong for part of the year. An unsupported
#: zone is an error, never a best-effort guess.
SUPPORTED_FALLBACK_ZONES: frozenset[str] = frozenset(
    {
        "Asia/Kolkata",  # +05:30, no DST, ever
        "Asia/Calcutta",  # legacy alias of the above
        "Asia/Dubai",  # +04:00, no DST
        "Asia/Singapore",  # +08:00, no DST
        "Asia/Tokyo",  # +09:00, no DST
        "UTC",
        "Etc/UTC",
    }
)

#: Zones a caller may reasonably try that the fallback must **refuse**.
#: Present so the refusal is documented and testable rather than incidental.
KNOWN_DST_ZONES: frozenset[str] = frozenset(
    {"Europe/London", "Europe/Berlin", "America/New_York", "Australia/Sydney"}
)

FALLBACK_OFFSETS: dict[str, int] = {
    "Asia/Kolkata": 5 * 3600 + 1800,
    "Asia/Calcutta": 5 * 3600 + 1800,
    "Asia/Dubai": 4 * 3600,
    "Asia/Singapore": 8 * 3600,
    "Asia/Tokyo": 9 * 3600,
    "UTC": 0,
    "Etc/UTC": 0,
}

assert set(FALLBACK_OFFSETS) == set(SUPPORTED_FALLBACK_ZONES), (
    "every supported fallback zone must have an offset, and vice versa"
)


class TimezoneError(Exception):
    """The requested zone cannot be resolved on this host."""


@dataclass(frozen=True)
class ResolvedTimezone:
    name: str
    tz: tzinfo
    source: str
    utc_offset_seconds: int
    note: str | None = None

    @property
    def is_fallback(self) -> bool:
        return self.source == SOURCE_FALLBACK

    def as_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "source": self.source,
            "utcOffsetSeconds": self.utc_offset_seconds,
            "note": self.note,
        }


def resolve(name: str) -> ResolvedTimezone:
    """Resolve an IANA zone name, falling back to a fixed offset if needed."""
    if not name or not name.strip():
        raise TimezoneError("timezone must not be empty")
    candidate = name.strip()

    if TZDATA_AVAILABLE:
        try:
            zone = ZoneInfo(candidate)
            offset = _offset_seconds(zone)
            return ResolvedTimezone(
                name=candidate,
                tz=zone,
                source=SOURCE_TZDATA,
                utc_offset_seconds=offset,
            )
        except ZoneInfoNotFoundError as exc:
            raise TimezoneError(
                f"unknown timezone {candidate!r} (tz database is present)"
            ) from exc
        except ValueError as exc:
            raise TimezoneError(f"invalid timezone {candidate!r}") from exc

    offset = FALLBACK_OFFSETS.get(candidate)
    if offset is None:
        if candidate in KNOWN_DST_ZONES:
            raise TimezoneError(
                f"timezone {candidate!r} observes daylight saving and this host has "
                "no tz database, so no fixed offset represents it. Its standard "
                "offset will not be used silently. Install tzdata (or set "
                "PYTHONTZPATH) to use this zone."
            )
        raise TimezoneError(
            f"unknown timezone {candidate!r}, and this host has no tz database. "
            f"Supported fallback zones: {', '.join(sorted(SUPPORTED_FALLBACK_ZONES))}. "
            "A zone absent from that list is rejected rather than approximated."
        )
    return ResolvedTimezone(
        name=candidate,
        tz=timezone(timedelta(seconds=offset), name=candidate),
        source=SOURCE_FALLBACK,
        utc_offset_seconds=offset,
        note=(
            "resolved from the fixed-offset fallback table because this host has no "
            "tz database; this zone has a constant offset year-round, so the fixed "
            "offset is the zone rather than an approximation of it"
        ),
    )


def _offset_seconds(zone: ZoneInfo) -> int:
    from datetime import datetime

    return int(datetime(2026, 1, 1, tzinfo=zone).utcoffset().total_seconds())  # type: ignore[union-attr]


def diagnostics() -> dict[str, object]:
    return {
        "tzdataAvailable": TZDATA_AVAILABLE,
        "searchPaths": list(__import__("zoneinfo").TZPATH),
        "supportedFallbackZones": sorted(SUPPORTED_FALLBACK_ZONES),
        "refusedDstZones": sorted(KNOWN_DST_ZONES),
        "note": (
            "When tzdata is unavailable, only zones with a year-round constant "
            "offset resolve, and each result that used one is tagged and warned. "
            "A zone that observes DST is refused rather than approximated by its "
            "standard offset. An unknown zone is always an error."
        ),
    }