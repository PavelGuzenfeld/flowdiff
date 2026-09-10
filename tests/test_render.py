from pathlib import Path

from flowdiff import render
from flowdiff.graph import Edge, Flow, Node


def node(name: str, status: str = "unchanged", path: str = "m.py") -> Node:
    return Node(name, name, Path(path), 0, 0, status)


def flow(frames: list[Node], edges: list[Edge], changed: list[Node] | None = None,
         entry: Node | None = None, tests: list[str] | None = None) -> Flow:
    return Flow(changed or [], entry, frames, edges, entry is None, tests or [])


def test_edge_tokens_are_the_graph_easy_literals():
    """Literals, not the module constants: comparing a constant to itself asserts nothing."""
    assert (render.SOLID, render.DASHED) == ("->", "- - >")
    assert render.MAX_GRAPH_NODES == 24 and render.MAX_NAMES_IN_VERDICT == 6


def test_label_marks_and_neutralises_graph_easy_syntax():
    assert render.label(node("f", "body")) == "f~"
    assert render.label(node("a[b]#|c", "added")) == "a(b)/c+"


def test_graph_easy_source_lists_edges_and_isolated_nodes():
    a, b, slot, lone = node("a"), node("b", "body"), node("->s", "slot"), node("lone")
    src = render.graph_easy_source(flow([a, b, slot, lone], [Edge("a", "b"), Edge("->s", "a", "registered")]))
    assert src.splitlines() == ["[ lone ]", "[ a ] -> [ b~ ]", "[ ->s ] - - > [ a ]"]


def test_graph_easy_source_never_repeats_a_connected_node():
    """A node that is only a source, or only a target, is still connected."""
    src = render.graph_easy_source(flow([node("a"), node("b"), node("c")],
                                        [Edge("a", "b"), Edge("b", "c")]))
    assert src.splitlines() == ["[ a ] -> [ b ]", "[ b ] -> [ c ]"]


def test_render_graph_passes_a_short_timeout_to_graph_easy(monkeypatch):
    monkeypatch.setattr(render.shutil, "which", lambda name: "/usr/bin/graph-easy")
    seen: dict[str, object] = {}

    class Ok:
        returncode = 0
        stdout = "BOX\n"

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        seen["capture_output"] = kwargs.get("capture_output")
        return Ok()

    monkeypatch.setattr(render.subprocess, "run", fake_run)
    assert render.render_graph(flow([node("a")], [])) == "BOX"
    assert seen["cmd"] == ["graph-easy", "--as=boxart", "--timeout=10"]
    assert seen["capture_output"] is True


def test_render_graph_falls_back_to_tree_without_graph_easy(monkeypatch):
    monkeypatch.setattr(render.shutil, "which", lambda name: None)
    out = render.render_graph(flow([node("a"), node("b", "body")], [Edge("a", "b")]))
    assert out == "a\n  b~"


def test_render_graph_falls_back_when_layout_fails(monkeypatch):
    monkeypatch.setattr(render.shutil, "which", lambda name: "/usr/bin/graph-easy")

    class Failed:
        returncode = 1
        stdout = ""
    monkeypatch.setattr(render.subprocess, "run", lambda *a, **k: Failed())
    out = render.render_graph(flow([node("a")], []))
    assert out.startswith("a") and "layout aborted" in out


def test_render_graph_treats_a_silent_success_as_a_failed_layout(monkeypatch):
    """graph-easy exits 0 with no output when the layouter gives up inside its timeout."""
    monkeypatch.setattr(render.shutil, "which", lambda name: "/usr/bin/graph-easy")

    class Silent:
        returncode = 0
        stdout = "  \n"
    monkeypatch.setattr(render.subprocess, "run", lambda *a, **k: Silent())
    assert render.render_graph(flow([node("a")], [])) == "a\n(graph-easy layout aborted; showing tree)"


def test_indented_tree_stops_at_a_cycle_without_losing_the_path():
    out = render.indented_tree(flow([node("a"), node("b")], [Edge("a", "b"), Edge("b", "a")]))
    assert out.splitlines() == ["a", "  b", "    a"]


def test_indented_tree_expands_a_shared_callee_under_each_caller():
    frames = [node("root"), node("left"), node("right"), node("shared")]
    edges = [Edge("root", "left"), Edge("root", "right"), Edge("left", "shared"), Edge("right", "shared")]
    assert render.indented_tree(flow(frames, edges)).splitlines() == [
        "root", "  left", "    shared", "  right", "    shared"]


def test_verdict_variants():
    entry = node("e")
    f = flow([entry], [], changed=[node("c", "body")], entry=entry)
    assert render.verdict(f).startswith("entry e  (1 frames, 1 changed: c~)")
    assert render.verdict(f).endswith("no covering tests")
    f = flow([node("c", "added")], [], changed=[node("c", "added")])
    assert render.verdict(f).startswith("1 new symbols, nothing older to enter from: c+")
    f = flow([node("c", "signature")], [], changed=[node("c", "signature")], tests=["t::x"])
    assert "two-body harness required" in render.verdict(f) and "covered by 1 test(s)" in render.verdict(f)


def test_verdict_truncates_long_name_lists():
    many = [node(f"n{i}", "body") for i in range(9)]
    text = render.verdict(flow(many, [], changed=many, entry=node("e")))
    assert "n5~ … (+3)" in text and "n6" not in text
    exactly = [node(f"n{i}", "body") for i in range(6)]
    text = render.verdict(flow(exactly, [], changed=exactly, entry=node("e")))
    assert "n5~)" in text and "…" not in text


def test_render_flow_compacts_large_graphs_unless_full(monkeypatch):
    monkeypatch.setattr(render, "render_graph", lambda f: "GRAPH")
    frames = [node(f"n{i}", "added", path=f"p{i % 2}.py") for i in range(25)]
    f = flow(frames, [], changed=frames)
    compact = render.render_flow(1, 1, f)
    assert "graph omitted" in compact and "GRAPH" not in compact and "p0.py" in compact
    assert "GRAPH" in render.render_flow(1, 1, f, full=True)


def test_render_flow_draws_a_graph_at_exactly_the_cap(monkeypatch):
    """24 frames draw; 25 compact. The boundary is the thing worth pinning."""
    monkeypatch.setattr(render, "render_graph", lambda f: "GRAPH")
    frames = [node(f"n{i}", "added") for i in range(24)]
    assert "GRAPH" in render.render_flow(1, 1, flow(frames, [], changed=frames))


def test_render_flow_small_graph_includes_tests(monkeypatch):
    monkeypatch.setattr(render, "render_graph", lambda f: "GRAPH")
    e = node("e")
    out = render.render_flow(2, 3, flow([e], [], changed=[node("c", "body")], entry=e, tests=["tests/t.py::t"]))
    assert out.splitlines()[0] == "flow 2/3" and "GRAPH" in out and "  tests/t.py::t" in out


def test_changed_by_file_groups_and_aligns():
    out = render.changed_by_file(flow([], [], changed=[node("a", "body", "x.py"), node("b", "added", "x.py"), node("c", "removed", "y/z.py")]))
    assert out.splitlines() == ["  x.py    a~ b+", "  y/z.py  c-"]
