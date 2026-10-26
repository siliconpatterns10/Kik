"""Tests for the phase-1 commands: run store, batching, identity mapping.

These tests are offline by construction. They build synthetic data in memory and
write into a temporary artifacts root, so nothing touches Supabase, Render or
Microsoft Graph, and no test depends on the network being up.
"""

import json
import sys
from pathlib import Path

TOOLKIT_ROOT = Path(__file__).resolve().parent.parent
if str(TOOLKIT_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOLKIT_ROOT))

from lib import identities as ident  # noqa: E402
from lib import people  # noqa: E402
from lib import runstore, transcripts  # noqa: E402

TMP = Path("/tmp/kik-phase1-tests")


def _fresh_root() -> Path:
    if TMP.exists():
        for child in sorted(TMP.rglob("*"), reverse=True):
            if child.is_file():
                child.unlink()
            else:
                child.rmdir()
    TMP.mkdir(parents=True, exist_ok=True)
    return TMP


# -- run store -----------------------------------------------------------


def test_run_id_shape_and_sortability() -> None:
    from datetime import datetime, timezone

    early = runstore.new_run_id(datetime(2026, 10, 20, 7, 0, 0, tzinfo=timezone.utc))
    late = runstore.new_run_id(datetime(2026, 10, 20, 8, 0, 0, tzinfo=timezone.utc))
    assert early.startswith("20261020T070000Z-")
    assert late.startswith("20261020T080000Z-")
    assert early < late, "run ids must sort chronologically"


def test_run_directory_is_refused_when_it_exists() -> None:
    root = _fresh_root()
    first = runstore.new_run(root, command="fetch", local_date="2026-10-20")
    try:
        runstore.new_run(
            root, command="fetch", local_date="2026-10-20", run_id=first.run_id
        )
    except runstore.RunError as exc:
        assert "will not be overwritten" in str(exc)
    else:
        raise AssertionError("an existing run directory must never be reused")


def test_invalid_run_id_is_refused() -> None:
    root = _fresh_root()
    for bad in ("nope", "2026-10-20", "../../etc"):
        try:
            runstore.new_run(root, command="fetch", local_date="2026-10-20", run_id=bad)
        except runstore.RunError:
            continue
        raise AssertionError(f"expected RunError for run id {bad!r}")


def test_manifest_records_hashes_and_completeness() -> None:
    root = _fresh_root()
    record = runstore.new_run(root, command="fetch", local_date="2026-10-20")
    record.write_json("bundle.json", {"meeting": {"id": "m1"}})
    record.manifest.completeness = {"complete": True, "meetingsFetched": 1}
    sealed = record.seal()

    produced = {item["name"]: item for item in sealed["produced"]}
    assert "bundle.json" in produced
    assert produced["bundle.json"]["sha256"] == runstore.sha256_file(
        record.directory / "bundle.json"
    )
    assert sealed["schemaVersion"] == runstore.SCHEMA_VERSION
    assert sealed["completeness"]["complete"] is True


def test_manifest_carries_no_credential() -> None:
    root = _fresh_root()
    record = runstore.new_run(root, command="doctor", local_date="2026-10-20")
    record.manifest.arguments = {
        "header": "Bearer abcdef1234567890",
        "note": "Authorization: Bearer xyz",
    }
    sealed = record.seal()
    rendered = json.dumps(sealed)
    assert "abcdef1234567890" not in rendered
    assert output_redacted() in rendered


def output_redacted() -> str:
    from lib import output as output_mod

    return output_mod.REDACTED


def test_content_hash_survives_manifest_redaction() -> None:
    """A run manifest must stay usable as evidence."""
    root = _fresh_root()
    record = runstore.new_run(root, command="fetch", local_date="2026-10-20")
    digest = "9ea4e98d5fb1ad437cb66d66d1f9c1105398ec7d3bbdb6df78013ce9fb0c6d58"
    record.manifest.sources = {"contentHash": digest, "providerMeetingId": "mtg_abc-123"}
    sealed = record.seal()
    assert sealed["sources"]["contentHash"] == digest
    assert sealed["sources"]["providerMeetingId"] == "mtg_abc-123"


def test_discover_runs_reads_manifest_headers() -> None:
    root = _fresh_root()
    record = runstore.new_run(root, command="logs", local_date="2026-10-19")
    record.seal()
    found = runstore.discover_runs(root)
    assert len(found) == 1
    assert found[0].command == "logs"
    assert found[0].local_date == "2026-10-19"
    assert found[0].manifest_path is not None


def test_discover_runs_handles_a_run_without_a_manifest() -> None:
    root = _fresh_root()
    orphan = root / "2026-10-20" / "20261020T000000Z-abc123"
    orphan.mkdir(parents=True)
    found = runstore.discover_runs(root)
    assert len(found) == 1
    assert found[0].manifest_path is None
    assert found[0].command is None


def test_find_artifact_refuses_escape() -> None:
    root = _fresh_root()
    record = runstore.new_run(root, command="fetch", local_date="2026-10-20")
    record.write_text("ok.txt", "fine")
    assert runstore.find_artifact(record.directory, "ok.txt", root=root).is_file()
    try:
        runstore.find_artifact(record.directory, "../manifest.json", root=root)
    except runstore.RunError as exc:
        assert "escapes" in str(exc)
    else:
        raise AssertionError("a manifest-listed name must not escape its run")


def test_find_artifact_reports_a_missing_file() -> None:
    root = _fresh_root()
    record = runstore.new_run(root, command="fetch", local_date="2026-10-20")
    try:
        runstore.find_artifact(record.directory, "absent.txt", root=root)
    except runstore.RunError as exc:
        assert "missing" in str(exc)
    else:
        raise AssertionError("expected a RunError for a missing artifact")


# -- transcript batching -------------------------------------------------


def _segments(count: int, chars_each: int = 60) -> list[dict]:
    return [
        {
            "id": f"seg-{i}",
            "sequence": i,
            "speakerName": "Speaker A" if i % 2 == 0 else "Speaker B",
            "startSeconds": i * 3,
            "endSeconds": i * 3 + 2,
            "text": f"segment number {i} " + "x" * (chars_each - len(str(i)) - 15),
        }
        for i in range(count)
    ]


def test_batching_is_deterministic() -> None:
    rows = _segments(40)
    plan_a = transcripts.plan_batches(
        transcripts.units_from_segments(rows, max_chars=600)[0],
        max_chars=600,
        mode="segments",
        content_sha256="abc",
        source_char_count=sum(len(r["text"]) for r in rows),
    )
    plan_b = transcripts.plan_batches(
        transcripts.units_from_segments(rows, max_chars=600)[0],
        max_chars=600,
        mode="segments",
        content_sha256="abc",
        source_char_count=sum(len(r["text"]) for r in rows),
    )
    assert [b.batch_id for b in plan_a.batches] == [b.batch_id for b in plan_b.batches]


def test_batch_id_depends_on_content_not_only_position() -> None:
    assert transcripts.batch_identifier(1, "aaa") != transcripts.batch_identifier(1, "bbb")
    assert transcripts.batch_identifier(1, "abc") == transcripts.batch_identifier(1, "abc")


def test_no_segment_is_lost_or_reordered() -> None:
    rows = _segments(80)
    units, _ = transcripts.units_from_segments(rows, max_chars=500)
    plan = transcripts.plan_batches(
        units,
        max_chars=500,
        mode="segments",
        content_sha256="abc",
        source_char_count=sum(len(r["text"]) for r in rows),
    )
    flat = [u for batch in plan.batches for u in batch.units]
    assert [u.sequence for u in flat] == [r["sequence"] for r in rows]
    assert "".join(u.text for u in flat) == "".join(r["text"] for r in rows)


def test_batches_never_exceed_the_limit_of_units() -> None:
    rows = _segments(50, chars_each=200)
    units, _ = transcripts.units_from_segments(rows, max_chars=700)
    plan = transcripts.plan_batches(
        units,
        max_chars=700,
        mode="segments",
        content_sha256="abc",
        source_char_count=sum(len(r["text"]) for r in rows),
    )
    for batch in plan.batches:
        assert sum(u.char_count for u in batch.units) <= 700


def test_oversized_segment_is_split_with_continuation_metadata() -> None:
    rows = [
        {
            "id": "big",
            "sequence": 0,
            "speakerName": "Speaker",
            "startSeconds": 0,
            "endSeconds": 60,
            "text": "w" * 5000,
        }
    ]
    units, notes = transcripts.units_from_segments(rows, max_chars=2000)
    assert len(units) == 3
    assert "".join(u.text for u in units) == rows[0]["text"]
    assert [u.continuation_index for u in units] == [1, 2, 3]
    assert all(u.continuation_total == 3 for u in units)
    assert all(u.sequence == 0 for u in units)
    assert all(u.segment_id == "big" for u in units)
    assert units[1].source_char_start == 2000
    assert notes and "split into 3 continuations" in notes[0]


def test_full_text_only_transcript_batches() -> None:
    text = ("A sentence about the meeting. " * 200).strip()
    units, notes = transcripts.units_from_full_text(text, max_chars=500)
    plan = transcripts.plan_batches(
        units,
        max_chars=500,
        mode="full-text",
        content_sha256="abc",
        source_char_count=len(text),
    )
    assert plan.mode == "full-text"
    assert "".join(u.text for b in plan.batches for u in b.units) == text
    assert plan.batch_count > 1


def test_empty_full_text_is_reported_not_batched() -> None:
    units, notes = transcripts.units_from_full_text("", max_chars=500)
    assert units == []
    assert notes


def test_mixed_sequence_presence_is_refused() -> None:
    rows = [
        {"id": "a", "sequence": 0, "text": "one"},
        {"id": "b", "sequence": None, "text": "two"},
    ]
    try:
        transcripts.units_from_segments(rows, max_chars=1000)
    except transcripts.BatchError as exc:
        assert "ambiguous" in str(exc)
    else:
        raise AssertionError("mixed sequence presence must be refused")


def test_duplicate_sequence_is_refused() -> None:
    rows = [
        {"id": "a", "sequence": 1, "text": "one"},
        {"id": "b", "sequence": 1, "text": "two"},
    ]
    try:
        transcripts.units_from_segments(rows, max_chars=1000)
    except transcripts.BatchError as exc:
        assert "duplicate" in str(exc)
    else:
        raise AssertionError("duplicate sequences must be refused")


def test_max_chars_bounds_are_enforced() -> None:
    for bad in (10, 100, 2_000_000):
        try:
            transcripts.plan_batches(
                [], max_chars=bad, mode="segments", content_sha256="x", source_char_count=0
            )
        except transcripts.BatchError:
            continue
        raise AssertionError(f"expected BatchError for --max-chars {bad}")


def test_read_rejects_a_batch_number_out_of_range() -> None:
    units, _ = transcripts.units_from_segments(_segments(3), max_chars=1000)
    plan = transcripts.plan_batches(
        units, max_chars=1000, mode="segments", content_sha256="a", source_char_count=0
    )
    for bad in (0, 99, -1):
        try:
            transcripts.render_batch(plan, bad)
        except transcripts.BatchError as exc:
            assert "does not exist" in str(exc)
        else:
            raise AssertionError(f"expected BatchError for batch {bad}")


def test_acknowledgement_is_recorded_as_an_acknowledgement() -> None:
    units, _ = transcripts.units_from_segments(_segments(3), max_chars=1000)
    plan = transcripts.plan_batches(
        units, max_chars=1000, mode="segments", content_sha256="a", source_char_count=0
    )
    transcripts.acknowledge(plan, 1)
    coverage = transcripts.coverage(plan)
    assert coverage["acknowledged"] == [1]
    assert coverage["notAcknowledged"] == list(range(2, plan.batch_count + 1))
    assert "not proof of comprehension" in coverage["acknowledgementMeaning"]


def test_manifest_states_no_token_estimate() -> None:
    units, _ = transcripts.units_from_segments(_segments(3), max_chars=1000)
    plan = transcripts.plan_batches(
        units, max_chars=1000, mode="segments", content_sha256="a", source_char_count=0
    )
    batching = plan.manifest()["batching"]
    assert batching["maxCharsUnit"] == "characters"
    assert batching["tokenEstimateAvailable"] is False
    assert "token" in batching["tokenEstimateNote"]


def test_plan_from_bundle_without_a_transcript_is_refused() -> None:
    try:
        transcripts.plan_from_bundle({"meeting": {"id": "m1"}, "transcript": {}}, max_chars=1000)
    except transcripts.BatchError as exc:
        assert "no" in str(exc).lower()
    else:
        raise AssertionError("a bundle with no source text must be refused")


# -- people / provider id extraction -------------------------------------


def test_extract_provider_id_from_documented_path() -> None:
    body = json.dumps({"data": {"meeting": {"id": "mtg_x"}}})
    found = people.extract_provider_meeting_id(body)
    assert found.provider_meeting_id == "mtg_x"
    assert found.json_path == "data.meeting.id"


def test_extract_provider_id_reports_path_for_alternate_shape() -> None:
    body = json.dumps({"meeting": {"id": "mtg_y"}})
    found = people.extract_provider_meeting_id(body)
    assert found.provider_meeting_id == "mtg_y"
    assert found.json_path == "meeting.id"


def test_extract_provider_id_never_guesses() -> None:
    body = json.dumps({"data": {"something": {"looks_like": "mtg_but_is_not_the_path"}}})
    found = people.extract_provider_meeting_id(body)
    assert found.provider_meeting_id is None
    assert found.json_path is None


def test_extract_provider_id_reports_unparseable_body() -> None:
    found = people.extract_provider_meeting_id("{not json")
    assert found.provider_meeting_id is None
    assert "not JSON" in (found.parse_error or "")


def test_extract_provider_id_from_non_string_id() -> None:
    found = people.extract_provider_meeting_id(json.dumps({"data": {"meeting": {"id": 42}}}))
    assert found.provider_meeting_id is None
    assert found.candidates


def test_attendee_extraction_counts_unknown_shapes() -> None:
    extract = people.extract_attendees(
        [{"name": "A", "email": "a@x"}, "B", 7]
    )
    assert extract.count == 2
    assert extract.malformed_count == 1


def test_attendee_extraction_handles_null_and_non_list() -> None:
    assert people.extract_attendees(None).count == 0
    assert people.extract_attendees({"name": "A"}).malformed_count == 1


def test_watched_address_reports_presence_without_attendance() -> None:
    report = people.watched_address_report(
        attendee_emails=["member@x.com", "vlsi_delivery@siliconpatterns.com"],
        source_account_email="vlsi_delivery@siliconpatterns.com",
    )
    by_address = {r["address"]: r for r in report}
    bot = by_address["teams@maestrolabs.com"]
    assert bot["inStoredAttendeeList"] is False
    assert "does not establish whether the bot joined" in bot["statement"]
    desk = by_address["vlsi_delivery@siliconpatterns.com"]
    assert desk["isSourceAccount"] is True
    assert desk["inStoredAttendeeList"] is True
    assert "does not establish that this account attended" in desk["statement"]


def test_watched_address_absence_is_reported_as_absence_only() -> None:
    """Both watched addresses absent must not become a claim about the meeting."""
    report = people.watched_address_report(attendee_emails=[], source_account_email=None)
    for entry in report:
        assert entry["inStoredAttendeeList"] is False
        assert entry["doesNotEstablish"] == (
            "attendance, or the absence of attendance"
        )
        assert "attendance" in entry["doesNotEstablish"]
        assert "did not" not in entry["statement"].lower()


def test_watched_addresses_are_compared_case_insensitively() -> None:
    report = people.watched_address_report(
        attendee_emails=["VLSI_Delivery@SiliconPatterns.com"]
    )
    desk = next(r for r in report if r["address"].startswith("vlsi_delivery"))
    assert desk["inStoredAttendeeList"] is True


# -- identity mapping ----------------------------------------------------


def _users() -> list[ident.UserRecord]:
    return ident.build_user_index(
        [
            {
                "id": "u1",
                "email": "a.person@example.com",
                "displayName": "A Person",
                "isActive": True,
                "roles": ["BU"],
                "dept": "DV",
            },
            {
                "id": "u2",
                "email": "gone@example.com",
                "displayName": "Gone Away",
                "isActive": False,
                "roles": ["BU"],
                "dept": None,
            },
            {
                "id": "u3",
                "email": "twin1@example.com",
                "displayName": "Same Name",
                "isActive": True,
                "roles": [],
                "dept": None,
            },
            {
                "id": "u4",
                "email": "twin2@example.com",
                "displayName": "Same Name",
                "isActive": True,
                "roles": [],
                "dept": None,
            },
        ]
    )


def test_exact_email_match_is_email_verified() -> None:
    result = ident.map_email(" A.Person@Example.com ", users=_users())
    assert result.state == ident.EXACT_EMAIL
    assert result.email_verified is True
    assert result.user["userId"] == "u1"


def test_email_matching_is_case_and_space_insensitive() -> None:
    assert ident.map_email("  A.PERSON@example.COM ", users=_users()).state == ident.EXACT_EMAIL


def test_unknown_email_is_unmatched_and_flagged_for_review() -> None:
    result = ident.map_email("nobody@example.com", users=_users())
    assert result.state == ident.UNMATCHED
    assert result.requires_agent_review is True


def test_inactive_email_match_is_reported_not_resolved() -> None:
    result = ident.map_email("gone@example.com", users=_users())
    assert result.state == ident.INACTIVE
    assert result.identity is None


def test_unique_exact_name_is_labelled_name_based() -> None:
    result = ident.resolve_label("A Person", users=_users(), alias_index={})
    assert result.state == ident.UNIQUE_EXACT_NAME
    assert result.email_verified is False
    assert "name-based" in result.evidence


def test_duplicate_display_name_is_ambiguous() -> None:
    result = ident.resolve_label("Same Name", users=_users(), alias_index={})
    assert result.state == ident.AMBIGUOUS
    assert len(result.candidates) == 2


def test_inactive_name_match_is_reported() -> None:
    result = ident.resolve_label("Gone Away", users=_users(), alias_index={})
    assert result.state == ident.INACTIVE


def test_explicit_alias_resolves_to_its_user() -> None:
    index = ident.build_alias_index(
        [
            {
                "id": "al1",
                "rawLabel": "Speaker Label",
                "kind": "PERSON",
                "userId": "u1",
                "canonicalName": "A Person",
                "scope": "GLOBAL",
                "meetingId": None,
            }
        ]
    )
    result = ident.resolve_label("speaker label", users=_users(), alias_index=index)
    assert result.state == ident.EXPLICIT_ALIAS
    assert result.user["userId"] == "u1"


def test_alias_kind_not_person_does_not_resolve() -> None:
    index = ident.build_alias_index(
        [
            {
                "id": "al2",
                "rawLabel": "Some Account",
                "kind": "ACCOUNT",
                "userId": "u1",
                "canonicalName": None,
                "scope": "GLOBAL",
                "meetingId": None,
            }
        ]
    )
    result = ident.resolve_label("Some Account", users=_users(), alias_index=index)
    assert result.state == ident.UNMATCHED
    assert result.identity is None


def test_meeting_scoped_alias_wins_over_global() -> None:
    """One meeting-scoped row and one global row for the same label is the
    *normal* case, not a collision, so the meeting-scoped row wins.

    The narrowing only counts meeting-scoped rows that actually belong to the
    meeting under investigation; a meeting-scoped row for a different meeting
    must not take precedence over the global default.
    """
    index = ident.build_alias_index(
        [
            {
                "id": "g",
                "rawLabel": "Label",
                "kind": "PERSON",
                "userId": "u1",
                "canonicalName": None,
                "scope": "GLOBAL",
                "meetingId": None,
            },
            {
                "id": "m",
                "rawLabel": "Label",
                "kind": "PERSON",
                "userId": "u3",
                "canonicalName": None,
                "scope": "MEETING",
                "meetingId": "M1",
            },
        ]
    )
    result = ident.resolve_label("Label", users=_users(), alias_index=index, meeting_id="M1")
    assert result.state == ident.EXPLICIT_ALIAS
    assert result.user["userId"] == "u3"
    assert "scope MEETING" in result.evidence


def test_meeting_scoped_alias_for_another_meeting_falls_back_to_global() -> None:
    index = ident.build_alias_index(
        [
            {
                "id": "g",
                "rawLabel": "Label",
                "kind": "PERSON",
                "userId": "u1",
                "canonicalName": None,
                "scope": "GLOBAL",
                "meetingId": None,
            },
            {
                "id": "m",
                "rawLabel": "Label",
                "kind": "PERSON",
                "userId": "u3",
                "canonicalName": None,
                "scope": "MEETING",
                "meetingId": "OTHER",
            },
        ]
    )
    result = ident.resolve_label("Label", users=_users(), alias_index=index, meeting_id="M1")
    assert result.state == ident.EXPLICIT_ALIAS
    assert result.user["userId"] == "u1", "a foreign meeting alias must not win"


def test_two_meeting_scoped_aliases_for_one_meeting_are_ambiguous() -> None:
    index = ident.build_alias_index(
        [
            {
                "id": "m1",
                "rawLabel": "Label",
                "kind": "PERSON",
                "userId": "u1",
                "canonicalName": None,
                "scope": "MEETING",
                "meetingId": "M1",
            },
            {
                "id": "m2",
                "rawLabel": "Label",
                "kind": "PERSON",
                "userId": "u3",
                "canonicalName": None,
                "scope": "MEETING",
                "meetingId": "M1",
            },
        ]
    )
    result = ident.resolve_label("Label", users=_users(), alias_index=index, meeting_id="M1")
    assert result.state == ident.AMBIGUOUS
    assert len(result.candidates) == 2


def test_conflicting_aliases_are_ambiguous_not_arbitrated() -> None:
    index = ident.build_alias_index(
        [
            {
                "id": "m1",
                "rawLabel": "Label",
                "kind": "PERSON",
                "userId": "u1",
                "canonicalName": None,
                "scope": "MEETING",
                "meetingId": "M1",
            },
            {
                "id": "m2",
                "rawLabel": "Label",
                "kind": "PERSON",
                "userId": "u2",
                "canonicalName": None,
                "scope": "MEETING",
                "meetingId": "M1",
            },
        ]
    )
    result = ident.resolve_label("Label", users=_users(), alias_index=index, meeting_id="M1")
    assert result.state == ident.AMBIGUOUS
    assert len(result.candidates) == 2


def test_near_miss_name_is_not_resolved() -> None:
    """The property that matters most: no fuzzy resolution."""
    for label in ("A Persn", "A. Person", "Person A", "APerson", "Persons"):
        result = ident.resolve_label(label, users=_users(), alias_index={})
        assert result.state == ident.UNMATCHED, label
        assert result.identity is None, label


def test_near_miss_candidates_are_labelled_alternatives() -> None:
    result = ident.resolve_label("Madhukara Reddy", users=_users(), alias_index={})
    assert result.state == ident.UNMATCHED
    for candidate in result.candidates:
        assert "not a match and not a recipient" in candidate["note"]


def test_label_normalisation_handles_punctuation_and_zero_width() -> None:
    assert ident.normalize_label("  A Person. ") == "a person"
    assert ident.normalize_label("A\u200bPerson") == "aperson"
    assert ident.normalize_label(None) == ""


def test_attendee_mapping_prefers_email_then_name() -> None:
    mappings = ident.map_attendees(
        [
            {"name": "Display Name", "email": "a.person@example.com"},
            {"name": "A Person", "email": None},
            {"name": "Unknown Person", "email": None},
        ],
        users=_users(),
        alias_index={},
    )
    assert [m.state for m in mappings] == [
        ident.EXACT_EMAIL,
        ident.UNIQUE_EXACT_NAME,
        ident.UNMATCHED,
    ]


def test_fallback_to_name_records_that_the_email_missed() -> None:
    mappings = ident.map_attendees(
        [{"name": "A Person", "email": "old.address@example.com"}],
        users=_users(),
        alias_index={},
    )
    assert mappings[0].state == ident.UNIQUE_EXACT_NAME
    assert "matched no user" in mappings[0].evidence


def test_resolved_states_are_exactly_three() -> None:
    assert ident.RESOLVED_STATES == {
        ident.EXACT_EMAIL,
        ident.EXPLICIT_ALIAS,
        ident.UNIQUE_EXACT_NAME,
    }
    assert ident.UNMATCHED not in ident.RESOLVED_STATES
    assert ident.AMBIGUOUS not in ident.RESOLVED_STATES
    assert ident.INACTIVE not in ident.RESOLVED_STATES


def test_no_fuzzy_matcher_exists_in_the_module() -> None:
    """Guard against the application's SINGLE_EDIT rule being ported back in."""
    source = Path(ident.__file__).read_text(encoding="utf-8")
    for forbidden in ("SINGLE_EDIT", "edit_distance", "editDistance", "levenshtein", "token_overlap"):
        assert forbidden not in source, f"{forbidden} must not appear in identities.py"


def test_user_columns_exclude_identity_and_credential_fields() -> None:
    for forbidden in ("supabaseUserId", "entraOid", "mustChangePassword", "invitedById"):
        assert forbidden not in ident.USER_COLUMNS, forbidden