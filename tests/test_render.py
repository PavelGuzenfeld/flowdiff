from pathlib import Path

from flowdiff import render
from flowdiff.graph import Edge, Flow, Node


def node(name: str, status: str = "unchanged", path: str = "m.py") -> Node:
    return Node(name, name, Path(path), 0, 0, status)


def flow(frames: list[Node], edges: list[Edge], changed: list[Node] | None = None,
         entry: Node | None = None, tests: list[str] | None = None) -> Flow:
    return Flow(changed or [], entry, frames, edges, entry is None, tests or [])


def test_label_marks_and_neutralises_graph_easy_syntax():
    assert render.label(node("f", "body")) == "f~"
    assert render.label(node("a[b]#|c", "added")) == "a(b)/c+"


def test_graph_easy_source_lists_edges_and_isolated_nodes():
    a, b, slot, lone = node("a"), node("b", "body"), node("->s", "slot"), node("lone")
    src = render.graph_easy_source(flow([a, b, slot, lone], [Edge("a", "b"), Edge("->s", "a", "registered")]))
    assert "[ a ] -> [ b~ ]" in src
    assert f"[ ->s ] {render.DASHED} [ a ]" in src
    assert "[ lone ]" in src


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


def test_indented_tree_survives_cycles():
    out = render.indented_tree(flow([node("a"), node("b")], [Edge("a", "b"), Edge("b", "a")]))
    assert out.count("a") >= 1 and len(out.splitlines()) <= 3


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


def test_render_flow_compacts_large_graphs_unless_full(monkeypatch):
    monkeypatch.setattr(render, "render_graph", lambda f: "GRAPH")
    frames = [node(f"n{i}", "added", path=f"p{i % 2}.py") for i in range(render.MAX_GRAPH_NODES + 1)]
    f = flow(frames, [], changed=frames)
    compact = render.render_flow(1, 1, f)
    assert "graph omitted" in compact and "GRAPH" not in compact and "p0.py" in compact
    assert "GRAPH" in render.render_flow(1, 1, f, full=True)


def test_render_flow_small_graph_includes_tests(monkeypatch):
    monkeypatch.setattr(render, "render_graph", lambda f: "GRAPH")
    e = node("e")
    out = render.render_flow(2, 3, flow([e], [], changed=[node("c", "body")], entry=e, tests=["tests/t.py::t"]))
    assert out.splitlines()[0] == "flow 2/3" and "GRAPH" in out and "  tests/t.py::t" in out


def test_changed_by_file_groups_and_aligns():
    out = render.changed_by_file(flow([], [], changed=[node("a", "body", "x.py"), node("b", "added", "x.py"), node("c", "removed", "y/z.py")]))
    assert out.splitlines() == ["  x.py    a~ b+", "  y/z.py  c-"]
