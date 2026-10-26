"""Artifact writing.

Artifacts hold real meeting content. Three properties are not negotiable:

- **containment** - nothing is written outside the configured artifacts root,
  including via `..`;
- **privacy** - owner-only permissions (0600 files, 0700 directories) where the
  filesystem supports them;
- **atomicity** - a reader never observes a half-written file, because the
  content is written to a temporary sibling and renamed into place.

Nothing here decides *what* to write; commands pass finished content in.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

FILE_MODE = 0o600
DIR_MODE = 0o700

TOOLKIT_VERSION = "0.1.0"


class ArtifactError(Exception):
    """A refused or failed artifact write."""


def ensure_directory(path: Path, *, root: Path) -> Path:
    resolved = _resolve_within(path, root)
    resolved.mkdir(parents=True, exist_ok=True)
    _chmod_quietly(resolved, DIR_MODE)
    return resolved


def run_directory(root: Path, local_date: str, reference: str) -> Path:
    """`<root>/<local-date>/<meeting-or-event-ref>`."""
    return root / local_date / reference


def write_json(path: Path, payload: Any, *, root: Path) -> Path:
    text = json.dumps(_jsonable(payload), indent=2, ensure_ascii=False, sort_keys=False)
    return write_text(path, text + "\n", root=root)


def write_text(path: Path, text: str, *, root: Path) -> Path:
    return write_bytes(path, text.encode("utf-8"), root=root)


def write_bytes(path: Path, payload: bytes, *, root: Path) -> Path:
    target = _resolve_within(path, root)
    target.parent.mkdir(parents=True, exist_ok=True)
    _chmod_quietly(target.parent, DIR_MODE)

    handle = None
    temp_name = None
    try:
        descriptor, temp_name = tempfile.mkstemp(
            prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent)
        )
        handle = os.fdopen(descriptor, "wb")
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
        handle.close()
        handle = None
        os.chmod(temp_name, FILE_MODE)
        os.replace(temp_name, target)
        temp_name = None
    except OSError as exc:
        raise ArtifactError(f"could not write {target}: {exc.strerror or exc}") from None
    finally:
        if handle is not None:
            handle.close()
        if temp_name is not None and os.path.exists(temp_name):
            try:
                os.unlink(temp_name)
            except OSError:
                pass

    _chmod_quietly(target, FILE_MODE)
    return target


def write_manifest(
    path: Path,
    *,
    command: str,
    status: str,
    produced: Iterable[str] | None = None,
    inputs: dict[str, Any] | None = None,
    root: Path,
    extra: dict[str, Any] | None = None,
) -> Path:
    payload: dict[str, Any] = {
        "command": command,
        "status": status,
        "generatedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "toolkitVersion": TOOLKIT_VERSION,
        "producedPaths": list(produced or []),
        "inputs": inputs or {},
    }
    if extra:
        payload.update(extra)
    return write_json(path, payload, root=root)


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ArtifactError(f"artifact does not exist: {path}") from None
    except json.JSONDecodeError as exc:
        raise ArtifactError(f"artifact is not valid JSON: {path} ({exc})") from None


def _chmod_quietly(path: Path, mode: int) -> None:
    try:
        os.chmod(path, mode)
    except OSError:
        # Some filesystems (FAT on removable media, certain container mounts)
        # refuse chmod. Containment still holds; only the mode is best-effort.
        pass


def _resolve_within(path: Path, root: Path) -> Path:
    if path is None:
        raise ArtifactError("artifact path is required")
    root_resolved = Path(os.path.abspath(str(root)))
    candidate = Path(path)
    absolute = candidate if candidate.is_absolute() else root_resolved / candidate
    normalised = Path(os.path.abspath(str(absolute)))

    if normalised != root_resolved and root_resolved not in normalised.parents:
        raise ArtifactError(
            f"refusing to write outside the artifacts root "
            f"({normalised.name} is not under {root_resolved.name})"
        )
    return normalised


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(v) for v in value]
    if isinstance(value, (datetime,)):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    return value


@dataclass(frozen=True)
class RunPaths:
    """The directory set one command run works in."""

    root: Path
    local_date: str
    reference: str

    def directory(self) -> Path:
        return run_directory(self.root, self.local_date, self.reference)

    def file(self, name: str) -> Path:
        return self.directory() / name

    def ensure(self) -> Path:
        return ensure_directory(self.directory(), root=self.root)