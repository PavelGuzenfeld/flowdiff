"""An in-memory stand-in for LspClient, so the graph layer is testable without a language server."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from flowdiff.lsp import Location, Position, Range, ServerConfig, Symbol


def rng(first: int, last: int) -> dict[str, Any]:
    return {"start": {"line": first, "character": 0}, "end": {"line": last, "character": 0}}


class FakeClient:
    """Serves a call graph declared as {caller: [callee, ...]} over named symbols."""

    def __init__(self, root: Path, symbols: dict[str, Symbol],
                 calls: dict[str, list[str]] | None = None,
                 references: dict[str, list[Location]] | None = None,
                 language: str = "python", references_need_open: bool = False,
                 import_kinds: tuple[str, ...] = ()):
        self.root = root
        self.symbols = symbols
        self.calls = calls or {}
        self._references = references or {}
        self.config = ServerConfig(language, "fake", (), frozenset({".py"}), language,
                                   references_need_open, import_kinds)
        self.opened: list[Path] = []
        self.prepared: list[str] = []

    def _item(self, name: str) -> dict[str, Any]:
        s = self.symbols[name]
        return {"name": s.name, "kind": s.kind, "uri": s.path.as_uri(),
                "range": rng(s.range.start.line, s.range.end.line),
                "selectionRange": rng(s.selection.start.line, s.selection.start.line)}

    def _name_of(self, item: dict[str, Any]) -> str:
        return item["name"]

    def open(self, path: Path, text: str) -> None:
        self.opened.append(path)

    def document_symbols(self, path: Path) -> list[Symbol]:
        return [s for s in self.symbols.values() if s.path == path]

    def prepare_call_hierarchy(self, path: Path, pos: Position) -> list[dict[str, Any]]:
        matches = [s.name for s in self.symbols.values()
                   if s.path == path and s.selection.start.line == pos.line]
        self.prepared += matches
        return [self._item(n) for n in matches]

    def incoming_calls(self, item: dict[str, Any]) -> list[dict[str, Any]]:
        target = self._name_of(item)
        return [{"from": self._item(caller)} for caller, callees in sorted(self.calls.items())
                if target in callees]

    def outgoing_calls(self, item: dict[str, Any]) -> list[dict[str, Any]]:
        return [{"to": self._item(c)} for c in self.calls.get(self._name_of(item), [])]

    def references(self, path: Path, pos: Position) -> list[Location]:
        for name, s in self.symbols.items():
            if s.path == path and s.selection.start.line == pos.line:
                return self._references.get(name, [])
        return []

    def definition(self, path: Path, pos: Position) -> list[Location]:
        return [Location(s.path, s.selection) for s in self.symbols.values()
                if s.path != path or s.selection.start.line != pos.line]


def symbol(name: str, path: Path, first: int, last: int | None = None, kind: int = 12) -> Symbol:
    last = first + 2 if last is None else last
    return Symbol(name, kind, f"({name})", Range(Position(first, 0), Position(last, 0)),
                  Range(Position(first, 4), Position(first, 4 + len(name))), path)


def chain_symbols(root: Path, *names: str, spacing: int = 10) -> dict[str, Symbol]:
    return {n: symbol(n, root / "m.py", i * spacing) for i, n in enumerate(names)}
