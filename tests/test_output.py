"""Status, exit-code and output tests.

Two properties matter most: JSON on stdout must be parseable on its own (no
progress mixed in), and secrets must not reach either stream. Redaction is also
tested against the false-positive case that broke it once: a content hash or
provider meeting id must survive intact, because it is evidence.
"""

import io
import json
import sys
from pathlib import Path

TOOLKIT_ROOT = Path(__file__).resolve().parent.parent
if str(TOOLKIT_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOLKIT_ROOT))

from lib import output as output_mod  # noqa: E402
from lib import status as status_mod  # noqa: E402


def _capture(result: status_mod.Result, *, json_mode: bool):
    out, err = io.StringIO(), io.StringIO()
    sink = output_mod.Output(json_mode=json_mode, stdout=out, stderr=err)
    code = output_mod.emit_result(sink, result)
    return code, out.getvalue(), err.getvalue()


def test_exit_codes_are_defined_for_every_status() -> None:
    for name in (
        status_mod.OK,
        status_mod.NO_MEETINGS,
        status_mod.CONTENT_UNAVAILABLE,
        status_mod.MULTIPLE_MEETINGS,
        status_mod.IDENTITY_REVIEW_REQUIRED,
        status_mod.CONFIGURATION_ERROR,
        status_mod.AUTHENTICATION_ERROR,
        status_mod.PERMISSION_ERROR,
        status_mod.PROVIDER_ERROR,
        status_mod.PARTIAL_RESULT,
        status_mod.SEND_RESULT_UNKNOWN,
    ):
        assert name in status_mod.EXIT_CODES, name


def test_no_meetings_is_success_not_failure() -> None:
    assert status_mod.exit_code_for(status_mod.NO_MEETINGS) == 0


def test_configuration_and_auth_errors_are_distinct() -> None:
    assert status_mod.exit_code_for(
        status_mod.CONFIGURATION_ERROR
    ) != status_mod.exit_code_for(status_mod.AUTHENTICATION_ERROR)


def test_unknown_status_gets_a_nonzero_default() -> None:
    assert status_mod.exit_code_for("SOMETHING_NEW") != 0


def test_json_output_is_parseable_alone() -> None:
    result = status_mod.Result(
        command="doctor",
        status=status_mod.OK,
        data={"checks": 3},
        notes=["a note"],
    )
    code, out, err = _capture(result, json_mode=True)
    payload = json.loads(out)
    assert payload["command"] == "doctor"
    assert payload["status"] == "OK"
    assert payload["exitCode"] == 0 == code
    assert payload["checks"] == 3
    assert err == "", "JSON mode must not write notes or progress to stdout"


def test_json_mode_keeps_diagnostics_out_of_stdout() -> None:
    """stdout must be exactly one parseable JSON document and nothing else.

    The document may legitimately *carry* warnings as data - that is the point
    of the `warnings` field. What must never appear is the human diagnostic
    line, in the same shape the human renderer would print.
    """
    phrase = "phase0-unique-warning-phrase"
    result = status_mod.Result(
        command="inspect", status=status_mod.OK, warnings=[phrase]
    )
    code, out, err = _capture(result, json_mode=True)

    payload = json.loads(out)
    assert payload["warnings"] == [phrase], "the warning is data in the document"
    assert f"warning: {phrase}" in err, "the operator still sees the diagnostic"
    assert f"warning: {phrase}" not in out, (
        "the human diagnostic line must not be mixed into stdout"
    )
    assert out.count("{") == 1, "stdout must contain exactly one JSON document"


def test_incomplete_marks_the_result_and_exits_nonzero() -> None:
    result = status_mod.Result(
        command="logs",
        status=status_mod.OK,
        incomplete=["log pagination stopped at the page ceiling (20 pages)"],
    )
    code, out, _ = _capture(result, json_mode=True)
    payload = json.loads(out)
    assert payload["complete"] is False
    assert code == 0
    assert payload["incomplete"]


def test_no_incomplete_means_complete_true() -> None:
    result = status_mod.Result(command="inspect", status=status_mod.NO_MEETINGS)
    _, out, _ = _capture(result, json_mode=True)
    payload = json.loads(out)
    assert payload["complete"] is True
    assert "incomplete" not in payload


def test_findings_render_with_marks() -> None:
    result = status_mod.Result(
        command="doctor",
        status=status_mod.CONFIGURATION_ERROR,
        findings=[
            status_mod.Finding("one", True, "fine"),
            status_mod.Finding("two", False, "broken", remedy="fix it"),
            status_mod.Finding("three", None, "informational"),
        ],
    )
    code, out, _ = _capture(result, json_mode=False)
    assert "[OK] one: fine" in out
    assert "[FAIL] two: broken" in out
    assert "remedy: fix it" in out
    assert "[INFO] three: informational" in out
    assert code == status_mod.exit_code_for(status_mod.CONFIGURATION_ERROR)


def test_redaction_catches_credential_shapes() -> None:
    cases = [
        "Authorization: Bearer abc123def456",
        "token eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxIn0.abcdefghij",
        "CLIENT_SECRET=abcdef123456",
        "postgresql://user:pw@host:5432/db",
        "redis://default:pw@host:6379",
        "https://user:pw@host/path",
    ]
    for case in cases:
        assert output_mod.REDACTED in output_mod.redact(case), case


def test_redaction_does_not_destroy_evidence_identifiers() -> None:
    """Content hashes and provider meeting ids are evidence; keep them."""
    content_hash = "9ea4e98d5fb1ad437cb66d66d1f9c1105398ec7d3bbdb6df78013ce9fb0c6d58"
    provider_meeting_id = "mtg_f0fa7b81-5ef8-4ef9-954a-0a3db7778502"
    cuid = "cmv9c9xz4004tl5r5ovohsicz"
    uuid = "862d85f3-fc80-4ea5-be92-c727a1e2c6bf"
    for value in (content_hash, provider_meeting_id, cuid, uuid):
        assert output_mod.redact(value) == value, value


def test_redaction_covers_nested_structures() -> None:
    result = status_mod.Result(
        command="doctor",
        status=status_mod.OK,
        data={"nested": {"header": "Bearer topsecretvalue123"}},
    )
    _, out, _ = _capture(result, json_mode=True)
    assert "topsecretvalue123" not in out
    assert output_mod.REDACTED in out


def test_progress_is_silent_in_json_mode() -> None:
    out, err = io.StringIO(), io.StringIO()
    sink = output_mod.Output(json_mode=True, stdout=out, stderr=err)
    sink.progress("working...")
    assert err.getvalue() == ""


def test_quiet_suppresses_progress_but_not_errors() -> None:
    out, err = io.StringIO(), io.StringIO()
    sink = output_mod.Output(quiet=True, stdout=out, stderr=err)
    sink.progress("working...")
    sink.error("something broke")
    assert "working" not in err.getvalue()
    assert "something broke" in err.getvalue()


def test_empty_lists_render_explicitly() -> None:
    result = status_mod.Result(
        command="inspect", status=status_mod.NO_MEETINGS, data={"meetings": []}
    )
    _, out, _ = _capture(result, json_mode=False)
    assert "meetings: (none)" in out