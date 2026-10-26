"""Fixture classification by content fingerprint.

Two fingerprints are supported and they are different computations:

- `rawTextSha256`   - sha256 over the exact `fullText` bytes, computed locally.
- `storedContentHash` - the application's stored `contentHash` column value,
  recorded by observation. **Its normalization algorithm and provenance have
  not been established.** It is not described as provider-versioned here.

A match against either list labels a record as a **known fixture by
fingerprint**. A record matching neither is `UNKNOWN` - which means "not
recognised", not "verified real". Titles never participate: a title alone must
not trigger exclusion.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

REGISTRY_PATH = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "known-fixtures.json"

MATCH_RAW_TEXT = "rawTextSha256"
MATCH_STORED = "storedContentHash"

CLASS_KNOWN_FIXTURE = "KNOWN_FIXTURE"
CLASS_UNKNOWN = "UNKNOWN"


@dataclass
class FixtureRegistry:
    raw_text_sha256: set[str] = field(default_factory=set)
    stored_content_hash: set[str] = field(default_factory=set)
    observed_at: str | None = None
    version: int | None = None
    loaded_from: Path | None = None
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return {
            "registryVersion": self.version,
            "observedAt": self.observed_at,
            "loadedFrom": str(self.loaded_from) if self.loaded_from else None,
            "rawTextSha256Count": len(self.raw_text_sha256),
            "storedContentHashCount": len(self.stored_content_hash),
            "note": (
                "A record matching either fingerprint is a KNOWN FIXTURE. "
                "Non-matching records are UNKNOWN, not verified real. The stored "
                "content hash normalization is not established."
            ),
        }


def load_registry(path: Path | None = None) -> FixtureRegistry:
    target = path or REGISTRY_PATH
    if not target.exists():
        return FixtureRegistry(loaded_from=None)
    data = json.loads(target.read_text(encoding="utf-8"))
    return FixtureRegistry(
        raw_text_sha256={_norm(v) for v in data.get("rawTextSha256", [])},
        stored_content_hash={_norm(v) for v in data.get("storedContentHash", [])},
        observed_at=data.get("observedAt"),
        version=data.get("registryVersion"),
        loaded_from=target,
        notes=[v for k, v in data.items() if k.startswith("//")],
    )


def _norm(value: str) -> str:
    return str(value).strip().lower()


def sha256_text(text: str) -> str:
    """sha256 over the exact UTF-8 bytes of the text. No normalisation."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass
class FixtureClassification:
    classification: str
    matched_field: str | None
    matched_value: str | None
    raw_text_sha256: str | None
    stored_content_hash: str | None

    @property
    def is_known_fixture(self) -> bool:
        return self.classification == CLASS_KNOWN_FIXTURE

    def as_dict(self) -> dict[str, object]:
        return {
            "classification": self.classification,
            "matchedField": self.matched_field,
            "matchedValue": self.matched_value,
            "rawTextSha256": self.raw_text_sha256,
            "storedContentHash": self.stored_content_hash,
            "meaning": (
                "KNOWN_FIXTURE: a registered fingerprint matched. "
                "UNKNOWN: no registered fingerprint matched - this is absence of "
                "recognition, not evidence that the content is real."
            ),
        }


def classify(
    registry: FixtureRegistry,
    *,
    full_text: str | None = None,
    stored_content_hash: str | None = None,
) -> FixtureClassification:
    raw_hash = sha256_text(full_text) if full_text is not None else None
    stored = _norm(stored_content_hash) if stored_content_hash else None

    if raw_hash and _norm(raw_hash) in registry.raw_text_sha256:
        return FixtureClassification(
            CLASS_KNOWN_FIXTURE, MATCH_RAW_TEXT, raw_hash, raw_hash, stored
        )
    if stored and stored in registry.stored_content_hash:
        return FixtureClassification(
            CLASS_KNOWN_FIXTURE, MATCH_STORED, stored, raw_hash, stored
        )
    return FixtureClassification(CLASS_UNKNOWN, None, None, raw_hash, stored)