"""Call graph around the changed symbols, flows as connected components, entry selection."""
from __future__ import annotations

import json
import subprocess
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .changes import ChangedSymbol, git, scan_kinds
from .lsp import LspClient, Position, Symbol, item_symbol

HINTS_DIR = Path(__file__).parent / "hints"
TEST_DIR_NAMES = frozenset({"test", "tests", "spec", "specs"})
MAX_TEST_FILES_OPENED = 200


@dataclass(frozen=True)
class Node:
    id: str
    name: str
    path: Path
    line: int
    col: int
    status: str = "unchanged"  # unchanged | body | signature | added | removed | slot
    namespaces: tuple[str, ...] = ()

    @property
    def qualified(self) -> str:
        return "::".join([*self.namespaces, self.name])

    @property
    def marker(self) -> str:
        return {"body": "~", "signature": "~", "added": "+", "removed": "-"}.get(self.status, "")

    @property
    def stable(self) -> bool:
        return self.status in ("unchanged", "body")


@dataclass(frozen=True)
class Edge:
    src: str
    dst: str
    kind: str = "call"  # call | registered


@dataclass
class Graph:
    nodes: dict[str, Node] = field(default_factory=dict)
    edges: set[Edge] = field(default_factory=set)
    warnings: list[str] = field(default_factory=list)

    def add(self, node: Node) -> Node:
        return self.nodes.setdefault(node.id, node)

    def callers(self, nid: str) -> set[str]:
        return {e.src for e in self.edges if e.dst == nid}

    def callees(self, nid: str) -> set[str]:
        return {e.dst for e in self.edges if e.src == nid}


@dataclass
class Flow:
    changed: list[Node]
    entry: Node | None
    frames: list[Node]
    edges: list[Edge]
    two_body: bool
    tests: list[str]
    language: str = ""

    @property
    def removed_only(self) -> bool:
        return all(n.status == "removed" for n in self.changed)


def node_of(sym: Symbol, status: str = "unchanged") -> Node:
    return Node(sym.id, sym.name, sym.path, sym.selection.start.line, sym.selection.start.character, status,
                sym.namespaces)


def build_graph(client: LspClient, changed: list[ChangedSymbol], hops: int) -> Graph:
    g = Graph()
    items: dict[str, dict[str, Any]] = {}
    frontier: deque[tuple[str, int]] = deque()

    for c in changed:
        node = g.add(node_of(c.symbol, c.status))
        if c.status == "removed":
            continue
        prepared = client.prepare_call_hierarchy(c.symbol.path, c.symbol.selection.start)
        if prepared:
            items[node.id] = prepared[0]
            frontier.append((node.id, 0))

    seen: set[str] = set()
    while frontier:
        nid, depth = frontier.popleft()
        if nid in seen or depth >= hops:
            continue
        seen.add(nid)
        item = items[nid]
        for call in client.incoming_calls(item):
            sym = item_symbol(call["from"])
            if not sym.path.is_relative_to(client.root):
                continue
            src = g.add(node_of(sym))
            g.edges.add(Edge(src.id, nid))
            items.setdefault(src.id, call["from"])
            frontier.append((src.id, depth + 1))
        for call in client.outgoing_calls(item):
            sym = item_symbol(call["to"])
            if not sym.path.is_relative_to(client.root):
                continue
            dst = g.add(node_of(sym))
            g.edges.add(Edge(nid, dst.id))
            items.setdefault(dst.id, call["to"])
            frontier.append((dst.id, depth + 1))

    add_hint_edges(client, g)
    if "callHierarchy/outgoingCalls" in getattr(client, "unsupported", ()):
        add_textual_callees(client, g, changed)
        g.warnings.append(f"{client.config.binary} has no callHierarchy/outgoingCalls; "
                          "callees come from call expressions in the changed bodies")
    for c in changed:
        if c.symbol.is_function and not c.symbol.nested and c.status != "removed" and not g.callers(c.symbol.id):
            g.warnings.append(f"{c.symbol.name}: no caller found — add a hint rule?")
    return g


def containment_links(changed: list[ChangedSymbol]) -> list[tuple[str, str]]:
    """A changed class and the changed methods inside it belong to one flow even without call edges."""
    links = []
    for outer in changed:
        for inner in changed:
            if outer is not inner and outer.symbol.path == inner.symbol.path \
                    and outer.symbol.range.overlaps_lines(inner.symbol.selection.start.line,
                                                          inner.symbol.selection.start.line) \
                    and outer.symbol.range != inner.symbol.range:
                links.append((outer.symbol.id, inner.symbol.id))
    return links


def add_hint_edges(client: LspClient, g: Graph) -> None:
    """ast-grep hint matches become edges only when the RHS resolves to a graph node."""
    by_name: dict[str, list[Node]] = {}
    for n in g.nodes.values():
        by_name.setdefault(n.name, []).append(n)
    if not by_name:
        return
    for rule in sorted(HINTS_DIR.glob("*.yml")):
        if f"language: {client.config.ast_grep_language}" not in rule.read_text():
            continue
        for match in run_ast_grep(rule, client.root):
            fn = match["metaVariables"]["single"].get("FN")
            slot = match["metaVariables"]["single"].get("SLOT")
            if not fn or not slot or fn["text"] not in by_name:
                continue
            path = Path(match["file"])
            path = path if path.is_absolute() else client.root / path
            pos = Position(fn["range"]["start"]["line"], fn["range"]["start"]["column"])
            client.open(path, path.read_text(encoding="utf-8", errors="replace"))
            for loc in client.definition(path, pos):
                for target in by_name[fn["text"]]:
                    if loc.path == target.path and loc.range.start.line == target.line:
                        slot_node = g.add(Node(f"slot:{slot['text']}", f"->{slot['text']}", path,
                                               slot["range"]["start"]["line"],
                                               slot["range"]["start"]["column"], "slot"))
                        g.edges.add(Edge(slot_node.id, target.id, "registered"))


CALL_KINDS = {"cpp": ("call_expression",), "python": ("call",)}


def add_textual_callees(client: LspClient, g: Graph, changed: list[ChangedSymbol]) -> None:
    """Direct callees of each changed body by ast-grep call expressions resolved through workspace/symbol;
    the fallback when the server cannot answer outgoingCalls (clangd before 17)."""
    language = client.config.ast_grep_language
    for c in changed:
        if c.status == "removed" or not c.symbol.is_function:
            continue
        text = c.symbol.path.read_text(encoding="utf-8", errors="replace")
        body = c.symbol.range.slice(text)
        names: list[str] = []
        for match in scan_kinds(language, c.symbol.path.suffix, body, CALL_KINDS.get(language, ())):
            callee = match["text"].split("(", 1)[0].strip().rsplit("::", 1)[-1].rsplit(".", 1)[-1].rsplit("->", 1)[-1]
            if callee.isidentifier() and callee not in names and callee != c.symbol.name.rsplit("::", 1)[-1]:
                names.append(callee)
        for name in names:
            for sym in client.workspace_symbols(name):
                if sym.name.rsplit("::", 1)[-1] == name and sym.is_function and sym.path.is_relative_to(client.root) \
                        and not is_test_path(sym.path, client.root):
                    dst = g.add(node_of(sym))
                    g.edges.add(Edge(c.symbol.id, dst.id))
                    break


def run_ast_grep(rule: Path, root: Path) -> list[dict[str, Any]]:
    out = subprocess.run(["ast-grep", "scan", "--rule", str(rule), "--json=compact", str(root)],
                         capture_output=True, text=True)
    if out.returncode not in (0, 1) or not out.stdout.strip():
        return []
    return json.loads(out.stdout)


def components(g: Graph, changed_ids: list[str], links: list[tuple[str, str]]) -> list[list[str]]:
    undirected: dict[str, set[str]] = {n: set() for n in g.nodes}
    for a, b in [(e.src, e.dst) for e in g.edges] + links:
        undirected[a].add(b)
        undirected[b].add(a)
    remaining = set(changed_ids)
    result = []
    while remaining:
        start = remaining.pop()
        seen = {start}
        queue = deque([start])
        while queue:
            n = queue.popleft()
            for m in undirected[n]:
                if m not in seen:
                    seen.add(m)
                    queue.append(m)
        member = [c for c in changed_ids if c in seen]
        remaining -= set(member)
        result.append(member)
    return result


def distances(g: Graph, start: str, limit: int, backwards: bool = False) -> dict[str, int]:
    step = g.callers if backwards else g.callees
    dist = {start: 0}
    queue = deque([start])
    while queue:
        n = queue.popleft()
        if dist[n] >= limit:
            continue
        for m in step(n):
            if m not in dist:
                dist[m] = dist[n] + 1
                queue.append(m)
    return dist


def select_entry(g: Graph, changed_ids: list[str], hops: int) -> Node | None:
    reachable = [cid for cid in changed_ids if g.nodes[cid].status != "removed"]
    if not reachable:
        return None
    best: tuple[int, int, str] | None = None
    for nid, node in g.nodes.items():
        if not node.stable:
            continue
        dist = distances(g, nid, hops)
        if not all(cid in dist for cid in reachable):
            continue
        key = (max(dist[cid] for cid in reachable), len(dist), nid)
        if best is None or key < best:
            best = key
    return g.nodes[best[2]] if best else None


def frames_for(g: Graph, entry: Node, changed_ids: list[str], hops: int) -> list[Node]:
    from_entry = distances(g, entry.id, hops)
    on_path: set[str] = {entry.id}
    for cid in changed_ids:
        if cid not in from_entry:
            continue
        to_changed = distances(g, cid, hops, backwards=True)
        on_path |= {nid for nid, d in from_entry.items()
                    if nid in to_changed and d + to_changed[nid] == from_entry[cid]}
    for cid in changed_ids:
        on_path |= g.callees(cid)
    return sorted((g.nodes[n] for n in on_path), key=lambda n: (from_entry.get(n.id, 99), n.name))


def open_test_files(client: LspClient, g: Graph) -> None:
    if not client.config.references_need_open:
        return
    listed = git(client.root, "ls-files", "--cached", "--others", "--exclude-standard").splitlines()
    tests = sorted(client.root / n for n in listed if Path(n).suffix in client.config.extensions
                   and is_test_path(client.root / n, client.root) and (client.root / n).is_file())
    if len(tests) > MAX_TEST_FILES_OPENED:
        g.warnings.append(f"{len(tests)} test files; references searched in the first {MAX_TEST_FILES_OPENED}")
    for path in tests[:MAX_TEST_FILES_OPENED]:
        client.open(path, path.read_text(encoding="utf-8", errors="replace"))


def import_lines(client: LspClient, path: Path, text: str) -> set[int]:
    lines: set[int] = set()
    for m in scan_kinds(client.config.ast_grep_language, path.suffix, text, client.config.import_kinds):
        lines.update(range(m["range"]["start"]["line"], m["range"]["end"]["line"] + 1))
    return lines


def covering_tests(client: LspClient, frames: list[Node], limit: int = 40) -> list[str]:
    tests: set[str] = set()
    cache: dict[Path, tuple[list[Symbol], set[int]]] = {}
    for frame in frames[:limit]:
        if frame.status == "slot":
            continue
        for loc in client.references(frame.path, Position(frame.line, frame.col)):
            if not is_test_path(loc.path, client.root):
                continue
            if loc.path not in cache:
                text = loc.path.read_text(encoding="utf-8", errors="replace")
                client.open(loc.path, text)
                cache[loc.path] = (client.document_symbols(loc.path), import_lines(client, loc.path, text))
            symbols, imports = cache[loc.path]
            if loc.range.start.line in imports:
                continue
            enclosing = [s for s in symbols
                         if s.is_function and s.range.overlaps_lines(loc.range.start.line, loc.range.start.line)]
            name = min(enclosing, key=lambda s: s.range.end.line - s.range.start.line).name if enclosing else "<module>"
            prefix = client.config.test_function_prefix
            if prefix and name != "<module>" and not name.startswith(prefix):
                continue
            tests.add(f"{loc.path.relative_to(client.root)}::{name}")
    return sorted(tests)


def is_test_path(path: Path, root: Path) -> bool:
    try:
        rel = path.relative_to(root)
    except ValueError:
        return False
    stem = rel.stem
    return bool(TEST_DIR_NAMES & set(rel.parts[:-1])) or stem.startswith("test_") or stem.endswith("_test")


def flows(client: LspClient, g: Graph, changed: list[ChangedSymbol], hops: int,
          with_tests: bool) -> list[Flow]:
    changed_ids = [c.symbol.id for c in changed]
    result = []
    if with_tests:
        open_test_files(client, g)
    for member in components(g, changed_ids, containment_links(changed)):
        entry = select_entry(g, member, hops)
        frames = frames_for(g, entry, member, hops) if entry else [g.nodes[m] for m in member]
        frame_ids = {f.id for f in frames}
        edges = sorted((e for e in g.edges if e.src in frame_ids and e.dst in frame_ids),
                       key=lambda e: (e.src, e.dst))
        tests = covering_tests(client, frames) if with_tests else []
        result.append(Flow([g.nodes[m] for m in member], entry, frames, edges, entry is None, tests,
                           client.config.language_id))
    return result
