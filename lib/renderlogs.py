"""Render log retrieval.

The API is `GET https://api.render.com/v1/logs` with `ownerId`, one or more
`resource` values (service ids - **not** `resource[]`, which returns
`400 unknown resource type`), optional `startTime`/`endTime`/`text`/`level` and
`limit`. The response carries `logs`, `hasMore` and `nextStartTime`/
`nextEndTime`, which is what pagination walks.

Completeness is reported explicitly. A missing log line is evidence about the
log retention window and the filter used - not evidence that a webhook did not
arrive.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from typing import Any

from . import localdate, status as status_mod
from .config import Config
from .http_client import HttpClient, HttpError, TransportError
from .output import Output

LOGS_ENDPOINT = "https://api.render.com/v1/logs"
MAX_LIMIT = 100


@dataclass
class LogPage:
    entries: list[dict[str, Any]]
    has_more: bool
    next_start_time: str | None
    next_end_time: str | None


def run(
    config: Config,
    *,
    date_text: str,
    timezone_name: str | None,
    text: str | None,
    level: str | None,
    limit: int,
    max_pages: int,
    owner_id: str | None,
    service_id: str | None,
    runtime: bool,
    persist: bool,
    output: Output,
) -> status_mod.Result:
    tz_name = timezone_name or config.timezone
    try:
        day = localdate.local_day_from_text(date_text, tz_name)
    except localdate.DateError as exc:
        return status_mod.Result(
            command="logs",
            status=status_mod.USAGE_ERROR,
            data={"requested": {"date": date_text, "timezone": tz_name}},
            findings=[status_mod.Finding("parse-date", False, str(exc))],
        )

    owner = owner_id or config.render.owner_id
    service = service_id or config.render.service_id
    findings: list[status_mod.Finding] = []
    if not owner:
        findings.append(
            status_mod.Finding(
                "render-owner-id",
                False,
                "no Render owner id in config or --owner-id",
                remedy="set [render] owner_id in the config",
            )
        )
    if not service:
        findings.append(
            status_mod.Finding(
                "render-service-id",
                False,
                "no Render service id in config or --service-id",
                remedy="set [render] service_id in the config",
            )
        )
    if findings:
        return status_mod.Result(
            command="logs",
            status=status_mod.CONFIGURATION_ERROR,
            data={"requested": {"date": day.day.isoformat(), "timezone": tz_name}},
            findings=findings,
        )

    try:
        api_key = config.render_key()
    except Exception as exc:  # ConfigError
        return status_mod.Result(
            command="logs",
            status=status_mod.CONFIGURATION_ERROR,
            data={"requested": {"date": day.day.isoformat()}},
            findings=[
                status_mod.Finding(
                    "render-api-key",
                    False,
                    str(exc),
                    remedy="add RENDER_API_KEY to the env file",
                )
            ],
        )

    page_size = max(1, min(limit, MAX_LIMIT))
    http = HttpClient(timeout_seconds=45.0, max_attempts=3)
    headers = {"Authorization": f"Bearer {api_key}", "Accept": "application/json"}

    entries: list[dict[str, Any]] = []
    incomplete: list[str] = []
    warnings: list[str] = []
    pages = 0
    has_more = False
    next_end = day.utc_end_text
    next_start: str | None = None

    while pages < max_pages:
        params = {
            "ownerId": owner,
            "resource": service,
            "startTime": day.utc_start_text,
            "endTime": next_end,
            "limit": str(page_size),
            "direction": "backward",
        }
        if text:
            params["text"] = text
        if level:
            params["level"] = level
        if runtime:
            params["type"] = "app"
        # A cursor paginator re-requests the boundary page; drop the last entry
        # to avoid duplicating it, which is what the API's own examples do.
        if next_start is not None:
            params["startTime"] = next_start

        query = "&".join(f"{k}={_q(v)}" for k, v in params.items())
        url = f"{LOGS_ENDPOINT}?{query}"

        try:
            response = http.get(url, headers=headers)
        except HttpError as exc:
            category = status_mod.PROVIDER_ERROR
            if exc.status in (401, 403):
                category = status_mod.AUTHENTICATION_ERROR
            return status_mod.Result(
                command="logs",
                status=category,
                data={
                    "requested": {"date": day.day.from_dict() if False else day.day.isoformat()},
                    "source": {"endpoint": LOGS_ENDPOINT, "ownerId": owner, "serviceId": service},
                    "error": {"message": exc.message, "detail": exc.detail},
                },
                warnings=warnings,
                incomplete=incomplete,
                notes=[f"retrieved {len(entries)} entries before the failure"],
            )
        except TransportError as exc:
            return status_mod.Result(
                command="logs",
                status=status_mod.PROVIDER_ERROR,
                data={
                    "requested": {"date": day.day.isoformat(), "timezone": tz_name},
                    "error": {"message": exc.message},
                },
                warnings=warnings,
                incomplete=incomplete,
                notes=[f"retrieved {len(entries)} entries before the failure"],
            )

        pages += 1
        body = response.json() or {}
        batch = body.get("logs", []) or []
        if pages > 1 and batch:
            batch = batch[:-1] if _first_id(batch) == _last_seen(entries) else batch
        entries.extend(batch)
        has_more = bool(body.get("hasMore"))
        next_start = body.get("nextStartTime")
        next_end = body.get("nextEndTime") or day.utc_start_text

        if not has_more or not batch:
            break

    if has_more:
        incomplete.append(
            f"log pagination stopped at the --max-pages ceiling ({max_pages} pages); "
            "additional entries exist"
        )

    grouped = _group(entries)
    counts = {
        "retrieved": len(entries),
        "pages": pages,
        "distinctMessages": len(grouped),
        "byLevel": dict(sorted(Counter(_label(entry, "level") for entry in entries).items())),
        "byType": dict(sorted(Counter(_label(entry, "type") for entry in entries).items())),
    }

    data: dict[str, Any] = {
        "requested": {
            "date": day.day.isoformat(),
            "timezone": tz_name,
            "textFilter": text,
            "levelFilter": level,
            "pageSize": page_size,
            "maxPages": max_pages,
        },
        "interval": day.as_dict(),
        "source": {
            "endpoint": LOGS_ENDPOINT,
            "ownerId": owner,
            "serviceId": service,
        },
        "retrieval": {
            "complete": not incomplete,
            "pagesFetched": pages,
            "pageSize": page_size,
            "hasMoreAtStop": has_more,
            "entriesRetrieved": len(entries),
        },
        "counts": counts,
        "groups": grouped,
        "interpretation": {
            "absence": (
                "No matching log line means no matching line was returned for this "
                "owner, service, interval and filter. It is not proof that a "
                "webhook was not delivered, nor that processing did not happen."
            ),
            "retention": (
                "Render retains logs for a bounded window. A date older than the "
                "retention window returns an empty result rather than an error."
            ),
        },
    }

    status = status_mod.OK if entries else status_mod.NO_MEETINGS
    notes = []
    if not entries:
        notes.append(
            "No log entries matched. The request succeeded; the filter or the "
            "retention window explains an empty result."
        )

    if persist:
        persisted = _persist(config, day, data, status)
        if persisted:
            data["persisted"] = persisted

    return status_mod.Result(
        command="logs",
        status=status,
        data=data,
        warnings=warnings,
        notes=notes,
        incomplete=incomplete,
    )


def _q(value: str) -> str:
    from urllib.parse import quote

    return quote(str(value), safe="")


def _first_id(batch: list[dict[str, Any]]) -> str | None:
    return batch[0].get("id") if batch else None


def _last_seen(entries: list[dict[str, Any]]) -> str | None:
    return entries[-1].get("id") if entries else None


def _label(entry: dict[str, Any], name: str) -> str:
    for label in entry.get("labels") or []:
        if label.get("name") == name:
            return str(label.get("value"))
    return "(none)"


def _group(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group identical messages with occurrence counts, preserving first/last.

    Messages are ANSI-stripped before grouping: Render returns Nest's coloured
    output verbatim, and identical lines differing only in colour escape codes
    would otherwise appear as separate groups - the opposite of the point.
    """
    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    order: list[tuple[str, str]] = []
    for entry in entries:
        raw_message = str(entry.get("message", ""))
        message = strip_ansi(raw_message)
        level = _label(entry, "level")
        key = (level, message)
        if key not in grouped:
            grouped[key] = {
                "level": level,
                "message": message,
                "count": 0,
                "firstAt": entry.get("timestamp"),
                "lastAt": entry.get("timestamp"),
                "encoded": raw_message != message,
            }
            order.append(key)
        bucket = grouped[key]
        bucket["count"] += 1
        bucket["lastAt"] = entry.get("timestamp")

    out = [grouped[key] for key in order]
    out.sort(key=lambda item: (-item["count"], str(item["firstAt"])))
    return out


_ANSI_PATTERN = re.compile(r"\x1b\[[0-9;]*[A-Za-z]|\x1b\][^\x07]*\x07|\x1b[@-Z\\-_]")


def strip_ansi(text: str) -> str:
    """Remove terminal colour/control sequences from a log message."""
    return _ANSI_PATTERN.sub("", text).replace("\r", "")

def _persist(
    config: Config,
    day: localdate.LocalDay,
    data: dict[str, Any],
    status: str,
) -> dict[str, Any] | None:
    """Write the retrieved log evidence into a new run directory."""
    from . import runstore

    try:
        record = runstore.new_run(
            config.runs_directory, command="logs", local_date=day.day.isoformat()
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
    record.manifest.arguments = dict(data.get("requested") or {})
    record.manifest.sources = dict(data.get("source") or {})
    record.write_json("log-report.json", data, note="retrieved log evidence")
    record.manifest.completeness = {
        "complete": bool((data.get("retrieval") or {}).get("complete")),
        "retrieval": data.get("retrieval"),
    }
    record.seal()
    return {
        "runId": record.run_id,
        "directory": str(record.directory),
        "files": record.relative_files(),
        "status": status,
    }
