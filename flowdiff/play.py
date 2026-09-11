"""play: build one harness per flow, run it against .flowdiff/base and the working tree, diff the
traces, run the covering tests on both sides. show: read the values the last play recorded."""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import cli, compare, container, env, graph, harness_cpp, harness_py, lsp, render, worktree

RUN_DIR = "run"


def run_dir(root: Path) -> Path:
    path = worktree.scratch_dir(root) / RUN_DIR
    path.mkdir(exist_ok=True)
    return path


def run_harness(interpreter: Path, harness: Path, tree: Path, side: str, out: Path, timeout: float) -> str | None:
    out.unlink(missing_ok=True)
    try:
        proc = subprocess.run([str(interpreter), str(harness)], cwd=tree, capture_output=True, text=True,
                              timeout=timeout, env=env.harness_env(tree, side, out))
    except subprocess.TimeoutExpired:
        return f"{side}: harness timed out after {timeout:.0f}s"
    if proc.returncode != 0:
        return f"{side}: harness exited {proc.returncode}\n{proc.stderr.strip()[-2000:]}"
    return None


def present(tree: Path, test: str) -> bool:
    """A test id exists on a side when its file is there and, unless module-level, so is its def."""
    file, _, name = test.partition("::")
    path = tree / file
    if not path.is_file():
        return False
    return name in ("", "<module>") or f"def {name}(" in path.read_text(encoding="utf-8", errors="replace")


def shared_tests(base: Path, root: Path, tests: list[str]) -> tuple[list[str], list[str]]:
    """Tests present on both sides, and the test files among them whose text differs between the sides."""
    shared = [t for t in tests if present(base, t) and present(root, t)]
    files = sorted({t.split("::")[0] for t in shared})
    changed = [f for f in files if (base / f).read_bytes() != (root / f).read_bytes()]
    return shared, changed


def node_ids(tree: Path, tests: list[str]) -> list[str]:
    return [t.split("::")[0] if t.endswith("::<module>") else t for t in tests if present(tree, t)]


def run_traced_tests(interpreter: Path, tree: Path, tests: list[str], frames: list[str], side: str, out: Path,
                     timeout: float) -> str | None:
    out.unlink(missing_ok=True)
    ids = node_ids(tree, tests)
    if not ids:
        return f"{side}: none of the covering tests exist on this side"
    # One basetemp for both sides: pytest wipes it per session, so tmp_path values repeat exactly.
    basetemp = out.parent / "basetemp"
    try:
        proc = subprocess.run([str(interpreter), "-m", "pytest", "-q", "-p", "no:cacheprovider",
                               "-p", "flowdiff.pytest_tracer", f"--basetemp={basetemp}", *ids], cwd=tree,
                              capture_output=True, text=True, timeout=timeout,
                              env=env.harness_env(tree, side, out, frames))
    except subprocess.TimeoutExpired:
        return f"{side}: covering tests timed out after {timeout:.0f}s"
    # 0 passed and 1 failed both traced the flow; anything else never ran it.
    if proc.returncode not in (0, 1):
        return f"{side}: pytest exited {proc.returncode}\n{(proc.stdout + proc.stderr).strip()[-2000:]}"
    return None


def run_tests(interpreter: Path, tree: Path, tests: list[str], timeout: float) -> dict[str, str]:
    results = {}
    for test in tests:
        if not present(tree, test):
            results[test] = "ABSENT"
            continue
        try:
            proc = subprocess.run([str(interpreter), "-m", "pytest", "-q", "-x", "-p", "no:cacheprovider", test],
                                  cwd=tree, capture_output=True, text=True, timeout=timeout,
                                  env=env.harness_env(tree))
            # pytest: 0 passed, 1 a test failed; anything else never ran the test.
            results[test] = {0: "PASS", 1: "FAIL"}.get(proc.returncode, f"ERROR({proc.returncode})")
        except subprocess.TimeoutExpired:
            results[test] = "TIMEOUT"
    return results


def test_delta(interpreter: Path, base: Path, root: Path, tests: list[str], timeout: float) -> list[str]:
    """One line per test whose outcome matters (anything not passing now, or fixed); the rest as counts."""
    before = run_tests(interpreter, base, tests, timeout)
    after = run_tests(interpreter, root, tests, timeout)
    lines, quiet = [], Counter()
    for t in tests:
        if after[t] != "PASS" or before[t] == "FAIL":
            lines.append(f"  {t}  {before[t]}→{after[t]}")
        else:
            quiet[f"{before[t]}→{after[t]}"] += 1
    if quiet:
        lines.append("  " + ", ".join(f"{n} {kind}" for kind, n in sorted(quiet.items())))
    return lines


@dataclass
class Plan:
    """How one flow gets run on a side: a harness, the covering tests, or nothing (empty slots)."""
    frames: list[str]
    note: str | None = None
    stop: str | None = None
    driver: str | None = None
    warnings: list[str] = field(default_factory=list)
    drive: Callable[[str, Path], str | None] | None = None
    delta: Callable[[], list[str]] | None = None
    trailer: list[str] = field(default_factory=list)


def python_plan(client: lsp.LspClient, g: graph.Graph, flow: graph.Flow, root: Path, base: Path, out_dir: Path,
                index: int, timeout: float) -> Plan:
    harness = harness_py.build(client, root, base, flow, g)
    harness_path = out_dir / f"flow{index}.py"
    harness_path.write_text(harness.source)
    frames = [harness_py.frame_id(root, f) for f in flow.frames if f.status != "slot"]
    traces = {side: out_dir / f"flow{index}.{side}.jsonl" for side in ("base", "head")}
    interpreter = env.python_interpreter(root)
    plan = Plan(frames, warnings=list(harness.warnings), driver=harness.driver)
    shared, changed_tests = shared_tests(base, root, flow.tests) if flow.tests else ([], [])
    if harness.complete:
        plan.drive = lambda side, tree: run_harness(interpreter, harness_path, tree, side, traces[side], timeout)
    elif shared:
        plan.note = f"driven by {len(shared)} covering test(s) present on both sides"
        if changed_tests:
            plan.warnings.append(f"the driving tests changed in this diff ({', '.join(changed_tests)}); "
                                 "a divergence may reflect the inputs rather than the code")
        plan.drive = lambda side, tree: run_traced_tests(interpreter, tree, shared, frames, side, traces[side], timeout)
    else:
        plan.stop = ("no call site with literal arguments and no covering test present on both sides; "
                     f"fill the slots in {harness_path} — harness not run")
    if flow.tests:
        plan.delta = lambda: test_delta(interpreter, base, root, flow.tests, timeout)
    return plan


def cpp_plan(ctr: container.Container, flow: graph.Flow, root: Path, base: Path, out_dir: Path, index: int,
             timeout: float, build_timeout: float) -> Plan:
    """C++ flows are driven by the built test executables that reach them, traced under gdb in the container."""
    frames = [harness_cpp.frame_id(root, f) for f in flow.frames if f.status != "slot"]
    plan = Plan(frames)
    test_files = sorted({t.split("::")[0] for t in flow.tests})
    head_exes = harness_cpp.test_executables(root, test_files)
    if not head_exes:
        plan.stop = ("no built test executable reaches this flow; C++ flows are driven by their covering tests "
                     "and the literal harness is not available yet")
        return plan
    built: set[Path] = {root}
    traces = {side: out_dir / f"flow{index}.{side}.jsonl" for side in ("base", "head")}
    gdb_frames = harness_cpp.gdb_frames(root, flow)
    tests = harness_cpp.test_symbols(flow.tests)

    def ensure_built(tree: Path) -> str | None:
        if tree in built:
            return None
        print(f"building {tree.relative_to(root)} in {ctr.image}", file=sys.stderr)
        failure = container.build(ctr, tree, build_timeout)
        built.add(tree)
        return failure

    def drive(side: str, tree: Path) -> str | None:
        failure = ensure_built(tree)
        if failure:
            return failure
        exes = harness_cpp.test_executables(tree, test_files)
        shared = [exes[f] for f in test_files if f in exes and f in head_exes]
        if not shared:
            return f"{side}: none of the covering test executables exist on this side"
        local = out_dir / f"flow{index}.{side}.gdb.jsonl" if tree == root else worktree.scratch_dir(tree) / "run" / f"flow{index}.jsonl"
        (local.parent).mkdir(parents=True, exist_ok=True)
        failure = harness_cpp.trace_tests(ctr, tree, shared, gdb_frames, tests, local, timeout)
        if failure:
            return failure
        traces[side].write_text(local.read_text())
        return None

    plan.note = f"driven by {len(head_exes)} covering test executable(s) under gdb"
    plan.drive = drive

    def delta() -> list[str]:
        before = harness_cpp.run_executables(ctr, base, list(head_exes.values()), timeout)
        after = harness_cpp.run_executables(ctr, root, list(head_exes.values()), timeout)
        return [f"  {exe}  {before.get(exe, 'ABSENT')}→{after[exe]}" for exe in head_exes.values()]

    plan.delta = delta
    device, variant = harness_cpp.linked_variant(ctr, root, list(head_exes.values()))
    plan.trailer.append(variant)
    if device:
        plan.stop = f"{variant}; these frames need the device: {', '.join(sorted(f.name for f in flow.frames))}"
    return plan


def run(args: argparse.Namespace) -> int:
    plans: dict[int, Plan] = {}
    base_holder: list[Path] = []

    def visit(client: lsp.LspClient, g: graph.Graph, flows: list[graph.Flow], analysis: cli.Analysis) -> None:
        root = client.root
        if not base_holder:
            base_holder.append(worktree.base_worktree(root, args.ref or "HEAD", args.clean_base))
        out_dir = run_dir(root)
        if client.config.language_id == "python":
            graph.open_test_files(client, g)
        for flow in flows:
            if flow.removed_only:
                continue
            index = len(plans) + 1
            if client.config.language_id == "python":
                plans[id(flow)] = python_plan(client, g, flow, root, base_holder[0], out_dir, index, args.run_timeout)
            elif analysis.container is not None:
                plans[id(flow)] = cpp_plan(analysis.container, flow, root, base_holder[0], out_dir, index,
                                           args.run_timeout, args.build_timeout)
            else:
                analysis.warnings.append(f"{flow.language} flows outside a container are analysed only")

    analysis = cli.analyse(args, visit)
    if isinstance(analysis, int):
        return analysis
    root = analysis.root
    shown = [f for f in analysis.flows if not f.removed_only]
    if not plans:
        cli.render_all(analysis, args.full, args.list_tests)
        return cli.EXIT_NOTHING

    out_dir = run_dir(root)
    index: list[dict] = []
    diverged = False
    for i, flow in enumerate(shown, 1):
        if id(flow) not in plans:
            print(render.render_flow(i, len(shown), flow, args.full, args.list_tests))
            print()
            continue
        plan = plans[id(flow)]
        analysis.warnings += plan.warnings
        traces = {side: out_dir / f"flow{i}.{side}.jsonl" for side in ("base", "head")}
        if plan.stop or plan.drive is None:
            print(render.render_flow(i, len(shown), flow, args.full, args.list_tests))
            print(plan.stop or "nothing can drive this flow")
            cli.render_all(cli.Analysis(root, analysis.revs, [], analysis.warnings), args.full)
            return cli.EXIT_NOTHING
        print(render.render_flow(i, len(shown), flow, args.full, args.list_tests, plan.note))
        if plan.note and flow.entry is not None:
            print(f"no call site with literal arguments; {plan.note}")
        for side, tree in (("base", base_holder[0]), ("head", root)):
            failure = plan.drive(side, tree)
            if failure:
                print(failure, file=sys.stderr)
                return cli.EXIT_TOOL_ERROR
        one_sided = {f for f, node in zip(plan.frames, [n for n in flow.frames if n.status != "slot"])
                     if node.status in ("added", "removed")}
        report = compare.Report(plan.frames, compare.load(traces["base"]), compare.load(traces["head"]), one_sided)
        print(compare.verdict(report))
        for line in plan.trailer:
            print(line)
        diverged = diverged or bool(report.differing())
        for frame in report.traced()[:args.depth]:
            print(compare.show(report, frame))
        if plan.delta is not None:
            print("\n".join(plan.delta()))
        index.append({"flow": i, "frames": plan.frames, "base": str(traces["base"]), "head": str(traces["head"]),
                      "driver": plan.driver})
        if i < len(shown):
            print()
    removed = [f for f in analysis.flows if f.removed_only]
    if removed:
        print("\n" + render.render_removed(removed))
    (out_dir / "index.json").write_text(json.dumps(index))
    sys.stdout.flush()
    for w in analysis.warnings:
        print(f"warning: {w}", file=sys.stderr)
    return cli.EXIT_DIFF if diverged and args.fail_on_diff else cli.EXIT_OK


def show(args: argparse.Namespace) -> int:
    root = cli.repo_root(args.repo)
    if root is None:
        return cli.EXIT_TOOL_ERROR
    index_path = root / worktree.SCRATCH / RUN_DIR / "index.json"
    if not index_path.exists():
        print("nothing recorded; run flowdiff play first", file=sys.stderr)
        return cli.EXIT_NOTHING
    query, _, arg = args.frame.partition("/")
    query, _, call = query.partition("#")
    if call and not call.isdigit():
        print(f"{args.frame}: the call selector after # must be a number", file=sys.stderr)
        return cli.EXIT_TOOL_ERROR
    shown = 0
    for entry in json.loads(index_path.read_text()):
        report = compare.Report(entry["frames"], compare.load(Path(entry["base"])), compare.load(Path(entry["head"])))
        for frame in compare.resolve(report, query):
            if call:
                print(compare.show_call(report, frame, int(call) - 1, arg or None))
            else:
                print(compare.show(report, frame, arg or None))
            shown += 1
    if not shown:
        print(f"{query}: not a frame of the last play", file=sys.stderr)
        return cli.EXIT_NOTHING
    return cli.EXIT_OK
