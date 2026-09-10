"""flowdiff [ref]: changed symbols → flows → rendered graph. Exit 0 rendered, 1 tool error, 2 nothing to show."""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

from . import changes, graph, lsp, render

EXIT_OK, EXIT_TOOL_ERROR, EXIT_NOTHING = 0, 1, 2
REQUIRED_BINARIES = {"git": "apt install git", "ast-grep": "cargo install ast-grep or a release binary",
                     "graph-easy": "apt install libgraph-easy-perl"}
SERVER_HINTS = {"clangd": "apt install clangd", "pyright-langserver": "npm install -g pyright"}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="flowdiff",
        description="changed symbols → flows → rendered graph. Exit 0 rendered, 1 tool error, 2 nothing to show.")
    parser.add_argument("ref", nargs="?", help="base revision; default compares the working tree to HEAD")
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--hops", type=int, default=3)
    parser.add_argument("--no-tests", action="store_true")
    parser.add_argument("--full", action="store_true", help="draw the graph even when it is large")
    parser.add_argument("--timeout", type=float, default=60.0, help="language server request timeout")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    missing = [f"{b}: {hint}" for b, hint in REQUIRED_BINARIES.items() if shutil.which(b) is None]
    if missing:
        print("missing tools:\n  " + "\n  ".join(missing), file=sys.stderr)
        return EXIT_TOOL_ERROR

    try:
        root = Path(changes.git(args.repo, "rev-parse", "--show-toplevel").strip())
    except subprocess.CalledProcessError:
        print(f"{args.repo}: not a git repository", file=sys.stderr)
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

    all_flows: list[graph.Flow] = []
    warnings: list[str] = []
    for config, server_hunks in by_server.items():
        client = lsp.LspClient(config, root, timeout=args.timeout)
        try:
            changed = changes.changed_symbols(client, revs, server_hunks, config.ast_grep_language)
            if not changed:
                continue
            g = graph.build_graph(client, changed, args.hops)
            warnings += g.warnings
            all_flows += graph.flows(client, g, changed, args.hops, not args.no_tests)
        except lsp.LspError as err:
            print(f"language server: {err}", file=sys.stderr)
            return EXIT_TOOL_ERROR
        finally:
            client.close()

    if not all_flows:
        print("changed hunks touch no symbols (formatting or comments only)")
        return EXIT_NOTHING

    for i, flow in enumerate(all_flows, 1):
        print(render.render_flow(i, len(all_flows), flow, args.full))
        if i < len(all_flows):
            print()
    for w in warnings:
        print(f"warning: {w}", file=sys.stderr)
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
