from pathlib import Path

import pytest

from flowdiff import graph, harness_py
from flowdiff.lsp import Location, Position, Range

from conftest import git
from fake_client import FakeClient, symbol


def test_module_name_follows_the_path_and_strips_src_and_init(tmp_path: Path):
    assert harness_py.module_name(tmp_path, tmp_path / "lib.py") == "lib"
    assert harness_py.module_name(tmp_path, tmp_path / "pkg" / "mod.py") == "pkg.mod"
    assert harness_py.module_name(tmp_path, tmp_path / "src" / "pkg" / "mod.py") == "pkg.mod"
    assert harness_py.module_name(tmp_path, tmp_path / "pkg" / "__init__.py") == "pkg"
    assert harness_py.module_name(tmp_path, tmp_path / "src.py") == "src"


def test_frame_id_is_the_posix_relative_path_and_name(tmp_path: Path):
    node = graph.node_of(symbol("f", tmp_path / "pkg" / "m.py", 3))
    assert harness_py.frame_id(tmp_path, node) == "pkg/m.py:f"


P = Path("t.py")


def test_literal_call_lifts_positional_and_keyword_literals():
    site = harness_py.literal_call("x = scale(1, -2, [3, 'a'], k={'b': None})\n", 0, "scale", P)
    assert site == harness_py.CallSite(P, 0, ("1", "-2", "[3, 'a']"), (("k", "{'b': None}"),))
    assert site.render() == "1, -2, [3, 'a'], k={'b': None}"


def test_literal_call_matches_attribute_callees_and_the_callee_line():
    text = "import lib\n\nlib.scale(\n    4,\n)\nlib.scale(5)\n"
    assert harness_py.literal_call(text, 2, "scale", P).args == ("4",)
    assert harness_py.literal_call(text, 5, "scale", P).args == ("5",)
    assert harness_py.literal_call(text, 3, "scale", P) is None


@pytest.mark.parametrize("text", ["scale(x)\n", "scale(*xs)\n", "scale(**kw)\n", "scale(f())\n",
                                  "other(1)\n", "scale(1\n"])
def test_literal_call_refuses_anything_it_would_have_to_invent(text):
    assert harness_py.literal_call(text, 0, "scale", P) is None


def test_literal_call_skips_a_non_literal_call_for_a_literal_one_on_the_same_line():
    assert harness_py.literal_call("scale(x) or scale(2)\n", 0, "scale", P).args == ("2",)


def test_parameters_drop_self_and_cls_and_keep_kwonly():
    text = "class C:\n    def m(self, a, b=1, *, c):\n        pass\n\n\ndef f(x, /, y):\n    pass\n"
    assert harness_py.parameters(text, 1, "m") == ["a", "b", "c"]
    assert harness_py.parameters(text, 5, "f") == ["x", "y"]
    assert harness_py.parameters(text, 0, "f") == []
    assert harness_py.parameters("def (\n", 0, "f") == []


def test_ranked_puts_test_call_sites_first_then_path_then_line(tmp_path: Path):
    sites = [(tmp_path / "b.py", 3), (tmp_path / "a.py", 9), (tmp_path / "tests" / "test_z.py", 7),
             (tmp_path / "a.py", 2)]
    assert harness_py.ranked(tmp_path, sites) == [(tmp_path / "tests" / "test_z.py", 7), (tmp_path / "a.py", 2),
                                                 (tmp_path / "a.py", 9), (tmp_path / "b.py", 3)]


def project(tmp_path: Path) -> tuple[FakeClient, graph.Node, graph.Node]:
    tmp_path.mkdir(exist_ok=True)
    (tmp_path / "lib.py").write_text("def scale(v):\n    return v * 2\n\n\ndef entry(x):\n    return scale(x)\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_lib.py").write_text("from lib import scale\n\n\ndef test_it():\n    assert scale(4) == 8\n")
    scale = symbol("scale", tmp_path / "lib.py", 0, last=1)
    entry = symbol("entry", tmp_path / "lib.py", 4, last=5)
    refs = {"scale": [Location(tmp_path / "lib.py", Range(Position(5, 11), Position(5, 16))),
                      Location(tmp_path / "tests" / "test_lib.py", Range(Position(4, 11), Position(4, 16)))]}
    client = FakeClient(tmp_path, {"scale": scale, "entry": entry}, {"entry": ["scale"]}, references=refs)
    return client, graph.node_of(scale, "body"), graph.node_of(entry)


def test_harvest_opens_the_callers_and_prefers_the_test_call_site(tmp_path: Path):
    client, scale, entry = project(tmp_path)
    site = harness_py.harvest(client, tmp_path, scale, [entry])
    assert site and site.path == tmp_path / "tests" / "test_lib.py" and site.args == ("4",)
    assert client.opened == [tmp_path / "lib.py"]


def test_harvest_is_none_when_every_call_site_passes_variables(tmp_path: Path):
    client, scale, entry = project(tmp_path)
    (tmp_path / "tests" / "test_lib.py").write_text("from lib import scale\n\n\ndef test_it():\n    assert scale(n) == 8\n")
    assert harness_py.harvest(client, tmp_path, scale, []) is None


def test_harvest_in_tree_scans_tracked_files_with_tests_first(repo: Path):
    (repo / "prod.py").write_text("from a import f\nf(9)\n")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_a.py").write_text("from a import f\n\nf(4)\n")
    (repo / "broken.py").write_text("def (\n")
    git(repo, "add", "prod.py", "tests", "broken.py")
    site = harness_py.harvest_in_tree(repo, "f")
    assert site and site.path == repo / "tests" / "test_a.py" and site.args == ("4",)
    (repo / "tests" / "test_a.py").write_text("from a import f\n\nf(n)\n")
    assert harness_py.harvest_in_tree(repo, "f").args == ("9",)
    assert harness_py.harvest_in_tree(repo, "nowhere") is None


def test_call_line_renders_the_site_or_named_empty_slots(tmp_path: Path):
    client, scale, entry = project(tmp_path)
    site = harness_py.CallSite(tmp_path / "t.py", 0, ("4",), (("k", "1"),))
    assert harness_py.call_line(tmp_path, scale, site) == ("lib.scale(4, k=1)", True)
    assert harness_py.call_line(tmp_path, scale, None) == ("lib.scale(v=<v>)", False)


def flow_of(entry, frames, changed=None) -> graph.Flow:
    return graph.Flow(changed or [f for f in frames if f.status != "unchanged"], entry, frames, [], entry is None, [])


def test_build_writes_a_single_body_harness_over_the_flow_frames(tmp_path: Path):
    client, scale, entry = project(tmp_path)
    slot = graph.Node("slot:x", "->x", tmp_path / "lib.py", 0, 0, "slot")
    harness = harness_py.build(client, tmp_path, tmp_path / ".flowdiff" / "base", flow_of(scale, [scale, slot]), [entry])
    assert harness.complete and harness.warnings == ()
    assert "import lib\n" in harness.source
    assert "trace(os.environ[\"FLOWDIFF_TREE\"], ['lib.py:scale'], os.environ[\"FLOWDIFF_OUT\"]):\n    lib.scale(4)\n" \
        in harness.source
    compile(harness.source, "harness", "exec")


def test_build_marks_an_incomplete_harness_when_nothing_lifts(tmp_path: Path):
    client, scale, entry = project(tmp_path)
    (tmp_path / "tests" / "test_lib.py").write_text("from lib import scale\n\n\ndef test_it():\n    assert scale(n) == 8\n")
    harness = harness_py.build(client, tmp_path, tmp_path, flow_of(scale, [scale]), [])
    assert not harness.complete and "lib.scale(v=<v>)" in harness.source


def test_two_body_harvests_each_side_and_warns_when_the_arguments_differ(repo: Path, tmp_path: Path):
    head = tmp_path / "head"
    client, scale, entry = project(head)
    (repo / "lib.py").write_text("def scale(v):\n    return v\n")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_lib.py").write_text("from lib import scale\nscale(7)\n")
    git(repo, "add", "lib.py", "tests")
    harness = harness_py.build(client, head, repo, flow_of(None, [scale]), [])
    assert harness.complete
    assert harness.warnings == ("scale: base and head harvested different arguments; a divergence may reflect "
                                "the inputs rather than the code",)
    assert "if os.environ[\"FLOWDIFF_SIDE\"] == \"base\":\n        lib.scale(7)\n    else:\n        lib.scale(4)\n" \
        in harness.source
    compile(harness.source, "harness", "exec")


def test_two_body_with_matching_arguments_has_no_warning_and_is_incomplete_when_a_side_lacks_a_site(repo: Path, tmp_path: Path):
    head = tmp_path / "head"
    client, scale, entry = project(head)
    (repo / "lib.py").write_text("def scale(v):\n    return v\n")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_lib.py").write_text("from lib import scale\nscale(4)\n")
    git(repo, "add", "lib.py", "tests")
    assert harness_py.build(client, head, repo, flow_of(None, [scale]), []).warnings == ()
    (repo / "tests" / "test_lib.py").write_text("from lib import scale\nscale(n)\n")
    harness = harness_py.build(client, head, repo, flow_of(None, [scale]), [])
    assert not harness.complete and "lib.scale(v=<v>)" in harness.source and "lib.scale(4)" in harness.source


def test_two_body_over_removed_symbols_only_is_incomplete_but_valid(repo: Path, tmp_path: Path):
    head = tmp_path / "head"
    client, scale, entry = project(head)
    removed = graph.node_of(client.symbols["scale"], "removed")
    harness = harness_py.build(client, head, repo, flow_of(None, [removed], [removed]), [])
    assert not harness.complete
    compile(harness.source, "harness", "exec")
