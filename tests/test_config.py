"""Configuration and env-file parsing tests.

All fixtures are synthetic. No real credential, address or meeting content
appears here - only invented values that exercise a parse rule.
"""

import sys
from pathlib import Path

TOOLKIT_ROOT = Path(__file__).resolve().parent.parent
if str(TOOLKIT_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOLKIT_ROOT))

from lib import config as config_mod  # noqa: E402


def _parse(text: str) -> dict[str, str]:
    parsed = config_mod.parse_env_text(text, origin="<test>")
    return {name: entry.value for name, entry in parsed.entries.items()}


def _expect_error(text: str, fragment: str) -> str:
    try:
        config_mod.parse_env_text(text, origin="<test>")
    except config_mod.ConfigError as exc:
        message = str(exc)
        assert fragment.lower() in message.lower(), (
            f"expected {fragment!r} in {message!r}"
        )
        return message
    raise AssertionError(f"expected a ConfigError mentioning {fragment!r}")


def test_blank_lines_and_comments() -> None:
    assert _parse("\n# just a comment\n\n   \nA=1\n") == {"A": "1"}


def test_whitespace_around_key_and_equals() -> None:
    assert _parse("   SPACED_KEY   =   value   ") == {"SPACED_KEY": "value"}
    assert _parse("\tTABBED\t=\tvalue") == {"TABBED": "value"}


def test_quoted_values() -> None:
    assert _parse('QS="quoted value"') == {"QS": "quoted value"}
    assert _parse("QSS='single quoted'") == {"QSS": "single quoted"}
    # Quotes stripped only when they wrap the whole value.
    assert _parse('PARTIAL="a" b') == {"PARTIAL": '"a" b'}


def test_escapes_in_double_quotes() -> None:
    assert _parse(r'ES="line\nbreak"') == {"ES": "line\nbreak"}
    assert _parse(r'ESC="a\"b"') == {"ESC": 'a"b'}


def test_empty_value_is_preserved() -> None:
    assert _parse("EMPTY=") == {"EMPTY": ""}
    assert _parse('EMPTYQ=""') == {"EMPTYQ": ""}


def test_export_prefix_is_accepted() -> None:
    assert _parse("export EXPORTED=yes") == {"EXPORTED": "yes"}


def test_duplicate_key_is_an_error_naming_itself() -> None:
    message = _expect_error("DUP=one\nDUP=two\n", "duplicate")
    assert "DUP" in message
    assert "lines 1 and 2" in message, "both line numbers must be named"

    # A duplicate further down the file must name the true first occurrence.
    later = _expect_error("A=1\nB=2\nC=3\nB=4\n", "duplicate")
    assert "'B'" in later
    assert "lines 2 and 4" in later


def test_missing_variable_error_names_the_variable() -> None:
    parsed = config_mod.parse_env_text("PRESENT=1\n", origin="<test>")
    try:
        parsed.require("ABSENT_KEY")
    except config_mod.ConfigError as exc:
        assert "ABSENT_KEY" in str(exc)
    else:
        raise AssertionError("expected a ConfigError for a missing variable")


def test_empty_required_variable_is_an_error() -> None:
    parsed = config_mod.parse_env_text("EMPTY_ONE=\n", origin="<test>")
    try:
        parsed.require("EMPTY_ONE")
    except config_mod.ConfigError as exc:
        assert "EMPTY_ONE" in str(exc)
    else:
        raise AssertionError("expected a ConfigError for an empty required variable")


def test_malformed_variable_names_are_refused() -> None:
    # Only genuinely malformed names: a leading digit, an embedded space, or a
    # quote. Dots and hyphens are accepted deliberately - this parser also reads
    # non-POSIX names, and refusing them caused real keys to be misparsed.
    for line in ("1BAD=value\n", "BAD NAME=value\n", 'BAD"NAME=value\n'):
        message = _expect_error(line, "invalid variable name")
        assert "line_number" not in message


def test_dots_and_hyphens_are_accepted_in_variable_names() -> None:
    assert _parse("dotted.name=1\nhyphen-name=2\n") == {
        "dotted.name": "1",
        "hyphen-name": "2",
    }


def test_errors_never_contain_values() -> None:
    secret = "super-secret-value-abcdef"
    message = _expect_error(f"GOOD_KEEY={secret}\nGOOD_KEEY=x\n", "duplicate")
    assert secret not in message

    line_error = _expect_error("NOT_AN_ASSIGNMENT\n", "KEY=VALUE")
    assert "NOT_AN_ASSIGNMENT" not in line_error, (
        "the offending line is withheld rather than echoed"
    )


def test_invalid_variable_name_is_rejected() -> None:
    _expect_error("1BAD=value\n", "invalid variable name")
    _expect_error("BAD NAME=value\n", "invalid variable name")


def test_inline_comment_stripping() -> None:
    assert _parse("A=value # trailing comment") == {"A": "value"}
    assert _parse("B=value#not-a-comment") == {"B": "value#not-a-comment"}
    assert _parse('C="kept # inside" # dropped') == {"C": "kept # inside"}


def test_secret_name_detection() -> None:
    assert config_mod.is_secret_name("CLIENT_SECRET")
    assert config_mod.is_secret_name("SUPABASE_SERVICE_ROLE_SECRET_KEY")
    assert config_mod.is_secret_name("DATABASE_URL")
    assert config_mod.is_secret_name("RENDER_API_KEY")
    assert not config_mod.is_secret_name("DIRECTORY_TENANT_ID")
    assert not config_mod.is_secret_name("MEETING_MAIL_FROM")


def test_describe_exposes_names_and_flags_only() -> None:
    parsed = config_mod.parse_env_text("MY_SECRET=abc123\nPLAIN=hello\n", origin="<test>")
    described = parsed.describe()
    assert set(described) == {"MY_SECRET", "PLAIN"}
    assert described["MY_SECRET"]["secret"] is True
    assert described["PLAIN"]["secret"] is False
    rendered = repr(described)
    assert "abc123" not in rendered
    assert "hello" not in rendered, "describe() must not carry values at all"


def test_missing_env_file_is_a_clear_error() -> None:
    try:
        config_mod.load_env_file("/nonexistent/kik-test.env")
    except config_mod.ConfigError as exc:
        assert "does not exist" in str(exc)
    else:
        raise AssertionError("expected a ConfigError for a missing env file")


def test_load_config_from_example() -> None:
    cfg = config_mod.load_config(TOOLKIT_ROOT / "config.example.toml")
    assert cfg.timezone == "Asia/Kolkata"
    assert cfg.runs_directory == Path("/root/kik-runs")
    assert cfg.mail.test_only is True
    assert cfg.mail.test_recipient == "yaswanth.ampolu@siliconpatterns.com"
    assert cfg.mail.sender == "vlsi_delivery@siliconpatterns.com"
    assert cfg.mail.secret_env_name == "CLIENT_SECRET"
    assert cfg.render.owner_id == "tea-dakgcrp594qs73e02bbg"
    assert cfg.render.service_id == "srv-dakksvfqj5pc73bi5j3g"
    assert cfg.app.frontend_url == "https://delivery-desk-frontend.vercel.app"


def test_invalid_toml_is_reported_not_raised_raw(tmp_path: Path | None = None) -> None:
    target = Path("/tmp/kik-test-bad-config.toml")
    target.write_text("this is not = = toml\n", encoding="utf-8")
    try:
        config_mod.load_config(target)
    except config_mod.ConfigError as exc:
        assert "TOML" in str(exc)
    else:
        raise AssertionError("expected a ConfigError for invalid TOML")
    finally:
        target.unlink(missing_ok=True)


def test_explicit_config_path_must_exist() -> None:
    try:
        config_mod.load_config("/nonexistent/kik-config.toml")
    except config_mod.ConfigError as exc:
        assert "does not exist" in str(exc)
    else:
        raise AssertionError("expected a ConfigError for a missing explicit config")