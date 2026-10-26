"""`identities` - attendees, speaker labels and user records, exact rules only.

Read-only and network-dependent by default, but `--bundle` resolves identities
from a saved bundle with no credentials and no network at all, which is how a
fetched meeting is re-examined later.

User fields are limited to `id, email, displayName, isActive, roles, dept`.
Nothing about credentials, invitations or identity-provider linkage is fetched,
because nothing here needs it.
"""

from __future__ import annotations

from typing import Any

from . import identities as identities_mod, people, status as status_mod
from .config import Config
from .output import Output
from .supabase import SupabaseClient, SupabaseError, eq, in_list, is_null

MEETING_COLUMNS = [
    "id",
    "title",
    "providerMeetingId",
    "startedAt",
    "attendees",
    "organizerEmail",
    "sourceAccountEmail",
    "transcriptStatus",
]


def run(
    config: Config,
    *,
    meeting_id: str | None,
    bundle_path: str | None,
    output: Output,
) -> status_mod.Result:
    if not meeting_id and not bundle_path:
        return status_mod.Result(
            command="identities",
            status=status_mod.USAGE_ERROR,
            findings=[
                status_mod.Finding(
                    "select-source",
                    False,
                    "either --meeting-id or --bundle is required",
                    remedy=(
                        "--bundle reuses saved evidence with no network or "
                        "credentials; --meeting-id queries Supabase read-only"
                    ),
                )
            ],
        )
    if bundle_path:
        return _from_bundle(bundle_path)
    return _from_supabase(config, meeting_id=meeting_id, output=output)


def _from_bundle(bundle_path: str) -> status_mod.Result:
    """Offline path: every fact comes from the saved bundle."""
    import json

    try:
        with open(bundle_path, encoding="utf-8") as handle:
            bundle = json.load(handle)
    except FileNotFoundError:
        return status_mod.Result(
            command="identities",
            status=status_mod.USAGE_ERROR,
            data={"requested": {"bundle": bundle_path}},
            findings=[status_mod.Finding("read-bundle", False, "bundle does not exist")],
        )
    except json.JSONDecodeError as exc:
        return status_mod.Result(
            command="identities",
            status=status_mod.USAGE_ERROR,
            data={"requested": {"bundle": bundle_path}},
            findings=[
                status_mod.Finding("read-bundle", False, f"bundle is not valid JSON: {exc}")
            ],
        )

    if not isinstance(bundle, dict) or "meeting" not in bundle:
        return status_mod.Result(
            command="identities",
            status=status_mod.USAGE_ERROR,
            data={"requested": {"bundle": bundle_path}},
            findings=[
                status_mod.Finding(
                    "read-bundle", False, "file has no `meeting` section"
                )
            ],
        )

    section = bundle.get("identities")
    source_block = {
        "mode": "bundle",
        "bundle": bundle_path,
        "networkUsed": False,
        "credentialsUsed": False,
        "runId": bundle.get("runId"),
        "retrievedAt": bundle.get("retrievedAt"),
    }
    meeting_block = {
        "id": bundle.get("meeting", {}).get("id"),
        "title": bundle.get("meeting", {}).get("title"),
        "providerMeetingId": bundle.get("meeting", {}).get("providerMeetingId"),
        "startedAt": bundle.get("meeting", {}).get("startedAt"),
    }

    if not isinstance(section, dict):
        return status_mod.Result(
            command="identities",
            status=status_mod.CONTENT_UNAVAILABLE,
            data={
                "source": source_block,
                "meeting": meeting_block,
                "attendees": {"count": bundle.get("attendees", {}).get("count")},
            },
            findings=[
                status_mod.Finding(
                    "identity-section",
                    None,
                    "this bundle carries no `identities` section, so it holds no "
                    "identity evidence to reproduce",
                    remedy="re-run fetch for this meeting, which includes it",
                )
            ],
        )

    return status_mod.Result(
        command="identities",
        status=_status_for(section),
        data={
            "source": source_block,
            "meeting": meeting_block,
            "attendees": {
                "count": bundle.get("attendees", {}).get("count"),
                "watchedAddresses": bundle.get("attendees", {}).get("watchedAddresses"),
            },
            "rules": section.get("rules"),
            "identityRulesSource": (
                "recorded in the bundle at retrieval time; not recomputed here"
            ),
            "identities": {
                "attendees": section.get("attendees"),
                "speakers": section.get("speakers"),
            },
            "summary": section.get("summary"),
            "requiresAgentReview": section.get("requiresAgentReview"),
        },
        notes=[
            "resolved entirely from the saved bundle: no network access and no "
            "credentials were used"
        ],
    )


def _from_supabase(config: Config, *, meeting_id: str | None, output: Output) -> status_mod.Result:
    try:
        client = SupabaseClient(config)
    except Exception as exc:  # ConfigError
        return status_mod.Result(
            command="identities",
            status=status_mod.CONFIGURATION_ERROR,
            findings=[status_mod.Finding("supabase-config", False, str(exc))],
        )

    try:
        output.progress(f"reading attendees and users for {meeting_id}")
        meeting_page = client.select(
            "meetings", columns=MEETING_COLUMNS, filters={"id": eq(meeting_id)}, limit=1
        )
        if not meeting_page.rows:
            return status_mod.Result(
                command="identities",
                status=status_mod.NOT_FOUND,
                data={"requested": {"meetingId": meeting_id}},
                findings=[
                    status_mod.Finding("meeting-lookup", False, "no meeting with that id")
                ],
            )
        meeting = meeting_page.rows[0]

        alias_rows = client.select_all(
            "speaker_aliases",
            columns=identities_mod.ALIAS_COLUMNS,
            filters={"meetingId": eq(meeting_id)},
            order="createdAt.asc",
        ).rows
        global_rows = client.select_all(
            "speaker_aliases",
            columns=identities_mod.ALIAS_COLUMNS,
            filters={"meetingId": is_null()},
            order="createdAt.asc",
        ).rows
        users_page = client.select_all(
            "users", columns=identities_mod.USER_COLUMNS, order="email.asc"
        )
        transcript_rows = client.select_all(
            "meeting_transcripts",
            columns=["id", "revision", "revisionStatus", "speakers"],
            filters={"meetingId": eq(meeting_id)},
            order="revision.asc",
        ).rows
        if transcript_rows:
            segment_rows = client.select_all(
                "transcript_segments",
                columns=["transcriptId", "speakerName"],
                filters={"transcriptId": in_list([t["id"] for t in transcript_rows])},
            ).rows
        else:
            segment_rows = []
    except SupabaseError as exc:
        return status_mod.Result(
            command="identities",
            status=exc.category,
            data={"error": {"message": exc.message, "detail": exc.detail}},
        )

    aliases = _merge(alias_rows, global_rows)
    users = identities_mod.build_user_index(users_page.rows)
    alias_index = identities_mod.build_alias_index(aliases)
    attendees = people.extract_attendees(meeting.get("attendees"))

    speaker_labels = _collect_speakers(transcript_rows, segment_rows)
    attendee_mappings = identities_mod.map_attendees(
        attendees.entries, users=users, alias_index=alias_index, meeting_id=meeting_id
    )
    speaker_mappings = identities_mod.map_labels(
        speaker_labels, users=users, alias_index=alias_index, meeting_id=meeting_id
    )
    unresolved = identities_mod.unresolved(attendee_mappings + speaker_mappings)

    incomplete: list[str] = []
    if not users_page.complete:
        incomplete.append("users")
    if meeting.get("transcriptStatus") in ("READY", "RECEIVED") and not transcript_rows:
        incomplete.append("transcript speaker labels (status is live but no rows returned)")

    section = {
        "summary": {
            "attendees": identities_mod.summarise_mappings(attendee_mappings),
            "speakers": identities_mod.summarise_mappings(speaker_mappings),
        },
        "requiresAgentReview": unresolved,
    }

    return status_mod.Result(
        command="identities",
        status=_status_for(section),
        data={
            "source": {
                "mode": "supabase",
                "networkUsed": True,
                "credentialsUsed": True,
                "userColumnsRequested": list(identities_mod.USER_COLUMNS),
            },
            "meeting": {
                "id": meeting.get("id"),
                "title": meeting.get("title"),
                "providerMeetingId": meeting.get("providerMeetingId"),
                "startedAt": meeting.get("startedAt"),
            },
            "rules": [
                "EXACT_EMAIL: normalised (trimmed, case-folded) email equality; the "
                "only email-verified rule",
                "EXPLICIT_ALIAS: a speaker_aliases row naming the label; a "
                "MEETING-scoped row beats a GLOBAL one",
                "UNIQUE_EXACT_NAME: exactly one active user with an equal normalised "
                "display name; name-based, not email-verified",
                "UNMATCHED / AMBIGUOUS / INACTIVE: reported, never resolved",
                "no fuzzy or spelling-distance matching; no alias creation; no writes",
            ],
            "aliasRows": aliases,
            "attendees": {
                "count": attendees.count,
                "watchedAddresses": people.watched_address_report(
                    attendee_emails=attendees.emails(),
                    organizer_email=meeting.get("organizerEmail"),
                    source_account_email=meeting.get("sourceAccountEmail"),
                ),
                "mappings": [m.as_dict() for m in attendee_mappings],
            },
            "speakers": {
                "labels": speaker_labels,
                "mappings": [m.as_dict() for m in speaker_mappings],
            },
            "summary": section["summary"],
            "requiresAgentReview": [m.as_dict() for m in unresolved],
            "identityMeaning": {
                "resolved": (
                    "each mapping states the evidence that produced it; a name-based "
                    "mapping is not email-verified"
                ),
                "unresolved": (
                    "an unmatched or ambiguous label is for the agent to judge from "
                    "the returned evidence; it is never converted into a recipient"
                ),
                "notPresented": (
                    "a label absent from the attendee list is absent from that list; "
                    "the list does not establish whether anyone joined"
                ),
            },
        },
        notes=[
            f"{len(unresolved)} label(s) require agent review"
            if unresolved
            else "every attendee and speaker label resolved by an exact rule"
        ],
        incomplete=incomplete,
    )


def _collect_speakers(
    transcripts: list[dict[str, Any]],
    segments: list[dict[str, Any]],
) -> list[str]:
    labels: list[str] = []
    canonical = next(
        (t for t in transcripts if t.get("revisionStatus") == "CANONICAL"),
        transcripts[0] if transcripts else None,
    )
    if canonical and isinstance(canonical.get("speakers"), list):
        for entry in canonical["speakers"]:
            name = entry.get("name") if isinstance(entry, dict) else entry
            if name and name not in labels:
                labels.append(str(name))
    canonical_id = canonical.get("id") if canonical else None
    for row in segments:
        if canonical_id and row.get("transcriptId") != canonical_id:
            continue
        name = row.get("speakerName")
        if name and name not in labels:
            labels.append(str(name))
    return labels


def _merge(
    meeting_rows: list[dict[str, Any]], global_rows: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    seen: dict[str, dict[str, Any]] = {}
    for row in list(meeting_rows) + list(global_rows):
        seen[str(row.get("id"))] = row
    return list(seen.values())


def _status_for(section: dict[str, Any]) -> str:
    review = section.get("requiresAgentReview") or []
    summary = section.get("summary") or {}
    resolved = 0
    for counts in summary.values():
        if isinstance(counts, dict):
            resolved += sum(counts.get(state, 0) for state in identities_mod.RESOLVED_STATES)
    if review and not resolved:
        return status_mod.IDENTITY_REVIEW_REQUIRED
    if review:
        return status_mod.PARTIAL_RESULT
    return status_mod.OK