"""Local-day arithmetic tests.

The IST case is the one that matters: an inclusive local midnight converted to a
UTC value, with an exclusive upper bound. A different timezone is tested to make
sure nothing is hardcoded to +05:30.
"""

import sys
from datetime import date, datetime, timezone
from pathlib import Path

TOOLKIT_ROOT = Path(__file__).resolve().parent.parent
if str(TOOLKIT_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOLKIT_ROOT))

from lib import localdate, timezones  # noqa: E402


def test_ist_midnight_conversion() -> None:
    day = localdate.local_day(date(2026, 10, 20), "Asia/Kolkata")
    assert day.utc_start_text == "2026-10-19T18:30:00Z"
    assert day.utc_end_text == "2026-10-20T18:30:00Z"


def test_bounds_are_inclusive_start_exclusive_end() -> None:
    day = localdate.local_day(date(2026, 10, 20), "Asia/Kolkata")
    start = localdate.parse_timestamp(day.utc_start_text)
    end = localdate.parse_timestamp(day.utc_end_text)
    assert day.contains_utc(start) is True, "the start instant is inside the day"
    assert day.contains_utc(end) is False, "the end instant is outside the day"


def test_end_is_exactly_one_day_after_start() -> None:
    day = localdate.local_day(date(2026, 10, 20), "Asia/Kolkata")
    assert day.utc_end - day.utc_start == __import__("datetime").timedelta(days=1)


def test_offset_is_reported() -> None:
    day = localdate.local_day(date(2026, 10, 20), "Asia/Kolkata")
    assert day.utc_offset_seconds == 5 * 3600 + 1800


def test_local_interval_reads_back_as_local_midnight() -> None:
    day = localdate.local_day(date(2026, 10, 20), "Asia/Kolkata")
    assert day.local_start_text == "2026-10-20T00:00:00+05:30"
    assert day.local_end_text == "2026-10-21T00:00:00+05:30"


def test_different_timezone_is_not_hardcoded() -> None:
    day = localdate.local_day(date(2026, 10, 20), "UTC")
    assert day.utc_start_text == "2026-10-20T00:00:00Z"
    assert day.utc_end_text == "2026-10-21T00:00:00Z"
    assert day.utc_offset_seconds == 0


def test_invalid_date_is_rejected() -> None:
    for bad in ("2026-13-01", "not-a-date", "2026/10/20", ""):
        try:
            localdate.parse_date(bad)
        except localdate.DateError:
            continue
        raise AssertionError(f"expected DateError for {bad!r}")


def test_unknown_timezone_is_rejected_with_a_useful_message() -> None:
    try:
        localdate.local_day(date(2026, 10, 20), "Mars/Olympus_Mons")
    except localdate.DateError as exc:
        assert "Mars/Olympus_Mons" in str(exc)
    else:
        raise AssertionError("expected DateError for an unknown timezone")


def test_fallback_use_is_flagged_when_tzdata_is_absent() -> None:
    day = localdate.local_day(date(2026, 10, 20), "Asia/Kolkata")
    if timezones.TZDATA_AVAILABLE:
        assert day.timezone_source == timezones.SOURCE_TZDATA
        assert day.warning is None
    else:
        assert day.timezone_source == timezones.SOURCE_FALLBACK
        assert day.warning is not None, (
            "a fixed-offset fallback must never be silent"
        )
        assert "Asia/Kolkata" in day.warning


def test_fallback_refuses_a_dst_zone() -> None:
    """A zone that observes DST must be refused, never given its standard offset.

    The scope of the fallback is contemporary meeting dates on a host without
    tzdata. Europe/London is +00:00 in winter and +01:00 in summer; returning
    +00:00 year-round would silently misdate every summer day boundary, so the
    correct behaviour is a refusal the operator can act on.
    """
    if timezones.TZDATA_AVAILABLE:
        return
    for zone in ("Europe/London", "Europe/Berlin", "America/New_York"):
        try:
            resolved = timezones.resolve(zone)
        except timezones.TimezoneError as exc:
            message = str(exc)
            assert "daylight saving" in message
            assert "no tz database" in message
            assert zone in message
        else:
            raise AssertionError(
                f"{zone} resolved to offset {resolved.utc_offset_seconds} from the "
                "fallback; a DST zone must not be approximated"
            )


def test_every_supported_fallback_zone_has_a_constant_offset() -> None:
    """Support means the offset never changes, so a fixed offset is exact."""
    assert timezones.SUPPORTED_FALLBACK_ZONES.isdisjoint(timezones.KNOWN_DST_ZONES)
    assert set(timezones.FALLBACK_OFFSETS) == set(timezones.SUPPORTED_FALLBACK_ZONES)


def test_supported_fallback_zones_resolve_offline() -> None:
    if timezones.TZDATA_AVAILABLE:
        return
    for zone in sorted(timezones.SUPPORTED_FALLBACK_ZONES):
        resolved = timezones.resolve(zone)
        assert resolved.source == timezones.SOURCE_FALLBACK
        assert resolved.utc_offset_seconds == timezones.FALLBACK_OFFSETS[zone]


def test_timestamp_parsing_forms() -> None:
    for text, expected in (
        ("2026-10-15T09:31:19.201", "2026-10-15T09:31:19Z"),
        ("2026-10-15T09:31:19.201Z", "2026-10-15T09:31:19Z"),
        ("2026-10-15T09:31:19+00:00", "2026-10-15T09:31:19Z"),
    ):
        parsed = localdate.parse_timestamp(text)
        assert parsed is not None, text
        assert localdate.format_utc(parsed) == expected, text


def test_unparseable_timestamp_returns_none() -> None:
    assert localdate.parse_timestamp(None) is None
    assert localdate.parse_timestamp("") is None
    assert localdate.parse_timestamp("yesterday") is None


def test_naive_timestamps_are_treated_as_utc() -> None:
    parsed = localdate.parse_timestamp("2026-10-15T09:31:19.201")
    assert parsed is not None
    assert parsed.tzinfo == timezone.utc


def test_run_directory_layout() -> None:
    day = localdate.local_day(date(2026, 10, 20), "Asia/Kolkata")
    path = localdate.run_directory(Path("/root/kik-runs"), day, "cm-example")
    assert str(path) == "/root/kik-runs/2026-10-20/cm-example"