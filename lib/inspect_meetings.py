"""Phase 1: inspect one local day of meeting and webhook activity.

Read-only. Reports what is stored and what is missing, in mechanical terms:

- meetings whose `startedAt` falls inside the local day's UTC interval;
- webhook inbox rows and immutable webhook events *received* inside the same
  interval, including rows with no linked meeting;
- provider summaries created inside the interval;
- a derived content-availability label per meeting.

What this does not do: decide why anything failed, decide whether a listed
attendee attended, or treat a provider summary as evidence that the in-house
analysis ran. Those are agent judgements; the raw counters are supplied instead.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from . import fixtures as fixtures_mod
from . import localdate, status as status_mod
from .config import Config
from .output import Output
from .supabase import SupabaseClient, SupabaseError, eq, gte, in_list, lt

CONTENT_BOTH = "BOTH"
CONTENT_TRANSCRIPT_ONLY = "TRANSCRIPT_ONLY"
CONTENT_SUMMARY_ONLY = "SUMMARY_ONLY"
CONTENT_NEITHER = "NEITHER"

MEETING_COLUMNS = [
    "id",
    "provider",
    "providerMeetingId",
    "title",
    "platform",
    "meetingUrl",
    "startedAt",
    "endedAt",
    "durationSeconds",
    "organizerName",
    "organizerEmail",
    "sourceAccountName",
    "sourceAccountEmail",
    "attendees",
    "transcriptStatus",
    "analysisStatus",
    "createdAt",
]

TRANSCRIPT_COLUMNS = [
    "id",
    "meetingId",
    "revision",
    "revisionStatus",
    "contentHash",
    "status",
    "error",
    "wordCount",
    "durationSeconds",
    "speakers",
    "anchorInstant",
    "anchorBusinessDate",
    "anchorSource",
    "receivedAt",
    "createdAt",
]

SUMMARY_COLUMNS = [
    "id",
    "meetingId",
    "template",
    "source",
    "analysisRunId",
    "createdAt",
]

INBOX_COLUMNS = [
    "id",
    "provider",
    "providerEventId",
    "eventType",
    "payloadVersion",
    "sentAt",
    "receivedAt",
    "status",
    "attemptCount",
    "lastError",
    "processedAt",
    "failedAt",
    "nextAttemptAt",
]

EVENT_COLUMNS = [
    "id",
    "provider",
    "providerEventId",
    "eventType",
    "payloadVersion",
    "sentAt",
    "receivedAt",
    "signatureVerified",
    "status",
    "error",
    "processedAt",
    "meetingId",
    "contentHash",
]

RUN_COLUMNS = [
    "id",
    "meetingId",
    "transcriptId",
    "attempt",
    "status",
    "runMode",
    "provider",
    "model",
    "errorCode",
    "error",
    "queuedAt",
    "startedAt",
    "completedAt",
    "latencyMs",
]


@dataclass
class InspectOutcome:
    result: status_mod.Result
    completeness: list[str] = field(default_factory=list)


def run(
    config: Config,
    *,
    date_text: str,
    timezone_name: str | None,
    exclude_fixtures: bool,
    limit: int,
    persist: bool,
    output: Output,
) -> status_mod.Result:
    tz_name = timezone_name or config.timezone
    try:
        day = localdate.local_day_from_text(date_text, tz_name)
    except localdate.DateError as exc:
        return status_mod.Result(
            command="inspect",
            status=status_mod.USAGE_ERROR,
            data={"requested": {"date": date_text, "timezone": tz_name}},
            findings=[status_mod.Finding("parse-date", False, str(exc))],
        )

    registry = fixtures_mod.load_registry()
    client = SupabaseClient(config)
    windows = {
        "meetingsStartedAt": (gte(day.utc_start_text), lt(day.utc_end_text)),
        "webhooksReceivedAt": (gte(day.utc_start_text), lt(day.utc_end_text)),
        "summariesCreatedAt": (gte(day.utc_start_text), lt(day.utc_end_text)),
    }

    incomplete: list[str] = []
    warnings: list[str] = []
    notes: list[str] = []

    try:
        output.progress(f"querying Supabase for {day.day.isoformat()} ({tz_name})")
        meetings_page = client.select_all(
            "meetings",
            columns=MEETING_COLUMNS,
            filters={
                "startedAt": windows["meetingsStartedAt"][0],
                "and": f"(startedAt.lt.{day.utc_end_text})",
            },
            order="startedAt.asc",
            max_rows=limit,
        )
    except SupabaseError as exc:
        return _provider_failure("inspect", exc, day, tz_name)

    meeting_ids = [row["id"] for row in meetings_page.rows]

    try:
        transcripts_by_meeting = _linked_transcripts(client, meeting_ids)
        summaries_by_meeting = _linked_summaries(client, meeting_ids)
        runs_by_meeting = _linked_runs(client, meeting_ids)
        inbox_page = client.select_all(
            "meeting_webhook_inbox",
            columns=INBOX_COLUMNS,
            filters={
                "receivedAt": windows["webhooksReceivedAt"][0],
                "and": f"(receivedAt.lt.{day.utc_end_text})",
            },
            order="receivedAt.asc",
        )
        events_page = client.select_all(
            "meeting_webhook_events",
            columns=EVENT_COLUMNS,
            filters={
                "receivedAt": windows["webhooksReceivedAt"][0],
                "and": f"(receivedAt.lt.{day.utc_end_text})",
            },
            order="receivedAt.asc",
        )
        summaries_in_window = client.select_all(
            "meeting_summaries",
            columns=SUMMARY_COLUMNS,
            filters={
                "createdAt": windows["summariesCreatedAt"][0],
                "and": f"(createdAt.lt.{day.utc_end_text})",
            },
            order="createdAt.asc",
        )
    except SupabaseError as exc:
        return _provider_failure("inspect", exc, day, tz_name)

    for label, page in (
        ("meetings", meetings_page),
        ("transcripts", transcripts_by_meeting),
        ("summaries", summaries_by_meeting),
        ("analysis runs", runs_by_meeting),
        ("webhook inbox", inbox_page),
        ("webhook events", events_page),
        ("summaries created in window", summaries_in_window),
    ):
        if not page.complete:
            incomplete.append(label)
        notes.extend(page.notes)

    report_meetings: list[dict[str, Any]] = []
    excluded = 0
    for row in meetings_page.rows:
        record = _describe_meeting(
            row,
            transcripts_by_meeting.rows,
            summaries_by_meeting.rows,
            runs_by_meeting.rows,
            registry,
        )
        if record["fixture"]["classification"] == fixtures_mod.CLASS_KNOWN_FIXTURE:
            if exclude_fixtures:
                excluded += 1
                continue
            warnings.append(
                f"meeting {row['id']} is a KNOWN FIXTURE by fingerprint "
                f"({record['fixture']['matchedField']})"
            )
        report_meetings.append(record)

    linked_webhooks = _describe_webhooks(
        inbox_page.rows,
        events_page.rows,
        meeting_ids=set(meeting_ids),
        known_meeting_titles={row["id"]: row.get("title") for row in meetings_page.rows},
        registry=registry,
    )

    orphan_count = sum(1 for item in linked_webhooks if item["linkedMeetingId"] is None)
    failed_without_meeting = sum(
        1
        for item in linked_webhooks
        if item["linkedMeetingId"] is None and item["status"] in ("FAILED",)
    )

    data: dict[str, Any] = {
        "requested": {
            "date": day.day.isoformat(),
            "timezone": tz_name,
            "excludeFixtures": exclude_fixtures,
        },
        "interval": day.as_dict(),
        "counts": {
            "meetingsStartedInInterval": len(meetings_page.rows),
            "meetingsReported": len(report_meetings),
            "meetingsExcludedAsFixtures": excluded,
            "webhookInboxRowsReceived": len(inbox_page.rows),
            "webhookEventsReceived": len(events_page.rows),
            "webhookRowsWithoutLinkedMeeting": orphan_count,
            "failedWebhookRowsWithoutLinkedMeeting": failed_without_meeting,
            "summariesCreatedInInterval": len(summaries_in_window.rows),
            "meetingsTotalInDatabase": meetings_page.total,
        },
        "meetings": report_meetings,
        "webhooks": linked_webhooks,
        "linkedContentOutsideInterval": _linked_outside_interval(
            transcripts_by_meeting.rows, summaries_by_meeting.rows, day
        ),
        "fixtureRegistry": registry.as_dict(),
        "interpretation": {
            "attendeeCount": (
                "A listed attendee is a provider-supplied meeting participant. "
                "It is not confirmed live attendance."
            ),
            "summarySource": (
                "A summary row carries a `source` of PROVIDER or AI. Only an AI "
                "row has an analysisRunId. A provider summary is not evidence "
                "that the in-house analysis ran successfully."
            ),
            "absence": (
                "An absent log line or an absent attendee entry is absence from "
                "that data source. It is not proof that an event did not happen."
            ),
        },
    }

    status = _status_for(data)
    if status == status_mod.EMPTY_OK:
        notes.append(
            "No rows matched. The request succeeded; there was simply nothing "
            "in this interval."
        )

    persisted = _persist(config, day, data, exclude_fixtures, status) if persist else None
    if persisted:
        data["persisted"] = persisted

    return status_mod.Result(
        command="inspect",
        status=status,
        data=data,
        warnings=warnings,
        notes=notes,
        incomplete=incomplete,
    )


def _persist(
    config: Config,
    day: localdate.LocalDay,
    data: dict[str, Any],
    exclude_fixtures: bool,
    status: str,
) -> dict[str, Any] | None:
    """Write the whole report into a new run directory, never over an old one."""
    from . import runstore

    try:
        record = runstore.new_run(
            config.runs_directory, command="inspect", local_date=day.day.isoformat()
        )
    except runstore.RunError:
        return None

    record.manifest.timezone = day.timezone_name
    record.manifest.timezone_source = day.timezone_source
    record.manifest.utc_interval = {
        "start": day.utc_start_text,
        "endExclusive": day.utc_end_text,
    }
    record.manifest.local_interval = {
        "start": day.local_start_text,
        "endExclusive": day.local_end_text,
    }
    record.manifest.arguments = {
        "date": day.day.isoformat(),
        "timezone": day.timezone_name,
        "excludeFixtures": exclude_fixtures,
    }
    record.manifest.warnings = list(data.get("warnings") or [])
    record.write_json("inspect-report.json", data, note="daily inspection report")
    record.manifest.completeness = {
        "complete": not data.get("incomplete"),
        "counts": data.get("counts"),
    }
    record.seal()
    return {
        "runId": record.run_id,
        "directory": str(record.directory),
        "files": record.relative_files(),
        "status": status,
    }


# -- helpers -------------------------------------------------------------


def _linked_transcripts(client: SupabaseClient, meeting_ids: list[str]):
    if not meeting_ids:
        from .supabase import Page

        return Page([], 0, 0, 0, True, 0)
    return client.select_all(
        "meeting_transcripts",
        columns=TRANSCRIPT_COLUMNS,
        filters={"meetingId": in_list(meeting_ids)},
        order="receivedAt.asc",
    )


def _linked_summaries(client: SupabaseClient, meeting_ids: list[str]):
    if not meeting_ids:
        from .supabase import Page

        return Page([], 0, 0, 0, True, 0)
    return client.select_all(
        "meeting_summaries",
        columns=SUMMARY_COLUMNS,
        filters={"meetingId": in_list(meeting_ids)},
        order="createdAt.asc",
    )


def _linked_runs(client: SupabaseClient, meeting_ids: list[str]):
    if not meeting_ids:
        from .supabase import Page

        return Page([], 0, 0, 0, True, 0)
    return client.select_all(
        "meeting_analysis_runs",
        columns=RUN_COLUMNS,
        filters={"meetingId": in_list(meeting_ids)},
        order="queuedAt.asc",
    )


def _describe_meeting(
    row: dict[str, Any],
    transcripts: list[dict[str, Any]],
    summaries: list[dict[str, Any]],
    runs: list[dict[str, Any]],
    registry: fixtures_mod.FixtureRegistry,
) -> dict[str, Any]:
    meeting_id = row["id"]
    mine_transcripts = [t for t in transcripts if t.get("meetingId") == meeting_id]
    mine_summaries = [s for s in summaries if s.get("meetingId") == meeting_id]
    mine_runs = [r for r in runs if r.get("meetingId") == meeting_id]

    canonical = next(
        (t for t in mine_transcripts if t.get("revisionStatus") == "CANONICAL"), None
    )
    has_transcript = bool(mine_transcripts)
    usable_transcript = [
        t for t in mine_transcripts if t.get("status") in ("READY", "RECEIVED")
    ]

    provider_summaries = [s for s in mine_summaries if s.get("source") == "PROVIDER"]
    ai_summaries = [s for s in mine_summaries if s.get("source") == "AI"]
    has_summary = bool(provider_summaries)

    if has_transcript and has_summary:
        content = CONTENT_BOTH
    elif has_transcript:
        content = CONTENT_TRANSCRIPT_ONLY
    elif has_summary:
        content = CONTENT_SUMMARY_ONLY
    else:
        content = CONTENT_NEITHER

    reference = canonical or (mine_transcripts[0] if mine_transcripts else None)
    fingerprint = fixtures_mod.classify(
        registry,
        full_text=None,  # full text is not fetched on inspect; only the stored hash
        stored_content_hash=reference.get("contentHash") if reference else None,
    )

    attendees = row.get("attendees") or []
    attendee_entries = []
    if isinstance(attendees, list):
        for entry in attendees:
            if isinstance(entry, dict):
                attendee_entries.append(
                    {
                        "name": entry.get("name"),
                        "email": entry.get("email"),
                        "isExternal": entry.get("is_external"),
                    }
                )
            elif isinstance(entry, str):
                attendee_entries.append({"name": entry, "email": None, "isExternal": None})

    return {
        "id": meeting_id,
        "provider": row.get("provider"),
        "providerMeetingId": row.get("providerMeetingId"),
        "title": row.get("title"),
        "platform": row.get("platform"),
        "organizerName": row.get("organizerName"),
        "organizerEmail": row.get("organizerEmail"),
        "sourceAccountName": row.get("sourceAccountName"),
        "sourceAccountEmail": row.get("sourceAccountEmail"),
        "startedAt": row.get("startedAt"),
        "endedAt": row.get("endedAt"),
        "durationSeconds": row.get("durationSeconds"),
        "localStartedAt": localdate.format_local(
            localdate.parse_timestamp(row.get("startedAt")), "Asia/Kolkata"
        ),
        "transcriptStatus": row.get("transcriptStatus"),
        "analysisStatus": row.get("analysisStatus"),
        "listedAttendeeCount": len(attendee_entries),
        "listedAttendees": attendee_entries,
        "attendanceCaveat": (
            "listed, not confirmed present"
        ),
        "transcript": {
            "present": has_transcript,
            "revisionCount": len(mine_transcripts),
            "canonicalTranscriptId": canonical.get("id") if canonical else None,
            "usableRevisionCount": len(usable_transcript),
            "wordCount": reference.get("wordCount") if reference else None,
            "contentHash": reference.get("contentHash") if reference else None,
            "anchorInstant": reference.get("anchorInstant") if reference else None,
            "anchorBusinessDate": reference.get("anchorBusinessDate") if reference else None,
            "anchorSource": reference.get("anchorSource") if reference else None,
            "receivedAt": reference.get("receivedAt") if reference else None,
            "error": reference.get("error") if reference else None,
            "receivedOutsideRequestedInterval": None,  # filled by caller-level check
        },
        "summaries": {
            "present": has_summary,
            "providerCount": len(provider_summaries),
            "aiCount": len(ai_summaries),
            "providerSummaryIds": [s["id"] for s in provider_summaries],
            "aiSummaryIds": [s["id"] for s in ai_summaries],
            "templates": sorted({s.get("template") for s in mine_summaries if s.get("template")}),
        },
        "analysis": {
            "runCount": len(mine_runs),
            "statuses": _tally(r.get("status") for r in mine_runs),
            "runModes": _tally(r.get("runMode") for r in mine_runs),
            "latestRun": _latest_run(mine_runs),
            "errorCodes": _tally(r.get("errorCode") for r in mine_runs if r.get("errorCode")),
        },
        "contentAvailability": content,
        "fixture": fingerprint.as_dict(),
    }


def _latest_run(runs: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not runs:
        return None
    ordered = sorted(runs, key=lambda r: str(r.get("queuedAt") or ""))
    run = ordered[-1]
    return {
        "id": run.get("id"),
        "status": run.get("status"),
        "runMode": run.get("runMode"),
        "attempt": run.get("attempt"),
        "provider": run.get("provider"),
        "model": run.get("model"),
        "errorCode": run.get("errorCode"),
        "error": run.get("error"),
        "queuedAt": run.get("queuedAt"),
        "startedAt": run.get("startedAt"),
        "completedAt": run.get("completedAt"),
    }


def _describe_webhooks(
    inbox_rows: list[dict[str, Any]],
    event_rows: list[dict[str, Any]],
    *,
    meeting_ids: set[str],
    known_meeting_titles: dict[str, Any],
    registry: fixtures_mod.FixtureRegistry,
) -> list[dict[str, Any]]:
    by_inbox = {row["id"]: row for row in inbox_rows}
    out: list[dict[str, Any]] = []

    for row in event_rows:
        meeting_id = row.get("meetingId")
        linked = meeting_id if meeting_id in meeting_ids else meeting_id
        out.append(
            {
                "source": "meeting_webhook_events",
                "id": row.get("id"),
                "provider": row.get("provider"),
                "providerEventId": row.get("providerEventId"),
                "eventType": row.get("eventType"),
                "status": row.get("status"),
                "signatureVerified": row.get("signatureVerified"),
                "receivedAt": row.get("receivedAt"),
                "sentAt": row.get("sentAt"),
                "processedAt": row.get("processedAt"),
                "linkedMeetingId": linked,
                "linkedMeetingTitle": known_meeting_titles.get(str(linked)),
                "linkedInInterval": linked in meeting_ids if linked else False,
                "error": row.get("error"),
                "contentHash": row.get("contentHash"),
                "providerMeetingIdFromPayload": None,
                "inboxRowId": row.get("inboxId") if "inboxId" in row else None,
                "inboxStatus": (by_inbox.get(row.get("inboxId"), {}) or {}).get("status")
                if "inboxId" in row
                else None,
            }
        )

    out.extend(
        {
            "source": "meeting_webhook_inbox",
            "id": row.get("id"),
            "provider": row.get("provider"),
            "providerEventId": row.get("providerEventId"),
            "eventType": row.get("eventType"),
            "status": row.get("status"),
            "signatureVerified": None,
            "receivedAt": row.get("receivedAt"),
            "sentAt": row.get("sentAt"),
            "processedAt": row.get("processedAt"),
            "failedAt": row.get("failedAt"),
            "attemptCount": row.get("attemptCount"),
            "nextAttemptAt": row.get("nextAttemptAt"),
            "linkedMeetingId": None,
            "linkedMeetingTitle": None,
            "linkedInInterval": False,
            "error": row.get("lastError"),
            "contentHash": None,
            "providerMeetingIdFromPayload": None,
            "inboxRowId": row.get("id"),
            "inboxStatus": row.get("status"),
            "note": (
                "Inbox rows carry no meetingId column in this schema. The provider "
                "meeting id is only available inside rawBody, which inspect does "
                "not fetch."
            ),
        }
        for row in inbox_rows
    )
    return out


def _linked_outside_interval(
    transcripts: list[dict[str, Any]],
    summaries: list[dict[str, Any]],
    day: localdate.LocalDay,
) -> list[dict[str, Any]]:
    """Linked records whose own timestamp falls outside the selected day."""
    out: list[dict[str, Any]] = []
    for row in transcripts:
        moment = localdate.parse_timestamp(row.get("receivedAt"))
        if moment is not None and not day.contains_utc(moment):
            out.append(
                {
                    "kind": "transcript",
                    "id": row.get("id"),
                    "meetingId": row.get("meetingId"),
                    "timestampField": "receivedAt",
                    "timestamp": row.get("receivedAt"),
                    "linkageLabel": "linked-to-selected-meeting, outside selected day",
                }
            )
    for row in summaries:
        moment = localdate.parse_timestamp(row.get("createdAt"))
        if moment is not None and not day.contains_utc(moment):
            out.append(
                {
                    "kind": "summary",
                    "id": row.get("id"),
                    "meetingId": row.get("meetingId"),
                    "timestampField": "createdAt",
                    "timestamp": row.get("createdAt"),
                    "linkageLabel": "linked-to-selected-meeting, outside selected day",
                }
            )
    return out


def _tally(values) -> dict[str, int]:
    counts: dict[str, int] = {}
    for value in values:
        key = str(value) if value is not None else "null"
        counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items()))


def _status_for(data: dict[str, Any]) -> str:
    counts = data["counts"]
    reported = counts["meetingsReported"]
    started = counts["meetingsStartedInInterval"]
    webhook_rows = counts["webhookInboxRowsReceived"] + counts["webhookEventsReceived"]

    # Nothing in the interval at all: meetings never arrived and no webhook was
    # received. This is the "no activity for this day" outcome, not an error.
    if started == 0 and webhook_rows == 0:
        return status_mod.NO_MEETINGS

    # Meetings matched but every one was withheld by --exclude-fixtures. The
    # search worked and found rows; saying NO_MEETINGS here would be a lie.
    if started > 0 and reported == 0:
        if counts["meetingsExcludedAsFixtures"]:
            return status_mod.OK
        return status_mod.NO_MEETINGS

    if reported > 1:
        return status_mod.MULTIPLE_MEETINGS

    only = data["meetings"][0]["contentAvailability"] if data["meetings"] else None
    if only == CONTENT_NEITHER:
        return status_mod.CONTENT_UNAVAILABLE
    return status_mod.OK


def _provider_failure(command: str, exc: SupabaseError, day: localdate.LocalDay, tz_name: str) -> status_mod.Result:
    return status_mod.Result(
        command=command,
        status=exc.category,
        data={
            "requested": {"date": day.day.isoformat(), "timezone": tz_name},
            "error": {"message": exc.message, "detail": exc.detail},
        },
    )