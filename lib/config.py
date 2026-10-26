"""Configuration loading for the Kik toolkit.

Two files are involved and they are deliberately different things:

- the **config** file (TOML, non-secret) says *where* the environment file is,
  which mail sender to use, and where artifacts go;
- the **env** file (KEY=VALUE, secret) holds credentials.

The env file is parsed, never sourced or executed. Only variable *names* may
appear in errors; values are never printed, logged, hashed or truncated here.
"""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_CONFIG_PATH = Path("/root/Kik/config.toml")
EXAMPLE_CONFIG_PATH = Path("/root/Kik/config.example.toml")

#: Names that must never reach stdout/stderr, even as a prefix or length.
SECRET_NAME_PATTERN = re.compile(
    r"(SECRET|TOKEN|KEY|PASSWORD|PASSWD|CREDENTIAL|CONNECTION|DATABASE_URL|REDIS_URL)",
    re.IGNORECASE,
)

_KEY_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]*$")


class ConfigError(Exception):
    """A configuration problem the operator can fix. Never contains values."""


def is_secret_name(name: str) -> bool:
    return bool(SECRET_NAME_PATTERN.search(name))


@dataclass(frozen=True)
class EnvEntry:
    name: str
    value: str
    line_number: int

    @property
    def is_secret(self) -> bool:
        return is_secret_name(self.name)


@dataclass
class EnvFile:
    """Parsed env file. Values live in memory only."""

    path: Path
    entries: dict[str, EnvEntry] = field(default_factory=dict)
    comments: int = 0

    def get(self, name: str) -> str | None:
        entry = self.entries.get(name)
        return entry.value if entry else None

    def require(self, name: str) -> str:
        value = self.get(name)
        if value is None:
            raise ConfigError(f"required variable is not set: {name}")
        if value == "":
            raise ConfigError(f"required variable is set but empty: {name}")
        return value

    def has(self, name: str) -> bool:
        return name in self.entries

    def names(self) -> list[str]:
        return sorted(self.entries)

    def describe(self) -> dict[str, dict[str, object]]:
        """Names, presence and secret-ness only. No values."""
        return {
            name: {
                "present": True,
                "empty": entry.value == "",
                "secret": entry.is_secret,
                "line": entry.line_number,
            }
            for name, entry in sorted(self.entries.items())
        }


def _strip_inline_comment(value: str) -> str:
    """Remove an unquoted trailing `# comment`.

    Only strips when the `#` is preceded by whitespace, so a value such as
    `abc#def` survives intact.
    """
    out = []
    quote: str | None = None
    prev = ""
    for ch in value:
        if quote:
            out.append(ch)
            if ch == quote:
                quote = None
        elif ch in ("'", '"'):
            quote = ch
            out.append(ch)
        elif ch == "#" and (prev == "" or prev.isspace()):
            break
        else:
            out.append(ch)
        prev = ch
    return "".join(out)


def _unquote(raw: str) -> str:
    value = raw.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        inner = value[1:-1]
        if value[0] == '"':
            inner = inner.replace('\\"', '"').replace("\\n", "\n").replace("\\t", "\t")
            inner = inner.replace("\\\\", "\\")
        return inner
    return value


def parse_env_text(text: str, *, origin: str = "<text>") -> EnvFile:
    """Parse an env file body.

    Supports blank lines, `#` comments, whitespace around key and `=`, optional
    `export `, single/double quoted values, and optional inline comments.
    Duplicate keys are a hard error that names the key and both line numbers -
    silently taking the last one would let a stale credential win.
    """
    path = Path(origin)
    result = EnvFile(path=path)

    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("#"):
            result.comments += 1
            continue

        body = line
        if body.startswith("export ") or body.startswith("export\t"):
            body = body[len("export "):].lstrip()

        if "=" not in body:
            raise ConfigError(
                f"{origin}:{line_number}: line is not a KEY=VALUE assignment "
                f"(key name withheld)"
            )

        raw_key, raw_value = body.split("=", 1)
        key = raw_key.strip()
        if not key:
            raise ConfigError(f"{origin}:{line_number}: empty variable name")
        # The key pattern intentionally allows dots and hyphens: this file is
        # also used for non-POSIX variable names, and a strict shell identifier
        # rule would reject names the project legitimately uses. Only genuinely
        # malformed names (starting with a digit, containing spaces, quotes or
        # `=`) are refused.
        if not re.match(r"^[A-Za-z_][A-Za-z0-9_.\-]*$", key):
            raise ConfigError(
                f"{origin}:{line_number}: invalid variable name {key!r} "
                f"(must start with a letter or underscore)"
            )

        value = _unquote(_strip_inline_comment(raw_value))

        existing = result.entries.get(key)
        if existing is not None:
            raise ConfigError(
                f"duplicate variable {key!r} in {origin} at lines "
                f"{existing.line_number} and {line_number}"
            )

        result.entries[key] = EnvEntry(name=key, value=value, line_number=line_number)

    return result


def load_env_file(path: str | os.PathLike[str]) -> EnvFile:
    env_path = Path(path).expanduser()
    if not env_path.exists():
        raise ConfigError(f"env file does not exist: {env_path}")
    if not env_path.is_file():
        raise ConfigError(f"env path is not a regular file: {env_path}")
    try:
        text = env_path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise ConfigError(f"env file is not valid UTF-8: {env_path}") from exc
    return parse_env_text(text, origin=str(env_path))


@dataclass
class SupabaseConfig:
    project_ref: str | None = None
    #: Fallback used only when the env file has neither URL variable.
    fallback_url: str | None = None


@dataclass
class RenderConfig:
    owner_id: str | None = None
    service_id: str | None = None


@dataclass
class MailConfig:
    sender: str = "vlsi_delivery@siliconpatterns.com"
    test_only: bool = True
    test_recipient: str = "yaswanth.ampolu@siliconpatterns.com"
    #: Which env variable holds the client secret value. Explicit on purpose:
    #: probing several variables would silently accept a wrong credential.
    secret_env_name: str = "CLIENT_SECRET"
    tenant_env_name: str = "DIRECTORY_TENANT_ID"
    client_id_env_name: str = "APPLICATION_CLIENT_ID"
    #: Addresses that may never receive an attendee-digest email. The service
    #: account is listed here on purpose: it is an attendee of the meetings it
    #: records, and it must never be a recipient. An empty list is treated as a
    #: configuration error by `deliver-attendees`, not as "no exclusions".
    excluded_recipients: list[str] = field(default_factory=lambda: [
        "vlsi_delivery@siliconpatterns.com",
        "teams@maestrolabs.com",
    ])


@dataclass
class AppLinks:
    frontend_url: str | None = None


@dataclass
class Config:
    path: Path
    env_file: Path
    timezone: str
    runs_directory: Path
    supabase: SupabaseConfig
    render: RenderConfig
    mail: MailConfig
    app: AppLinks
    env: EnvFile

    # -- derived views -----------------------------------------------------

    def supabase_url(self) -> str:
        for name in ("SUPABASE_URL", "NEXT_PUBLIC_SUPABASE_URL"):
            value = self.env.get(name)
            if value:
                return value.rstrip("/")
        if self.supabase.fallback_url:
            return self.supabase.fallback_url.rstrip("/")
        if self.supabase.project_ref:
            return f"https://{self.supabase.project_ref}.supabase.co"
        raise ConfigError(
            "no Supabase URL available: set SUPABASE_URL in the env file or "
            "supabase.project_ref in the config"
        )

    def supabase_key(self) -> str:
        """Service-role key. Preference order is explicit, not a probe."""
        for name in (
            "SUPABASE_SERVICE_ROLE_SECRET_KEY",
            "SUPABASE_SECRET_KEY",
            "SUPABASE_SERVICE_ROLE_KEY",
        ):
            value = self.env.get(name)
            if value:
                return value
        raise ConfigError(
            "no Supabase service key found; looked for "
            "SUPABASE_SERVICE_ROLE_SECRET_KEY, SUPABASE_SECRET_KEY, "
            "SUPABASE_SERVICE_ROLE_KEY"
        )

    def supabase_key_name(self) -> str:
        for name in (
            "SUPABASE_SERVICE_ROLE_SECRET_KEY",
            "SUPABASE_SECRET_KEY",
            "SUPABASE_SERVICE_ROLE_KEY",
        ):
            if self.env.get(name):
                return name
        return "(none)"

    def render_key(self) -> str:
        return self.env.require("RENDER_API_KEY")

    def mail_secret(self) -> str:
        return self.env.require(self.mail.secret_env_name)

    def graph_tenant(self) -> str:
        return self.env.require(self.mail.tenant_env_name)

    def graph_client_id(self) -> str:
        return self.env.require(self.mail.client_id_env_name)

    def runs_path(self) -> Path:
        return self.runs_directory

    def source_paths(self) -> dict[str, Path]:
        return {"frontend_url": Path("")} if False else {}


DEFAULTS = {
    "env_file": "/root/.env",
    "timezone": "Asia/Kolkata",
    "runs_directory": "/root/kik-runs",
}


def _as_str(value: object, *, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"config field {field_name} must be a non-empty string")
    return value.strip()


def _as_str_list(section: dict[str, object], key: str) -> list[str]:
    """Validate an optional TOML array-of-strings mail setting.

    The list may be present but empty; emptiness is a policy decision made by
    the command that consumes it, not an error here.
    """
    raw = section.get(key)
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ConfigError(f"config field {key} must be an array of email addresses")
    out: list[str] = []
    for item in raw:
        if not isinstance(item, str) or not item.strip():
            raise ConfigError(f"config field {key} must contain non-empty strings")
        out.append(item.strip())
    return out


def load_config(path: str | os.PathLike[str] | None = None) -> Config:
    config_path = Path(path).expanduser() if path else DEFAULT_CONFIG_PATH
    data: dict[str, object] = {}

    if config_path.exists():
        if not config_path.is_file():
            raise ConfigError(f"config path is not a regular file: {config_path}")
        try:
            with config_path.open("rb") as handle:
                data = tomllib.load(handle)
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError(f"config file is not valid TOML: {config_path} ({exc})")
    elif path is not None:
        raise ConfigError(f"config file does not exist: {config_path}")

    env_file = Path(
        _as_str(data.get("env_file", DEFAULTS["env_file"]), field_name="env_file")
    ).expanduser()
    timezone = _as_str(data.get("timezone", DEFAULTS["timezone"]), field_name="timezone")
    runs_directory = Path(
        _as_str(
            data.get("runs_directory", DEFAULTS["runs_directory"]),
            field_name="runs_directory",
        )
    ).expanduser()

    supabase_raw = data.get("supabase") or {}
    render_raw = data.get("render") or {}
    mail_raw = data.get("mail") or {}
    app_raw = data.get("app") or {}
    if not isinstance(supabase_raw, dict) or not isinstance(render_raw, dict):
        raise ConfigError("config sections [supabase] and [render] must be tables")
    if not isinstance(mail_raw, dict) or not isinstance(app_raw, dict):
        raise ConfigError("config sections [mail] and [app] must be tables")

    def opt_str(section: dict, key: str) -> str | None:
        value = section.get(key)
        if value is None:
            return None
        return _as_str(value, field_name=f"{key}")

    test_only = mail_raw.get("test_only", True)
    if not isinstance(test_only, bool):
        raise ConfigError("mail.test_only must be a boolean")

    config = Config(
        path=config_path,
        env_file=env_file,
        timezone=timezone,
        runs_directory=runs_directory,
        supabase=SupabaseConfig(
            project_ref=opt_str(supabase_raw, "project_ref"),
            fallback_url=opt_str(supabase_raw, "fallback_url"),
        ),
        render=RenderConfig(
            owner_id=opt_str(render_raw, "owner_id"),
            service_id=opt_str(render_raw, "service_id"),
        ),
        mail=MailConfig(
            sender=opt_str(mail_raw, "sender") or MailConfig.sender,
            test_only=test_only,
            test_recipient=opt_str(mail_raw, "test_recipient")
            or MailConfig.test_recipient,
            secret_env_name=opt_str(mail_raw, "secret_env_name")
            or MailConfig.secret_env_name,
            tenant_env_name=opt_str(mail_raw, "tenant_env_name")
            or MailConfig.tenant_env_name,
            client_id_env_name=opt_str(mail_raw, "client_id_env_name")
            or MailConfig.client_id_env_name,
            excluded_recipients=_as_str_list(mail_raw, "excluded_recipients")
            if "excluded_recipients" in mail_raw else MailConfig().excluded_recipients,
        ),
        app=AppLinks(frontend_url=opt_str(app_raw, "frontend_url")),
        env=EnvFile(path=env_file),
    )
    return config


def attach_env(config: Config) -> Config:
    """Load the env file named by the config. Separate step so `doctor` can
    report a missing env file as a finding rather than a traceback."""
    config.env = load_env_file(config.env_file)
    return config


def ensure_runs_directory(config: Config) -> Path:
    path = config.runs_directory
    path.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass
    return path


def template_config_text() -> str:
    return EXAMPLE_CONFIG_PATH.read_text(encoding="utf-8") if EXAMPLE_CONFIG_PATH.exists() else ""