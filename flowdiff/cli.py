"""flowdiff [ref] renders the changed flows; play runs them on both revisions; show reads the recorded
values. Exit 0 rendered or ran, 1 tool error, 2 nothing to show or no harness ran, 3 divergence under
--fail-on-diff."""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import changes, graph, lsp, render

EXIT_OK, EXIT_TOOL_ERROR, EXIT_NOTHING, EXIT_DIFF = 0, 1, 2, 3
REQUIRED_BINARIES = {"git": "apt install git", "ast-grep": "cargo install ast-grep or a release binary",
                     "graph-easy": "apt install libgraph-easy-perl"}
SERVER_HINTS = {"clangd": "apt install clangd", "pyright-langserver": "npm install -g pyright"}
VERBS = ("play", "show")

Visitor = Callable[[lsp.LspClient, graph.Graph, list[graph.Flow]], None]


@dataclass
class Analysis:
    root: Path
    revs: changes.Revisions
    flows: list[graph.Flow] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("ref", nargs="?", help="base revision; default compares the working tree to HEAD")
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--hops", type=int, default=3)
    parser.add_argument("--no-tests", action="store_true")
    parser.add_argument("--full", action="store_true", help="draw the graph even when it is large")
    parser.add_argument("--timeout", type=float, default=60.0, help="language server request timeout")


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
    parser.add_argument("frame", help="frame name, path:name, or frame/argument")
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


def analyse(args: argparse.Namespace, visit: Visitor | None = None) -> Analysis | int:
    if (code := check_tools()) != EXIT_OK:
        return code
    root = repo_root(args.repo)
    if root is None:
        return EXIT_TOOL_ERROR

    revs = changes.Revisions(root, args.ref or "HEAD", None if args.ref is None else "HEAD")
    hunks = changes.diff_hunks(revs)
    if not hunks:
        print("no changes between", revs.base, "and", "working tree" if revs.head is None else revs.head)
        return EXIT_NOTHING

    by_server: dict[lsp.ServerConfig, list[changes.Hunk]] = {}
    for h in hunks:
        config = lsp.server_for(h.path, root)
        if config is not None:
            by_server.setdefault(config, []).append(h)
    if not by_server:
        print("no changed files in a supported language")
        return EXIT_NOTHING

    unavailable = [f"{c.binary}: {SERVER_HINTS[c.binary]}" for c in by_server if shutil.which(c.binary) is None]
    if unavailable:
        print("missing language servers:\n  " + "\n  ".join(unavailable), file=sys.stderr)
        return EXIT_TOOL_ERROR

    analysis = Analysis(root, revs)
    for config, server_hunks in by_server.items():
        client = lsp.LspClient(config, root, timeout=args.timeout)
        try:
            changed = changes.changed_symbols(client, revs, server_hunks, config.ast_grep_language)
            if not changed:
                continue
            g = graph.build_graph(client, changed, args.hops)
            flows = graph.flows(client, g, changed, args.hops, not args.no_tests)
            if visit is not None:
                visit(client, g, flows)
            analysis.flows += flows
            analysis.warnings += g.warnings
        except lsp.LspError as err:
            print(f"language server: {err}", file=sys.stderr)
            return EXIT_TOOL_ERROR
        finally:
            client.close()

    if not analysis.flows:
        print("changed hunks touch no symbols (formatting or comments only)")
        return EXIT_NOTHING
    return analysis


def render_all(analysis: Analysis, full: bool) -> None:
    for i, flow in enumerate(analysis.flows, 1):
        print(render.render_flow(i, len(analysis.flows), flow, full))
        if i < len(analysis.flows):
            print()
    for w in analysis.warnings:
        print(f"warning: {w}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] in VERBS:
        from . import play
        if argv[0] == "play":
            return play.run(build_play_parser().parse_args(argv[1:]))
        return play.show(build_show_parser().parse_args(argv[1:]))
    args = build_parser().parse_args(argv)
    analysis = analyse(args)
    if isinstance(analysis, int):
        return analysis
    render_all(analysis, args.full)
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
