"""A Python harness for one flow: import the entry's module, call it with arguments lifted from a
real call site (decisions 18, 19), or one body per revision when no stable entry exists (16)."""
from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

from .changes import git
from .graph import Flow, Graph, Node, is_test_path
from .lsp import LspClient, Position

HARNESS = """\
import os
import sys

sys.path[:0] = [os.environ["FLOWDIFF_TOOL"]]
from flowdiff.trace_py import trace

{imports}
with trace(os.environ["FLOWDIFF_TREE"], {frames!r}, os.environ["FLOWDIFF_OUT"]):
{body}
"""


@dataclass(frozen=True)
class CallSite:
    path: Path
    line: int
    args: tuple[str, ...]
    kwargs: tuple[tuple[str, str], ...]

    def render(self) -> str:
        return ", ".join([*self.args, *(f"{k}={v}" for k, v in self.kwargs)])


@dataclass(frozen=True)
class Harness:
    source: str
    complete: bool
    warnings: tuple[str, ...] = ()


def module_name(root: Path, path: Path) -> str:
    parts = list(path.resolve().relative_to(root.resolve()).with_suffix("").parts)
    if len(parts) > 1 and parts[0] == "src":
        del parts[0]
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def frame_id(root: Path, node: Node) -> str:
    return f"{node.path.resolve().relative_to(root.resolve()).as_posix()}:{node.name}"


def callee_name(func: ast.expr) -> str | None:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def literal_call(text: str, line: int, name: str, path: Path) -> CallSite | None:
    """The call to `name` whose callee sits on 0-based `line`, when ast.literal_eval accepts every argument."""
    try:
        module = ast.parse(text)
    except SyntaxError:
        return None
    for node in ast.walk(module):
        if not isinstance(node, ast.Call) or callee_name(node.func) != name:
            continue
        if not (node.func.lineno - 1 <= line <= (node.func.end_lineno or node.func.lineno) - 1):
            continue
        try:
            for arg in node.args:
                ast.literal_eval(arg)
            for kw in node.keywords:
                if kw.arg is None:
                    raise ValueError("**kwargs")
                ast.literal_eval(kw.value)
        except ValueError:
            continue
        return CallSite(path, line, tuple(ast.unparse(a) for a in node.args),
                        tuple((kw.arg or "", ast.unparse(kw.value)) for kw in node.keywords))
    return None


def parameters(text: str, line: int, name: str) -> list[str]:
    try:
        module = ast.parse(text)
    except SyntaxError:
        return []
    for node in ast.walk(module):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name \
                and node.lineno - 1 <= line <= (node.end_lineno or node.lineno) - 1:
            names = [a.arg for a in [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]]
            return [n for n in names if n not in ("self", "cls")]
    return []


def ranked(root: Path, sites: list[tuple[Path, int]]) -> list[tuple[Path, int]]:
    return sorted(sites, key=lambda s: (not is_test_path(s[0], root), str(s[0]), s[1]))


def harvest(client: LspClient, root: Path, target: Node, callers: list[Node]) -> CallSite | None:
    for caller in callers:
        client.open(caller.path, caller.path.read_text(encoding="utf-8", errors="replace"))
    refs = client.references(target.path, Position(target.line, target.col))
    for path, line in ranked(root, [(loc.path, loc.range.start.line) for loc in refs]):
        site = literal_call(path.read_text(encoding="utf-8", errors="replace"), line, target.name, path)
        if site:
            return site
    return None


def harvest_up(client: LspClient, root: Path, g: Graph, entry: Node) -> tuple[Node, CallSite] | None:
    """The entry's own call sites first, then its callers level by level; the frame that yields a literal drives the flow."""
    level, seen = [entry], {entry.id}
    while level:
        for node in level:
            callers = [g.nodes[c] for c in sorted(g.callers(node.id)) if g.nodes[c].status != "slot"]
            site = harvest(client, root, node, callers)
            if site:
                return node, site
        above: list[Node] = []
        for node in level:
            for caller in sorted(g.callers(node.id)):
                if caller not in seen and g.nodes[caller].status != "slot":
                    seen.add(caller)
                    above.append(g.nodes[caller])
        level = above
    return None


def harvest_in_tree(tree: Path, name: str) -> CallSite | None:
    """Base-side harvest without a language server: every call to `name` in the tree's own files."""
    candidates: list[tuple[Path, int]] = []
    for rel in git(tree, "ls-files", "--", "*.py").splitlines():
        path = tree / rel
        try:
            module = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except (SyntaxError, OSError):
            continue
        candidates += [(path, n.func.lineno - 1) for n in ast.walk(module)
                       if isinstance(n, ast.Call) and callee_name(n.func) == name]
    for path, line in ranked(tree, candidates):
        site = literal_call(path.read_text(encoding="utf-8", errors="replace"), line, name, path)
        if site:
            return site
    return None


def call_line(root: Path, node: Node, site: CallSite | None) -> tuple[str, bool]:
    module = module_name(root, node.path)
    if site is not None:
        return f"{module}.{node.name}({site.render()})", True
    text = node.path.read_text(encoding="utf-8", errors="replace")
    slots = ", ".join(f"{p}=<{p}>" for p in parameters(text, node.line, node.name))
    return f"{module}.{node.name}({slots})", False


def build(client: LspClient, root: Path, base: Path, flow: Flow, g: Graph) -> Harness:
    frames = [frame_id(root, f) for f in flow.frames if f.status != "slot"]
    if flow.entry is None:
        return two_body(client, root, base, flow, frames)
    found = harvest_up(client, root, g, flow.entry)
    driver, site = found if found else (flow.entry, None)
    line, complete = call_line(root, driver, site)
    imports = f"import {module_name(root, driver.path)}\n"
    warnings = () if driver is flow.entry else \
        (f"{flow.entry.name}: driven from {driver.name}, the nearest caller with a literal call site",)
    return Harness(HARNESS.format(imports=imports, frames=frames, body=f"    {line}"), complete, warnings)


def two_body(client: LspClient, root: Path, base: Path, flow: Flow, frames: list[str]) -> Harness:
    targets = [n for n in flow.changed if n.status not in ("removed", "slot")]
    modules = sorted({module_name(root, n.path) for n in targets})
    head_lines, base_lines, warnings = [], [], []
    complete = bool(targets)
    for node in targets:
        head_site = harvest(client, root, node, [])
        base_site = harvest_in_tree(base, node.name)
        if head_site and base_site and (head_site.args, head_site.kwargs) != (base_site.args, base_site.kwargs):
            warnings.append(f"{node.name}: base and head harvested different arguments; "
                            "a divergence may reflect the inputs rather than the code")
        head_line, head_ok = call_line(root, node, head_site)
        base_line, base_ok = call_line(root, node, base_site)
        complete = complete and head_ok and base_ok
        head_lines.append(head_line)
        base_lines.append(base_line)
    body = "    if os.environ[\"FLOWDIFF_SIDE\"] == \"base\":\n" \
        + "".join(f"        {l}\n" for l in base_lines or ["pass"]) + "    else:\n" \
        + "".join(f"        {l}\n" for l in head_lines or ["pass"])
    imports = "".join(f"import {m}\n" for m in modules)
    return Harness(HARNESS.format(imports=imports, frames=frames, body=body.rstrip("\n")), complete,
                   tuple(warnings))
