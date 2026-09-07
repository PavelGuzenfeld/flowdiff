import dataclasses
from pathlib import Path

import pytest

from flowdiff import graph
from flowdiff.changes import ChangedSymbol
from flowdiff.graph import Edge, Graph, Node
from flowdiff.lsp import Position, Range, Symbol

ROOT = Path("/repo")


def node(name: str, status: str = "unchanged", line: int = 0) -> Node:
    return Node(name, name, ROOT / "m.py", line, 4, status)


def chain(*names: str, statuses: dict[str, str] | None = None) -> Graph:
    g = Graph()
    for n in names:
        g.add(node(n, (statuses or {}).get(n, "unchanged")))
    for a, b in zip(names, names[1:]):
        g.edges.add(Edge(a, b))
    return g


def test_node_and_edge_defaults():
    n = Node("i", "n", ROOT / "m.py", 1, 2)
    assert n.status == "unchanged" and n.marker == ""
    assert Edge("a", "b").kind == "call"


def test_nodes_and_edges_are_hashable_and_immutable():
    """Nodes are dict keys and edges live in a set, so both must stay frozen."""
    n = Node("i", "n", ROOT / "m.py", 1, 2)
    with pytest.raises(dataclasses.FrozenInstanceError):
        n.status = "body"  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        Edge("a", "b").kind = "registered"  # type: ignore[misc]
    assert len({Edge("a", "b"), Edge("a", "b")}) == 1


def test_components_separates_three_groups():
    g = chain("a", "b")
    g.add(node("c"))
    g.add(node("d"))
    g.edges.add(Edge("c", "d"))
    g.add(node("e"))
    assert sorted(graph.components(g, ["a", "c", "e"], [])) == [["a"], ["c"], ["e"]]


def test_components_keeps_members_in_the_given_order():
    g = chain("a", "b", "c")
    assert graph.components(g, ["c", "a"], []) == [["c", "a"]]


def test_markers_and_stability():
    assert node("a", "body").marker == "~" and node("a", "signature").marker == "~"
    assert node("a", "added").marker == "+" and node("a", "removed").marker == "-"
    assert node("a").marker == "" and node("a", "slot").marker == ""
    assert node("a").stable and node("a", "body").stable
    assert not node("a", "signature").stable and not node("a", "added").stable and not node("a", "slot").stable


def test_callers_and_callees():
    g = chain("a", "b", "c")
    assert g.callers("b") == {"a"} and g.callees("b") == {"c"} and g.callers("a") == set()


def test_components_split_and_join_via_links():
    g = chain("a", "b")
    g.add(node("c"))
    assert sorted(graph.components(g, ["a", "c"], [])) == [["a"], ["c"]]
    assert graph.components(g, ["a", "c"], [("a", "c")]) == [["a", "c"]]


def test_distances_forward_and_backward():
    g = chain("a", "b", "c", "d")
    assert graph.distances(g, "a", 2) == {"a": 0, "b": 1, "c": 2}
    assert graph.distances(g, "d", 3, backwards=True) == {"d": 0, "c": 1, "b": 2, "a": 3}


def test_entry_is_nearest_stable_frame():
    g = chain("a", "b", "c", statuses={"c": "signature"})
    assert graph.select_entry(g, ["c"], 3).id == "b"
    g = chain("a", "b", "c", statuses={"c": "body"})
    assert graph.select_entry(g, ["c"], 3).id == "c"


def test_entry_absent_when_out_of_reach_or_all_removed():
    unstable = {"b": "signature", "c": "signature", "d": "signature", "e": "added"}
    g = chain("a", "b", "c", "d", "e", statuses=unstable)
    assert graph.select_entry(g, ["e"], 3) is None
    assert graph.select_entry(g, ["e"], 4).id == "a"
    g = chain("a", statuses={"a": "removed"})
    assert graph.select_entry(g, ["a"], 3) is None


def test_entry_must_reach_every_changed_symbol():
    g = chain("a", "b", "c", statuses={"c": "added"})
    g.add(node("x", "added"))
    g.edges.add(Edge("a", "x"))
    assert graph.select_entry(g, ["c", "x"], 3).id == "a"


def test_frames_are_on_path_plus_callees():
    g = chain("a", "b", "c", statuses={"c": "body"})
    for extra in ("side", "leaf"):
        g.add(node(extra))
    g.edges.add(Edge("a", "side"))
    g.edges.add(Edge("c", "leaf"))
    frames = graph.frames_for(g, g.nodes["a"], ["c"], 3)
    assert [f.id for f in frames] == ["a", "b", "c", "leaf"]


def test_is_test_path():
    assert graph.is_test_path(ROOT / "tests" / "x.py", ROOT)
    assert graph.is_test_path(ROOT / "pkg" / "test_x.py", ROOT)
    assert graph.is_test_path(ROOT / "pkg" / "x_test.py", ROOT)
    assert not graph.is_test_path(ROOT / "pkg" / "x.py", ROOT)
    assert not graph.is_test_path(Path("/elsewhere/tests/x.py"), ROOT)


def changed(name: str, kind: int, first: int, last: int, status: str) -> ChangedSymbol:
    s = Symbol(name, kind, "", Range(Position(first, 0), Position(last, 0)),
               Range(Position(first, 4), Position(first, 5)), ROOT / "m.py")
    return ChangedSymbol(s, status)


def test_containment_links_join_class_and_method():
    cls = changed("C", 5, 0, 10, "added")
    meth = changed("m", 6, 2, 4, "added")
    other = changed("f", 12, 20, 22, "added")
    links = graph.containment_links([cls, meth, other])
    assert links == [(cls.symbol.id, meth.symbol.id)]


def test_node_of_carries_selection_position():
    c = changed("f", 12, 3, 5, "body")
    n = graph.node_of(c.symbol, c.status)
    assert (n.line, n.col, n.status, n.name) == (3, 4, "body", "f")
