"""Tests for the artifact writer: atomic writes, permissions, layout.

Artifacts hold real meeting content, so the writer must never leave a partial
file visible, must never default to world-readable permissions, and must refuse
to write outside the configured artifacts root.
"""

import json
import os
import stat
import sys
from pathlib import Path

TOOLKIT_ROOT = Path(__file__).resolve().parent.parent
if str(TOOLKIT_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOLKIT_ROOT))

from lib import artifacts  # noqa: E402

TMP_ROOT = Path("/tmp/kik-artifact-tests")


def _root() -> Path:
    if TMP_ROOT.exists():
        for child in sorted(TMP_ROOT.rglob("*"), reverse=True):
            if child.is_file():
                child.unlink()
            else:
                child.rmdir()
    TMP_ROOT.mkdir(parents=True, exist_ok=True)
    return TMP_ROOT


def test_write_json_creates_parents_and_round_trips() -> None:
    root = _root()
    target = root / "2026-10-20" / "cm-example" / "bundle.json"
    artifacts.write_json(target, {"a": 1, "nested": {"b": [1, 2, 3]}}, root=root)
    assert target.exists()
    assert json.loads(target.read_text(encoding="utf-8")) == {
        "a": 1,
        "nested": {"b": [1, 2, 3]},
    }


def test_written_file_is_private() -> None:
    root = _root()
    target = root / "private.json"
    artifacts.write_json(target, {"secret": "meeting content"}, root=root)
    mode = target.stat().st_mode
    assert not mode & stat.S_IROTH, "other users must not be able to read artifacts"
    assert not mode & stat.S_IWOTH
    assert mode & stat.S_IRUSR


def test_no_temporary_file_is_left_behind() -> None:
    root = _root()
    target = root / "clean.json"
    artifacts.write_json(target, {"x": 1}, root=root)
    leftovers = [
        p.name for p in target.parent.iterdir() if p.name != target.name
    ]
    assert leftovers == [], f"unexpected leftovers: {leftovers}"


def test_overwrite_replaces_content_atomically() -> None:
    root = _root()
    target = root / "replace.json"
    artifacts.write_json(target, {"version": 1}, root=root)
    artifacts.write_json(target, {"version": 2}, root=root)
    assert json.loads(target.read_text(encoding="utf-8")) == {"version": 2}


def test_write_outside_root_is_refused() -> None:
    root = _root()
    outside = Path("/tmp/kik-artifact-escape.json")
    try:
        artifacts.write_json(outside, {"x": 1}, root=root)
    except artifacts.ArtifactError as exc:
        assert "outside" in str(exc)
    else:
        raise AssertionError("writing outside the artifacts root must fail")
    finally:
        outside.unlink(missing_ok=True)


def test_path_traversal_is_refused() -> None:
    root = _root()
    try:
        artifacts.write_json(root / ".." / "escaped.json", {"x": 1}, root=root)
    except artifacts.ArtifactError:
        pass
    else:
        raise AssertionError("traversal out of the root must fail")
    assert not (root.parent / "escaped.json").exists()


def test_write_text_round_trips_and_is_private() -> None:
    root = _root()
    target = root / "notes.txt"
    artifacts.write_text(target, "line one\nline two\n", root=root)
    assert target.read_text(encoding="utf-8") == "line one\nline two\n"
    assert not target.stat().st_mode & stat.S_IROTH


def test_ensure_directory_applies_private_mode() -> None:
    root = _root()
    created = artifacts.ensure_directory(root / "a" / "b", root=root)
    assert created.is_dir()
    mode = created.stat().st_mode
    assert mode & stat.S_IRWXU
    assert not mode & stat.S_IROTH


def test_run_directory_layout_matches_the_documented_shape() -> None:
    root = _root()
    path = artifacts.run_directory(root, "2026-10-20", "cm-meeting-id")
    assert path == root / "2026-10-20" / "cm-meeting-id"


def test_registry_and_manifest_helpers() -> None:
    root = _root()
    target = root / "manifest.json"
    artifacts.write_manifest(
        target,
        command="inspect",
        status="NO_MEETINGS",
        produced=["bundle.json"],
        inputs={"date": "2026-10-20"},
        root=root,
    )
    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload["command"] == "inspect"
    assert payload["status"] == "NO_MEETINGS"
    assert payload["producedPaths"] == ["bundle.json"]
    assert payload["generatedAt"].endswith("Z")
    assert "toolkitVersion" in payload


def test_bytes_written_are_not_redecoded() -> None:
    root = _root()
    target = root / "raw.txt"
    payload = "emoji 🙂 and a tab\t".encode("utf-8")
    artifacts.write_bytes(target, payload, root=root)
    assert target.read_bytes() == payload


def test_file_mode_constant_is_owner_only() -> None:
    assert artifacts.FILE_MODE == 0o600
    assert artifacts.DIR_MODE == 0o700