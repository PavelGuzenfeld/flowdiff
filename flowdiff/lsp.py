"""JSON-RPC client for clangd and pyright over stdio, covering the six requests flowdiff uses."""
from __future__ import annotations

import json
import shutil
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# LSP SymbolKind numbers: method, constructor, function.
FUNCTION_KINDS = frozenset({6, 9, 12})
# The above plus the container kinds a flow can enter: class, enum, struct.
TRACKED_KINDS = FUNCTION_KINDS | frozenset({5, 10, 23})
NAMESPACE_KIND = 3


@dataclass(frozen=True)
class Position:
    line: int
    character: int

    def to_lsp(self) -> dict[str, int]:
        return {"line": self.line, "character": self.character}


@dataclass(frozen=True)
class Range:
    start: Position
    end: Position

    @classmethod
    def from_lsp(cls, r: dict[str, Any]) -> Range:
        return cls(Position(r["start"]["line"], r["start"]["character"]),
                   Position(r["end"]["line"], r["end"]["character"]))

    def overlaps_lines(self, first: int, last: int) -> bool:
        return self.start.line <= last and first <= self.end.line

    def slice(self, text: str) -> str:
        lines = text.splitlines()
        if self.start.line >= len(lines):
            return ""
        chunk = lines[self.start.line:self.end.line + 1]
        chunk[-1] = chunk[-1][:self.end.character]
        chunk[0] = chunk[0][self.start.character:]
        return "\n".join(chunk)


@dataclass(frozen=True)
class Symbol:
    name: str
    kind: int
    detail: str
    range: Range
    selection: Range
    path: Path
    nested: bool = False
    # Enclosing named namespaces, outermost first; what a harness must prefix to call the symbol.
    namespaces: tuple[str, ...] = ()

    @property
    def is_function(self) -> bool:
        return self.kind in FUNCTION_KINDS

    @property
    def id(self) -> str:
        return f"{self.path}:{self.name}:{self.selection.start.line}"


@dataclass(frozen=True)
class Location:
    path: Path
    range: Range

    @classmethod
    def from_lsp(cls, loc: dict[str, Any]) -> Location:
        if "targetUri" in loc:
            return cls(path_of(loc["targetUri"]), Range.from_lsp(loc["targetSelectionRange"]))
        return cls(path_of(loc["uri"]), Range.from_lsp(loc["range"]))


@dataclass(frozen=True)
class ServerConfig:
    language_id: str
    binary: str
    extra_args: tuple[str, ...]
    extensions: frozenset[str]
    ast_grep_language: str
    # pyright answers textDocument/references only for open documents; clangd has a background index.
    references_need_open: bool = False
    # tree-sitter node kinds whose references are imports, not uses.
    import_kinds: tuple[str, ...] = ()
    # Only functions with this prefix count as covering tests; None keeps every reaching function.
    test_function_prefix: str | None = None
    # A server run inside a container: docker run ... <image> goes first, and file URIs are rewritten
    # from the host root to the mount point on the way out and back on the way in.
    command_prefix: tuple[str, ...] = ()
    uri_map: tuple[str, str] | None = None

    @property
    def command(self) -> list[str]:
        return [*self.command_prefix, self.binary, *self.extra_args]

    def to_server(self, text: str) -> str:
        return text.replace(self.uri_map[0], self.uri_map[1]) if self.uri_map else text

    def from_server(self, text: str) -> str:
        return text.replace(self.uri_map[1], self.uri_map[0]) if self.uri_map else text


CPP_EXTENSIONS = frozenset({".c", ".cc", ".cpp", ".cxx", ".h", ".hh", ".hpp", ".cu"})
PYTHON_EXTENSIONS = frozenset({".py"})


def find_compile_db(root: Path) -> Path | None:
    candidates = [root, *sorted(root.glob("build*"))]
    found = [d / "compile_commands.json" for d in candidates if (d / "compile_commands.json").is_file()]
    return max(found, key=lambda p: p.stat().st_mtime) if found else None


def server_for(path: Path, root: Path, container: Any = None) -> ServerConfig | None:
    suffix = path.suffix
    if suffix in CPP_EXTENSIONS:
        if container is not None:
            return ServerConfig("cpp", "clangd", ("--background-index", f"--compile-commands-dir={container.workdir}/builddir"),
                                CPP_EXTENSIONS, "cpp",
                                command_prefix=tuple(container.clangd_command(root, root / ".flowdiff")),
                                uri_map=(uri_of(root), "file://" + container.workdir))
        db = find_compile_db(root)
        args = ("--background-index", f"--compile-commands-dir={db.parent}") if db else ("--background-index",)
        return ServerConfig("cpp", "clangd", args, CPP_EXTENSIONS, "cpp")
    if suffix in PYTHON_EXTENSIONS:
        return ServerConfig("python", "pyright-langserver", ("--stdio",), PYTHON_EXTENSIONS, "python",
                            references_need_open=True,
                            import_kinds=("import_statement", "import_from_statement"),
                            test_function_prefix="test")
    return None


def uri_of(path: Path) -> str:
    return path.resolve().as_uri()


def path_of(uri: str) -> Path:
    if not uri.startswith("file://"):
        raise ValueError(f"unsupported uri {uri}")
    return Path(uri[len("file://"):])


class LspError(RuntimeError):
    pass


class LspUnsupported(LspError):
    """JSON-RPC -32601: the server does not implement the method."""


METHOD_NOT_FOUND = -32601


class LspClient:
    def __init__(self, config: ServerConfig, root: Path, timeout: float = 60.0):
        if shutil.which(config.command[0]) is None:
            raise LspError(f"{config.command[0]} not found on PATH")
        self.config = config
        self.root = root.resolve()
        self.timeout = timeout
        self._proc = subprocess.Popen(config.command, cwd=self.root, stdin=subprocess.PIPE,
                                      stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        self._next_id = 0
        self._replies: dict[int, dict[str, Any]] = {}
        self._cond = threading.Condition()
        self._write_lock = threading.Lock()
        self._versions: dict[Path, int] = {}
        self.unsupported: set[str] = set()
        self._progress: dict[Any, bool] = {}
        threading.Thread(target=self._read_loop, daemon=True).start()
        self.request("initialize", {
            "processId": None,
            "rootUri": uri_of(self.root),
            "capabilities": {"textDocument": {
                "documentSymbol": {"hierarchicalDocumentSymbolSupport": True},
                "callHierarchy": {"dynamicRegistration": False}},
                "window": {"workDoneProgress": True}},
            "workspaceFolders": [{"uri": uri_of(self.root), "name": self.root.name}],
        })
        self.notify("initialized", {})

    def wait_for_index(self, timeout: float, grace: float = 2.0) -> bool:
        """Block until every $/progress the server began has ended. clangd reports background indexing this
        way; call hierarchy and references answer from that index. True when idle, False on timeout."""
        with self._cond:
            if not self._cond.wait_for(lambda: bool(self._progress), grace):
                return True
            return self._cond.wait_for(lambda: all(self._progress.values()), timeout)

    def close(self) -> None:
        try:
            self.request("shutdown", None, timeout=5)
            self.notify("exit", None)
        except (LspError, BrokenPipeError):
            pass
        self._proc.kill()

    def _send(self, msg: dict[str, Any]) -> None:
        body = self.config.to_server(json.dumps(msg)).encode()
        assert self._proc.stdin is not None
        with self._write_lock:
            self._proc.stdin.write(b"Content-Length: %d\r\n\r\n" % len(body) + body)
            self._proc.stdin.flush()

    def _read_loop(self) -> None:
        out = self._proc.stdout
        assert out is not None
        while True:
            header = out.readline()
            if not header:
                return
            if not header.lower().startswith(b"content-length:"):
                continue
            length = int(header.split(b":")[1])
            while out.readline() not in (b"\r\n", b"\n", b""):
                pass
            msg = json.loads(self.config.from_server(out.read(length).decode("utf-8", "replace")))
            if "method" in msg and "id" in msg:
                self._answer_server_request(msg)
            elif "id" in msg:
                with self._cond:
                    self._replies[msg["id"]] = msg
                    self._cond.notify_all()
            elif msg.get("method") == "$/progress":
                kind = msg["params"].get("value", {}).get("kind")
                if kind in ("begin", "end"):
                    with self._cond:
                        self._progress[msg["params"].get("token")] = kind == "end"
                        self._cond.notify_all()

    def _answer_server_request(self, msg: dict[str, Any]) -> None:
        result: Any = None
        if msg["method"] == "workspace/configuration":
            result = [None] * len(msg["params"]["items"])
        elif msg["method"] == "window/workDoneProgress/create":
            with self._cond:
                self._progress.setdefault(msg["params"].get("token"), False)
        self._send({"jsonrpc": "2.0", "id": msg["id"], "result": result})

    def request(self, method: str, params: Any, timeout: float | None = None) -> Any:
        self._next_id += 1
        rid = self._next_id
        self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        deadline = timeout or self.timeout
        with self._cond:
            if not self._cond.wait_for(lambda: rid in self._replies, deadline):
                raise LspError(f"{self.config.binary}: {method} timed out after {deadline}s")
            reply = self._replies.pop(rid)
        if "error" in reply:
            if reply["error"].get("code") == METHOD_NOT_FOUND:
                raise LspUnsupported(f"{method}: {reply['error'].get('message')}")
            raise LspError(f"{method}: {reply['error'].get('message')}")
        return reply.get("result")

    def notify(self, method: str, params: Any) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def open(self, path: Path, text: str) -> None:
        path = path.resolve()
        if path in self._versions:
            self._versions[path] += 1
            self.notify("textDocument/didChange", {
                "textDocument": {"uri": uri_of(path), "version": self._versions[path]},
                "contentChanges": [{"text": text}]})
            return
        self._versions[path] = 1
        self.notify("textDocument/didOpen", {"textDocument": {
            "uri": uri_of(path), "languageId": self.config.language_id, "version": 1, "text": text}})

    def document_symbols(self, path: Path) -> list[Symbol]:
        result = self.request("textDocument/documentSymbol", {"textDocument": {"uri": uri_of(path)}}) or []
        symbols: list[Symbol] = []
        for item in result:
            self._flatten(item, path.resolve(), symbols)
        return symbols

    def _flatten(self, item: dict[str, Any], path: Path, out: list[Symbol], nested: bool = False,
                 namespaces: tuple[str, ...] = ()) -> None:
        if "location" in item:
            rng = Range.from_lsp(item["location"]["range"])
            out.append(Symbol(item["name"], item["kind"], "", rng, rng, path, nested))
            return
        out.append(Symbol(item["name"], item["kind"], item.get("detail") or "",
                          Range.from_lsp(item["range"]), Range.from_lsp(item["selectionRange"]), path, nested, namespaces))
        inner = namespaces + ((item["name"],) if item["kind"] == NAMESPACE_KIND and "anonymous" not in item["name"] else ())
        for child in item.get("children") or []:
            self._flatten(child, path, out, nested or item["kind"] in FUNCTION_KINDS, inner)

    def prepare_call_hierarchy(self, path: Path, pos: Position) -> list[dict[str, Any]]:
        return self.request("textDocument/prepareCallHierarchy", {
            "textDocument": {"uri": uri_of(path)}, "position": pos.to_lsp()}) or []

    def incoming_calls(self, item: dict[str, Any]) -> list[dict[str, Any]]:
        return self.request("callHierarchy/incomingCalls", {"item": item}) or []

    def outgoing_calls(self, item: dict[str, Any]) -> list[dict[str, Any]]:
        """clangd before 17 has no outgoingCalls; callees are then simply absent from the graph."""
        if "callHierarchy/outgoingCalls" in self.unsupported:
            return []
        try:
            return self.request("callHierarchy/outgoingCalls", {"item": item}) or []
        except LspUnsupported:
            self.unsupported.add("callHierarchy/outgoingCalls")
            return []

    def workspace_symbols(self, query: str) -> list[Symbol]:
        result = self.request("workspace/symbol", {"query": query}) or []
        return [Symbol(item["name"], item["kind"], item.get("containerName") or "",
                       Range.from_lsp(item["location"]["range"]), Range.from_lsp(item["location"]["range"]),
                       path_of(item["location"]["uri"])) for item in result if "location" in item]

    def references(self, path: Path, pos: Position) -> list[Location]:
        result = self.request("textDocument/references", {
            "textDocument": {"uri": uri_of(path)}, "position": pos.to_lsp(),
            "context": {"includeDeclaration": False}}) or []
        return [Location.from_lsp(r) for r in result]

    def definition(self, path: Path, pos: Position) -> list[Location]:
        result = self.request("textDocument/definition", {
            "textDocument": {"uri": uri_of(path)}, "position": pos.to_lsp()}) or []
        if isinstance(result, dict):
            result = [result]
        return [Location.from_lsp(r) for r in result]


def item_symbol(item: dict[str, Any]) -> Symbol:
    return Symbol(item["name"], item["kind"], item.get("detail") or "",
                  Range.from_lsp(item["range"]), Range.from_lsp(item["selectionRange"]),
                  path_of(item["uri"]))
