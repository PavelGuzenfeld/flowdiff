import dataclasses
import json
import os
import sys
import time
from pathlib import Path

import pytest

from flowdiff import lsp
from flowdiff.lsp import Location, LspClient, LspError, Position, Range, ServerConfig, Symbol

FAKE = Path(__file__).with_name("fake_lsp_server.py")


def fake_config() -> ServerConfig:
    return ServerConfig("python", sys.executable, (str(FAKE),), frozenset({".py"}), "python")


@pytest.fixture
def client(tmp_path: Path):
    c = LspClient(fake_config(), tmp_path, timeout=5)
    yield c
    c.close()


def test_missing_binary_is_reported():
    with pytest.raises(LspError, match="not found"):
        LspClient(ServerConfig("x", "definitely-not-a-binary-xyz", (), frozenset(), "x"), Path("."))


def test_document_symbols_are_flattened_with_ids(client: LspClient, tmp_path: Path):
    path = tmp_path / "m.py"
    client.open(path, "class C:\n    def m(self):\n        return 1\n")
    syms = client.document_symbols(path)
    assert [s.name for s in syms] == ["C", "m"]
    assert syms[1].is_function and not syms[0].is_function
    assert syms[1].detail == "(self) -> int"
    assert syms[1].id == f"{path.resolve()}:m:1"


def test_server_requests_are_answered(client: LspClient):
    time.sleep(0.1)
    assert client.request("test/configReply", None) == [None, None]


def test_reopen_sends_didchange_not_didopen(client: LspClient, tmp_path: Path):
    path = tmp_path / "m.py"
    client.open(path, "x = 1\n")
    client.open(path, "x = 2\n")
    assert client._versions[path.resolve()] == 2


def test_call_hierarchy_roundtrip(client: LspClient, tmp_path: Path):
    path = tmp_path / "m.py"
    client.open(path, "")
    items = client.prepare_call_hierarchy(path, Position(1, 8))
    assert lsp.item_symbol(items[0]).id == f"{path.resolve()}:m:1"
    callers = client.incoming_calls(items[0])
    assert lsp.item_symbol(callers[0]["from"]).name == "caller"
    assert client.outgoing_calls(items[0]) == []


def test_references_and_definition_locations(client: LspClient, tmp_path: Path):
    path = tmp_path / "m.py"
    client.open(path, "")
    refs = client.references(path, Position(1, 8))
    assert refs == [Location(path.resolve(), Range(Position(8, 4), Position(8, 5)))]
    defs = client.definition(path, Position(8, 4))
    assert defs[0].range.start == Position(1, 8)


def test_request_timeout_raises(tmp_path: Path):
    c = LspClient(fake_config(), tmp_path, timeout=0.2)
    try:
        with pytest.raises(LspError, match="timed out"):
            c.request("test/never", None)
    finally:
        c.close()


def test_per_request_timeout_overrides_the_client_default(tmp_path: Path):
    """A short per-call timeout must win over a long client default, not be ignored."""
    c = LspClient(fake_config(), tmp_path, timeout=30)
    try:
        started = time.monotonic()
        with pytest.raises(LspError, match="0.3"):
            c.request("test/never", None, timeout=0.3)
        assert time.monotonic() - started < 5
    finally:
        c.close()


def test_a_server_that_exits_immediately_surfaces_as_an_error(tmp_path: Path):
    dead = ServerConfig("python", sys.executable, ("-c", "pass"), frozenset({".py"}), "python")
    with pytest.raises(LspError, match="timed out"):
        LspClient(dead, tmp_path, timeout=0.4)


def test_send_frames_messages_with_a_content_length_header(tmp_path: Path):
    c = LspClient(fake_config(), tmp_path, timeout=5)
    try:
        written: list[bytes] = []

        class Stub:
            def write(self, data: bytes) -> None:
                written.append(data)

            def flush(self) -> None:
                pass

        c._proc.stdin = Stub()  # type: ignore[assignment]
        c.notify("test/framing", {"a": 1})
        payload = json.dumps({"jsonrpc": "2.0", "method": "test/framing",
                              "params": {"a": 1}}).encode()
        assert written == [b"Content-Length: %d\r\n\r\n" % len(payload) + payload]
    finally:
        c._proc.kill()


def test_range_slice_single_and_multi_line():
    text = "abcdef\nghijkl\nmnopqr\n"
    assert Range(Position(0, 1), Position(0, 4)).slice(text) == "bcd"
    assert Range(Position(0, 3), Position(2, 2)).slice(text) == "def\nghijkl\nmn"
    assert Range(Position(9, 0), Position(9, 1)).slice(text) == ""


def test_range_overlaps_lines():
    r = Range(Position(3, 0), Position(5, 0))
    assert r.overlaps_lines(5, 7) and r.overlaps_lines(0, 3)
    assert not r.overlaps_lines(6, 8) and not r.overlaps_lines(0, 2)


def test_uri_roundtrip_and_rejection(tmp_path: Path):
    assert lsp.path_of(lsp.uri_of(tmp_path)) == tmp_path.resolve()
    with pytest.raises(ValueError):
        lsp.path_of("https://example.invalid/x")


def test_location_from_lsp_accepts_both_shapes():
    plain = {"uri": "file:///a.py", "range": {"start": {"line": 1, "character": 2}, "end": {"line": 1, "character": 3}}}
    link = {"targetUri": "file:///b.py", "targetRange": plain["range"], "targetSelectionRange": plain["range"]}
    assert Location.from_lsp(plain).path == Path("/a.py")
    assert Location.from_lsp(link).path == Path("/b.py")


def test_find_compile_db_prefers_newest(tmp_path: Path):
    old = tmp_path / "build-old"
    new = tmp_path / "build-new"
    for d in (old, new):
        d.mkdir()
        (d / "compile_commands.json").write_text("[]")
    past = time.time() - 100
    os.utime(old / "compile_commands.json", (past, past))
    assert lsp.find_compile_db(tmp_path) == new / "compile_commands.json"
    assert lsp.find_compile_db(tmp_path / "nowhere") is None


def test_server_for_by_extension(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(lsp, "find_compile_db", lambda root: None)
    cpp = lsp.server_for(Path("x.cpp"), tmp_path)
    assert cpp and cpp.binary == "clangd" and cpp.command == ["clangd", "--background-index"]
    monkeypatch.setattr(lsp, "find_compile_db", lambda root: tmp_path / "build" / "compile_commands.json")
    cpp = lsp.server_for(Path("x.hpp"), tmp_path)
    assert cpp and f"--compile-commands-dir={tmp_path / 'build'}" in cpp.command
    py = lsp.server_for(Path("x.py"), tmp_path)
    assert py and py.command == ["pyright-langserver", "--stdio"]
    assert lsp.server_for(Path("x.rs"), tmp_path) is None


def test_pyright_needs_documents_open_for_references_and_clangd_does_not(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(lsp, "find_compile_db", lambda root: None)
    py = lsp.server_for(Path("x.py"), tmp_path)
    cpp = lsp.server_for(Path("x.cpp"), tmp_path)
    assert py and py.references_need_open is True
    assert py.import_kinds == ("import_statement", "import_from_statement")
    assert py.test_function_prefix == "test"
    assert cpp and cpp.references_need_open is False and cpp.import_kinds == ()
    assert cpp.test_function_prefix is None


def test_flatten_marks_functions_inside_functions_as_nested_but_not_methods():
    r = {"start": {"line": 0, "character": 0}, "end": {"line": 9, "character": 0}}
    outer = {"name": "outer", "kind": 12, "range": r, "selectionRange": r,
             "children": [{"name": "inner", "kind": 12, "range": r, "selectionRange": r,
                           "children": [{"name": "deeper", "kind": 12, "range": r, "selectionRange": r}]}]}
    klass = {"name": "C", "kind": 5, "range": r, "selectionRange": r,
             "children": [{"name": "m", "kind": 6, "range": r, "selectionRange": r,
                           "children": [{"name": "closure", "kind": 12, "range": r, "selectionRange": r}]}]}
    out: list[lsp.Symbol] = []
    client = lsp.LspClient.__new__(lsp.LspClient)
    client._flatten(outer, Path("/f.py"), out)
    client._flatten(klass, Path("/f.py"), out)
    assert [(s.name, s.nested) for s in out] == [("outer", False), ("inner", True), ("deeper", True),
                                                ("C", False), ("m", False), ("closure", True)]


def test_symbol_kinds_are_the_lsp_numbers():
    """Pinned as literals: 6 method, 9 constructor, 12 function, 5 class, 10 enum, 23 struct."""
    assert lsp.FUNCTION_KINDS == frozenset({6, 9, 12})
    assert lsp.TRACKED_KINDS == frozenset({5, 6, 9, 10, 12, 23})
    assert 13 not in lsp.TRACKED_KINDS  # variable


def test_symbol_reports_function_kinds_only():
    r = Range(Position(0, 0), Position(0, 1))
    assert Symbol("f", 12, "", r, r, Path("/f.py")).is_function
    assert not Symbol("C", 5, "", r, r, Path("/f.py")).is_function


def test_lsp_value_types_are_frozen():
    """Symbols are dict keys and positions are compared, so these must not be mutable."""
    r = Range(Position(0, 0), Position(0, 1))
    frozen = [Position(1, 1), r, Symbol("f", 12, "", r, r, Path("/f.py")),
              Location(Path("/f.py"), r),
              ServerConfig("python", "x", (), frozenset({".py"}), "python")]
    for value in frozen:
        field = next(iter(vars(value)))
        with pytest.raises(dataclasses.FrozenInstanceError):
            setattr(value, field, None)


def test_slice_at_the_last_line_of_the_text():
    """The out-of-range guard is >=, so the final line must still slice."""
    text = "abc\ndef\n"
    assert Range(Position(1, 0), Position(1, 3)).slice(text) == "def"
    assert Range(Position(2, 0), Position(2, 3)).slice(text) == ""
