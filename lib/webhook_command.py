"""`webhook` - read selected content out of a preserved raw webhook body.

`fetch` stores raw bodies as private files and prints only a digest, because a
5 MB payload dumped into a terminal puts meeting content into scrollback. This
command is the deliberate opposite: an explicit, targeted read of one body.

Two modes:

- `--inbox-id <id> --run <dir>` reads the artifact `fetch` already wrote, with
  no network access at all;
- `--path <file>` reads any JSON file directly.

`--path` selects a value inside the document. Nothing is rewritten, and the
command reports whether the bytes it produced match the file it read.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from . import runstore, status as status_mod
from .config import Config
from .output import Output

MAX_PRINT_CHARS = 20_000


def run_raw(
    config: Config,
    *,
    run_directory: str | None,
    run_id: str | None,
    inbox_id: str | None,
    path: str | None,
    json_path: str | None,
    list_keys: bool,
    max_chars: int,
    output: Output,
) -> status_mod.Result:
    target: Path
    provenance: dict[str, Any]

    if run_id and not run_directory:
        found = [
            run
            for run in runstore.discover_runs(config.runs_directory)
            if run.run_id == run_id
        ]
        if not found:
            return status_mod.Result(
                command="webhook raw",
                status=status_mod.NOT_FOUND,
                data={"requested": {"runId": run_id}},
                findings=[
                    status_mod.Finding(
                        "locate-run",
                        False,
                        f"no run with id {run_id} under {config.runs_directory}",
                        remedy="use `kik.py runs` to list run ids",
                    )
                ],
            )
        run_directory = str(found[0].directory)
        provenance_run_id = run_id
    else:
        provenance_run_id = None

    if path:
        target = Path(path).expanduser()
        provenance = {"mode": "direct-file", "networkUsed": False}
    else:
        if not run_directory or not inbox_id:
            return status_mod.Result(
                command="webhook raw",
                status=status_mod.USAGE_ERROR,
                findings=[
                    status_mod.Finding(
                        "select-body",
                        False,
                        "either --path, or both --run and --inbox-id, are required",
                        remedy=(
                            "fetch writes webhook-raw-<inbox-id>.json into the run "
                            "directory; the run manifest lists them"
                        ),
                    )
                ],
            )
        run_path = Path(run_directory).expanduser()
        candidate = run_path / f"webhook-raw-{inbox_id}.json"
        if not candidate.is_file():
            manifest = run_path / "manifest.json"
            available: list[str] = []
            if manifest.is_file():
                try:
                    payload = json.loads(manifest.read_text(encoding="utf-8"))
                    available = [
                        item.get("name", "")
                        for item in payload.get("produced", [])
                        if str(item.get("name", "")).startswith("webhook-raw-")
                    ]
                except Exception:  # noqa: BLE001
                    available = []
            return status_mod.Result(
                command="webhook raw",
                status=status_mod.NOT_FOUND,
                data={
                    "requested": {"run": str(run_path), "inboxId": inbox_id},
                    "rawBodiesInRun": available,
                },
                findings=[
                    status_mod.Finding(
                        "locate-body",
                        False,
                        f"no raw body artifact for inbox id {inbox_id} in this run",
                        remedy=(
                            "run fetch with --include-raw-bodies, or list the run "
                            "manifest's produced files"
                        ),
                    )
                ],
            )
        target = candidate
        provenance = {
            "mode": "run-artifact",
            "networkUsed": False,
            "runDirectory": str(run_path),
            "inboxId": inbox_id,
            "resolvedFromRunId": provenance_run_id,
        }

    if not target.is_file():
        return status_mod.Result(
            command="webhook raw",
            status=status_mod.NOT_FOUND,
            data={"requested": {"path": str(target)}},
            findings=[status_mod.Finding("read-body", False, "file does not exist")],
        )

    raw_bytes = target.read_bytes()
    try:
        text = raw_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        return status_mod.Result(
            command="webhook raw",
            status=status_mod.PROVIDER_ERROR,
            data={"requested": {"path": str(target)}},
            findings=[
                status_mod.Finding("decode-body", False, f"body is not valid UTF-8: {exc}")
            ],
        )

    try:
        document = json.loads(text)
    except json.JSONDecodeError as exc:
        return status_mod.Result(
            command="webhook raw",
            status=status_mod.PROVIDER_ERROR,
            data={
                "requested": {"path": str(target)},
                "sha256": runstore.sha256_text(text),
                "charCount": len(text),
                "error": {"message": f"body is not valid JSON at offset {exc.pos}"},
            },
            findings=[status_mod.Finding("parse-body", False, "body is not valid JSON")],
        )

    digest = runstore.sha256_text(text)
    base: dict[str, Any] = {
        "requested": {
            "path": str(target),
            "jsonPath": json_path,
            "listKeys": list_keys,
        },
        "source": provenance,
        "artifact": {
            "sha256": digest,
            "charCount": len(text),
            "byteCount": len(raw_bytes),
            "sha256IsOverExactBytes": True,
            "modified": False,
        },
    }

    if list_keys:
        base["structure"] = _describe_structure(document)
        base["status"] = status_mod.OK
        return status_mod.Result(
            command="webhook raw",
            status=status_mod.OK,
            data=base,
            notes=[
                "structure only: key paths and value types, with no meeting content"
            ],
        )

    if not json_path:
        return status_mod.Result(
            command="webhook raw",
            status=status_mod.USAGE_ERROR,
            data=base,
            findings=[
                status_mod.Finding(
                    "select-content",
                    False,
                    "no selector given; refusing to print a whole webhook body",
                    remedy=(
                        "pass --path data.meeting.title to read one value, or "
                        "--list-keys to see the structure"
                    ),
                )
            ],
        )

    found, value, note = _resolve(document, json_path)
    base["selection"] = {
        "jsonPath": json_path,
        "found": found,
        "type": type(value).__name__ if found else None,
        "note": note,
    }
    if not found:
        return status_mod.Result(
            command="webhook raw",
            status=status_mod.NOT_FOUND,
            data=base,
            findings=[
                status_mod.Finding(
                    "select-content",
                    False,
                    f"no value at {json_path}",
                    remedy="use --list-keys to see the available paths",
                )
            ],
        )

    rendered = value if isinstance(value, str) else json.dumps(value, indent=2, ensure_ascii=False)
    truncated = len(rendered) > max_chars
    if truncated:
        rendered = rendered[:max_chars]
    base["content"] = rendered
    base["contentTruncated"] = truncated
    if truncated:
        base["truncationNote"] = (
            f"value is {len(rendered) if not truncated else 'longer than'} "
            f"{max_chars} characters and was cut for display; the artifact file is "
            "unchanged and its hash above covers the full bytes"
        )

    return status_mod.Result(
        command="webhook raw",
        status=status_mod.OK,
        data=base,
        notes=["the artifact file was read, not modified; the hash covers its exact bytes"],
    )


def _describe_structure(document: Any, prefix: str = "", depth: int = 0) -> list[dict[str, Any]]:
    """Key paths and types only, bounded in depth. No values."""
    out: list[dict[str, Any]] = []
    if depth > 4:
        return out
    if isinstance(document, dict):
        for key in sorted(document):
            path = f"{prefix}.{key}" if prefix else key
            value = document[key]
            if isinstance(value, dict):
                out.append({"path": path, "type": "object", "keyCount": len(value)})
                out.extend(_describe_structure(value, path, depth + 1))
            elif isinstance(value, list):
                out.append({"path": path, "type": "array", "length": len(value)})
                if value and isinstance(value[0], dict):
                    out.extend(_describe_structure(value[0], f"{path}[0]", depth + 1))
            else:
                out.append(
                    {
                        "path": path,
                        "type": type(value).__name__,
                        "charCount": len(str(value)) if not isinstance(value, (int, float, bool)) and value is not None else None,
                    }
                )
    return out


def _resolve(document: Any, json_path: str) -> tuple[bool, Any, str | None]:
    """Dot/bracket path lookup, e.g. `data.transcript.segments[0].text`."""
    node = document
    for token in _tokenize(json_path):
        if isinstance(token, int):
            if not isinstance(node, list) or token >= len(node) or token < 0:
                return False, None, f"index {token} is out of range"
            node = node[token]
            continue
        if not isinstance(node, dict):
            return False, None, f"{token!r} selected from a {type(node).__name__}"
        if token not in node:
            return False, None, f"no key {token!r}"
        node = node[token]
    return True, node, None


def _tokenize(json_path: str) -> list[Any]:
    tokens: list[Any] = []
    current = ""
    index = 0
    while index < len(json_path):
        char = json_path[index]
        if char == ".":
            if current:
                tokens.append(current)
                current = ""
        elif char == "[":
            if current:
                tokens.append(current)
                current = ""
            closing = json_path.find("]", index)
            if closing == -1:
                tokens.append(json_path[index:])
                break
            raw = json_path[index + 1 : closing].strip().strip("'\"")
            tokens.append(int(raw) if raw.lstrip("-").isdigit() else raw)
            index = closing
        else:
            current += char
        index += 1
    if current:
        tokens.append(current)
    return tokens