"""`doctor` - configuration diagnostics.

Local checks by default. `--network` adds reachability and authentication
checks. It never sends mail, at any level, under any flag.

Every check reports one of OK / FAIL / INFO. A FAIL is something an operator
must fix; INFO is a fact worth knowing that is not a problem.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

from . import fixtures as fixtures_mod
from . import localdate, status as status_mod, timezones
from .config import Config, ConfigError, is_secret_name
from .http_client import HttpClient, HttpError, TransportError
from .output import Output

GRAPH_HOST = "graph.microsoft.com"
TOKEN_HOST = "login.microsoftonline.com"
REQUIRED_ENV_NON_SECRET = ("DIRECTORY_TENANT_ID", "APPLICATION_CLIENT_ID")


def run(config: Config, *, network: bool, output: Output) -> status_mod.Result:
    findings: list[status_mod.Finding] = []
    warnings: list[str] = []
    info: dict[str, Any] = {}

    # -- 1. config file ----------------------------------------------------

    findings.append(
        status_mod.Finding(
            "config-file",
            True,
            f"loaded {config.path}",
        )
    )
    info["configuration"] = {
        "configFile": str(config.path),
        "envFile": str(config.env_file),
        "timezone": config.timezone,
        "runsDirectory": str(config.runs_directory),
    }

    # -- 2. artifact directory --------------------------------------------

    runs_ok, runs_detail = _check_runs_directory(config)
    findings.append(status_mod.Finding("runs-directory", runs_ok, runs_detail))

    # -- 3. env file -------------------------------------------------------

    findings.append(
        status_mod.Finding(
            "env-file",
            True,
            f"parsed {len(config.env.entries)} variables "
            f"({config.env.comments} comment lines)",
        )
    )

    # -- 4. timezone -------------------------------------------------------

    tz_diagnostics = timezones.diagnostics()
    info["timezoneDiagnostics"] = tz_diagnostics
    if not tz_diagnostics["tzdataAvailable"]:
        findings.append(
        status_mod.Finding(
            "tzdata",
            None,
            (
                "this host has no tz database; local-day calculations use the "
                "fixed-offset fallback, which is limited to zones with a constant "
                "year-round offset and warns on every use"
            ),
            remedy=(
                "install the tzdata content (or set PYTHONTZPATH to a zoneinfo "
                "tree) for DST-correct behaviour and any other timezone"
            ),
        )
    )

    try:
        day = localdate.local_day(localdate.parse_date("2026-10-20"), config.timezone)
        findings.append(
            status_mod.Finding(
                "timezone",
                True,
                f"{config.timezone} resolves from {day.timezone_source}; "
                f"2026-10-20 local is {day.utc_start_text} inclusive to "
                f"{day.utc_end_text} exclusive",
            )
        )
        if day.warning:
            warnings.append(day.warning)
        info["localDayExample"] = day.as_dict()
    except localdate.DateError as exc:
        findings.append(
            status_mod.Finding(
                "timezone",
                False,
                str(exc),
                remedy="set `timezone` to an IANA zone name such as Asia/Kolkata",
            )
        )

    # -- 5. Supabase configuration ----------------------------------------

    supabase_ok = True
    try:
        url = config.supabase_url()
        key_name = config.supabase_key_name()
        parsed = urlparse(url)
        if parsed.scheme != "https" or not parsed.hostname:
            supabase_ok = False
            findings.append(
                status_mod.Finding(
                    "supabase-url",
                    False,
                    f"Supabase URL is not an https origin (host {parsed.hostname or 'none'})",
                    remedy="check SUPABASE_URL in the env file",
                )
            )
        else:
            findings.append(
                status_mod.Finding(
                    "supabase-url",
                    True,
                    f"origin {parsed.scheme}://{parsed.hostname}",
                )
            )
    except ConfigError as exc:
        supabase_ok = False
        key_name = "(none)"
        findings.append(
            status_mod.Finding(
                "supabase-url",
                False,
                str(exc),
                remedy="set SUPABASE_URL or supabase.project_ref",
            )
        )

    if key_name == "(none)":
        supabase_ok = False
        findings.append(
            status_mod.Finding(
                "supabase-key",
                False,
                "no Supabase service key variable found",
                remedy=(
                    "set SUPABASE_SERVICE_ROLE_SECRET_KEY or SUPABASE_SECRET_KEY "
                    "in the env file"
                ),
            )
        )
    else:
        entry = config.env.entries.get(key_name)
        findings.append(
            status_mod.Finding(
                "supabase-key",
                True,
                f"using variable {key_name}"
                + (f" (value marked secret)" if entry and entry.is_secret else ""),
            )
        )
    info["supabase"] = {
        "urlVariableUsed": "SUPABASE_URL" if config.env.has("SUPABASE_URL") else "(fallback)",
        "keyVariableUsed": key_name,
        "configured": supabase_ok,
    }

    # -- 6. Render ---------------------------------------------------------

    render_present = config.env.has("RENDER_API_KEY")
    findings.append(
        status_mod.Finding(
            "render-api-key",
            True if render_present else None,
            "RENDER_API_KEY is present" if render_present else "RENDER_API_KEY is absent",
            remedy=None if render_present else "add RENDER_API_KEY to enable the logs command",
        )
    )
    findings.append(
        status_mod.Finding(
            "render-identifiers",
            True if (config.render.owner_id and config.render.service_id) else None,
            (
                f"owner {config.render.owner_id}, service {config.render.service_id}"
                if config.render.owner_id and config.render.service_id
                else "owner or service id not configured"
            ),
            remedy=(
                None
                if config.render.owner_id and config.render.service_id
                else "set [render] owner_id and service_id"
            ),
        )
    )
    info["render"] = {
        "ownerId": config.render.owner_id,
        "serviceId": config.render.service_id,
        "apiKeyPresent": render_present,
    }

    # -- 7. mail policy ----------------------------------------------------

    findings.append(
        status_mod.Finding(
            "mail-policy",
            True if config.mail.test_only else False,
            (
                f"test_only is ON: live mail is restricted to "
                f"{_mask_email(config.mail.test_recipient)}"
                if config.mail.test_only
                else "test_only is OFF: live mail would go to intended recipients"
            ),
            remedy=None
            if config.mail.test_only
            else "set mail.test_only = true until the workflow is verified end to end",
        )
    )
    sender, recipient = _normalize_email(config.mail.sender), _normalize_email(
        config.mail.test_recipient
    )
    if not _looks_like_email(sender):
        findings.append(
            status_mod.Finding("mail-sender", False, "mail.sender is not a valid address")
        )
    else:
        findings.append(status_mod.Finding("mail-sender", True, _mask_email(sender)))
    if not _looks_like_email(recipient):
        findings.append(
            status_mod.Finding(
                "mail-test-recipient",
                False,
                "mail.test_recipient is not a valid address",
            )
        )
    else:
        findings.append(
            status_mod.Finding("mail-test-recipient", True, _mask_email(recipient))
        )
    info["mail"] = {
        "sender": sender,
        "testOnly": config.mail.test_only,
        "testRecipient": recipient,
        "secretEnvName": config.mail.secret_env_name,
        "tenantEnvName": config.mail.tenant_env_name,
        "clientIdEnvName": config.mail.client_id_env_name,
    }

    # -- 8. app link -------------------------------------------------------

    if config.app.frontend_url:
        parsed = urlparse(config.app.frontend_url)
        scheme_ok = parsed.scheme in ("http", "https")
        findings.append(
            status_mod.Finding(
                "frontend-url",
                scheme_ok,
                f"{config.app.frontend_url}"
                if scheme_ok
                else "frontend_url has an unsupported scheme",
                remedy=None if scheme_ok else "use an http(s) origin",
            )
        )
    else:
        findings.append(
            status_mod.Finding(
                "frontend-url",
                None,
                "no frontend_url configured; the Open Delivery Desk button will be omitted",
                remedy="set [app] frontend_url only once it is verified",
            )
        )

    # -- 9. Graph credential names ----------------------------------------

    for label, name in (
        ("graph-tenant-variable", config.mail.tenant_env_name),
        ("graph-client-id-variable", config.mail.client_id_env_name),
        ("graph-secret-variable", config.mail.secret_env_name),
    ):
        present = config.env.has(name)
        empty = present and config.env.get(name) == ""
        findings.append(
            status_mod.Finding(
                label,
                True if present and not empty else False,
                f"{name} is set" if present and not empty else f"{name} is not usable",
                remedy=None if present and not empty else f"set {name} in the env file",
            )
        )
    configured_secret = config.mail.secret_env_name
    other_secret_vars = [
        name
        for name in config.env.names()
        if name != configured_secret and is_secret_name(name)
    ]
    findings.append(
        status_mod.Finding(
            "graph-secret-selection",
            True,
            (
                f"exactly one secret variable is selected explicitly: {configured_secret}. "
                "Alternatives are not probed."
            ),
        )
    )
    if "CLIENT_SECRET_VALUE" in other_secret_vars and configured_secret != "CLIENT_SECRET_VALUE":
        findings.append(
            status_mod.Finding(
                "graph-secret-history",
                None,
                "CLIENT_SECRET_VALUE is present but not selected (it previously "
                "returned AADSTS7000215); CLIENT_SECRET is the verified working name",
            )
        )

    # -- 10. fixture registry ---------------------------------------------

    registry = fixtures_mod.load_registry()
    findings.append(
        status_mod.Finding(
            "fixture-registry",
            True if registry.loaded_from else None,
            (
                f"{len(registry.raw_text_sha256)} raw-text and "
                f"{len(registry.stored_content_hash)} stored-hash fingerprints "
                f"registered ({registry.loaded_from})"
                if registry.loaded_from
                else "no fixture registry found; nothing will be classified as a known fixture"
            ),
        )
    )

    # -- 11. secret hygiene ------------------------------------------------

    info["secretHygiene"] = {
        "secretVariablesPresent": sorted(
            name for name in config.env.names() if is_secret_name(name)
        ),
        "note": (
            "Names only. No value, prefix, suffix or length is ever emitted by "
            "this toolkit."
        ),
    }

    # -- 12. optional network checks --------------------------------------

    if network:
        findings.extend(_network_checks(config, output))
    else:
        findings.append(
            status_mod.Finding(
                "network-checks",
                None,
                "skipped; pass --network to verify reachability and authentication "
                "(no mail is ever sent)",
            )
        )

    hard_failures = [f for f in findings if f.ok is False]
    status = status_mod.CONFIGURATION_ERROR if hard_failures else status_mod.OK
    if not hard_failures and any(f.ok is None for f in findings):
        status = status_mod.OK

    data: dict[str, Any] = {
        "checkedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "networkChecks": network,
        "config": info,
        "counts": {
            "checks": len(findings),
            "ok": sum(1 for f in findings if f.ok is True),
            "fail": len(hard_failures),
            "info": sum(1 for f in findings if f.ok is None),
        },
    }

    return status_mod.Result(
        command="doctor",
        status=status,
        data=data,
        findings=findings,
        warnings=warnings,
    )


def _check_runs_directory(config: Config) -> tuple[bool | None, str]:
    path = config.runs_directory
    inside_git = _inside_git_worktree(path)
    exists = path.exists()
    detail = f"{path}"
    if inside_git:
        return (
            False,
            f"{detail} is inside a Git worktree; meeting content must not be committable",
        )
    if "Process-desk" in str(path):
        return (
            False,
            f"{detail} points inside the production application repository; "
            "choose a path outside a git clone",
        )
    if _inside_toolkit(path):
        return (
            False,
            f"{detail} is inside the toolkit directory; generated artifacts must "
            "live outside the code tree",
        )
    if not exists:
        return (False, f"{detail} does not exist yet")
    if not path.is_dir():
        return (False, f"{detail} is not a directory")
    mode = oct(path.stat().st_mode & 0o777)
    return (True, f"{detail} exists (mode {mode})")


def _inside_git_worktree(path) -> bool:
    candidate = path
    if not candidate.is_absolute():
        candidate = path.resolve()
    for parent in [candidate, *candidate.parents]:
        if (parent / ".git").exists():
            return True
    return False


def _inside_toolkit(path) -> bool:
    """True when `path` is the toolkit root itself or sits beneath it."""
    try:
        toolkit_root = __file__.rsplit("/lib/", 1)[0]
        resolved = path.resolve() if path.exists() else path
        root = __import__("pathlib").Path(toolkit_root).resolve()
        resolved = __import__("pathlib").Path(resolved)
        return resolved == root or root in resolved.parents
    except Exception:  # noqa: BLE001 - a check must never crash doctor
        return False


def _network_checks(config: Config, output: Output) -> list[status_mod.Finding]:
    findings: list[status_mod.Finding] = []
    http = HttpClient(timeout_seconds=20.0, max_attempts=2)

    # Supabase reachability + schema visibility.
    try:
        url = config.supabase_url()
        key = config.supabase_key()
        output.progress("checking Supabase connectivity")
        response = http.get(
            f"{url}/rest/v1/",
            headers={
                "apikey": key,
                "Authorization": f"Bearer {key}",
                "Accept": "application/json",
            },
        )
        document = response.json() or {}
        tables = sorted(document.get("definitions", {}).keys())
        meeting_tables = [t for t in tables if t.startswith("meeting") or t in ("users", "speaker_aliases", "transcript_segments")]
        findings.append(
            status_mod.Finding(
                "supabase-reachability",
                True,
                f"{response.status} from {urlparse(url).hostname}; "
                f"{len(tables)} tables exposed, {len(meeting_tables)} relevant",
            )
        )
    except HttpError as exc:
        findings.append(
            status_mod.Finding(
                "supabase-reachability",
                False,
                f"{exc.message}"
                + (f" ({exc.detail})" if exc.detail else ""),
                remedy="verify the service key and that the project is running",
            )
        )
    except TransportError as exc:
        findings.append(
            status_mod.Finding(
                "supabase-reachability",
                False,
                exc.message,
                remedy="verify network egress from this host",
            )
        )
    except ConfigError as exc:
        findings.append(status_mod.Finding("supabase-reachability", False, str(exc)))

    # Render reachability.
    if config.env.has("RENDER_API_KEY"):
        try:
            output.progress("checking Render API connectivity")
            response = http.get(
                "https://api.render.com/v1/owners?limit=1",
                headers={
                    "Authorization": f"Bearer {config.render_key()}",
                    "Accept": "application/json",
                },
            )
            owners = response.json() or []
            found = len(owners)
            findings.append(
                status_mod.Finding(
                    "render-reachability",
                    True,
                    f"{response.status}; {found} owner visible",
                )
            )
        except HttpError as exc:
            category_ok = exc.status not in (401, 403)
            findings.append(
                status_mod.Finding(
                    "render-reachability",
                    False,
                    f"{exc.message}" + (f" ({exc.detail})" if exc.detail else ""),
                    remedy="check RENDER_API_KEY scope" if not category_ok else "retry later",
                )
            )
        except TransportError as exc:
            findings.append(status_mod.Finding("render-reachability", False, exc.message))
    else:
        findings.append(
            status_mod.Finding(
                "render-reachability",
                None,
                "skipped; RENDER_API_KEY is not configured",
            )
        )

    # Graph token acquisition. This obtains a token and inspects nothing else.
    try:
        output.progress("checking Microsoft Graph token acquisition")
        token_response = http.post(
            f"https://{TOKEN_HOST}/{config.graph_tenant()}/oauth2/v2.0/token",
            data=_form(
                {
                    "client_id": config.graph_client_id(),
                    "client_secret": config.mail_secret(),
                    "scope": "https://graph.microsoft.com/.default",
                    "grant_type": "client_credentials",
                }
            ).encode("utf-8"),
            content_type="application/x-www-form-urlencoded",
            retry=False,
        )
        payload = token_response.json() or {}
        token = payload.get("access_token")
        if token:
            scopes = _scopes_claim(payload)
            findings.append(
                status_mod.Finding(
                    "graph-authentication",
                    True,
                    f"{token_response.status}; token obtained "
                    f"({payload.get('expires_in')}s lifetime). "
                    f"Scopes reported: {scopes or '(not reported)'}. "
                    "The token itself is never printed.",
                )
            )
        else:
            findings.append(
                status_mod.Finding(
                    "graph-authentication",
                    False,
                    "token endpoint returned no access_token",
                )
            )
    except HttpError as exc:
        findings.append(
            status_mod.Finding(
                "graph-authentication",
                False,
                f"{exc.message}" + (f" ({exc.detail})" if exc.detail else ""),
                remedy=(
                    "AADSTS7000215 means the wrong secret variable was used; the "
                    "verified working one is CLIENT_SECRET"
                    if exc.detail and "AADSTS7000215" in exc.detail
                    else "verify tenant id, application id and secret variable"
                ),
            )
        )
    except TransportError as exc:
        findings.append(
            status_mod.Finding(
                "graph-authentication",
                False,
                exc.message,
                remedy="verify outbound HTTPS to login.microsoftonline.com",
            )
        )
    except ConfigError as exc:
        findings.append(status_mod.Finding("graph-authentication", False, str(exc)))

    findings.append(
        status_mod.Finding(
            "graph-send-capability",
            None,
            "not tested by doctor. Sending is only attempted by send-review with "
            "explicit approval, and is never performed to discover permissions.",
        )
    )
    return findings


def _form(values: dict[str, str]) -> str:
    from urllib.parse import urlencode

    return urlencode(values)


def _scopes_claim(payload: dict[str, Any]) -> str | None:
    roles = payload.get("roles")
    if isinstance(roles, list) and roles:
        return ",".join(str(r) for r in roles)
    scope = payload.get("scope")
    return str(scope) if scope else None


def _normalize_email(value: str) -> str:
    return value.strip().lower()


def _looks_like_email(value: str) -> bool:
    if value.count("@") != 1:
        return False
    local, _, domain = value.partition("@")
    return bool(local) and "." in domain and " " not in value


def _mask_email(value: str) -> str:
    """Show the address's shape without publishing a colleague's full address
    in logs. Domain is kept because it is the part that matters for policy."""
    local, _, domain = value.partition("@")
    if not domain:
        return "(invalid address)"
    if len(local) <= 2:
        masked = local[0] + "*" * max(0, len(local) - 1)
    else:
        masked = local[0] + "*" * (len(local) - 2) + local[-1]
    return f"{masked}@{domain}"