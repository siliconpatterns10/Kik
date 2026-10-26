"""Local date-interval handling.

A "business day" is a local midnight-to-midnight interval converted to UTC for
queries. For Asia/Kolkata that is +05:30 with no DST, so 2026-10-20 local is
2026-10-19T18:30:00Z inclusive to 2026-10-20T18:30:00Z exclusive. The
arithmetic uses the tz database when the host has one, and otherwise falls back
to a fixed offset **with a warning** - see `timezones.py`.

Interval endpoints are always reported as inclusive start / exclusive end.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from . import timezones

BUSINESS_TZ = "Asia/Kolkata"


class DateError(Exception):
    """A date or timezone the operator can fix."""


@dataclass(frozen=True)
class LocalDay:
    """One local calendar day and its UTC equivalent."""

    day: date
    timezone_name: str
    timezone_source: str
    utc_offset_seconds: int
    local_start: datetime
    local_end: datetime
    utc_start: datetime
    utc_end: datetime
    warning: str | None = None

    @property
    def utc_start_text(self) -> str:
        return to_postgrest(self.utc_start)

    @property
    def utc_end_text(self) -> str:
        return to_postgrest(self.utc_end)

    @property
    def local_start_text(self) -> str:
        return iso(self.local_start)

    @property
    def local_end_text(self) -> str:
        return iso(self.local_end)

    def contains_utc(self, value: datetime | None) -> bool:
        if value is None:
            return False
        moment = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return self.utc_start <= moment.astimezone(timezone.utc) < self.utc_end

    def as_dict(self) -> dict[str, object]:
        return {
            "localDate": self.day.isoformat(),
            "timezone": self.timezone_name,
            "timezoneSource": self.timezone_source,
            "utcOffsetSeconds": self.utc_offset_seconds,
            "localInterval": {
                "start": self.local_start_text,
                "endExclusive": self.local_end_text,
            },
            "utcInterval": {
                "start": self.utc_start_text,
                "endExclusive": self.utc_end_text,
            },
        }


def iso(moment: datetime) -> str:
    """ISO 8601 with a fixed-width offset, e.g. 2026-10-20T00:00:00+05:30."""
    return moment.isoformat(timespec="seconds")


def to_postgrest(moment: datetime) -> str:
    """UTC instant in the `...Z` form PostgREST compares against timestamptz."""
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_date(text: str) -> date:
    try:
        return date.fromisoformat(text.strip())
    except (ValueError, TypeError) as exc:
        raise DateError(f"--date must be YYYY-MM-DD, got {text!r}") from exc


def local_day(day: date, timezone_name: str) -> LocalDay:
    try:
        resolved = timezones.resolve(timezone_name)
    except timezones.TimezoneError as exc:
        raise DateError(str(exc)) from exc

    start = datetime(day.year, day.month, day.day, 0, 0, 0, tzinfo=resolved.tz)
    end = start + timedelta(days=1)
    warning = None
    if resolved.is_fallback:
        warning = (
            f"timezone {resolved.name} was resolved from the fixed-offset fallback "
            f"({resolved.utc_offset_seconds // 3600:+03d}:"
            f"{(abs(resolved.utc_offset_seconds) % 3600) // 60:02d}) because this host "
            "has no tz database. This zone has a constant offset year-round, so the "
            "fixed offset is exact for it; other zones are refused rather than "
            "approximated."
        )
    return LocalDay(
        day=day,
        timezone_name=resolved.name,
        timezone_source=resolved.source,
        utc_offset_seconds=resolved.utc_offset_seconds,
        local_start=start,
        local_end=end,
        utc_start=start.astimezone(timezone.utc),
        utc_end=end.astimezone(timezone.utc),
        warning=warning,
    )


def local_day_from_text(day_text: str, timezone_name: str) -> LocalDay:
    return local_day(parse_date(day_text), timezone_name)


def parse_timestamp(value: str | None) -> datetime | None:
    """Parse the timestamp forms PostgREST and the Render API return.

    Values without an offset are treated as UTC, which is what both sources
    store. A `Z` suffix and a `+00:00` suffix are equivalent.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        try:
            parsed = datetime.fromisoformat(text.split(".")[0])
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def format_local(moment: datetime | None, timezone_name: str) -> str | None:
    if moment is None:
        return None
    try:
        resolved = timezones.resolve(timezone_name)
    except timezones.TimezoneError:
        return None
    aware = moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)
    return aware.astimezone(resolved.tz).strftime("%Y-%m-%d %H:%M:%S %z")


def format_utc(moment: datetime | None) -> str | None:
    if moment is None:
        return None
    aware = moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)
    return aware.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def run_directory(config_runs_path, local_day_value: LocalDay, meeting_ref: str):
    """`<runs>/<local-date>/<meeting-or-event-ref>` as required by the layout."""
    return config_runs_path / local_day_value.day.isoformat() / meeting_ref