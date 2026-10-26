#!/usr/bin/env python3
"""Minimal stdlib test runner - no pytest on this host.

Usage:
    python3 /root/Kik/tests/run_tests.py
    python3 /root/Kik/tests/run_tests.py test_config

Discovers `test_*` functions in `tests/test_*.py`, prints one line per test and
a summary, and exits non-zero if anything failed.
"""

from __future__ import annotations

import importlib
import inspect
import sys
import traceback
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
TOOLKIT_ROOT = TESTS_DIR.parent
if str(TOOLKIT_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOLKIT_ROOT))
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))


def discover(selector: str | None) -> list[tuple[str, callable]]:
    found: list[tuple[str, callable]] = []
    for path in sorted(TESTS_DIR.glob("test_*.py")):
        module_name = path.stem
        if selector and selector not in module_name:
            continue
        module = importlib.import_module(module_name)
        for name, function in sorted(vars(module).items()):
            if name.startswith("test_") and inspect.isfunction(function):
                found.append((f"{module_name}::{name}", function))
    return found


def main(argv: list[str]) -> int:
    selector = argv[1] if len(argv) > 1 else None
    tests = discover(selector)
    if not tests:
        print(f"no tests matched selector {selector!r}", file=sys.stderr)
        return 2

    failures: list[tuple[str, str]] = []
    for name, function in tests:
        try:
            function()
        except Exception:  # noqa: BLE001 - the runner reports, it does not judge
            failures.append((name, traceback.format_exc()))
            print(f"FAIL  {name}")
        else:
            print(f"ok    {name}")

    print()
    print(f"{len(tests) - len(failures)} passed, {len(failures)} failed, {len(tests)} total")
    if failures:
        print()
        for name, trace in failures:
            print("=" * 78)
            print(name)
            print(trace)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))