"""flowdiff [ref] renders the changed flows; play runs them on both revisions; show reads the recorded
values. Exit 0 rendered or ran, 1 tool error, 2 nothing to show or no harness ran, 3 divergence under
--fail-on-diff."""
from __future__ import annotations

import argparse
import shutil
from collections import Counter
import subprocess
import sys
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable

from . import changes, container, graph, lsp, render, worktree

EXIT_OK, EXIT_TOOL_ERROR, EXIT_NOTHING, EXIT_DIFF = 0, 1, 2, 3
REQUIRED_BINARIES = {"git": "apt install git", "ast-grep": "cargo install ast-grep or a release binary",
                     "graph-easy": "apt install libgraph-easy-perl"}
SERVER_HINTS = {"clangd": "apt install clangd", "pyright-langserver": "npm install -g pyright"}
VERBS = ("play", "show", "keep")


@dataclass
class Analysis:
    root: Path
    revs: changes.Revisions
    flows: list[graph.Flow] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    container: container.Container | None = None
    # The same flows on the base revision, by position, when --split asked for them (decision 37).
    base_flows: list[graph.Flow] = field(default_factory=list)


Visitor = Callable[[lsp.LspClient, graph.Graph, list[graph.Flow], Analysis], None]


def add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("ref", nargs="?", help="base revision, or base..head; default compares the working tree to HEAD")
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--hops", type=int, default=3)
    parser.add_argument("--no-tests", action="store_true")
    parser.add_argument("--list-tests", action="store_true", help="name the covering tests, not only their count")
    parser.add_argument("--full", action="store_true", help="draw the graph even when it is large")
    parser.add_argument("--split", action="store_true", help="draw the base revision's graph beside the head's")
    parser.add_argument("--timeout", type=float, default=60.0, help="language server request timeout")
    parser.add_argument("--image", help="the project's dev image for C++ (default: <repo>:dev when it exists)")
    parser.add_argument("--build-timeout", type=float, default=1800.0, help="seconds for a build inside the container")
    parser.add_argument("--no-build", action="store_true", help="use the build directories as they are; never build")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="flowdiff", description=__doc__)
    add_common(parser)
    return parser


def build_play_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="flowdiff play",
                                     description="run every flow's harness on base and head, diff the traces")
    add_common(parser)
    parser.add_argument("--depth", type=int, default=0, help="frames whose values are printed inline")
    parser.add_argument("--clean-base", action="store_true", help="recreate the .flowdiff/base worktree")
    parser.add_argument("--fail-on-diff", action="store_true", help=f"exit {EXIT_DIFF} when values diverge")
    parser.add_argument("--run-timeout", type=float, default=300.0, help="seconds per harness or test run")
    return parser


def build_show_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="flowdiff show", description="values recorded by the last play")
    parser.add_argument("frame", help="frame, frame/leaf.path, frame#N for one call whole, frame#N/leaf.path")
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    return parser


def build_keep_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="flowdiff keep",
                                     description="write tests/flow_<frame>.py from the last play's recorded calls")
    parser.add_argument("frame", nargs="?", help="frame to keep; default is the frame the harness called")
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    return parser


def check_tools() -> int:
    missing = [f"{b}: {hint}" for b, hint in REQUIRED_BINARIES.items() if shutil.which(b) is None]
    if missing:
        print("missing tools:\n  " + "\n  ".join(missing), file=sys.stderr)
        return EXIT_TOOL_ERROR
    return EXIT_OK


def repo_root(repo: Path) -> Path | None:
    try:
        return Path(changes.git(repo, "rev-parse", "--show-toplevel").strip())
    except subprocess.CalledProcessError:
        print(f"{repo}: not a git repository", file=sys.stderr)
        return None


def revisions(root: Path, ref: str | None) -> changes.Revisions:
    """No ref: the working tree against HEAD. A: HEAD against A. A..B: B against A."""
    if ref is None:
        return changes.Revisions(root, "HEAD", None)
    base, sep, head = ref.partition("..")
    if not sep:
        return changes.Revisions(root, ref, "HEAD")
    return changes.Revisions(root, base or "HEAD", head or "HEAD")


def head_checkout(root: Path, revs: changes.Revisions) -> tuple[Path, changes.Revisions]:
    """A..B with both resolved in the main repo (inside the worktree HEAD~N would count from B), B checked out."""
    base, head = (changes.git(root, "rev-parse", "--verify", f"{r}^{{commit}}").strip() for r in (revs.base, revs.head))
    tree = worktree.head_worktree(root, head)
    return tree, changes.Revisions(tree, base, head)


def analyse(args: argparse.Namespace, visit: Visitor | None = None) -> Analysis | int:
    if (code := check_tools()) != EXIT_OK:
        return code
    root = repo_root(args.repo)
    if root is None:
        return EXIT_TOOL_ERROR

    revs = revisions(root, args.ref)
    try:
        if ".." in (args.ref or ""):
            root, revs = head_checkout(root, revs)
        hunks = changes.diff_hunks(revs)
    except worktree.WorktreeError as err:
        print(err, file=sys.stderr)
        return EXIT_TOOL_ERROR
    except subprocess.CalledProcessError as err:
        detail = (err.stderr or "").splitlines()
        reason = next((line.removeprefix("fatal: ") for line in detail if line.startswith("fatal:")), "failed")
        print(f"git diff {revs.base}: {reason}", file=sys.stderr)
        return EXIT_TOOL_ERROR
    if not hunks:
        print("no changes between", revs.base, "and", "working tree" if revs.head is None else revs.head)
        return EXIT_NOTHING

    # A changed test is a covering test (decision 39), never a frame: pytest is its only caller.
    source_hunks = [h for h in hunks if not graph.is_test_path(root / h.path, root)]
    ctr = None
    if any(h.path.suffix in lsp.CPP_EXTENSIONS for h in source_hunks):
        try:
            ctr = container.detect(root, args.image)
            if ctr is not None:
                ctr = replace(ctr, packages=container.packages_of(root, [h.path for h in source_hunks]))
            if ctr is not None and not args.no_build:
                print(f"building the working tree in {ctr.image}", file=sys.stderr)
                failure = container.build(ctr, root, args.build_timeout)
                if failure:
                    print(failure, file=sys.stderr)
                    return EXIT_TOOL_ERROR
        except container.ContainerError as err:
            print(f"container: {err}", file=sys.stderr)
            return EXIT_TOOL_ERROR
    by_server: dict[lsp.ServerConfig, list[changes.Hunk]] = {}
    for h in source_hunks:
        config = lsp.server_for(h.path, root, ctr)
        if config is not None:
            by_server.setdefault(config, []).append(h)
    if not by_server:
        print("only test files changed" if not source_hunks else "no changed files in a supported language")
        return EXIT_NOTHING

    unavailable = [f"{c.binary}: {SERVER_HINTS[c.binary]}" for c in by_server
                   if not c.command_prefix and shutil.which(c.binary) is None]
    if unavailable:
        print("missing language servers:\n  " + "\n  ".join(unavailable), file=sys.stderr)
        return EXIT_TOOL_ERROR

    analysis = Analysis(root, revs, container=ctr)
    for config, server_hunks in by_server.items():
        client = lsp.LspClient(config, root, timeout=args.timeout)
        try:
            changed = changes.changed_symbols(client, revs, server_hunks, config.ast_grep_language)
            if not changed:
                continue
            # clangd loads the compile database and starts indexing at the first didOpen, which changed_symbols sent.
            if config.language_id == "cpp" and not client.wait_for_index(args.build_timeout):
                analysis.warnings.append("clangd was still indexing when asked; callers and tests may be incomplete")
            g = graph.build_graph(client, changed, args.hops)
            flows = graph.flows(client, g, changed, args.hops, not args.no_tests)
            if visit is not None:
                visit(client, g, flows, analysis)
            analysis.flows += flows
            analysis.warnings += g.warnings
            if args.split:
                analysis.base_flows += base_side(analysis, config, changed, flows, args)
        except lsp.LspError as err:
            print(f"language server: {err}", file=sys.stderr)
            return EXIT_TOOL_ERROR
        except worktree.WorktreeError as err:
            print(err, file=sys.stderr)
            return EXIT_TOOL_ERROR
        finally:
            client.close()

    if not analysis.flows:
        print("changed hunks touch no symbols (formatting or comments only)")
        return EXIT_NOTHING
    return analysis


def base_side(analysis: Analysis, config: lsp.ServerConfig, changed: list[changes.ChangedSymbol],
              flows: list[graph.Flow], args: argparse.Namespace) -> list[graph.Flow]:
    """The head flows rebuilt on the base worktree with a server rooted there, one per head flow by position."""
    base = worktree.base_worktree(analysis.root, analysis.revs.base)
    if analysis.container is not None and not args.no_build:
        failure = container.build(analysis.container, base, args.build_timeout)
        if failure:
            analysis.warnings.append(f"base graph skipped: {failure.splitlines()[0]}")
            return []
    sample = next(iter(config.extensions))
    base_config = lsp.server_for(Path(f"x{sample}"), base, analysis.container)
    if base_config is None:
        return []
    client = lsp.LspClient(base_config, base, timeout=args.timeout)
    try:
        base_changed = changes.base_symbols(client, analysis.root, base, changed)
        if not base_changed:
            return []
        if config.language_id == "cpp":
            client.wait_for_index(args.build_timeout)
        g = graph.build_graph(client, base_changed, args.hops)
        base_flows = graph.flows(client, g, base_changed, args.hops, False)
    finally:
        client.close()
    by_name = {frozenset(n.name for n in f.changed): f for f in base_flows}
    return [by_name.get(frozenset(n.name for n in f.changed if n.status != "added"),
                        graph.Flow([], None, [], [], True, [], f.language)) for f in flows]


def render_all(analysis: Analysis, full: bool, list_tests: bool = False) -> None:
    shown = [f for f in analysis.flows if not f.removed_only]
    bases = [b for f, b in zip(analysis.flows, analysis.base_flows) if not f.removed_only]
    for i, flow in enumerate(shown, 1):
        if i <= len(bases):
            base = bases[i - 1]
            left = render.render_graph(base) if base.frames else "(not in the base revision)"
            print(f"flow {i}/{len(shown)}")
            print(render.split_view(left, render.render_graph(flow)))
            print(render.verdict(flow))
            if list_tests and flow.tests:
                print("\n".join(f"  {t}" for t in flow.tests))
        else:
            print(render.render_flow(i, len(shown), flow, full, list_tests))
        if i < len(shown):
            print()
    removed = [f for f in analysis.flows if f.removed_only]
    if removed:
        print(("\n" if shown else "") + render.render_removed(removed))
    sys.stdout.flush()
    for w, n in Counter(analysis.warnings).items():
        print(f"warning: {w}" + (f" (×{n})" if n > 1 else ""), file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] in VERBS:
        from . import keep, play
        if argv[0] == "play":
            return play.run(build_play_parser().parse_args(argv[1:]))
        if argv[0] == "keep":
            return keep.run(build_keep_parser().parse_args(argv[1:]))
        return play.show(build_show_parser().parse_args(argv[1:]))
    args = build_parser().parse_args(argv)
    analysis = analyse(args)
    if isinstance(analysis, int):
        return analysis
    render_all(analysis, args.full, args.list_tests)
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
