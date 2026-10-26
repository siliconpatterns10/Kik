"""Render log grouping tests.

Only the pure grouping helpers are exercised here. Nothing in this module talks
to the Render API: a test that hit a live service would be slow, flaky, and
would eventually assert against somebody else's outage.
"""

import sys
from pathlib import Path

TOOLKIT_ROOT = Path(__file__).resolve().parent.parent
if str(TOOLKIT_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOLKIT_ROOT))

from lib import renderlogs  # noqa: E402


def _entry(message: str, *, level: str = "info", ts: str = "2026-10-20T02:00:00Z"):
    return {
        "id": f"id-{abs(hash((message, ts))) % 100000}",
        "message": message,
        "timestamp": ts,
        "labels": [{"name": "level", "value": level}],
    }


def test_repeated_messages_are_grouped_with_counts() -> None:
    entries = [
        _entry("same line", ts="2026-10-20T02:00:00Z"),
        _entry("same line", ts="2026-10-20T02:00:01Z"),
        _entry("same line", ts="2026-10-20T02:00:02Z"),
        _entry("other line", ts="2026-10-20T02:00:03Z"),
    ]
    groups = renderlogs._group(entries)
    assert len(groups) == 2
    top = groups[0]
    assert top["count"] == 3
    assert top["message"] == "same line"
    assert top["firstAt"] == "2026-10-20T02:00:00Z"
    assert top["lastAt"] == "2026-10-20T02:00:02Z"


def test_different_levels_are_not_merged() -> None:
    entries = [
        _entry("identical", level="info"),
        _entry("identical", level="error"),
    ]
    groups = renderlogs._group(entries)
    assert len(groups) == 2
    assert {g["level"] for g in groups} == {"info", "error"}


def test_ansi_colour_codes_do_not_split_a_group() -> None:
    """Render returns Nest's coloured output verbatim.

    Without stripping, the same logical line painted differently would appear as
    several groups of one - defeating the grouping the command exists for.
    """
    painted = "\x1b[31m[Nest] ERROR\x1b[39m consolidation failed"
    plain = "[Nest] ERROR consolidation failed"
    groups = renderlogs._group([_entry(painted), _entry(painted)])
    assert len(groups) == 1
    assert groups[0]["count"] == 2
    assert groups[0]["message"] == plain
    assert groups[0]["encoded"] is True

    mixed = renderlogs._group([_entry(painted), _entry(plain)])
    assert len(mixed) == 1, "coloured and uncoloured forms are the same message"


def test_strip_ansi_handles_real_shapes() -> None:
    assert renderlogs.strip_ansi("\x1b[31mred\x1b[39m") == "red"
    assert renderlogs.strip_ansi("\x1b[38;5;3myellow\x1b[39m") == "yellow"
    assert renderlogs.strip_ansi("\x1b[0m\x1b[1mbold\x1b[22m") == "bold"
    assert renderlogs.strip_ansi("carriage\rreturn") == "carriagereturn"
    assert renderlogs.strip_ansi("plain text") == "plain text"


def test_groups_are_ordered_by_frequency_then_time() -> None:
    entries = [
        _entry("rare", ts="2026-10-20T01:00:00Z"),
        _entry("common", ts="2026-10-20T02:00:00Z"),
        _entry("common", ts="2026-10-20T02:00:01Z"),
    ]
    groups = renderlogs._group(entries)
    assert [g["message"] for g in groups] == ["common", "rare"]


def test_empty_input_groups_to_nothing() -> None:
    assert renderlogs._group([]) == []


def test_label_lookup_reports_absence_rather_than_guessing() -> None:
    entry = {"labels": [{"name": "level", "value": "error"}]}
    assert renderlogs._label(entry, "level") == "error"
    assert renderlogs._label(entry, "type") == "(none)"
    assert renderlogs._label({"labels": None}, "level") == "(none)"


def test_query_encoding_escapes_filter_values() -> None:
    assert renderlogs._q("a b&c=d") == "a%20b%26c%3Dd"
    assert renderlogs._q("plain") == "plain"