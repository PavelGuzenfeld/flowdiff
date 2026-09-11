"""keep: turn a played frame into a permanent test in the repo's own dialect (decisions 47, 48).

The head side's recorded calls become assertions; a call whose arguments are not all literals is
skipped, since the tool never invents a value. Registering the file with the build stays the user's job.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from . import cli, compare, harness_py, worktree
from .graph import TEST_DIR_NAMES

RUN_DIR = "run"
MAX_CASES = 12
TEST_DIR_PRIORITY = ("tests", "test", "spec", "specs")


def detect_dialect(test_dir: Path) -> str:
    """pytest, then unittest, then plain asserts (decision 48). A module-level test_ function is pytest's mark;
    most pytest suites never import pytest."""
    texts = [p.read_text(encoding="utf-8", errors="replace") for p in sorted(test_dir.glob("**/*.py"))] \
        if test_dir.is_dir() else []
    if any(re.search(r"^\s*(import|from) pytest\b|^def test_\w+\(", t, re.M) for t in texts):
        return "pytest"
    if any(re.search(r"^\s*(import|from) unittest\b", t, re.M) for t in texts):
        return "unittest"
    return "plain"


def test_dir_of(root: Path) -> Path:
    assert set(TEST_DIR_PRIORITY) == TEST_DIR_NAMES
    for name in TEST_DIR_PRIORITY:
        if (root / name).is_dir():
            return root / name
    return root / "tests"


def literal(value: object) -> str | None:
    """Python source for a recorded value, or None when the summariser kept only a stand-in."""
    if isinstance(value, dict) and "type" in value:
        return None
    if isinstance(value, dict):
        items = [(k, literal(v)) for k, v in value.items()]
        return None if any(v is None for _, v in items) else "{" + ", ".join(f"{k!r}: {v}" for k, v in items) + "}"
    if isinstance(value, list):
        items = [literal(v) for v in value]
        return None if any(v is None for v in items) else "[" + ", ".join(items) + "]"  # type: ignore[arg-type]
    return repr(value)


def call_source(module: str, name: str, args: dict) -> str | None:
    parts = []
    for key, value in args.items():
        text = literal(value)
        if text is None:
            return None
        if key.startswith("**"):
            parts.append(f"**{text}")
        elif key.startswith("*"):
            parts.append(f"*{text}")
        else:
            parts.append(text)
    return f"{module}.{name}({', '.join(parts)})"


def cases(module: str, name: str, calls: list[compare.Call]) -> list[tuple[str, str, bool]]:
    """(call, expected, whole) per distinct recorded call; whole is False when only a summary can be asserted."""
    out: list[tuple[str, str, bool]] = []
    for call in calls:
        source = call_source(module, name, call.args)
        if source is None or call.raises is not None or call.result is compare.MISSING:
            continue
        expected = literal(call.result)
        case = (source, expected, True) if expected is not None else (source, json.dumps(call.result), False)
        if case not in out:
            out.append(case)
    return out[:MAX_CASES]


HEADER = '"""Golden flow test written by flowdiff keep; rerun keep after a deliberate behaviour change."""\n'


def emit(module: str, name: str, found: list[tuple[str, str, bool]], dialect: str) -> str:
    imports = [f"import {module}"]
    if any(not whole for _, _, whole in found):
        imports.append("from flowdiff.summarise import summarise")

    def check(source: str, expected: str, whole: bool) -> str:
        actual = source if whole else f"summarise({source})"
        return f"self.assertEqual({actual}, {expected})" if dialect == "unittest" else f"assert {actual} == {expected}"

    if dialect == "unittest":
        imports.insert(0, "import unittest")
        body = [f"class Flow{name.title().replace('_', '')}(unittest.TestCase):"]
        for i, (source, expected, whole) in enumerate(found, 1):
            body += [f"    def test_{i}(self):", f"        {check(source, expected, whole)}", ""]
        body.append("")
    elif dialect == "pytest":
        body = []
        for i, (source, expected, whole) in enumerate(found, 1):
            body += [f"def test_flow_{name}_{i}():", f"    {check(source, expected, whole)}", "", ""]
        body = body[:-1]
    else:
        body = [check(source, expected, whole) for source, expected, whole in found] + [""]
    return HEADER + "\n".join(imports) + "\n\n\n" + "\n".join(body).rstrip("\n") + "\n"


def run(args: argparse.Namespace) -> int:
    root = cli.repo_root(args.repo)
    if root is None:
        return cli.EXIT_TOOL_ERROR
    index_path = root / worktree.SCRATCH / RUN_DIR / "index.json"
    if not index_path.exists():
        print("nothing recorded; run flowdiff play first", file=sys.stderr)
        return cli.EXIT_NOTHING
    entries = json.loads(index_path.read_text())
    frame = args.frame
    if frame is None:
        drivers = [e.get("driver") for e in entries if e.get("driver")]
        if not drivers:
            print("the last play was driven by covering tests, not a harness; name the frame to keep: "
                  "flowdiff keep <frame>", file=sys.stderr)
            return cli.EXIT_NOTHING
        frame = drivers[0]
    for entry in entries:
        report = compare.Report(entry["frames"], {}, compare.load(Path(entry["head"])))
        for full in compare.resolve(report, frame):
            path, name = full.rsplit(":", 1)
            module = harness_py.module_name(root, root / path)
            found = cases(module, name, report.head.get(full, []))
            if not found:
                print(f"{full}: no recorded call has literal arguments and a return; nothing to keep", file=sys.stderr)
                return cli.EXIT_NOTHING
            test_dir = test_dir_of(root)
            test_dir.mkdir(exist_ok=True)
            target = test_dir / f"flow_{name}.py"
            target.write_text(emit(module, name, found, detect_dialect(test_dir)))
            print(f"{target.relative_to(root)}: {len(found)} case(s), {detect_dialect(test_dir)} dialect; "
                  "register it with your test runner if it needs registering")
            return cli.EXIT_OK
    print(f"{frame}: not a frame of the last play", file=sys.stderr)
    return cli.EXIT_NOTHING
