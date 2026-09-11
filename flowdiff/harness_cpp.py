"""The C++ side of play: which built test executables reach the flow, and how to run one under gdb in the
container so the flow's frames are traced (decisions 20 to 24, 28, 39, 41, 42)."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from . import container, trace_gdb, worktree
from .graph import Flow, Node

BUILD_DIR = container.BUILD_DIR
LITERAL = re.compile(r"""^(?:[-+]?(?:0[xX][0-9a-fA-F]+|\d+(?:\.\d*)?(?:[eE][-+]?\d+)?[uUlLfF]*)|true|false|nullptr|NULL|'(?:\\.|[^'\\])'|"(?:\\.|[^"\\])*")$""")


def frame_id(root: Path, node: Node) -> str:
    return f"{node.path.resolve().relative_to(root.resolve()).as_posix()}:{node.name}"


def compile_db(tree: Path) -> list[dict]:
    path = tree / BUILD_DIR / "compile_commands.json"
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return []


def test_executables(tree: Path, test_files: list[str]) -> dict[str, str]:
    """test file (tree-relative) -> executable (tree-relative), through the compile database's outputs.

    Meson writes objects under <target>.p/, so the executable is the .p directory's parent path
    without the suffix; the mapping is only trusted when that file exists in the build tree."""
    build = tree / BUILD_DIR
    found: dict[str, str] = {}
    for entry in compile_db(tree):
        source = (Path(entry.get("directory", str(build))) / entry["file"]).resolve()
        output = entry.get("output", "")
        try:
            rel = source.relative_to(tree.resolve()).as_posix()
        except ValueError:
            continue
        if rel not in test_files or not output or ".p/" not in output:
            continue
        exe = output.split(".p/", 1)[0]
        if (build / exe).is_file():
            found[rel] = f"{BUILD_DIR}/{exe}"
    return found


def literal_call(text: str, line: int, name: str) -> list[str] | None:
    """Arguments of the call to `name` on 0-based `line` when every one is a literal, else None."""
    short = name.rsplit("::", 1)[-1]
    for candidate in text.splitlines()[line:line + 1]:
        match = re.search(re.escape(short) + r"\s*\((.*)\)\s*;?", candidate)
        if not match:
            return None
        args = split_arguments(match.group(1))
        return args if args is not None and all(LITERAL.match(a.strip()) for a in args) else None
    return None


def split_arguments(text: str) -> list[str] | None:
    """Top-level comma split; None when parentheses do not balance on this line."""
    out, depth, current, quote = [], 0, "", None
    for ch in text:
        if quote:
            current += ch
            if ch == quote:
                quote = None
            continue
        if ch in "\"'":
            quote = ch
        elif ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
            if depth < 0:
                return None
        elif ch == "," and depth == 0:
            out.append(current)
            current = ""
            continue
        current += ch
    if depth != 0:
        return None
    return [a for a in out + [current] if a.strip()] if (out or current.strip()) else []


@dataclass(frozen=True)
class TestRun:
    """One test executable, run under gdb on one side with the flow's frames instrumented."""
    ctr: container.Container
    executable: str
    frames: dict[str, str]
    tests: dict[str, str]

    def script(self, tree: Path, out_rel: str) -> str:
        return trace_gdb.script(self.ctr.workdir, self.frames, self.tests, f"{self.ctr.workdir}/{out_rel}")


def gdb_frames(root: Path, flow: Flow) -> dict[str, str]:
    return {frame_id(root, f): f.name for f in flow.frames if f.status not in ("slot", "removed")}


def test_symbols(tests: list[str]) -> dict[str, str]:
    """Function names whose start marks a test's calls: the id's function, and the TEST(name) macro's test_name."""
    out: dict[str, str] = {}
    for test in tests:
        _, _, name = test.partition("::")
        if name and name != "<module>":
            out[name] = test
            out[f"test_{name}"] = test
    return out


def trace_tests(ctr: container.Container, tree: Path, executables: list[str], frames: dict[str, str],
                tests: dict[str, str], out: Path, timeout: float) -> str | None:
    """Run every executable under gdb in `tree`, appending to `out` (a path under the tree); the failure text or None."""
    scratch = worktree.scratch_dir(tree) / "run"
    scratch.mkdir(exist_ok=True)
    out.unlink(missing_ok=True)
    out.touch()
    for i, exe in enumerate(executables):
        part = scratch / f"gdb{i}.jsonl"
        part.unlink(missing_ok=True)
        script_path = scratch / f"gdb{i}.py"
        script_path.write_text(trace_gdb.script(ctr.workdir, frames, tests, ctr.path(tree, part)))
        cmd = trace_gdb.gdb_command(ctr.path(tree, script_path), f"{ctr.workdir}/{exe}")
        try:
            proc = ctr.run(tree, cmd, timeout=timeout)
        except Exception as exc:  # TimeoutExpired or docker failure
            return f"{exe}: gdb did not finish: {exc}"
        if not part.exists():
            return f"{exe}: gdb wrote no trace\n{(proc.stdout + proc.stderr).strip()[-2000:]}"
        with out.open("a") as merged:
            merged.write(part.read_text())
    return None


def run_executables(ctr: container.Container, tree: Path, executables: list[str], timeout: float) -> dict[str, str]:
    results = {}
    for exe in executables:
        try:
            proc = ctr.run(tree, [f"{ctr.workdir}/{exe}"], timeout=timeout)
            results[exe] = "PASS" if proc.returncode == 0 else "FAIL"
        except Exception:
            results[exe] = "TIMEOUT"
    return results


def linked_variant(ctr: container.Container, tree: Path, executables: list[str]) -> tuple[list[str], str]:
    """Device libraries the executables link, and the verdict line naming the variant (decisions 41, 42)."""
    libs: list[str] = []
    for exe in executables:
        libs += container.needed_libraries(ctr, tree, f"{ctr.workdir}/{exe}")
    device = container.device_libraries(libs)
    if device:
        return device, f"linked: {', '.join(device)} (device build)"
    return [], "linked: no device libraries (host build)"
