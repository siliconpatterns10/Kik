# Kik test fixtures

Sanitized, synthetic fixtures only.

Do **not** place real transcripts, real provider summaries, real attendee lists,
or real email addresses used by colleagues here. Phase 0 tests build their
inputs in memory; anything added later must be invented content.

`known-fixtures.json` holds the fingerprint registry used for fixture
classification. It is a registry of *observed* hashes, not a list of "bad"
records, and a match only ever changes a label - never a decision about whether
data is real.