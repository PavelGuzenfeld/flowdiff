"""build_graph, add_hint_edges, covering_tests and flows against a fake language server."""
import shutil
from pathlib import Path

import pytest

from flowdiff import graph
from flowdiff.changes import ChangedSymbol
from flowdiff.lsp import Location, Position, Range

from conftest import git
from fake_client import FakeClient, chain_symbols, symbol


def changed(symbols: dict, *specs: tuple[str, str]) -> list[ChangedSymbol]:
    return [ChangedSymbol(symbols[name], status) for name, status in specs]


def test_build_graph_walks_both_directions(tmp_path: Path):
    syms = chain_symbols(tmp_path, "top", "mid", "leaf")
    client = FakeClient(tmp_path, syms, {"top": ["mid"], "mid": ["leaf"]})
    g = graph.build_graph(client, changed(syms, ("mid", "body")), hops=3)
    assert set(g.nodes) == {syms[n].id for n in ("top", "mid", "leaf")}
    assert graph.Edge(syms["top"].id, syms["mid"].id) in g.edges
    assert graph.Edge(syms["mid"].id, syms["leaf"].id) in g.edges


def test_build_graph_honours_the_hop_bound(tmp_path: Path):
    syms = chain_symbols(tmp_path, "a", "b", "c", "d")
    calls = {"a": ["b"], "b": ["c"], "c": ["d"]}
    near = graph.build_graph(FakeClient(tmp_path, syms, calls), changed(syms, ("a", "body")), hops=1)
    assert set(near.nodes) == {syms["a"].id, syms["b"].id}
    far = graph.build_graph(FakeClient(tmp_path, syms, calls), changed(syms, ("a", "body")), hops=3)
    assert set(far.nodes) == {syms[n].id for n in ("a", "b", "c", "d")}


def test_build_graph_skips_frames_outside_the_repo(tmp_path: Path):
    inside = symbol("mine", tmp_path / "m.py", 0)
    outside = symbol("stdlib", Path("/usr/lib/python3.12/typing.py"), 0)
    syms = {"mine": inside, "stdlib": outside}
    g = graph.build_graph(FakeClient(tmp_path, syms, {"mine": ["stdlib"]}),
                          changed(syms, ("mine", "body")), hops=3)
    assert set(g.nodes) == {inside.id}
    assert g.edges == set()


def test_build_graph_does_not_query_removed_symbols(tmp_path: Path):
    syms = chain_symbols(tmp_path, "gone", "live")
    client = FakeClient(tmp_path, syms, {"live": ["gone"]})
    g = graph.build_graph(client, changed(syms, ("gone", "removed")), hops=3)
    assert client.prepared == []
    assert set(g.nodes) == {syms["gone"].id}


def test_build_graph_keeps_going_past_a_removed_symbol(tmp_path: Path):
    """The removed-symbol skip must not abandon the symbols that follow it."""
    syms = chain_symbols(tmp_path, "gone", "kept", "caller")
    client = FakeClient(tmp_path, syms, {"caller": ["kept"]})
    g = graph.build_graph(client, changed(syms, ("gone", "removed"), ("kept", "body")), hops=3)
    assert client.prepared == ["kept"]
    assert set(g.nodes) == {syms[n].id for n in ("gone", "kept", "caller")}


def test_build_graph_keeps_every_caller_of_a_frame(tmp_path: Path):
    syms = chain_symbols(tmp_path, "one", "two", "target")
    client = FakeClient(tmp_path, syms, {"one": ["target"], "two": ["target"]})
    g = graph.build_graph(client, changed(syms, ("target", "body")), hops=3)
    assert g.callers(syms["target"].id) == {syms["one"].id, syms["two"].id}


def test_build_graph_keeps_every_callee_of_a_frame(tmp_path: Path):
    syms = chain_symbols(tmp_path, "source", "first", "second")
    client = FakeClient(tmp_path, syms, {"source": ["first", "second"]})
    g = graph.build_graph(client, changed(syms, ("source", "body")), hops=3)
    assert g.callees(syms["source"].id) == {syms["first"].id, syms["second"].id}


def test_build_graph_bounds_the_incoming_walk_by_hops(tmp_path: Path):
    """Callers count against the same bound as callees."""
    syms = chain_symbols(tmp_path, "far", "near", "target")
    calls = {"far": ["near"], "near": ["target"]}
    tight = graph.build_graph(FakeClient(tmp_path, syms, calls), changed(syms, ("target", "body")), hops=1)
    assert set(tight.nodes) == {syms["target"].id, syms["near"].id}
    loose = graph.build_graph(FakeClient(tmp_path, syms, calls), changed(syms, ("target", "body")), hops=2)
    assert set(loose.nodes) == {syms[n].id for n in ("far", "near", "target")}


def test_build_graph_still_filters_out_of_repo_frames_among_local_ones(tmp_path: Path):
    """The out-of-repo skip must not abandon the frames listed after it."""
    syms = {
        "mine": symbol("mine", tmp_path / "m.py", 0),
        "stdlib": symbol("stdlib", Path("/usr/lib/python3.12/typing.py"), 10),
        "also_mine": symbol("also_mine", tmp_path / "m.py", 20),
    }
    g = graph.build_graph(FakeClient(tmp_path, syms, {"mine": ["stdlib", "also_mine"]}),
                          changed(syms, ("mine", "body")), hops=3)
    assert set(g.nodes) == {syms["mine"].id, syms["also_mine"].id}


def test_build_graph_warns_only_for_uncalled_functions(tmp_path: Path):
    syms = chain_symbols(tmp_path, "orphan", "called", "caller")
    klass = symbol("Klass", tmp_path / "m.py", 40, kind=5)
    syms["Klass"] = klass
    closure = symbol("closure", tmp_path / "m.py", 50)
    syms["closure"] = closure.__class__(**{**closure.__dict__, "nested": True})
    client = FakeClient(tmp_path, syms, {"caller": ["called"]})
    g = graph.build_graph(client, changed(syms, ("orphan", "body"), ("called", "body"),
                                          ("Klass", "body"), ("closure", "body")), hops=3)
    assert g.warnings == ["orphan: no caller found — add a hint rule?"]


@pytest.mark.skipif(shutil.which("ast-grep") is None, reason="ast-grep not installed")
def test_textual_callees_fill_in_when_outgoing_calls_are_unsupported(tmp_path: Path):
    source = tmp_path / "gst" / "t.cpp"
    source.parent.mkdir()
    source.write_text("int helper(int x) { return x; }\n"
                      "int crop(int v) {\n    auto p = ns::Transform::transform(v, 1);\n    return helper(v) + printf(\"x\") + obj->method(2);\n}\n"
                      "int method(int) { return 0; }\n")
    (tmp_path / "tests").mkdir()
    crop = symbol("crop", source, 1, last=4, kind=12)
    helper = symbol("helper", source, 0, last=0)
    transform = symbol("ns::Transform::transform", tmp_path / "gst" / "u.cpp", 3)
    method = symbol("method", tmp_path / "tests" / "test_t.cpp", 1)
    syms = {"crop": crop, "helper": helper, "ns::Transform::transform": transform, "method": method}
    client = FakeClient(tmp_path, syms, {}, language="cpp")
    client.unsupported.add("callHierarchy/outgoingCalls")
    g = graph.build_graph(client, changed(syms, ("crop", "body")), hops=3)
    assert {g.nodes[e.dst].name for e in g.edges if e.src == crop.id} == {"helper", "ns::Transform::transform"}
    assert any("callees come from call expressions" in w for w in g.warnings)
    client.unsupported.clear()
    g = graph.build_graph(client, changed(syms, ("crop", "body")), hops=3)
    assert not any(e.src == crop.id for e in g.edges)


@pytest.mark.skipif(shutil.which("ast-grep") is None, reason="ast-grep not installed")
def test_textual_callees_fill_in_for_typescript_too(tmp_path: Path):
    source = tmp_path / "src" / "t.ts"
    source.parent.mkdir()
    source.write_text("function helper(x: number): number { return x; }\n"
                      "function run(v: number): number {\n    return helper(v);\n}\n")
    run = symbol("run", source, 1, last=3, kind=12)
    helper = symbol("helper", source, 0, last=0)
    syms = {"run": run, "helper": helper}
    client = FakeClient(tmp_path, syms, {}, language="typescript")
    client.unsupported.add("callHierarchy/outgoingCalls")
    g = graph.build_graph(client, changed(syms, ("run", "body")), hops=3)
    assert {g.nodes[e.dst].name for e in g.edges if e.src == run.id} == {"helper"}


@pytest.mark.skipif(shutil.which("ast-grep") is None, reason="ast-grep not installed")
def test_the_fallback_also_fires_when_only_prepare_call_hierarchy_is_unsupported(tmp_path: Path):
    """Godot never gets past prepareCallHierarchy, so outgoingCalls itself never enters unsupported
    through the normal path - the trigger must check for either method (issue #46)."""
    source = tmp_path / "gst" / "t.cpp"
    source.parent.mkdir()
    source.write_text("int helper(int x) { return x; }\nint crop(int v) { return helper(v); }\n\n")
    crop = symbol("crop", source, 1, last=2, kind=12)
    helper = symbol("helper", source, 0, last=0)
    syms = {"crop": crop, "helper": helper}
    client = FakeClient(tmp_path, syms, {}, language="cpp")
    client.unsupported.add("textDocument/prepareCallHierarchy")
    g = graph.build_graph(client, changed(syms, ("crop", "body")), hops=3)
    assert graph.Edge(crop.id, helper.id) in g.edges


def test_gdscript_callees_are_resolved_through_definition_not_workspace_symbol(tmp_path: Path, monkeypatch):
    """Godot's LSP has no workspace/symbol either (issue #46); the call site's own position, not a
    name search, is what resolves the callee. ast-grep's own GDScript parsing (a custom grammar the
    user builds and registers, not bundled here) is stubbed so this runs without that setup."""
    source = tmp_path / "player.gd"
    source.write_text("func take_damage(amount):\n\thealth -= amount\n\tdie()\n\n"
                      "func die():\n\tqueue_free()\n")
    take_damage = symbol("take_damage", source, 0, last=3, kind=6)
    die = symbol("die", source, 4, last=5, kind=6)
    syms = {"take_damage": take_damage, "die": die}
    client = FakeClient(tmp_path, syms, {}, language="gdscript")
    client.unsupported.add("textDocument/prepareCallHierarchy")
    monkeypatch.setattr(graph, "scan_kinds",
                        lambda *a, **kw: [{"range": {"start": {"line": 2, "column": 1}}}])
    g = graph.build_graph(client, changed(syms, ("take_damage", "body")), hops=3)
    assert graph.Edge(take_damage.id, die.id) in g.edges


def test_gdscript_callees_skip_a_removed_or_non_function_symbol_but_keep_going(tmp_path: Path, monkeypatch):
    """A removed symbol or a non-function one (e.g. a changed constant) must not be scanned for calls,
    and skipping one must not stop the ones after it (issue #46)."""
    source = tmp_path / "player.gd"
    source.write_text("x")
    gone = symbol("gone", source, 0, last=1, kind=6)
    const = symbol("HEALTH_MAX", source, 2, last=3, kind=13)
    take_damage = symbol("take_damage", source, 4, last=5, kind=6)
    die = symbol("die", source, 10, last=11, kind=6)
    client = FakeClient(tmp_path, {"take_damage": take_damage, "die": die}, {}, language="gdscript")
    monkeypatch.setattr(graph, "scan_kinds", lambda *a, **kw: [
        {"range": {"start": {"line": 0, "column": 0}}},   # inside gone's range - must never be scanned
        {"range": {"start": {"line": 2, "column": 0}}},   # inside const's range - must never be scanned
        {"range": {"start": {"line": 4, "column": 0}}}])  # take_damage's real call
    monkeypatch.setattr(client, "definition", lambda path, pos: [Location(source, die.selection)])
    changed_syms = [ChangedSymbol(gone, "removed"), ChangedSymbol(const, "body"),
                    ChangedSymbol(take_damage, "body")]
    g = graph.Graph()
    graph.add_gdscript_callees(client, g, changed_syms)
    assert g.edges == {graph.Edge(take_damage.id, die.id)}


def test_gdscript_callees_skip_one_out_of_range_match_but_still_resolve_the_next(tmp_path: Path, monkeypatch):
    """One call outside the changed function's own range must not stop the scan of the calls after it."""
    source = tmp_path / "player.gd"
    source.write_text("x")
    take_damage = symbol("take_damage", source, 0, last=1, kind=6)
    die = symbol("die", source, 10, last=11, kind=6)
    client = FakeClient(tmp_path, {"take_damage": take_damage, "die": die}, {}, language="gdscript")
    monkeypatch.setattr(graph, "scan_kinds", lambda *a, **kw: [
        {"range": {"start": {"line": 9, "column": 0}}},   # outside take_damage's range - must be skipped
        {"range": {"start": {"line": 0, "column": 0}}}])  # inside it - must still resolve
    monkeypatch.setattr(client, "definition", lambda path, pos: [Location(source, die.selection)])
    g = graph.Graph()
    graph.add_gdscript_callees(client, g, [ChangedSymbol(take_damage, "body")])
    assert g.edges == {graph.Edge(take_damage.id, die.id)}


def test_gdscript_callees_skip_one_bad_definition_result_but_still_resolve_the_next(tmp_path: Path, monkeypatch):
    """A definition() result outside the repo, or under a test path, must not stop the scan of the
    results after it - and either filter alone must drop its own result, never both required at once.
    Each bad result resolves to a real decoy symbol, so an unfiltered one would add its own wrong edge."""
    source = tmp_path / "player.gd"
    source.write_text("x")
    take_damage = symbol("take_damage", source, 0, last=1, kind=6)
    die = symbol("die", source, 10, last=11, kind=6)
    outside = Path("/outside/repo.gd")
    outside_decoy = symbol("outside_decoy", outside, 0, last=1, kind=6)
    test_file = tmp_path / "tests" / "test_x.gd"
    test_decoy = symbol("test_decoy", test_file, 0, last=1, kind=6)
    client = FakeClient(tmp_path, {"take_damage": take_damage, "die": die,
                                  "outside_decoy": outside_decoy, "test_decoy": test_decoy}, {},
                        language="gdscript")
    monkeypatch.setattr(graph, "scan_kinds", lambda *a, **kw: [{"range": {"start": {"line": 0, "column": 0}}}])
    monkeypatch.setattr(client, "definition", lambda path, pos: [
        Location(outside, outside_decoy.selection),
        Location(test_file, test_decoy.selection),
        Location(source, die.selection)])
    g = graph.Graph()
    graph.add_gdscript_callees(client, g, [ChangedSymbol(take_damage, "body")])
    assert g.edges == {graph.Edge(take_damage.id, die.id)}


def test_gdscript_callees_pick_the_symbol_whose_own_line_matches_not_the_first_other_one(tmp_path: Path,
                                                                                          monkeypatch):
    """Three candidates at three different lines: the one actually at the definition's line must win,
    not merely "some other function" (issue #46)."""
    source = tmp_path / "player.gd"
    source.write_text("x")
    take_damage = symbol("take_damage", source, 0, last=1, kind=6)
    decoy = symbol("decoy", source, 5, last=6, kind=6)
    die = symbol("die", source, 10, last=11, kind=6)
    client = FakeClient(tmp_path, {"take_damage": take_damage, "decoy": decoy, "die": die}, {},
                        language="gdscript")
    monkeypatch.setattr(graph, "scan_kinds", lambda *a, **kw: [{"range": {"start": {"line": 0, "column": 0}}}])
    monkeypatch.setattr(client, "definition", lambda path, pos: [Location(source, die.selection)])
    g = graph.Graph()
    graph.add_gdscript_callees(client, g, [ChangedSymbol(take_damage, "body")])
    assert g.edges == {graph.Edge(take_damage.id, die.id)}


def test_build_graph_tolerates_a_symbol_with_no_hierarchy(tmp_path: Path):
    syms = {"ghost": symbol("ghost", tmp_path / "other.py", 99)}
    client = FakeClient(tmp_path, {}, {})
    client.symbols = {}
    g = graph.build_graph(client, [ChangedSymbol(syms["ghost"], "body")], hops=3)
    assert set(g.nodes) == {syms["ghost"].id}


def test_flows_pick_the_stable_entry_and_its_frames(tmp_path: Path):
    syms = chain_symbols(tmp_path, "entry", "middle", "target")
    client = FakeClient(tmp_path, syms, {"entry": ["middle"], "middle": ["target"]})
    marked = changed(syms, ("target", "signature"))
    g = graph.build_graph(client, marked, hops=3)
    flows = graph.flows(client, g, marked, hops=3, with_tests=False)
    assert len(flows) == 1
    assert flows[0].entry is not None and flows[0].entry.name == "middle"
    assert [f.name for f in flows[0].frames] == ["middle", "target"]
    assert flows[0].two_body is False and flows[0].tests == []


def test_flows_split_unrelated_changes(tmp_path: Path):
    syms = chain_symbols(tmp_path, "a", "b", "x", "y")
    client = FakeClient(tmp_path, syms, {"a": ["b"], "x": ["y"]})
    marked = changed(syms, ("b", "body"), ("y", "body"))
    g = graph.build_graph(client, marked, hops=3)
    flows = graph.flows(client, g, marked, hops=3, with_tests=False)
    assert len(flows) == 2
    assert {f.changed[0].name for f in flows} == {"b", "y"}


def test_flows_report_two_body_when_nothing_stable_is_in_reach(tmp_path: Path):
    syms = {"lonely": symbol("lonely", tmp_path / "m.py", 0)}
    client = FakeClient(tmp_path, syms, {})
    marked = changed(syms, ("lonely", "signature"))
    g = graph.build_graph(client, marked, hops=3)
    flow = graph.flows(client, g, marked, hops=3, with_tests=False)[0]
    assert flow.entry is None and flow.two_body is True
    assert [f.name for f in flow.frames] == ["lonely"]


def test_covering_tests_names_the_enclosing_test_function(tmp_path: Path):
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_m.py").write_text("\n" * 6 + "def test_it():\n    under_test()\n")
    target = symbol("under_test", tmp_path / "m.py", 0)
    test_fn = symbol("test_it", tests_dir / "test_m.py", 5, last=9)
    syms = {"under_test": target, "test_it": test_fn}
    refs = {"under_test": [Location(tests_dir / "test_m.py", Range(Position(7, 4), Position(7, 8)))]}
    client = FakeClient(tmp_path, syms, {}, references=refs)
    assert graph.covering_tests(client, [graph.node_of(target)]) == ["tests/test_m.py::test_it"]


def test_covering_tests_ignores_non_test_references(tmp_path: Path):
    target = symbol("under_test", tmp_path / "m.py", 0)
    caller = symbol("prod", tmp_path / "app.py", 5, last=9)
    syms = {"under_test": target, "prod": caller}
    refs = {"under_test": [Location(tests_dir_ref := tmp_path / "app.py", Range(Position(7, 4), Position(7, 8)))]}
    assert tests_dir_ref.name == "app.py"
    client = FakeClient(tmp_path, syms, {}, references=refs)
    assert graph.covering_tests(client, [graph.node_of(target)]) == []


def test_covering_tests_falls_back_to_module_scope(tmp_path: Path):
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_m.py").write_text("import m\nm.under_test()\n")
    target = symbol("under_test", tmp_path / "m.py", 0)
    client = FakeClient(tmp_path, {"under_test": target}, {},
                        references={"under_test": [Location(tests_dir / "test_m.py",
                                                            Range(Position(2, 0), Position(2, 4)))]})
    assert graph.covering_tests(client, [graph.node_of(target)]) == ["tests/test_m.py::<module>"]


def test_covering_tests_keep_only_prefixed_functions_when_the_server_names_a_prefix(tmp_path: Path):
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_m.py").write_text("under_test()\n" + "\n" * 5 + "def fixture():\n    under_test()\n"
                                        + "\n\ndef test_it():\n    under_test()\n")
    target = symbol("under_test", tmp_path / "m.py", 0)
    fixture = symbol("fixture", tests_dir / "test_m.py", 6, last=7)
    test_fn = symbol("test_it", tests_dir / "test_m.py", 10, last=11)
    at = lambda line: Location(tests_dir / "test_m.py", Range(Position(line, 4), Position(line, 14)))
    refs = {"under_test": [at(0), at(7), at(11)]}
    syms = {"under_test": target, "fixture": fixture, "test_it": test_fn}
    with_prefix = FakeClient(tmp_path, syms, {}, references=refs, test_function_prefix="test")
    assert graph.covering_tests(with_prefix, [graph.node_of(target)]) == ["tests/test_m.py::<module>",
                                                                          "tests/test_m.py::test_it"]
    without = FakeClient(tmp_path, syms, {}, references=refs)
    assert graph.covering_tests(without, [graph.node_of(target)]) == ["tests/test_m.py::<module>",
                                                                      "tests/test_m.py::fixture",
                                                                      "tests/test_m.py::test_it"]


def test_covering_tests_skips_slot_frames(tmp_path: Path):
    slot = graph.Node("slot:x", "->x", tmp_path / "m.py", 0, 0, "slot")
    client = FakeClient(tmp_path, {}, {})
    assert graph.covering_tests(client, [slot]) == []
    assert client.opened == []


@pytest.mark.skipif(shutil.which("ast-grep") is None, reason="ast-grep not installed")
def test_covering_tests_drops_references_on_import_lines(tmp_path: Path):
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_m.py").write_text("from m import (\n    under_test,\n)\nunder_test()\n\n\n"
                                         "def test_it():\n    under_test()\n")
    target = symbol("under_test", tmp_path / "m.py", 0)
    test_fn = symbol("test_it", tests_dir / "test_m.py", 6, last=7)
    at = lambda line, col: Location(tests_dir / "test_m.py", Range(Position(line, col), Position(line, col + 10)))
    refs = {"under_test": [at(1, 4), at(3, 0), at(7, 4)]}
    client = FakeClient(tmp_path, {"under_test": target, "test_it": test_fn}, {}, references=refs,
                        import_kinds=("import_statement", "import_from_statement"))
    assert graph.covering_tests(client, [graph.node_of(target)]) == ["tests/test_m.py::<module>",
                                                                     "tests/test_m.py::test_it"]


def test_open_test_files_opens_the_git_listed_test_files(repo: Path):
    (repo / "tests").mkdir()
    (repo / "tests" / "test_a.py").write_text("import a\n")
    (repo / "tests" / "notes.txt").write_text("not code\n")
    (repo / "b_test.py").write_text("import a\n")
    (repo / "tests" / "test_gone.py").write_text("import a\n")
    git(repo, "add", "tests", "b_test.py")
    (repo / "tests" / "test_gone.py").unlink()
    (repo / "tests" / "test_c.py").write_text("import a\n")
    (repo / "tests" / "ignored_test.py").write_text("import a\n")
    (repo / ".gitignore").write_text("ignored_test.py\n")
    client = FakeClient(repo, {}, {}, references_need_open=True)
    g = graph.Graph()
    graph.open_test_files(client, g)
    assert client.opened == [repo / "b_test.py", repo / "tests" / "test_a.py", repo / "tests" / "test_c.py"]
    assert g.warnings == []


def test_open_test_files_is_a_noop_for_a_server_with_a_workspace_index(repo: Path):
    (repo / "test_a.py").write_text("import a\n")
    client = FakeClient(repo, {}, {}, references_need_open=False)
    graph.open_test_files(client, graph.Graph())
    assert client.opened == []


def test_open_test_files_caps_the_count_and_warns(repo: Path, monkeypatch):
    (repo / "test_a.py").write_text("import a\n")
    (repo / "test_b.py").write_text("import a\n")
    client = FakeClient(repo, {}, {}, references_need_open=True)
    monkeypatch.setattr(graph, "MAX_TEST_FILES_OPENED", 2)
    g = graph.Graph()
    graph.open_test_files(client, g)
    assert client.opened == [repo / "test_a.py", repo / "test_b.py"] and g.warnings == []
    client.opened.clear()
    monkeypatch.setattr(graph, "MAX_TEST_FILES_OPENED", 1)
    graph.open_test_files(client, g)
    assert client.opened == [repo / "test_a.py"]
    assert g.warnings == ["2 test files; references searched in the first 1"]


def test_flows_opens_test_files_once_before_searching_references(repo: Path):
    (repo / "test_a.py").write_text("import a\n")
    syms = chain_symbols(repo, "x", "y")
    client = FakeClient(repo, syms, {"x": ["y"]}, references_need_open=True)
    marked = changed(syms, ("x", "body"), ("y", "body"))
    g = graph.build_graph(client, marked, hops=3)
    graph.flows(client, g, marked, hops=3, with_tests=True)
    assert client.opened.count(repo / "test_a.py") == 1
    client.opened.clear()
    graph.flows(client, g, marked, hops=3, with_tests=False)
    assert client.opened == []


@pytest.mark.skipif(shutil.which("ast-grep") is None, reason="ast-grep not installed")
def test_hint_rule_adds_a_registered_edge_for_a_vfunc(tmp_path: Path):
    source = tmp_path / "element.cpp"
    source.write_text("static void handler(void) {}\n"
                      "void init(Klass* klass) {\n"
                      "    klass->set_property = handler;\n"
                      "    klass->pool = pool;\n"
                      "}\n")
    handler = symbol("handler", source, 0, last=0)
    client = FakeClient(tmp_path, {"handler": handler}, {}, language="cpp")
    g = graph.Graph()
    g.add(graph.node_of(handler, "body"))
    graph.add_hint_edges(client, g)
    registered = [e for e in g.edges if e.kind == "registered"]
    assert len(registered) == 1
    assert g.nodes[registered[0].src].name == "->set_property"
    assert registered[0].dst == handler.id


def test_add_hint_edges_returns_early_on_an_empty_graph(tmp_path: Path):
    client = FakeClient(tmp_path, {}, {}, language="cpp")
    g = graph.Graph()
    graph.add_hint_edges(client, g)
    assert g.edges == set() and client.opened == []


def test_run_ast_grep_returns_empty_when_the_rule_matches_nothing(tmp_path: Path):
    rule = graph.HINTS_DIR / "gobject.yml"
    (tmp_path / "empty.cpp").write_text("int main() { return 0; }\n")
    assert graph.run_ast_grep(rule, tmp_path) == []
