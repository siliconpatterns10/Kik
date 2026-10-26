"""`transcript` - deterministic batching over a saved or freshly fetched bundle.

Reads a bundle file. No network, no credentials: batching a fetched meeting must
work on a host that has neither, which is the point of persisting evidence.

Batches contain source text and nothing else. No summary, no interpretation, no
token estimate.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
import json

from . import artifacts, runstore, status as status_mod
from . import transcripts as transcripts_mod
from .config import Config
from .output import Output, emit_result


def run_batches(
    config: Config,
    *,
    bundle_path: str,
    max_chars: int,
    persist: bool,
    output: Output,
) -> status_mod.Result:
    path = Path(bundle_path).expanduser()
    if not path.is_file():
        return status_mod.Result(
            command="transcript batches",
            status=status_mod.USAGE_ERROR,
            data={"requested": {"bundle": str(path)}},
            findings=[status_mod.Finding("read-bundle", False, "bundle does not exist")],
        )

    try:
        bundle = transcripts_mod.load_bundle(path)
    except Exception as exc:  # noqa: BLE001 - ArtifactError / BatchError
        return status_mod.Result(
            command="transcript batches",
            status=status_mod.USAGE_ERROR,
            data={"requested": {"bundle": str(path)}},
            findings=[status_mod.Finding("read-bundle", False, str(exc))],
        )

    try:
        plan = transcripts_mod.plan_from_bundle(bundle, max_chars=max_chars)
    except transcripts_mod.BatchError as exc:
        return status_mod.Result(
            command="transcript batches",
            status=status_mod.CONTENT_UNAVAILABLE,
            data={
                "requested": {"bundle": str(path), "maxChars": max_chars},
                "meeting": _meeting_ref(bundle),
            },
            findings=[status_mod.Finding("plan-batches", False, str(exc))],
        )

    manifest = plan.manifest()
    persisted: dict[str, Any] | None = None
    warnings: list[str] = []
    incomplete: list[str] = []

    if persist:
        persisted = _persist_plan(config, bundle, plan, path, warnings, incomplete)

    data: dict[str, Any] = {
        "requested": {
            "bundle": str(path),
            "maxChars": max_chars,
            "maxCharsUnit": "characters",
            "persist": persist,
        },
        "meeting": _meeting_ref(bundle),
        "source": {
            "mode": "bundle",
            "networkUsed": False,
            "credentialsUsed": False,
            "bundleRunId": bundle.get("runId"),
        },
        "batching": manifest["batching"],
        "coverage": manifest["coverage"],
        "batches": manifest["batches"],
        "sourceCounts": manifest["source"],
        "notes": manifest["notes"],
    }
    if persisted:
        data["persisted"] = persisted

    for warning in bundle.get("completeness", {}).get("truncatedSections", []) or []:
        incomplete.append(f"source bundle section was truncated: {warning}")

    status = status_mod.OK
    if plan.batch_count == 0:
        status = status_mod.CONTENT_UNAVAILABLE

    return status_mod.Result(
        command="transcript batches",
        status=status,
        data=data,
        warnings=warnings,
        notes=[
            "batching mode: " + plan.mode,
            "character counts only; no token estimate is produced",
        ],
        incomplete=incomplete,
    )


def run_read(
    config: Config,
    *,
    bundle_path: str,
    batch_number: int,
    max_chars: int | None,
    acknowledge: bool,
    plan_path: str | None = None,
    persist: bool,
    output: Output,
) -> status_mod.Result:
    path = Path(bundle_path).expanduser()
    if not path.is_file():
        return status_mod.Result(
            command="transcript read",
            status=status_mod.USAGE_ERROR,
            data={"requested": {"bundle": str(path)}},
            findings=[status_mod.Finding("read-bundle", False, "bundle does not exist")],
        )

    try:
        bundle = transcripts_mod.load_bundle(path)
        if plan_path:
            from . import batchplan
            saved = json.loads(Path(plan_path).read_text(encoding="utf-8"))
            plan = batchplan.plan_from_manifest(bundle, saved)
        elif max_chars is not None:
            plan = transcripts_mod.plan_from_bundle(bundle, max_chars=max_chars)
        else:
            raise transcripts_mod.BatchError("provide --plan or --max-chars")
    except Exception as exc:  # noqa: BLE001
        return status_mod.Result(
            command="transcript read",
            status=status_mod.CONTENT_UNAVAILABLE,
            data={"requested": {"bundle": str(path)}},
            findings=[status_mod.Finding("plan-batches", False, str(exc))],
        )

    if acknowledge:
        try:
            transcripts_mod.acknowledge(plan, batch_number)
        except transcripts_mod.BatchError as exc:
            return status_mod.Result(
                command="transcript read",
                status=status_mod.USAGE_ERROR,
                data={"requested": {"batch": batch_number}},
                findings=[status_mod.Finding("acknowledge", False, str(exc))],
            )

    try:
        payload = transcripts_mod.render_batch(plan, batch_number, include_text=True)
    except transcripts_mod.BatchError as exc:
        return status_mod.Result(
            command="transcript read",
            status=status_mod.USAGE_ERROR,
            data={
                "requested": {"batch": batch_number},
                "batchCount": plan.batch_count,
            },
            findings=[status_mod.Finding("read-batch", False, str(exc))],
        )

    persisted = None
    warnings: list[str] = []
    incomplete: list[str] = []
    if persist:
        persisted = _persist_plan(config, bundle, plan, path, warnings, incomplete)

    return status_mod.Result(
        command="transcript read",
        status=status_mod.OK,
        data={
            "requested": {
                "bundle": str(path),
                "batch": batch_number,
                "maxChars": max_chars,
            },
            "meeting": _meeting_ref(bundle),
            "source": {
                "mode": "bundle",
                "networkUsed": False,
                "credentialsUsed": False,
            },
            "batch": payload,
            "coverage": transcripts_mod.coverage(plan),
            "persisted": persisted,
        },
        warnings=warnings,
        notes=[
            "batch text is source text, in source order, unmodified",
            "acknowledgement records that a caller reported reading, not that it "
            "was understood",
        ],
        incomplete=incomplete,
    )


def _persist_plan(
    config: Config,
    bundle: dict[str, Any],
    plan: transcripts_mod.BatchPlan,
    bundle_path: Path,
    warnings: list[str],
    incomplete: list[str],
) -> dict[str, Any] | None:
    """Write the plan beside the bundle's own run, never over another run."""
    meeting_id = bundle.get("meeting", {}).get("id") or "unknown-meeting"
    local_date = None
    started = bundle.get("meeting", {}).get("startedAt")
    from . import localdate

    moment = localdate.parse_timestamp(started)
    if moment is not None:
        formatted = localdate.format_local(moment, config.timezone)
        if formatted:
            local_date = formatted.split(" ")[0]
    if not local_date:
        local_date = _date_from_bundle_path(bundle_path) or "undated"

    try:
        record = runstore.new_run(
            config.runs_directory,
            command="transcript batches",
            local_date=local_date,
        )
    except runstore.RunError as exc:
        warnings.append(f"could not persist the batch plan: {exc}")
        return None

    record.manifest.timezone = config.timezone
    record.manifest.arguments = {
        "bundle": str(bundle_path),
        "maxChars": plan.max_chars,
        "meetingId": meeting_id,
    }
    record.manifest.sources = {
        "bundle": str(bundle_path),
        "bundleRunId": bundle.get("runId"),
        "meetingId": meeting_id,
    }
    persisted_manifest = plan.manifest()
    persisted_manifest["source"]["bundlePath"] = str(bundle_path)
    import hashlib
    persisted_manifest["source"]["bundleSha256"] = hashlib.sha256(bundle_path.read_bytes()).hexdigest()
    record.write_json(
        "batches.json", persisted_manifest, note="deterministic batch plan with boundaries and hashes"
    )
    for batch in plan.batches:
        record.write_text(
            f"batch-{batch.batch_number:03d}.txt",
            batch.text,
            note=f"source text for batch {batch.batch_number}, unmodified",
        )
    record.manifest.completeness = {
        "complete": plan.complete,
        "batchCount": plan.batch_count,
        "batchedCharCount": plan.total_char_count,
        "sourceCharCount": plan.source_char_count,
    }
    record.seal()
    return {
        "runId": record.run_id,
        "directory": str(record.directory),
        "files": record.relative_files(),
    }


def _date_from_bundle_path(bundle_path: Path) -> str | None:
    for part in reversed(bundle_path.parts):
        if len(part) == 10 and part[4] == "-" and part[7] == "-":
            return part
    return None


def _meeting_ref(bundle: dict[str, Any]) -> dict[str, Any]:
    meeting = bundle.get("meeting", {}) or {}
    transcript = bundle.get("transcript", {}) or {}
    return {
        "id": meeting.get("id"),
        "title": meeting.get("title"),
        "providerMeetingId": meeting.get("providerMeetingId"),
        "startedAt": meeting.get("startedAt"),
        "transcriptId": transcript.get("id"),
        "transcriptRevision": transcript.get("revision"),
        "segmentCount": transcript.get("segmentCount"),
        "contentHash": transcript.get("contentHash"),
    }