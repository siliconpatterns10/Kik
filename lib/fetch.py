"""`fetch` - the complete available evidence for one selected meeting.

Read-only. Writes everything under `/root/kik-runs/<local-date>/<run-id>/`, and
the run id is never reused, so a previous fetch is never overwritten.

Two things this command does deliberately, both from observed failure:

1. **Failed webhook rows with a null `meetingId` are included.** On three of the
   dates examined, the provider's `meeting.transcribed` event arrived without a
   usable transcript body and was rejected before any meeting could be linked.
   Excluding those rows would hide the only evidence of the failure.
2. **Raw webhook bodies are preserved but not printed.** The body is the whole
   payload, and dumping it into a terminal would put meeting content and business
   data into scrollback. `fetch` stores it privately and prints only a digest; a
   separate command reads selected content.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

from . import fixtures as fixtures_mod
from . import identities as identities_mod
from . import localdate, people, runstore, status as status_mod
from .config import Config
from .http_client import HttpClient, HttpError, TransportError
from .output import Output
from .supabase import (  # noqa: F401
    SupabaseClient,
    SupabaseError,
    eq,
    gte,
    in_list,
    is_null,
    lt,
)

DEFAULT_RAW_BODY_LIMIT = 5_000_000  # 5 MB per inbox row, refused above this

MEETING_COLUMNS = [
    "id", "provider", "providerMeetingId", "title", "platform", "meetingUrl",
    "startedAt", "endedAt", "durationSeconds", "organizerName", "organizerEmail",
    "sourceAccountName", "sourceAccountEmail", "attendees", "transcriptStatus",
    "analysisStatus", "createdAt", "updatedAt",
]

TRANSCRIPT_COLUMNS = [
    "id", "meetingId", "revision", "revisionStatus", "contentHash", "language",
    "durationSeconds", "wordCount", "speakers", "fullText", "status", "error",
    "anchorInstant", "anchorBusinessDate", "anchorSource", "receivedAt",
    "createdAt", "updatedAt",
]

SEGMENT_COLUMNS = [
    "id", "transcriptId", "revision", "sequence", "speakerName", "language",
    "text", "startSeconds", "endSeconds", "createdAt",
]

SUMMARY_COLUMNS = [
    "id", "meetingId", "template", "templateId", "source", "text",
    "analysisRunId", "createdAt", "updatedAt",
]

ACTION_ITEM_COLUMNS = [
    "id", "meetingId", "summaryId", "providerActionId", "title", "status",
    "assigneeName", "assigneeEmail", "assigneeUserId", "createdAt", "updatedAt",
]

RUN_COLUMNS = [
    "id", "meetingId", "transcriptId", "attempt", "status", "runMode", "provider",
    "model", "promptBundleVersion", "inputHash", "factsHash", "errorCode", "error",
    "queuedAt", "startedAt", "completedAt", "latencyMs", "usage",
]

EVENT_COLUMNS = [
    "id", "provider", "providerEventId", "eventType", "payloadVersion", "sentAt",
    "receivedAt", "signatureVerified", "status", "error", "processedAt",
    "meetingId", "inboxId", "contentHash",
]

INBOX_COLUMNS = [
    "id", "provider", "providerEventId", "eventType", "payloadVersion", "sentAt",
    "receivedAt", "status", "attemptCount", "lastError", "processedAt", "failedAt",
    "nextAttemptAt", "rawBodyHash", "deliveryFingerprint",
]


@dataclass
class FetchTarget:
    meeting_ids: list[str]
    selected_by: str
    all_matches: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "selectedBy": self.selected_by,
            "meetingIds": self.meeting_ids,
            "matchCount": len(self.all_matches),
            "matches": self.all_matches,
        }


def run(
    config: Config,
    *,
    meeting_id: str | None,
    date_text: str | None,
    timezone_name: str | None,
    all_matches: bool,
    include_raw_bodies: bool,
    raw_body_limit: int,
    output: Output,
) -> status_mod.Result:
    tz_name = timezone_name or config.timezone
    if not meeting_id and not date_text:
        return status_mod.Result(
            command="fetch",
            status=status_mod.USAGE_ERROR,
            findings=[
                status_mod.Finding(
                    "select-meeting",
                    False,
                    "either --meeting-id or --date is required",
                    remedy=(
                        "pass --meeting-id <id> to fetch one meeting, or --date "
                        "<YYYY-MM-DD> to list the meetings in a local day"
                    ),
                )
            ],
        )

    try:
        client = SupabaseClient(config)
    except Exception as exc:  # ConfigError
        return status_mod.Result(
            command="fetch",
            status=status_mod.CONFIGURATION_ERROR,
            findings=[status_mod.Finding("supabase-config", False, str(exc))],
        )

    day: localdate.LocalDay | None = None
    warnings: list[str] = []
    if date_text:
        try:
            day = localdate.local_day_from_text(date_text, tz_name)
        except localdate.DateError as exc:
            return status_mod.Result(
                command="fetch",
                status=status_mod.USAGE_ERROR,
                data={"requested": {"date": date_text, "timezone": tz_name}},
                findings=[status_mod.Finding("parse-date", False, str(exc))],
            )
        if day.warning:
            warnings.append(day.warning)

    local_date = day.day.isoformat() if day else localdate.parse_date(
        _date_of_today()
    ).isoformat()

    try:
        target = _resolve_target(
            client, meeting_id=meeting_id, day=day, all_matches=all_matches, output=output
        )
    except SupabaseError as exc:
        return status_mod.Result(
            command="fetch",
            status=exc.category,
            data={"error": {"message": exc.message, "detail": exc.detail}},
        )

    if isinstance(target, status_mod.Result):
        return target

    if day is None:
        # --meeting-id alone: date the run directory from the meeting's own day
        # in the configured timezone, so a fetch lands beside that day's runs.
        meeting = _fetch_meetings(client, [target.meeting_ids[0]]).rows
        if meeting:
            moment = localdate.parse_timestamp(meeting[0].get("startedAt"))
            if moment is not None:
                local_date = localdate.format_local(moment, tz_name).split(" ")[0]

    try:
        run_record = runstore.new_run(
            config.runs_directory, command="fetch", local_date=local_date
        )
    except runstore.RunError as exc:
        return status_mod.Result(
            command="fetch",
            status=status_mod.CONFIGURATION_ERROR,
            findings=[status_mod.Finding("create-run", False, str(exc))],
        )

    incomplete: list[str] = []
    notes: list[str] = []
    manifest = run_record.manifest
    manifest.timezone = tz_name
    manifest.timezone_source = day.timezone_source if day else None
    if day:
        manifest.utc_interval = {
            "start": day.utc_start_text,
            "endExclusive": day.utc_end_text,
        }
        manifest.local_interval = {
            "start": day.local_start_text,
            "endExclusive": day.local_end_text,
        }
    manifest.arguments = _redact_arguments(
        {
            "meetingId": meeting_id,
            "date": date_text,
            "timezone": tz_name,
            "all": all_matches,
            "includeRawBodies": include_raw_bodies,
            "rawBodyLimit": raw_body_limit,
        }
    )
    manifest.errors = []
    manifest.warnings = list(warnings)
    manifest.sources = {
        "supabase": {"origin": client.base_url.rsplit("/", 1)[0]},
        "keyVariableUsed": client.key_name,
    }

    try:
        bundles = [
            _fetch_one(
                client,
                run_record,
                meeting_id=mid,
                day=day,
                tz_name=tz_name,
                include_raw_bodies=include_raw_bodies,
                raw_body_limit=raw_body_limit,
                incomplete=incomplete,
                notes=notes,
                output=output,
            )
            for mid in target.meeting_ids
        ]
    except SupabaseError as exc:
        manifest.errors.append(exc.message)
        manifest.completeness = {"complete": False, "retrieval": "failed"}
        run_record.seal()
        return status_mod.Result(
            command="fetch",
            status=exc.category,
            data={
                "target": target.as_dict(),
                "run": _run_summary(run_record),
                "error": {"message": exc.message, "detail": exc.detail},
            },
            warnings=warnings,
            incomplete=incomplete,
        )

    manifest.completeness = {
        "complete": not incomplete,
        "meetingsRequested": len(target.meeting_ids),
        "meetingsFetched": len(bundles),
        "truncatedSections": incomplete,
    }
    sealed = run_record.seal()

    bundles_without_raw = [
        {k: v for k, v in bundle.items() if k != "_raw_bodies"} for bundle in bundles
    ]
    for bundle, record in zip(bundles_without_raw, bundles):
        record["artifacts"] = {
            "directory": str(run_record.directory),
            "manifest": str(run_record.directory / "manifest.json"),
        }

    primary = bundles_without_raw[0] if bundles_without_raw else None
    if primary is not None:
        run_record.write_json("meeting-bundle.json", primary)
        sealed = run_record.seal()
        bundles_without_raw[0] = primary

    status = status_mod.OK
    if not bundles:
        status = status_mod.NO_MEETINGS
    elif any(
        b["content"]["availability"] == "NEITHER" for b in bundles_without_raw
    ):
        status = status_mod.CONTENT_UNAVAILABLE
    elif len(bundles) > 1:
        status = status_mod.MULTIPLE_MEETINGS

    notes.append(
        f"artifacts written to {run_record.directory} (run id {run_record.run_id}); "
        "this run directory is new and no previous run was modified"
    )
    if any(b.get("webhooks", {}).get("failedWithoutMeeting") for b in bundles_without_raw):
        notes.append(
            "at least one failed webhook row has no linked meeting; it is included "
            "via the provider meeting id recorded in its payload, and the linkage "
            "method is stated per row"
        )

    data: dict[str, Any] = {
        "requested": manifest.arguments,
        "target": target.as_dict(),
        "run": _run_summary(run_record),
        "manifest": sealed,
        "meetings": [_bundle_summary(b) for b in bundles_without_raw],
    }
    if len(bundles_without_raw) == 1:
        data["meeting"] = bundles_without_raw[0]
    if len(bundles_without_raw) > 1:
        data["bundlesWritten"] = [
            name for name in run_record.relative_files()
        ]

    return status_mod.Result(
        command="fetch",
        status=status,
        data=data,
        warnings=warnings,
        notes=notes,
        incomplete=incomplete,
    )


def _date_of_today() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).date().isoformat()


def _resolve_target(
    client: SupabaseClient,
    *,
    meeting_id: str | None,
    day: localdate.LocalDay | None,
    all_matches: bool,
    output: Output,
):
    """Return a Target, or a finished Result when selection is impossible."""
    if meeting_id:
        page = _fetch_meetings(client, [meeting_id])
        if not page.rows:
            return status_mod.Result(
                command="fetch",
                status=status_mod.NOT_FOUND,
                data={"requested": {"meetingId": meeting_id}},
                findings=[
                    status_mod.Finding(
                        "meeting-lookup", False, f"no meeting with id {meeting_id}"
                    )
                ],
            )
        return FetchTarget(
            meeting_ids=[meeting_id],
            selected_by="--meeting-id",
            all_matches=[_match_summary(row) for row in page.rows],
        )

    assert day is not None
    page = client.select_all(
        "meetings",
        columns=MEETING_COLUMNS,
        filters={
            "startedAt": gte(day.utc_start_text),
            "and": f"(startedAt.lt.{day.utc_end_text})",
        },
        order="startedAt.asc",
    )
    matches = [_match_summary(row) for row in page.rows]
    if not matches:
        return status_mod.Result(
            command="fetch",
            status=status_mod.NO_MEETINGS,
            data={
                "requested": {"date": day.day.isoformat(), "timezone": day.timezone_name},
                "interval": day.as_dict(),
                "matches": [],
            },
            notes=[
                "no meeting started in this interval; the request succeeded and "
                "there was nothing to select"
            ],
        )
    if len(matches) > 1 and not all_matches:
        return status_mod.Result(
            command="fetch",
            status=status_mod.MULTIPLE_MEETINGS,
            data={
                "requested": {"date": day.day.isoformat(), "timezone": day.timezone_name},
                "interval": day.as_dict(),
                "matches": matches,
                "matchCount": len(matches),
            },
            findings=[
                status_mod.Finding(
                    "select-meeting",
                    False,
                    f"{len(matches)} meetings started in this interval; "
                    "the first is never chosen automatically",
                    remedy=(
                        "re-run with --meeting-id <id> for one meeting, or --all to "
                        "fetch every match into separate bundles"
                    ),
                )
            ],
        )
    return FetchTarget(
        meeting_ids=[m["id"] for m in matches],
        selected_by="--date (--all)" if len(matches) > 1 else "--date (single match)",
        all_matches=matches,
    )


def _match_summary(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row.get("id"),
        "title": row.get("title"),
        "providerMeetingId": row.get("providerMeetingId"),
        "startedAt": row.get("startedAt"),
        "endedAt": row.get("endedAt"),
        "organizerName": row.get("organizerName"),
        "organizerEmail": row.get("organizerEmail"),
        "transcriptStatus": row.get("transcriptStatus"),
        "analysisStatus": row.get("analysisStatus"),
    }


def _fetch_meetings(client: SupabaseClient, ids: list[str]):
    return client.select(
        "meetings",
        columns=MEETING_COLUMNS,
        filters={"id": in_list(ids)},
        limit=max(len(ids), 1),
    )


def _bundle_summary(bundle: dict[str, Any]) -> dict[str, Any]:
    return {
        "meetingId": bundle["meeting"]["id"],
        "title": bundle["meeting"]["title"],
        "contentAvailability": bundle["content"]["availability"],
        "transcriptStatus": bundle["meeting"]["transcriptStatus"],
        "summaryCount": bundle["summaries"]["count"],
        "segmentCount": bundle["transcript"]["segmentCount"],
        "analysisRunCount": bundle["analysis"]["runCount"],
        "webhookEventCount": bundle["webhooks"]["eventCount"],
        "failedWebhooksWithoutMeeting": bundle["webhooks"]["failedWithoutMeeting"],
        "fixture": bundle["fixture"]["classification"],
        "artifact": bundle.get("artifacts"),
    }


def _run_summary(run_record: runstore.Run) -> dict[str, Any]:
    return {
        "runId": run_record.run_id,
        "directory": str(run_record.directory),
        "files": run_record.relative_files(),
    }


def _redact_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    return arguments


# -- the bundle itself ----------------------------------------------------


def _fetch_one(
    client: SupabaseClient,
    run_record: runstore.Run,
    *,
    meeting_id: str,
    day: localdate.LocalDay | None,
    tz_name: str,
    include_raw_bodies: bool,
    raw_body_limit: int,
    incomplete: list[str],
    notes: list[str],
    output: Output,
) -> dict[str, Any]:
    output.progress(f"fetching meeting {meeting_id}")

    meeting_page = _fetch_meetings(client, [meeting_id])
    if not meeting_page.rows:
        raise SupabaseError(
            f"meeting {meeting_id} disappeared between selection and fetch",
            category=status_mod.NOT_FOUND,
        )
    meeting = meeting_page.rows[0]

    transcripts = client.select_all(
        "meeting_transcripts",
        columns=TRANSCRIPT_COLUMNS,
        filters={"meetingId": eq(meeting_id)},
        order="revision.asc",
    )
    summaries = client.select_all(
        "meeting_summaries",
        columns=SUMMARY_COLUMNS,
        filters={"meetingId": eq(meeting_id)},
        order="createdAt.asc",
    )
    action_items = client.select_all(
        "meeting_action_items",
        columns=ACTION_ITEM_COLUMNS,
        filters={"meetingId": eq(meeting_id)},
        order="createdAt.asc",
    )
    runs = client.select_all(
        "meeting_analysis_runs",
        columns=RUN_COLUMNS,
        filters={"meetingId": eq(meeting_id)},
        order="queuedAt.asc",
    )
    events = client.select_all(
        "meeting_webhook_events",
        columns=EVENT_COLUMNS,
        filters={"meetingId": eq(meeting_id)},
        order="receivedAt.asc",
    )

    for label, page in (
        ("transcripts", transcripts),
        ("summaries", summaries),
        ("action items", action_items),
        ("analysis runs", runs),
        ("webhook events", events),
    ):
        if not page.complete and label not in incomplete:
            incomplete.append(f"{meeting_id}:{label}")
        notes.extend(page.notes)

    canonical = next(
        (t for t in transcripts.rows if t.get("revisionStatus") == "CANONICAL"), None
    )
    reference = canonical or (transcripts.rows[0] if transcripts.rows else None)

    segments = []
    if reference and reference.get("id"):
        segment_page = client.select_all(
            "transcript_segments",
            columns=SEGMENT_COLUMNS,
            filters={"transcriptId": eq(reference["id"])},
            order="sequence.asc",
        )
        if not segment_page.complete:
            incomplete.append(f"{meeting_id}:transcript_segments")
        segments = segment_page.rows
        notes.extend(segment_page.notes)

    alias_rows = client.select_all(
        "speaker_aliases",
        columns=identities_mod.ALIAS_COLUMNS,
        filters={"meetingId": eq(meeting_id)},
        order="createdAt.asc",
    ).rows
    global_aliases = client.select_all(
        "speaker_aliases",
        columns=identities_mod.ALIAS_COLUMNS,
        filters={"meetingId": is_null()},
        order="createdAt.asc",
    ).rows
    aliases = _merge_aliases(alias_rows, global_aliases)

    user_rows = client.select_all(
        "users",
        columns=identities_mod.USER_COLUMNS,
        order="email.asc",
    ).rows
    users = identities_mod.build_user_index(user_rows)
    alias_index = identities_mod.build_alias_index(aliases)

    webhooks = _collect_webhooks(
        client,
        run_record,
        meeting=meeting,
        events=events.rows,
        include_raw_bodies=include_raw_bodies,
        raw_body_limit=raw_body_limit,
        incomplete=incomplete,
        notes=notes,
    )

    attendees = people.extract_attendees(meeting.get("attendees"))
    watched = people.watched_address_report(
        attendee_emails=attendees.emails(),
        organizer_email=meeting.get("organizerEmail"),
        source_account_email=meeting.get("sourceAccountEmail"),
    )

    registry = fixtures_mod.load_registry()
    fingerprint = fixtures_mod.classify(
        registry,
        full_text=reference.get("fullText") if reference else None,
        stored_content_hash=reference.get("contentHash") if reference else None,
    )

    availability = _availability(transcripts.rows, summaries.rows)

    bundle: dict[str, Any] = {
        "schemaVersion": runstore.SCHEMA_VERSION,
        "retrievedAt": run_record.manifest.created_at,
        "runId": run_record.run_id,
        "selection": {
            "requestedDate": day.day.isoformat() if day else None,
            "timezone": tz_name,
            "timezoneSource": day.timezone_source if day else None,
            "linkedContentOutsideRequestedDay": _outside_day(
                transcripts.rows, summaries.rows, day
            ),
        },
        "meeting": {
            "id": meeting.get("id"),
            "provider": meeting.get("provider"),
            "providerMeetingId": meeting.get("providerMeetingId"),
            "title": meeting.get("title"),
            "platform": meeting.get("platform"),
            "meetingUrl": meeting.get("meetingUrl"),
            "startedAt": meeting.get("startedAt"),
            "endedAt": meeting.get("endedAt"),
            "localStartedAt": localdate.format_local(
                localdate.parse_timestamp(meeting.get("startedAt")), tz_name
            ),
            "durationSeconds": meeting.get("durationSeconds"),
            "organizerName": meeting.get("organizerName"),
            "organizerEmail": meeting.get("organizerEmail"),
            "sourceAccountName": meeting.get("sourceAccountName"),
            "sourceAccountEmail": meeting.get("sourceAccountEmail"),
            "transcriptStatus": meeting.get("transcriptStatus"),
            "analysisStatus": meeting.get("analysisStatus"),
        },
        "attendees": {
            "count": attendees.count,
            "malformedEntryCount": attendees.malformed_count,
            "entryShapes": sorted(set(attendees.shapes_seen)),
            "entries": attendees.entries,
            "evidenceNote": (
                "This is the provider-supplied attendee array as stored. A listed "
                "attendee is a meeting participant record, not confirmed live "
                "attendance."
            ),
            "watchedAddresses": watched,
        },
        "transcript": {
            "present": bool(transcripts.rows),
            "revisionCount": len(transcripts.rows),
            "id": reference.get("id") if reference else None,
            "revision": reference.get("revision") if reference else None,
            "revisionStatus": reference.get("revisionStatus") if reference else None,
            "status": reference.get("status") if reference else None,
            "error": reference.get("error") if reference else None,
            "language": reference.get("language") if reference else None,
            "wordCount": reference.get("wordCount") if reference else None,
            "durationSeconds": reference.get("durationSeconds") if reference else None,
            "speakers": reference.get("speakers") if reference else None,
            "anchorInstant": reference.get("anchorInstant") if reference else None,
            "anchorBusinessDate": reference.get("anchorBusinessDate")
            if reference
            else None,
            "anchorSource": reference.get("anchorSource") if reference else None,
            "receivedAt": reference.get("receivedAt") if reference else None,
            "contentHash": reference.get("contentHash") if reference else None,
            "fullText": reference.get("fullText") if reference else None,
            "fullTextSha256": runstore.sha256_text(reference["fullText"])
            if reference and reference.get("fullText")
            else None,
            "segmentCount": len(segments),
            "segments": [_segment(row, tz_name) for row in segments],
            "revisions": [
                {
                    "id": row.get("id"),
                    "revision": row.get("revision"),
                    "revisionStatus": row.get("revisionStatus"),
                    "status": row.get("status"),
                    "wordCount": row.get("wordCount"),
                    "contentHash": row.get("contentHash"),
                    "receivedAt": row.get("receivedAt"),
                    "error": row.get("error"),
                }
                for row in transcripts.rows
            ],
        },
        "summaries": {
            "count": len(summaries.rows),
            "providerCount": sum(
                1 for s in summaries.rows if s.get("source") == "PROVIDER"
            ),
            "aiCount": sum(1 for s in summaries.rows if s.get("source") == "AI"),
            "items": [
                {
                    "id": row.get("id"),
                    "template": row.get("template"),
                    "templateId": row.get("templateId"),
                    "source": row.get("source"),
                    "analysisRunId": row.get("analysisRunId"),
                    "createdAt": row.get("createdAt"),
                    "text": row.get("text"),
                    "textSha256": runstore.sha256_text(row["text"])
                    if row.get("text")
                    else None,
                    "charCount": len(row.get("text") or ""),
                }
                for row in summaries.rows
            ],
            "provenanceNote": (
                "`source` distinguishes a provider summary from an AI-written one. "
                "Only an AI row carries an analysisRunId. A provider summary is not "
                "evidence that the in-house analysis ran."
            ),
        },
        "storedActionItems": {
            "count": len(action_items.rows),
            "items": [
                {
                    "id": row.get("id"),
                    "summaryId": row.get("summaryId"),
                    "providerActionId": row.get("providerActionId"),
                    "title": row.get("title"),
                    "status": row.get("status"),
                    "assigneeName": row.get("assigneeName"),
                    "assigneeEmail": row.get("assigneeEmail"),
                    "assigneeUserId": row.get("assigneeUserId"),
                    "createdAt": row.get("createdAt"),
                }
                for row in action_items.rows
            ],
            "provenanceNote": (
                "These rows are a projection stored by the application. They record "
                "what the database holds, not independent proof that an assignment "
                "was spoken. Their provenance relative to the provider payload has "
                "not been established, so they are not labelled as provider items."
            ),
        },
        "analysis": {
            "runCount": len(runs.rows),
            "runs": [
                {
                    "id": row.get("id"),
                    "transcriptId": row.get("transcriptId"),
                    "attempt": row.get("attempt"),
                    "status": row.get("status"),
                    "runMode": row.get("runMode"),
                    "provider": row.get("provider"),
                    "model": row.get("model"),
                    "promptBundleVersion": row.get("promptBundleVersion"),
                    "errorCode": row.get("errorCode"),
                    "error": row.get("error"),
                    "queuedAt": row.get("queuedAt"),
                    "startedAt": row.get("startedAt"),
                    "completedAt": row.get("completedAt"),
                    "latencyMs": row.get("latencyMs"),
                    "usage": row.get("usage"),
                }
                for row in runs.rows
            ],
        },
        "webhooks": webhooks,
        "content": {
            "availability": availability,
            "availabilityMeaning": {
                "BOTH": "a transcript and a provider summary are stored",
                "TRANSCRIPT_ONLY": "a transcript is stored with no provider summary",
                "SUMMARY_ONLY": "a provider summary is stored with no transcript",
                "NEITHER": "neither is stored; the meeting row exists alone",
            },
        },
        "identities": _identity_section(
            attendees=attendees.entries,
            speakers=_speaker_labels(reference, segments),
            users=users,
            alias_index=alias_index,
            aliases=aliases,
            meeting_id=meeting_id,
        ),
        "fixture": fingerprint.as_dict(),
        "sourceIds": {
            "meetingId": meeting.get("id"),
            "providerMeetingId": meeting.get("providerMeetingId"),
            "transcriptIds": [row.get("id") for row in transcripts.rows],
            "segmentIds": [row.get("id") for row in segments],
            "summaryIds": [row.get("id") for row in summaries.rows],
            "actionItemIds": [row.get("id") for row in action_items.rows],
            "analysisRunIds": [row.get("id") for row in runs.rows],
            "webhookEventIds": [row.get("id") for row in events.rows],
            "webhookInboxIds": sorted(
                {
                    item["inbox"]["id"]
                    for item in webhooks["items"]
                    if item.get("inbox")
                }
            ),
        },
        "completeness": {
            "complete": not incomplete,
            "truncatedSections": [s for s in incomplete if s.startswith(meeting_id)],
            "notes": [],
        },
    }

    if include_raw_bodies:
        bundle["rawBodies"] = _raw_body_index(webhooks)

    name = f"bundle-{meeting_id}.json"
    run_record.write_json(name, bundle, note="complete fetched evidence bundle")
    return bundle


def _segment(row: dict[str, Any], tz_name: str) -> dict[str, Any]:
    return {
        "id": row.get("id"),
        "sequence": row.get("sequence"),
        "revision": row.get("revision"),
        "speakerName": row.get("speakerName"),
        "language": row.get("language"),
        "startSeconds": row.get("startSeconds"),
        "endSeconds": row.get("endSeconds"),
        "charCount": len(row.get("text") or ""),
        "text": row.get("text"),
    }


def _speaker_labels(reference: dict[str, Any] | None, segments: list[dict[str, Any]]) -> list[str]:
    labels: list[str] = []
    if reference and isinstance(reference.get("speakers"), list):
        for entry in reference["speakers"]:
            name = entry.get("name") if isinstance(entry, dict) else entry
            if name and name not in labels:
                labels.append(name)
    for row in segments:
        name = row.get("speakerName")
        if name and name not in labels:
            labels.append(name)
    return labels


def _merge_aliases(meeting_rows: list[dict[str, Any]], global_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: dict[str, dict[str, Any]] = {}
    for row in list(meeting_rows) + list(global_rows):
        key = str(row.get("id"))
        seen[key] = row
    return list(seen.values())


def _identity_section(
    *,
    attendees: list[dict[str, Any]],
    speakers: list[str],
    users: list[identities_mod.UserRecord],
    alias_index: dict[str, list[identities_mod.AliasRecord]],
    aliases: list[dict[str, Any]],
    meeting_id: str,
) -> dict[str, Any]:
    attendee_mappings = identities_mod.map_attendees(
        attendees, users=users, alias_index=alias_index, meeting_id=meeting_id
    )
    speaker_mappings = identities_mod.map_labels(
        speakers, users=users, alias_index=alias_index, meeting_id=meeting_id
    )
    return {
        "rules": [
            "EXACT_EMAIL: normalised (trimmed, case-folded) email equality",
            "EXPLICIT_ALIAS: a speaker_aliases row naming the label; a "
            "MEETING-scoped row beats a GLOBAL one",
            "UNIQUE_EXACT_NAME: exactly one active user whose normalised display "
            "name equals the normalised label; name-based, not email-verified",
            "UNMATCHED / AMBIGUOUS / INACTIVE: reported, never resolved",
            "no fuzzy or spelling-distance matching is implemented",
        ],
        "userRecordCount": len(users),
        "aliasRowCount": len(aliases),
        "aliasRows": aliases,
        "attendees": [m.as_dict() for m in attendee_mappings],
        "speakers": [m.as_dict() for m in speaker_mappings],
        "summary": {
            "attendees": identities_mod.summarise_mappings(attendee_mappings),
            "speakers": identities_mod.summarise_mappings(speaker_mappings),
        },
        "requiresAgentReview": [
            m.as_dict()
            for m in identities_mod.unresolved(attendee_mappings + speaker_mappings)
        ],
    }


def _availability(transcripts: list[dict[str, Any]], summaries: list[dict[str, Any]]) -> str:
    has_transcript = bool(transcripts)
    has_summary = any(s.get("source") == "PROVIDER" for s in summaries)
    if has_transcript and has_summary:
        return "BOTH"
    if has_transcript:
        return "TRANSCRIPT_ONLY"
    if has_summary:
        return "SUMMARY_ONLY"
    return "NEITHER"


def _outside_day(
    transcripts: list[dict[str, Any]], summaries: list[dict[str, Any]], day: localdate.LocalDay | None
) -> list[dict[str, Any]]:
    if day is None:
        return []
    out: list[dict[str, Any]] = []
    for row in transcripts:
        moment = localdate.parse_timestamp(row.get("receivedAt"))
        if moment is not None and not day.contains_utc(moment):
            out.append(
                {
                    "kind": "transcript",
                    "id": row.get("id"),
                    "timestampField": "receivedAt",
                    "timestamp": row.get("receivedAt"),
                    "label": "linked to the selected meeting, outside the selected day",
                }
            )
    for row in summaries:
        moment = localdate.parse_timestamp(row.get("createdAt"))
        if moment is not None and not day.contains_utc(moment):
            out.append(
                {
                    "kind": "summary",
                    "id": row.get("id"),
                    "timestampField": "createdAt",
                    "timestamp": row.get("createdAt"),
                    "label": "linked to the selected meeting, outside the selected day",
                }
            )
    return out


# -- webhooks ------------------------------------------------------------


def _collect_webhooks(
    client: SupabaseClient,
    run_record: runstore.Run,
    *,
    meeting: dict[str, Any],
    events: list[dict[str, Any]],
    include_raw_bodies: bool,
    raw_body_limit: int,
    incomplete: list[str],
    notes: list[str],
) -> dict[str, Any]:
    """Linked events, plus inbox rows reached through the provider meeting id."""
    inbox_ids = sorted({row["inboxId"] for row in events if row.get("inboxId")})
    inbox_rows: list[dict[str, Any]] = []
    if inbox_ids:
        inbox_rows = client.select_all(
            "meeting_webhook_inbox",
            columns=INBOX_COLUMNS,
            filters={"id": in_list(inbox_ids)},
            order="receivedAt.asc",
        ).rows

    provider_meeting_id = meeting.get("providerMeetingId")
    orphans: list[dict[str, Any]] = []
    orphan_inbox_rows: list[dict[str, Any]] = []

    if provider_meeting_id:
        # Events with no meetingId at all. The provider id lives in rawPayload.
        candidate_events = client.select_all(
            "meeting_webhook_events",
            columns=EVENT_COLUMNS + ["rawPayload"],
            filters={"meetingId": is_null()},
            order="receivedAt.desc",
        ).rows
        for row in candidate_events:
            extracted = people.extract_provider_meeting_id(_payload_text(row.get("rawPayload")))
            if extracted.provider_meeting_id == provider_meeting_id:
                orphans.append(
                    {
                        "event": {
                            k: v for k, v in row.items() if k != "rawPayload"
                        },
                        "linkage": {
                            "method": "PROVIDER_MEETING_ID_IN_PAYLOAD",
                            "providerMeetingId": provider_meeting_id,
                            "jsonPath": extracted.json_path,
                            "caveat": (
                                "This row has meetingId = null. It is associated here "
                                "by the provider meeting id found in its own payload, "
                                "not by a database relationship."
                            ),
                        },
                    }
                )
                if row.get("inboxId"):
                    orphan_inbox_rows.append({"id": row["inboxId"]})

    # Inbox rows whose event is null-linked but which reference this meeting.
    if provider_meeting_id:
        candidate_inbox = client.select_all(
            "meeting_webhook_inbox",
            columns=INBOX_COLUMNS + ["rawBody"],
            filters={"status": eq("FAILED")},
            order="receivedAt.desc",
        ).rows
        known = {row["id"] for row in inbox_rows}
        for row in candidate_inbox:
            if row["id"] in known:
                continue
            extracted = people.extract_provider_meeting_id(row.get("rawBody"))
            if extracted.provider_meeting_id == provider_meeting_id:
                body = row.get("rawBody") or ""
                inbox_rows.append({k: v for k, v in row.items() if k != "rawBody"})
                orphan_inbox_rows.append(
                    {
                        "id": row["id"],
                        "linkage": {
                            "method": "PROVIDER_MEETING_ID_IN_RAW_BODY",
                            "providerMeetingId": provider_meeting_id,
                            "jsonPath": extracted.json_path,
                            "caveat": (
                                "This inbox row has no meetingId column at all. It is "
                                "associated here by the provider meeting id found in "
                                "its own raw body."
                            ),
                        },
                        "status": row.get("status"),
                        "eventType": row.get("eventType"),
                        "lastError": row.get("lastError"),
                    }
                )

    items: list[dict[str, Any]] = []
    for row in events:
        items.append(
            {
                "source": "meeting_webhook_events",
                "event": {k: v for k, v in row.items()},
                "inbox": next(
                    (i for i in inbox_rows if i["id"] == row.get("inboxId")), None
                ),
                "linkage": {
                    "method": "MEETING_FOREIGN_KEY" if row.get("meetingId") else "NONE",
                    "meetingId": row.get("meetingId"),
                },
            }
        )
    for orphan in orphans:
        items.append(
            {
                "source": "meeting_webhook_events",
                "event": orphan["event"],
                "inbox": next(
                    (i for i in inbox_rows if i["id"] == orphan["event"].get("inboxId")),
                    None,
                ),
                "linkage": orphan["linkage"],
            }
        )

    raw_bodies: list[dict[str, Any]] = []
    if include_raw_bodies:
        for row in inbox_rows:
            identifier = row.get("id")
            fetched = client.select(
                "meeting_webhook_inbox",
                columns=["id", "rawBody", "rawBodyHash"],
                filters={"id": eq(identifier)},
                limit=1,
            ).rows
            if not fetched:
                continue
            body = fetched[0].get("rawBody") or ""
            if len(body) > raw_body_limit:
                incomplete.append(
                    f"raw body for inbox {identifier} exceeds the "
                    f"--raw-body-limit of {raw_body_limit} characters and was not stored"
                )
                continue
            name = f"webhook-raw-{identifier}.json"
            run_record.write_text(
                name,
                body,
                note=f"exact rawBody for meeting_webhook_inbox {identifier}",
            )
            raw_bodies.append(
                {
                    "inboxId": identifier,
                    "artifact": name,
                    "sha256": runstore.sha256_text(body),
                    "charCount": len(body),
                    "storedRawBodyHash": fetched[0].get("rawBodyHash"),
                    "hashMatchesStored": (
                        fetched[0].get("rawBodyHash") == runstore.sha256_text(body)
                    ),
                }
            )
        if raw_bodies:
            notes.append(
                f"{len(raw_bodies)} raw webhook body file(s) written to the run "
                "directory; they are not printed, and their content is unchanged"
            )

    failed_without_meeting = sum(
        1
        for item in items
        if item["event"].get("status") == "FAILED" and not item["event"].get("meetingId")
    )

    return {
        "eventCount": len(items),
        "inboxRowCount": len(inbox_rows),
        "items": items,
        "failedWithoutMeeting": failed_without_meeting,
        "orphanLinkage": orphan_inbox_rows,
        "rawBodies": raw_bodies,
        "linkageNote": (
            "`linkage.method` states how each row was associated with this meeting: "
            "a database foreign key, the provider meeting id inside its own payload, "
            "or none. A row without a foreign key is not evidence of a normal flow."
        ),
    }


def _raw_body_index(webhooks: dict[str, Any]) -> dict[str, Any]:
    return {
        "count": len(webhooks["rawBodies"]),
        "items": webhooks["rawBodies"],
        "readCommand": "python3 /root/Kik/kik.py webhook raw --run <dir> --inbox-id <id>",
    }


def _payload_text(payload: Any) -> str | None:
    import json

    if payload is None:
        return None
    if isinstance(payload, str):
        return payload
    try:
        return json.dumps(payload)
    except (TypeError, ValueError):
        return None


def _run_status_for_exception(error: SupabaseError) -> str:
    return error.category