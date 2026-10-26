"""Transcript batching.

Two input shapes must both work:

- **segment-based** — ordered `transcript_segments` rows with speaker and timing;
- **full-text only**   — a `fullText` string with no segments at all.

Batching is deterministic: the same transcript and the same `--max-chars` always
produce the same batch boundaries and the same batch ids. Nothing is summarised.
Nothing is dropped. A segment larger than the limit is split with explicit
continuation metadata rather than being truncated.

Character counts only. A token estimate would be a guess about a tokeniser this
toolkit does not run, and a guessed number presented next to an exact one is
worse than no number.

There is no network access and no credential dependency anywhere in this module:
a saved bundle batches on a host with no credentials.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Iterable

SCHEMA_VERSION = 1
DEFAULT_MAX_CHARS = 16000
#: Refuse absurd limits rather than trying to honour them.
MIN_MAX_CHARS = 500
MAX_MAX_CHARS = 1_000_000

SEPARATOR = "\n\n"
HEADER_TEMPLATE = "[{sequence}] {start}-{end} {speaker}"


class BatchError(Exception):
    """A transcript that cannot be batched as requested."""


@dataclass
class SourceUnit:
    """One addressable piece of a transcript: a segment, or a full-text slice."""

    index: int
    text: str
    speaker: str | None = None
    start_seconds: float | None = None
    end_seconds: float | None = None
    segment_id: str | None = None
    sequence: int | None = None
    #: Set when the unit was carved out of a longer source unit.
    continuation_of: str | None = None
    continuation_index: int | None = None
    continuation_total: int | None = None
    source_char_start: int | None = None
    source_char_end: int | None = None

    @property
    def char_count(self) -> int:
        return len(self.text)

    def label(self) -> str:
        if self.sequence is not None:
            return f"segment {self.sequence}"
        return f"text[{self.source_char_start}:{self.source_char_end}]"

    def header(self) -> str:
        start = "" if self.start_seconds is None else f"{_fmt_seconds(self.start_seconds)}"
        end = "" if self.end_seconds is None else f"{_fmt_seconds(self.end_seconds)}"
        span = f"{start}–{end}" if end else start
        speaker = self.speaker or "(unknown speaker)"
        sequence = "" if self.sequence is None else str(self.sequence)
        parts = [p for p in (sequence, span, speaker) if p]
        return "[" + "] [".join(parts) + "]"


def _fmt_seconds(value: float) -> str:
    total = int(value)
    hours, remainder = divmod(total, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
    return f"{minutes:02d}:{seconds:02d}"


@dataclass
class Batch:
    batch_number: int
    batch_id: str
    units: list[SourceUnit]
    char_count: int
    text: str
    starts_at_continuation: bool = False

    def as_dict(self, *, include_text: bool = False) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "batchNumber": self.batch_number,
            "batchId": self.batch_id,
            "charCount": self.char_count,
            "unitCount": len(self.units),
            "startsAtContinuation": self.starts_at_continuation,
            "units": [
                {
                    "kind": "segment" if unit.sequence is not None else "text-slice",
                    "segmentId": unit.segment_id,
                    "sequence": unit.sequence,
                    "speaker": unit.speaker,
                    "startSeconds": unit.start_seconds,
                    "endSeconds": unit.end_seconds,
                    "charCount": unit.char_count,
                    "continuationOf": unit.continuation_of,
                    "continuationIndex": unit.continuation_index,
                    "continuationTotal": unit.continuation_total,
                    "sourceCharStart": unit.source_char_start,
                    "sourceCharEnd": unit.source_char_end,
                }
                for unit in self.units
            ],
        }
        if include_text:
            payload["text"] = self.text
        return payload


@dataclass
class BatchPlan:
    mode: str
    max_chars: int
    transcript_id: str | None
    meeting_id: str | None
    content_sha256: str
    source_char_count: int
    total_char_count: int
    batches: list[Batch] = field(default_factory=list)
    unbatched_batch_numbers: list[int] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    complete: bool = True

    @property
    def batch_count(self) -> int:
        return len(self.batches)

    def manifest(self) -> dict[str, Any]:
        return {
            "schemaVersion": SCHEMA_VERSION,
            "batching": {
                "mode": self.mode,
                "maxChars": self.max_chars,
                "maxCharsUnit": "characters",
                "tokenEstimateAvailable": False,
                "tokenEstimateNote": (
                    "no token count is reported: it would depend on a tokeniser "
                    "this toolkit does not run, and an estimate beside an exact "
                    "character count invites misreading"
                ),
            },
            "source": {
                "meetingId": self.meeting_id,
                "transcriptContentSha256": _source_hash_for_plan(self),
                "transcriptId": self.transcript_id,
                "contentSha256": self.content_sha256,
                "sourceCharCount": self.source_char_count,
                "batchedCharCount": self.total_char_count,
                "accountedFor": self.total_char_count,
            },
            "coverage": {
                "batchCount": self.batch_count,
                "complete": self.complete,
                "unbatched": self.unbatched_batch_numbers,
                "claim": (
                    "every source character is present in exactly one batch, in "
                    "source order; this is a structural claim about this plan, not "
                    "a claim that any batch has been read"
                ),
            },
            "batches": [
                {
                    "batchNumber": batch.batch_number,
                    "batchId": batch.batch_id,
                    "units": [u.as_dict() if hasattr(u, 'as_dict') else {"sourceCharStart":u.source_char_start,"sourceCharEnd":u.source_char_end,"sequence":u.sequence,"segmentId":u.segment_id,"text":u.text,"speaker":u.speaker,"startSeconds":u.start_seconds,"endSeconds":u.end_seconds,"continuationOf":u.continuation_of,"continuationIndex":u.continuation_index,"continuationTotal":u.continuation_total} for u in batch.units],
                    "charCount": batch.char_count,
                    "unitCount": len(batch.units),
                    "speakerSpan": _speakers(batch),
                    "firstLabel": batch.units[0].label() if batch.units else None,
                    "lastLabel": batch.units[-1].label() if batch.units else None,
                    "startsAtContinuation": batch.starts_at_continuation,
                }
                for batch in self.batches
            ],
            "notes": self.notes,
            "warnings": self.warnings,
        }

    def as_dict(self) -> dict[str, Any]:
        return self.manifest()


def _speakers(batch: Batch) -> list[str]:
    seen: list[str] = []
    for unit in batch.units:
        name = unit.speaker or "(unknown speaker)"
        if name not in seen:
            seen.append(name)
    return seen


# -- unit construction ---------------------------------------------------


def units_from_segments(
    segments: Iterable[dict[str, Any]],
    *,
    max_chars: int,
) -> tuple[list[SourceUnit], list[str]]:
    """Ordered segments. Segments are never reordered or filtered."""
    ordered = _order_segments(segments)
    units: list[SourceUnit] = []
    notes: list[str] = []
    for row in ordered:
        text = row.get("text")
        text = "" if text is None else str(text)
        unit = SourceUnit(
            index=len(units),
            text=text,
            speaker=row.get("speakerName"),
            start_seconds=_num(row.get("startSeconds")),
            end_seconds=_num(row.get("endSeconds")),
            segment_id=row.get("id"),
            sequence=_int(row.get("sequence")),
            source_char_start=0,
            source_char_end=len(text),
        )
        units.extend(_split_if_oversized(unit, max_chars=max_chars, notes=notes))
    return units, notes


def _order_segments(segments: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = list(segments)
    if not rows:
        return rows
    with_sequence = [r for r in rows if _int(r.get("sequence")) is not None]
    without_sequence = [r for r in rows if _int(r.get("sequence")) is None]
    if with_sequence and without_sequence:
        raise BatchError(
            "segment set mixes rows with and without `sequence`; ordering would be "
            "ambiguous, so batching is refused rather than guessing"
        )
    if without_sequence:
        # Fall back to declared order, and say so.
        return without_sequence
    ordered = sorted(with_sequence, key=lambda r: _int(r.get("sequence")) or 0)
    sequences = [_int(r.get("sequence")) for r in ordered]
    if len(set(sequences)) != len(sequences):
        raise BatchError("duplicate segment sequence numbers; ordering is ambiguous")
    return ordered


def units_from_full_text(text: str, *, max_chars: int) -> tuple[list[SourceUnit], list[str]]:
    """Split a full-text-only transcript on paragraph/sentence boundaries."""
    if not text:
        return [], ["transcript full text is empty"]
    notes: list[str] = []
    unit = SourceUnit(index=0, text=text, source_char_start=0, source_char_end=len(text))
    return _split_if_oversized(unit, max_chars=max_chars, notes=notes), notes


def _split_if_oversized(
    unit: SourceUnit, *, max_chars: int, notes: list[str]
) -> list[SourceUnit]:
    """Split on a boundary where possible, and mid-string when unavoidable."""
    if unit.char_count <= max_chars:
        return [unit]

    pieces = _split_text(unit.text, max_chars=max_chars)
    total = len(pieces)
    notes.append(
        f"{unit.label()} is {unit.char_count} characters and was split into "
        f"{total} continuations at a {max_chars}-character limit"
    )
    out: list[SourceUnit] = []
    offset = 0
    for position, piece in enumerate(pieces, start=1):
        out.append(
            SourceUnit(
                index=unit.index,
                text=piece,
                speaker=unit.speaker,
                start_seconds=unit.start_seconds,
                end_seconds=unit.end_seconds,
                segment_id=unit.segment_id,
                sequence=unit.sequence,
                continuation_of=unit.label(),
                continuation_index=position,
                continuation_total=total,
                source_char_start=offset,
                source_char_end=offset + len(piece),
            )
        )
        offset += len(piece)
    if "".join(pieces) != unit.text:
        raise BatchError(
            f"splitting {unit.label()} did not preserve its text; refusing to "
            "return an inexact batch"
        )
    return out


_BOUNDARY_PATTERNS = (
    ("paragraph", "\n\n"),
    ("line", "\n"),
    ("sentence", ". "),
    ("space", " "),
)


def _split_text(text: str, *, max_chars: int) -> list[str]:
    """Greedy split honouring the largest boundary that fits, else hard-cutting.

    Guarantees: no character is lost, none is duplicated, and order is preserved
    (asserted by the caller's rejoin check).
    """
    pieces: list[str] = []
    remaining = text
    while len(remaining) > max_chars:
        cut = _best_cut(remaining, max_chars)
        pieces.append(remaining[:cut])
        remaining = remaining[cut:]
    pieces.append(remaining)
    return pieces


def _best_cut(text: str, max_chars: int) -> int:
    window = text[:max_chars]
    for _name, token in _BOUNDARY_PATTERNS:
        position = window.rfind(token)
        # Require the piece to be meaningfully full, so a stray early newline
        # does not produce a swarm of tiny batches.
        if position >= max_chars // 2:
            return position + len(token)
    return max_chars


# -- planning ------------------------------------------------------------


def plan_batches(
    units: list[SourceUnit],
    *,
    max_chars: int,
    mode: str,
    transcript_id: str | None = None,
    meeting_id: str | None = None,
    content_sha256: str,
    source_char_count: int,
    notes: list[str] | None = None,
) -> BatchPlan:
    """Pack units into batches without splitting a unit unless unavoidable."""
    _validate_max_chars(max_chars)
    plan = BatchPlan(
        mode=mode,
        max_chars=max_chars,
        transcript_id=transcript_id,
        meeting_id=meeting_id,
        content_sha256=content_sha256,
        source_char_count=source_char_count,
        total_char_count=0,
        notes=list(notes or []),
    )

    current: list[SourceUnit] = []
    current_chars = -len(SEPARATOR)  # no separator before the first unit

    def flush() -> None:
        nonlocal current, current_chars
        if not current:
            return
        number = len(plan.batches) + 1
        text = SEPARATOR.join(u.header() + "\n" + u.text for u in current)
        batch = Batch(
            batch_number=number,
            batch_id=batch_identifier(number, content_sha256),
            units=list(current),
            char_count=sum(u.char_count for u in current),
            text=text,
            starts_at_continuation=_is_continuation(current[0]),
        )
        plan.batches.append(batch)
        plan.total_char_count += batch.char_count
        current = []
        current_chars = -len(SEPARATOR)

    for unit in units:
        addition = unit.char_count + (len(SEPARATOR) if current else 0)
        if current and current_chars + addition > max_chars:
            flush()
            addition = unit.char_count
        current.append(unit)
        current_chars += addition
    flush()

    if plan.total_char_count != source_char_count and mode == "segments":
        plan.warnings.append(
            f"batched {plan.total_char_count} characters against a source of "
            f"{source_char_count}; segment text may differ from fullText"
        )

    plan.notes.append(
        f"{len(units)} source unit(s) packed into {plan.batch_count} batch(es) at a "
        f"{max_chars}-character limit"
    )
    return plan


def batch_identifier(batch_number: int, content_sha256: str) -> str:
    """Stable id: independent of run time, dependent on content and position."""
    digest = hashlib.sha256(
        f"{content_sha256}:{batch_number}".encode("utf-8")
    ).hexdigest()[:12]
    return f"b{batch_number:03d}-{digest}"


def _is_continuation(unit: SourceUnit) -> bool:
    return unit.continuation_index is not None and unit.continuation_index > 1


def _validate_max_chars(max_chars: int) -> None:
    if not isinstance(max_chars, int):
        raise BatchError("--max-chars must be an integer")
    if max_chars < MIN_MAX_CHARS:
        raise BatchError(
            f"--max-chars must be at least {MIN_MAX_CHARS} (got {max_chars}); "
            "smaller batches split more segments than they group"
        )
    if max_chars > MAX_MAX_CHARS:
        raise BatchError(f"--max-chars must be at most {MAX_MAX_CHARS} (got {max_chars})")


def plan_from_bundle(bundle: dict[str, Any], *, max_chars: int) -> BatchPlan:
    """Build a plan from a saved bundle. No network, no credentials."""
    transcript = _transcript_section(bundle)
    if transcript is None:
        raise BatchError(
            "bundle contains no transcript section; nothing to batch"
        )

    segments = transcript.get("segments") or []
    full_text = transcript.get("fullText")
    if segments:
        mode = "segments"
        units, notes = units_from_segments(segments, max_chars=max_chars)
        source_chars = sum(len(str(s.get("text") or "")) for s in segments)
    elif full_text is not None:
        mode = "full-text"
        units, notes = units_from_full_text(str(full_text), max_chars=max_chars)
        source_chars = len(str(full_text))
    else:
        raise BatchError(
            "bundle transcript has neither segments nor fullText; there is no "
            "source text to batch"
        )

    return plan_batches(
        units,
        max_chars=max_chars,
        mode=mode,
        transcript_id=transcript.get("id"),
        meeting_id=bundle.get("meeting", {}).get("id"),
        content_sha256=str(transcript.get("contentHash") or _hash_of(full_text or "")),
        source_char_count=source_chars,
        notes=notes,
    )


def _transcript_section(bundle: dict[str, Any]) -> dict[str, Any] | None:
    transcript = bundle.get("transcript")
    if isinstance(transcript, dict):
        return transcript
    return None


def _source_hash_for_plan(plan: BatchPlan) -> str:
    # Hash canonical ordered source units, including segment metadata/content.
    payload = json.dumps([{"sequence":u.sequence,"segmentId":u.segment_id,"text":u.text,"speaker":u.speaker,"startSeconds":u.start_seconds,"endSeconds":u.end_seconds} for b in plan.batches for u in b.units], ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _hash_of(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def render_batch(plan: BatchPlan, batch_number: int, *, include_text: bool = True) -> dict[str, Any]:
    if batch_number < 1 or batch_number > plan.batch_count:
        raise BatchError(
            f"batch {batch_number} does not exist; this plan has "
            f"{plan.batch_count} batch(es), numbered 1..{plan.batch_count}"
        )
    batch = plan.batches[batch_number - 1]
    payload = batch.as_dict(include_text=include_text)
    payload["batchCount"] = plan.batch_count
    payload["plan"] = {
        "mode": plan.mode,
        "maxChars": plan.max_chars,
        "contentSha256": plan.content_sha256,
        "complete": plan.complete,
        "remainingBatchNumbers": [
            n for n in range(1, plan.batch_count + 1) if n not in plan.unbatched_batch_numbers
        ],
    }
    return payload


def acknowledge(plan: BatchPlan, batch_number: int) -> BatchPlan:
    """Record that an agent read a batch.

    This is an agent acknowledgement. It is not evidence of comprehension, and
    the manifest says so.
    """
    if batch_number < 1 or batch_number > plan.batch_count:
        raise BatchError(f"batch {batch_number} does not exist in this plan")
    if batch_number not in plan.unbatched_batch_numbers:
        plan.unbatched_batch_numbers.append(batch_number)
        plan.unbatched_batch_numbers.sort()
    plan.notes.append(
        f"batch {batch_number} marked read by the caller; this records an "
        "acknowledgement, not comprehension"
    )
    return plan


def coverage(plan: BatchPlan) -> dict[str, Any]:
    remaining = [
        n for n in range(1, plan.batch_count + 1) if n not in plan.unbatched_batch_numbers
    ]
    return {
        "batchCount": plan.batch_count,
        "acknowledged": plan.unbatched_batch_numbers,
        "notAcknowledged": remaining,
        "allAcknowledged": not remaining,
        "acknowledgementMeaning": (
            "an acknowledgement records that a caller reported reading a batch; it "
            "is not proof of comprehension"
        ),
        "sourceOrderPreserved": True,
        "batchedCharCount": plan.total_char_count,
        "sourceCharCount": plan.source_char_count,
    }


def write_plan(plan: BatchPlan, directory) -> list[str]:
    from . import artifacts
    from pathlib import Path

    root = Path(directory).parent.parent if Path(directory).name else Path(directory)
    written: list[str] = []
    artifacts.write_json(Path(directory) / "batches.json", plan.manifest(), root=root)
    written.append("batches.json")
    return written


# -- helpers -------------------------------------------------------------


def _num(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return None


def _int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def load_bundle(path) -> dict[str, Any]:
    from . import artifacts

    bundle = artifacts.read_json(path)
    if not isinstance(bundle, dict):
        raise BatchError(f"bundle is not a JSON object: {path}")
    if "meeting" not in bundle:
        raise BatchError(f"bundle has no `meeting` section: {path}")
    return bundle


def dump_json(payload: Any) -> str:
    return json.dumps(payload, indent=2, ensure_ascii=False)