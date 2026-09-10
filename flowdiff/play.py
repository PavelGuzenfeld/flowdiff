"""play: build one harness per flow, run it against .flowdiff/base and the working tree, diff the
traces, run the covering tests on both sides. show: read the values the last play recorded."""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from . import cli, compare, env, graph, harness_py, lsp, render, worktree

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
    try:
        proc = subprocess.run([str(interpreter), "-m", "pytest", "-q", "-p", "no:cacheprovider",
                               "-p", "flowdiff.pytest_tracer", *ids], cwd=tree, capture_output=True, text=True,
                              timeout=timeout, env=env.harness_env(tree, side, out, frames))
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
    before = run_tests(interpreter, base, tests, timeout)
    after = run_tests(interpreter, root, tests, timeout)
    return [f"  {t}  {before[t]}→{after[t]}" for t in tests]


def run(args: argparse.Namespace) -> int:
    harnesses: dict[int, harness_py.Harness] = {}
    base_holder: list[Path] = []

    def visit(client: lsp.LspClient, g: graph.Graph, flows: list[graph.Flow]) -> None:
        if client.config.language_id != "python":
            return
        root = client.root
        if not base_holder:
            base_holder.append(worktree.base_worktree(root, args.ref or "HEAD", args.clean_base))
        graph.open_test_files(client, g)
        for flow in flows:
            if not flow.removed_only:
                harnesses[id(flow)] = harness_py.build(client, root, base_holder[0], flow, g)

    analysis = cli.analyse(args, visit)
    if isinstance(analysis, int):
        return analysis
    root = analysis.root
    shown = [f for f in analysis.flows if not f.removed_only]
    for flow in shown:
        if flow.language != "python":
            analysis.warnings.append(f"{flow.language} flows are analysed only; play is Python-only for now")
    if not harnesses:
        cli.render_all(analysis, args.full, args.list_tests)
        return cli.EXIT_NOTHING

    out_dir = run_dir(root)
    interpreter = env.python_interpreter(root)
    index: list[dict] = []
    diverged = False
    for i, flow in enumerate(shown, 1):
        print(render.render_flow(i, len(shown), flow, args.full, args.list_tests))
        if id(flow) not in harnesses:
            print()
            continue
        harness = harnesses[id(flow)]
        harness_path = out_dir / f"flow{i}.py"
        harness_path.write_text(harness.source)
        analysis.warnings += harness.warnings
        frames = [harness_py.frame_id(root, f) for f in flow.frames if f.status != "slot"]
        traces = {side: out_dir / f"flow{i}.{side}.jsonl" for side in ("base", "head")}
        shared, changed_tests = shared_tests(base_holder[0], root, flow.tests) if flow.tests else ([], [])
        if harness.complete:
            def drive(side: str, tree: Path) -> str | None:
                return run_harness(interpreter, harness_path, tree, side, traces[side], args.run_timeout)
        elif shared:
            print(f"no call site with literal arguments; driven by {len(shared)} covering test(s) present on both sides")
            if changed_tests:
                analysis.warnings.append(f"the driving tests changed in this diff ({', '.join(changed_tests)}); "
                                         "a divergence may reflect the inputs rather than the code")

            def drive(side: str, tree: Path) -> str | None:
                return run_traced_tests(interpreter, tree, shared, frames, side, traces[side], args.run_timeout)
        else:
            print("no call site with literal arguments and no covering test present on both sides; "
                  f"fill the slots in {harness_path} — harness not run")
            cli.render_all(cli.Analysis(root, analysis.revs, [], analysis.warnings), args.full)
            return cli.EXIT_NOTHING
        for side, tree in (("base", base_holder[0]), ("head", root)):
            failure = drive(side, tree)
            if failure:
                print(failure, file=sys.stderr)
                return cli.EXIT_TOOL_ERROR
        report = compare.Report(frames, compare.load(traces["base"]), compare.load(traces["head"]))
        print(compare.verdict(report))
        diverged = diverged or bool(report.differing())
        for frame in report.traced()[:args.depth]:
            print(compare.show(report, frame))
        if flow.tests:
            print("\n".join(test_delta(interpreter, base_holder[0], root, flow.tests, args.run_timeout)))
        index.append({"flow": i, "frames": frames, "base": str(traces["base"]), "head": str(traces["head"])})
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
    shown = 0
    for entry in json.loads(index_path.read_text()):
        report = compare.Report(entry["frames"], compare.load(Path(entry["base"])), compare.load(Path(entry["head"])))
        for frame in compare.resolve(report, query):
            print(compare.show(report, frame, arg or None))
            shown += 1
    if not shown:
        print(f"{query}: not a frame of the last play", file=sys.stderr)
        return cli.EXIT_NOTHING
    return cli.EXIT_OK
