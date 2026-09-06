"""Terminal rendering: one merged graph-easy boxart graph per flow, with an indented-tree fallback."""
from __future__ import annotations

import shutil
import subprocess

from .graph import Flow, Node

DASHED = "- - >"
SOLID = "->"
MAX_GRAPH_NODES = 24
MAX_NAMES_IN_VERDICT = 6


def label(node: Node) -> str:
    text = f"{node.name}{node.marker}"
    return text.replace("]", ")").replace("[", "(").replace("#", "").replace("|", "/")


def graph_easy_source(flow: Flow) -> str:
    lines = [f"[ {label(n)} ]" for n in flow.frames if n.id not in {e.src for e in flow.edges} | {e.dst for e in flow.edges}]
    by_id = {n.id: n for n in flow.frames}
    for e in flow.edges:
        arrow = DASHED if e.kind == "registered" else SOLID
        lines.append(f"[ {label(by_id[e.src])} ] {arrow} [ {label(by_id[e.dst])} ]")
    return "\n".join(lines) + "\n"


def render_graph(flow: Flow, timeout: int = 10) -> str:
    if shutil.which("graph-easy") is None:
        return indented_tree(flow)
    proc = subprocess.run(["graph-easy", "--as=boxart", f"--timeout={timeout}"],
                          input=graph_easy_source(flow), capture_output=True, text=True)
    if proc.returncode != 0 or not proc.stdout.strip():
        return indented_tree(flow) + "\n(graph-easy layout aborted; showing tree)"
    return proc.stdout.rstrip("\n")


def indented_tree(flow: Flow) -> str:
    by_id = {n.id: n for n in flow.frames}
    children: dict[str, list[str]] = {}
    for e in flow.edges:
        children.setdefault(e.src, []).append(e.dst)
    roots = [n.id for n in flow.frames if not any(e.dst == n.id for e in flow.edges)]
    out: list[str] = []

    def walk(nid: str, depth: int, seen: set[str]) -> None:
        out.append("  " * depth + label(by_id[nid]))
        if nid in seen:
            return
        for c in sorted(children.get(nid, []), key=lambda i: by_id[i].name):
            walk(c, depth + 1, seen | {nid})

    for r in roots or [n.id for n in flow.frames[:1]]:
        walk(r, 0, set())
    return "\n".join(out)


def verdict(flow: Flow) -> str:
    names = [f"{n.name}{n.marker}" for n in flow.changed]
    shown = ", ".join(names[:MAX_NAMES_IN_VERDICT])
    if len(names) > MAX_NAMES_IN_VERDICT:
        shown += f" … (+{len(names) - MAX_NAMES_IN_VERDICT})"
    if all(n.status == "added" for n in flow.changed) and flow.entry is None:
        head = f"{len(names)} new symbols, nothing older to enter from: {shown}"
    elif flow.entry is None:
        head = f"no stable entry within {len(flow.frames)} frames of {shown}; two-body harness required"
    else:
        head = f"entry {flow.entry.name}  ({len(flow.frames)} frames, {len(flow.changed)} changed: {shown})"
    tests = f"covered by {len(flow.tests)} test(s)" if flow.tests else "no covering tests"
    return f"{head}\n{tests}"


def changed_by_file(flow: Flow) -> str:
    groups: dict[str, list[str]] = {}
    for n in flow.changed:
        groups.setdefault(str(n.path), []).append(f"{n.name}{n.marker}")
    width = max(len(p) for p in groups)
    return "\n".join(f"  {p:<{width}}  {' '.join(names)}" for p, names in sorted(groups.items()))


def render_flow(index: int, total: int, flow: Flow, full: bool = False) -> str:
    title = f"flow {index}/{total}"
    if len(flow.frames) > MAX_GRAPH_NODES and not full:
        body = f"{changed_by_file(flow)}\n  ({len(flow.frames)} frames; graph omitted above {MAX_GRAPH_NODES}, --full draws it)"
    else:
        body = render_graph(flow)
    tests = "\n".join(f"  {t}" for t in flow.tests)
    parts = [title, body, verdict(flow)]
    if tests:
        parts.append(tests)
    return "\n".join(parts)
