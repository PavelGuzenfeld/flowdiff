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


CPP_SUFFIXES = (".cpp", ".cc", ".cxx", ".c")


def detect_cpp_dialect(test_dir: Path) -> tuple[str, str]:
    """(dialect, include line) in decision 48's order: the repo's own harness header, gtest, Catch2, doctest."""
    texts = [p.read_text(encoding="utf-8", errors="replace") for p in sorted(test_dir.glob("**/*"))
             if p.suffix in CPP_SUFFIXES or p.suffix in (".h", ".hpp")] if test_dir.is_dir() else []
    local = [m.group(1) for t in texts for m in re.finditer(r'^#include\s+"([^"]*test_harness[^"]*)"', t, re.M)]
    if local:
        return "harness", f'#include "{local[0]}"'
    if any("gtest/gtest.h" in t for t in texts):
        return "gtest", "#include <gtest/gtest.h>"
    if any("catch2/" in t or "catch.hpp" in t for t in texts):
        return "catch2", "#include <catch2/catch_test_macros.hpp>"
    if any("doctest.h" in t for t in texts):
        return "doctest", "#include <doctest/doctest.h>"
    return "plain", "#include <cassert>"


def cpp_literal(value: object) -> str | None:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, str):
        return json.dumps(value)
    if value is None:
        return "nullptr"
    return None


def cpp_cases(name: str, calls: list[compare.Call]) -> list[tuple[str, list[tuple[str, str]]]]:
    """(call expression, [(member path, literal)]) per distinct call whose arguments are all scalars;
    a struct return is asserted one field level down, a scalar return as a whole."""
    out: list[tuple[str, list[tuple[str, str]]]] = []
    for call in calls:
        args = [cpp_literal(v) for v in call.args.values()]
        if call.raises is not None or call.result is compare.MISSING or any(a is None for a in args):
            continue
        source = f"{name}({', '.join(a for a in args if a is not None)})"
        result = call.result
        if isinstance(result, dict) and "fields" in result:
            checks = [(f".{k}", lit) for k, v in result["fields"].items() if (lit := cpp_literal(v)) is not None]
        else:
            lit = cpp_literal(result)
            checks = [("", lit)] if lit is not None else []
        if checks and (source, checks) not in out:
            out.append((source, checks))
    return out[:MAX_CASES]


def emit_cpp(source_include: str, name: str, found: list, dialect: str, include: str) -> str:
    short = name.rsplit("::", 1)[-1]
    lines = [f"// {HEADER.strip().strip(chr(34))}", f'#include "{source_include}"', include, ""]
    macro = {"harness": "TEST({n})", "gtest": "TEST(FlowKeep, {n})", "catch2": 'TEST_CASE("{n}")',
             "doctest": 'TEST_CASE("{n}")'}.get(dialect)
    check = {"harness": "ASSERT_EQ({a}, {b});", "gtest": "EXPECT_EQ({a}, {b});", "catch2": "CHECK({a} == {b});",
             "doctest": "CHECK({a} == {b});"}.get(dialect, "assert({a} == {b});")
    for i, (call, checks) in enumerate(found, 1):
        case = f"flow_{short}_{i}"
        body = [f"    auto result = {call};"] + [f"    {check.format(a='result' + member, b=lit)}" for member, lit in checks]
        if macro:
            lines += [macro.format(n=case) + " {", *body, "}", ""]
        else:
            lines += [f"static void {case}() {{", *body, "}", ""]
    if not macro:
        lines += ["int main() {", *[f"    flow_{short}_{i}();" for i in range(1, len(found) + 1)], "    return 0;", "}", ""]
    return "\n".join(lines)


def keep_cpp(root: Path, full: str, calls: list[compare.Call], qualified: str | None = None) -> int:
    path, name = full.split(":", 1)
    found = cpp_cases(qualified or name, calls)
    if not found:
        print(f"{full}: no recorded call has scalar arguments and an assertable return; nothing to keep", file=sys.stderr)
        return cli.EXIT_NOTHING
    test_dir = test_dir_of(root)
    test_dir.mkdir(exist_ok=True)
    dialect, include = detect_cpp_dialect(test_dir)
    short = name.rsplit("::", 1)[-1]
    target = test_dir / f"flow_{short}.cpp"
    header = Path(path).with_suffix(".hpp").name if (root / Path(path).with_suffix(".hpp")).is_file() else Path(path).name
    target.write_text(emit_cpp(header, name, found, dialect, include))
    print(f"{target.relative_to(root)}: {len(found)} case(s), {dialect} dialect; add it to the build to run it")
    return cli.EXIT_OK


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
            path, name = full.split(":", 1)   # C++ names carry ::, so split at the path's colon, not the last
            if Path(path).suffix in CPP_SUFFIXES:
                return keep_cpp(root, full, report.head.get(full, []), entry.get("names", {}).get(full))
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
