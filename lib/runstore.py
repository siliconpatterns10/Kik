"""Run directories, manifests and content hashing for reused evidence.

Every command that retrieves evidence writes it under

    <runs_directory>/<local-date>/<run-id>/

where `<run-id>` is a sortable timestamp plus a short random suffix. A run is
never overwritten: `new_run` fails loudly if the directory already exists, and a
collision is an error rather than a silent merge.

The manifest is the index a later command reads. It records what was retrieved,
from where, with which hashes, and whether retrieval was complete - so an agent
can reuse evidence offline instead of querying again.

Credentials and tokens never enter a manifest. `lib.output` redaction is applied
defensively on top of the caller's own discipline.
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import re
import string
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from . import artifacts
from . import output as output_mod

SCHEMA_VERSION = 1
TOOLKIT_VERSION = "0.1.0"

_RUN_ID_PATTERN = re.compile(r"^[0-9]{8}T[0-9]{6}Z-[a-z0-9]{6}$")


class RunError(Exception):
    """A refused or failed run-directory operation."""


def new_run_id(now: datetime | None = None) -> str:
    """Sortable UTC timestamp plus a short random suffix."""
    moment = now or datetime.now(timezone.utc)
    stamp = moment.strftime("%Y%m%dT%H%M%SZ")
    suffix = "".join(random.choice(string.ascii_lowercase + string.digits) for _ in range(6))
    return f"{stamp}-{suffix}"


@dataclass
class RunManifest:
    run_id: str
    command: str
    local_date: str
    created_at: str
    schema_version: int = SCHEMA_VERSION
    toolkit_version: str = TOOLKIT_VERSION
    timezone: str | None = None
    timezone_source: str | None = None
    utc_interval: dict[str, str] | None = None
    local_interval: dict[str, str] | None = None
    arguments: dict[str, Any] = field(default_factory=dict)
    sources: dict[str, Any] = field(default_factory=dict)
    produced: list[dict[str, Any]] = field(default_factory=list)
    completeness: dict[str, Any] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "runId": self.run_id,
            "command": self.command,
            "localDate": self.local_date,
            "createdAt": self.created_at,
            "schemaVersion": self.schema_version,
            "toolkitVersion": self.toolkit_version,
            "timezone": self.timezone,
            "timezoneSource": self.timezone_source,
            "utcInterval": self.utc_interval,
            "localInterval": self.local_interval,
            "arguments": self.arguments,
            "sources": self.sources,
            "produced": self.produced,
            "completeness": self.completeness,
            "errors": self.errors,
            "warnings": self.warnings,
            "notes": self.notes,
        }


class Run:
    """A single artifact directory, built up and then sealed with a manifest."""

    def __init__(self, root: Path, run_id: str, local_date: str, command: str) -> None:
        self.root = Path(root)
        self.run_id = run_id
        self.local_date = local_date
        self.command = command
        self.directory = artifacts.ensure_directory(
            artifacts.run_directory(self.root, local_date, run_id), root=self.root
        )
        self.manifest = RunManifest(
            run_id=run_id,
            command=command,
            local_date=local_date,
            created_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        )

    # -- writing -----------------------------------------------------------

    def write_json(self, name: str, payload: Any, *, note: str | None = None) -> str:
        path = self.directory / name
        artifacts.write_json(path, payload, root=self.root)
        self._record(name, path, note=note)
        return name

    def write_text(self, name: str, text: str, *, note: str | None = None) -> str:
        path = self.directory / name
        artifacts.write_text(path, text, root=self.root)
        self._record(name, path, note=note)
        return name

    def write_bytes(self, name: str, payload: bytes, *, note: str | None = None) -> str:
        path = self.directory / name
        artifacts.write_bytes(path, payload, root=self.root)
        self._record(name, path, note=note)
        return name

    def _record(self, name: str, path: Path, *, note: str | None = None) -> None:
        self.manifest.produced.append(
            {
                "name": name,
                "sha256": sha256_file(path),
                "bytes": path.stat().st_size,
                "note": note,
            }
        )

    # -- sealing -----------------------------------------------------------

    def seal(self) -> dict[str, Any]:
        """Write the manifest last, so a partial run is visible as one."""
        payload = _redact(self.manifest.as_dict())
        artifacts.write_json(self.directory / "manifest.json", payload, root=self.root)
        return payload

    def relative_files(self) -> list[str]:
        return [item["name"] for item in self.manifest.produced]


def new_run(root: Path, *, command: str, local_date: str, run_id: str | None = None) -> Run:
    chosen = run_id or new_run_id()
    if not _RUN_ID_PATTERN.match(chosen):
        raise RunError(
            f"invalid run id {chosen!r}: expected YYYYMMDDTHHMMSSZ-xxxxxx"
        )
    target = artifacts.run_directory(Path(root), local_date, chosen)
    if target.exists():
        raise RunError(
            f"run directory already exists and will not be overwritten: {target}"
        )
    return Run(Path(root), chosen, local_date, command)


def open_run(root: Path, local_date: str, run_id: str) -> Run:
    """Open an existing run for reading. Never creates one."""
    directory = artifacts.run_directory(Path(root), local_date, run_id)
    if not directory.is_dir():
        raise RunError(f"run does not exist: {directory}")
    run = Run.__new__(Run)
    run.root = Path(root)
    run.run_id = run_id
    run.local_date = local_date
    run.command = "reopen"
    run.directory = directory
    run.manifest = RunManifest(
        run_id=run_id, command="reopen", local_date=local_date, created_at=""
    )
    return run


def read_manifest(path: Path) -> dict[str, Any]:
    payload = artifacts.read_json(path)
    if not isinstance(payload, dict):
        raise RunError(f"manifest is not an object: {path}")
    if "schemaVersion" not in payload:
        raise RunError(f"manifest has no schemaVersion: {path}")
    return payload


# -- hashing -------------------------------------------------------------


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_text(text: str) -> str:
    """sha256 over exact UTF-8 bytes. No normalisation, by design."""
    return sha256_bytes(text.encode("utf-8"))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


# -- redaction -----------------------------------------------------------


def _redact(value: Any) -> Any:
    if isinstance(value, str):
        return output_mod.redact(value)
    if isinstance(value, dict):
        return {k: _redact(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_redact(v) for v in value]
    return value


# -- discovery -----------------------------------------------------------


@dataclass
class DiscoveredRun:
    local_date: str
    run_id: str
    directory: Path
    command: str | None
    created_at: str | None
    manifest_path: Path | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "localDate": self.local_date,
            "runId": self.run_id,
            "command": self.command,
            "createdAt": self.created_at,
            "directory": str(self.directory),
            "hasManifest": self.manifest_path is not None,
        }


def discover_runs(root: Path, *, local_date: str | None = None) -> list[DiscoveredRun]:
    """List runs newest-first. Reads the manifest only for its headers."""
    base = Path(root)
    if not base.is_dir():
        return []
    dates = [local_date] if local_date else sorted(
        (d.name for d in base.iterdir() if d.is_dir()), reverse=True
    )
    found: list[DiscoveredRun] = []
    for date_name in dates:
        date_dir = base / date_name
        if not date_dir.is_dir():
            continue
        for run_dir in sorted((d for d in date_dir.iterdir() if d.is_dir()), reverse=True):
            manifest_path = run_dir / "manifest.json"
            command = None
            created_at = None
            has_manifest = manifest_path.is_file()
            if has_manifest:
                try:
                    payload = artifacts.read_json(manifest_path)
                    command = payload.get("command")
                    created_at = payload.get("createdAt")
                except Exception:  # noqa: BLE001 - a broken manifest is still a run
                    command = "(unreadable manifest)"
            found.append(
                DiscoveredRun(
                    local_date=date_name,
                    run_id=run_dir.name,
                    directory=run_dir,
                    command=command,
                    created_at=created_at,
                    manifest_path=manifest_path if has_manifest else None,
                )
            )
    return found


def find_artifact(run_directory: Path, name: str, *, root: Path) -> Path:
    """Resolve a manifest-listed artifact, refusing to escape the run."""
    target = (Path(run_directory) / name).resolve()
    base = Path(run_directory).resolve()
    if base != target and base not in target.parents:
        raise RunError(f"artifact name escapes its run directory: {name}")
    if not target.is_file():
        raise RunError(f"artifact listed in the manifest is missing: {name}")
    return target


def summarise_produced(items: Iterable[dict[str, Any]]) -> list[str]:
    return [str(item.get("name")) for item in items]