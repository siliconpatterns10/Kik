"""Fixture fingerprint classification tests.

The rules under test:

- two independent fingerprint fields, and which one matched is reported;
- a stored content hash is recorded by observation, not described as
  provider-versioned;
- no match means UNKNOWN, never "verified real";
- a title alone never classifies;
- exclusion is opt-in, and only ever suppresses a record - it never rewrites it.
"""

import json
import sys
from pathlib import Path

TOOLKIT_ROOT = Path(__file__).resolve().parent.parent
if str(TOOLKIT_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOLKIT_ROOT))

from lib import fixtures  # noqa: E402

RAW_FIXTURE_TEXT = "synthetic webhook-setup transcript used only by tests"


def _registry() -> fixtures.FixtureRegistry:
    raw = fixtures.sha256_text(RAW_FIXTURE_TEXT)
    return fixtures.FixtureRegistry(
        raw_text_sha256={raw},
        stored_content_hash={"a" * 64},
        observed_at="2026-10-20",
        version=1,
        loaded_from=Path("<test>"),
    )


def test_registry_file_loads() -> None:
    registry = fixtures.load_registry()
    assert registry.loaded_from is not None
    assert len(registry.raw_text_sha256) >= 1
    assert len(registry.stored_content_hash) >= 1


def test_raw_text_match_reports_the_raw_field() -> None:
    result = fixtures.classify(_registry(), full_text=RAW_FIXTURE_TEXT)
    assert result.classification == fixtures.CLASS_KNOWN_FIXTURE
    assert result.matched_field == fixtures.MATCH_RAW_TEXT
    assert result.matched_value == fixtures.sha256_text(RAW_FIXTURE_TEXT)
    assert result.is_known_fixture


def test_stored_hash_match_reports_the_stored_field() -> None:
    result = fixtures.classify(_registry(), stored_content_hash="A" * 64)
    assert result.classification == fixtures.CLASS_KNOWN_FIXTURE
    assert result.matched_field == fixtures.MATCH_STORED, (
        "stored-hash matching is case-insensitive but reports the stored field"
    )


def test_no_match_is_unknown_not_real() -> None:
    result = fixtures.classify(
        _registry(), full_text="some other transcript", stored_content_hash="b" * 64
    )
    assert result.classification == fixtures.CLASS_UNKNOWN
    assert result.matched_field is None
    assert not result.is_known_fixture
    assert "not evidence" in result.as_dict()["meaning"]


def test_title_is_never_an_input() -> None:
    signature = str(
        __import__("inspect").signature(fixtures.classify)
    )
    assert "title" not in signature, "classify() must not accept a title"


def test_missing_stored_hash_does_not_crash_or_match() -> None:
    result = fixtures.classify(_registry(), full_text=None, stored_content_hash=None)
    assert result.classification == fixtures.CLASS_UNKNOWN
    assert result.raw_text_sha256 is None


def test_hash_is_over_exact_bytes() -> None:
    assert fixtures.sha256_text("a b") != fixtures.sha256_text("a  b")
    assert fixtures.sha256_text("a b") != fixtures.sha256_text("a b\n")


def test_registry_notes_record_the_provenance_gap() -> None:
    payload = json.loads(fixtures.REGISTRY_PATH.read_text(encoding="utf-8"))
    assert "rawTextSha256" in payload
    assert "storedContentHash" in payload
    combined = " ".join(str(v) for v in payload.values())
    assert "NOT established" in combined, (
        "the registry must state that the stored hash's provenance is unknown"
    )


def test_registry_never_asserts_the_stored_hash_is_provider_sourced() -> None:
    """The phrase may only appear as an explicit prohibition, never as a claim."""
    payload = json.loads(fixtures.REGISTRY_PATH.read_text(encoding="utf-8"))
    stored_note = str(payload.get("//storedContentHash", "")).lower()
    assert "do not describe it as provider-versioned" in stored_note
    claim = stored_note.replace("do not describe it as provider-versioned.", "")
    assert "provider" not in claim, (
        "nothing outside the prohibition may attribute the stored hash to the provider"
    )


def test_empty_registry_classifies_nothing() -> None:
    empty = fixtures.FixtureRegistry()
    result = fixtures.classify(empty, full_text=RAW_FIXTURE_TEXT)
    assert result.classification == fixtures.CLASS_UNKNOWN


def test_as_dict_reports_both_hashes_and_the_rule() -> None:
    result = fixtures.classify(_registry(), full_text=RAW_FIXTURE_TEXT)
    payload = result.as_dict()
    assert payload["matchedField"] == fixtures.MATCH_RAW_TEXT
    assert payload["rawTextSha256"] == fixtures.sha256_text(RAW_FIXTURE_TEXT)
    assert "KNOWN_FIXTURE" in payload["meaning"]