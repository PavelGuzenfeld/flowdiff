import json
from pathlib import Path

from flowdiff import compare


def write(path: Path, *records: dict) -> Path:
    path.write_text("".join(json.dumps(dict(seq=i, **r)) + "\n" for i, r in enumerate(records, 1)))
    return path


def enter(frame, **args):
    return {"event": "enter", "frame": frame, "args": args}


def exit_(frame, value=None, raises=None):
    rec = {"event": "exit", "frame": frame}
    rec.update({"raises": raises} if raises else {"return": value})
    return rec


def test_load_pairs_nested_and_recursive_calls_by_nesting(tmp_path: Path):
    calls = compare.load(write(tmp_path / "t.jsonl", enter("f", n=2), enter("f", n=1), exit_("f", 1), exit_("f", 2)))
    assert [(c.seq, c.end, c.args, c.result) for c in calls["f"]] == [(1, 4, {"n": 2}, 2), (2, 3, {"n": 1}, 1)]


def test_load_records_raises_and_tolerates_missing_or_orphan_records(tmp_path: Path):
    calls = compare.load(write(tmp_path / "t.jsonl", exit_("g", 0), enter("h", x=1), exit_("h", raises="KeyError")))
    assert "g" not in calls and calls["h"][0].raises == "KeyError" and calls["h"][0].values() == {"x": 1, "raises": "KeyError"}
    assert compare.load(tmp_path / "absent.jsonl") == {}


def test_an_unfinished_call_has_no_return_in_its_values():
    call = compare.Call(1, {"x": 1})
    assert call.values() == {"x": 1} and call.observed_at("return") == 1


def report(base, head, frames=("f", "g")) -> compare.Report:
    return compare.Report(list(frames), base, head)


def test_divergences_name_the_field_and_time_them_by_when_head_observed_them():
    base = {"f": [compare.Call(1, {"x": 1}, 4, end=4)], "g": [compare.Call(2, {"y": 1}, 2, end=3)]}
    head = {"f": [compare.Call(1, {"x": 1}, 6, end=4)], "g": [compare.Call(2, {"y": 1}, 3, end=3)]}
    r = report(base, head)
    assert r.divergences("f") == [compare.Divergence("f", 0, "return", 4, 6, 4)]
    assert r.divergences("g") == [compare.Divergence("g", 0, "return", 2, 3, 3)]
    assert r.differing() == ["f", "g"]
    origin = r.origin()
    assert origin and origin.frame == "g" and origin.describe() == "return 2 → 3"


def test_an_argument_divergence_is_timed_at_the_call_start():
    base = {"f": [compare.Call(1, {"x": 1}, 4, end=4)], "g": [compare.Call(2, {"y": 1}, 2, end=3)]}
    head = {"f": [compare.Call(1, {"x": 9}, 4, end=4)], "g": [compare.Call(2, {"y": 1}, 3, end=3)]}
    origin = report(base, head).origin()
    assert origin and (origin.frame, origin.field, origin.at) == ("f", "x", 1)
    assert origin.describe() == "x 1 → 9"


def test_raises_versus_return_is_a_divergence_on_both_fields():
    base = {"f": [compare.Call(1, {}, 4, end=2)]}
    head = {"f": [compare.Call(1, {}, raises="ValueError", end=2)]}
    fields = [(d.field, d.base, d.head) for d in report(base, head).divergences("f")]
    assert fields == [("raises", compare.MISSING, "ValueError"), ("return", 4, compare.MISSING)]
    assert [d.describe() for d in report(base, head).divergences("f")] == ["raises — → \"ValueError\"", "return 4 → —"]


def test_a_call_present_on_one_side_only_is_a_divergence_in_its_own_right():
    base = {"f": [compare.Call(1, {"x": 1}, 2, end=2)]}
    head = {"f": [compare.Call(1, {"x": 1}, 2, end=2), compare.Call(3, {"x": 5}, 6, end=4)]}
    only = report(base, head).divergences("f")
    assert len(only) == 1 and only[0].describe() == "call #2 only on head" and only[0].at == 3
    reverse = report(head, base).divergences("f")
    assert reverse[0].describe() == "call #2 only on base" and reverse[0].at == 3


def test_added_and_removed_frames_do_not_diverge_by_existing_on_one_side_only():
    base = {"f": [compare.Call(1, {"x": 1}, 4, end=6)], "old": [compare.Call(2, {}, 0, end=3)]}
    head = {"f": [compare.Call(1, {"x": 1}, 9, end=6)], "new": [compare.Call(2, {}, 0, end=3)],
            "new": [compare.Call(2, {}, 0, end=3), compare.Call(4, {}, 1, end=5)]}
    r = compare.Report(["f", "new", "old"], base, head, one_sided={"new", "old"})
    assert r.divergences("new") == [] and r.divergences("old") == []
    assert r.differing() == ["f"] and r.traced() == ["f", "new", "old"]
    assert compare.verdict(r) == "1 of 3 frames differ: f (return); origin f"
    plain = compare.Report(["f", "new", "old"], base, head)
    assert plain.differing() == ["f", "new", "old"]
    assert compare.verdict(compare.Report(["new"], {}, {"new": head["new"]}, {"new"})) \
        == "identical: 1 frames traced, no value differs"


def test_traced_keeps_flow_order_and_drops_frames_neither_side_reached():
    r = report({"g": [compare.Call(1)]}, {}, frames=("f", "g", "h"))
    assert r.traced() == ["g"] and r.differing() == ["g"]
    assert r.divergences("f") == [] and r.origin().frame == "g"


def test_verdict_wording_for_each_outcome():
    assert compare.verdict(report({}, {})) == "no flow frame was reached: the harness ran but traced nothing"
    same = {"f": [compare.Call(1, {"x": 1}, 2, end=2)]}
    assert compare.verdict(report(same, same)) == "identical: 1 frames traced, no value differs"
    base = {"f": [compare.Call(1, {"x": 1}, 4, end=4)], "g": [compare.Call(2, {}, 2, end=3)]}
    head = {"f": [compare.Call(1, {"x": 1}, 6, end=4)], "g": [compare.Call(2, {}, 3, end=3)]}
    assert compare.verdict(report(base, head, frames=("lib.py:f", "g")).__class__(["lib.py:f", "lib.py:g"],
                           {"lib.py:f": base["f"], "lib.py:g": base["g"]},
                           {"lib.py:f": head["f"], "lib.py:g": head["g"]})) \
        == "2 of 2 frames differ: f (return); g (return); origin g"


def obj(type_name: str, **fields) -> dict:
    return {"type": type_name, "fields": fields}


def test_leaves_fold_object_fields_and_descend_into_dicts_and_equal_length_lists():
    base = obj("Cfg", a=1, tags=["x", "y"], inner=obj("In", k=1), gone=0, d={"p": 1, "q": 2})
    head = obj("Cfg", a=1, tags=["x", "z"], inner=obj("In", k=2), new=True, d={"p": 1, "q": 3, "r": 4})
    assert compare.leaves(base, head, "return") == [
        ("return.tags[1]", "y", "z"), ("return.inner.k", 1, 2), ("return.gone", 0, compare.MISSING),
        ("return.d.q", 2, 3), ("return.d.r", compare.MISSING, 4), ("return.new", compare.MISSING, True)]
    assert compare.leaves(obj("A", x=1), obj("B", x=1), "r") == [("r", obj("A", x=1), obj("B", x=1))]
    assert compare.leaves([1, 2], [1, 2, 3], "r") == [("r", [1, 2], [1, 2, 3])]
    assert compare.leaves({"k": obj("A", x=1)}, {"k": 5}, "r") == [("r.k", obj("A", x=1), 5)]
    assert compare.leaves(obj("A", x=1), obj("A", x=1), "r") == [] and compare.leaves(3, 3, "r") == []


def test_divergences_are_per_leaf_and_the_verdict_names_paths_not_values():
    base = {"f": [compare.Call(1, {"cfg": obj("Cfg", a=1, b=[1, 2])}, obj("Out", ok=True), end=2)]}
    head = {"f": [compare.Call(1, {"cfg": obj("Cfg", a=1, b=[1, 3], c=None)}, obj("Out", ok=False), end=2)]}
    r = report(base, head, frames=("f",))
    assert [d.field for d in r.divergences("f")] == ["cfg.b[1]", "cfg.c", "return.ok"]
    assert r.changed_paths("f") == "cfg.b[1], cfg.c, return.ok"
    assert r.changed_paths("f", limit=2) == "cfg.b[1], cfg.c, +1 more"
    assert compare.verdict(r) == "1 of 1 frames differ: f (cfg.b[1], cfg.c, return.ok); origin f"


def test_changed_paths_dedupe_across_calls_and_name_one_sided_calls():
    base = {"f": [compare.Call(1, {"x": 1}, 4, end=2), compare.Call(3, {"x": 1}, 4, end=4)]}
    head = {"f": [compare.Call(1, {"x": 1}, 5, end=2), compare.Call(3, {"x": 1}, 6, end=4), compare.Call(5, {}, 0, end=6)]}
    assert report(base, head, frames=("f",)).changed_paths("f") == "return, call #3 only on head"


def test_show_prints_differing_leaves_and_groups_identical_calls():
    base = {"f": [compare.Call(1, {"x": 1, "y": "a"}, 4, end=2)]}
    head = {"f": [compare.Call(1, {"x": 1}, 6, end=2), compare.Call(3, {"x": 2}, 8, end=4)]}
    assert compare.show(report(base, head), "f").splitlines() == [
        "f  base 1 call(s), head 2 call(s)",
        "  calls #1", '    y       "a"  →  —', "    return  4  →  6",
        "  calls #2", "    x       —  →  2", "    return  —  →  8"]


def test_show_groups_repeated_outcomes_and_counts_identical_calls():
    same_in = {"lang": "py"}
    base = {"f": [compare.Call(i, same_in, obj("Cfg", a=1), end=i) for i in (1, 3, 4, 7)] + [compare.Call(5, {}, None, end=5)]}
    head = {"f": [compare.Call(1, same_in, obj("Cfg", a=1, n=True), end=1), compare.Call(3, same_in, obj("Cfg", a=1, n=True), end=3),
                  compare.Call(4, same_in, obj("Cfg", a=1, n=True), end=4), compare.Call(7, same_in, obj("Cfg", a=1, n=False), end=7),
                  compare.Call(5, {}, None, end=5)]}
    assert compare.show(report(base, head), "f").splitlines() == [
        "f  base 5 call(s), head 5 call(s)",
        "  calls #1-#3 (×3)", "    return.n  —  →  true",
        "  calls #4", "    return.n  —  →  false",
        "  1 identical call(s): #5"]
    assert compare.call_labels([0, 2, 3, 4, 5, 8]) == "#1,#3-#6,#9"


def test_show_with_a_path_prints_that_leaf_whole_for_every_call_even_when_equal():
    base = {"f": [compare.Call(1, {"x": 1}, obj("Cfg", tags=["a", "b"]), end=2)]}
    head = {"f": [compare.Call(1, {"x": 1, "z": 0}, obj("Cfg", tags=["a", "c"]), end=2)]}
    r = report(base, head)
    assert compare.show(r, "f", "x").splitlines()[1:] == ["  calls #1", "    x  1  →  1"]
    assert compare.show(r, "f", "z").splitlines()[1:] == ["  calls #1", "    z  —  →  0"]
    assert compare.show(r, "f", "return.tags[1]").splitlines()[2] == '    return.tags[1]  "b"  →  "c"'
    assert compare.show(r, "f", "return.tags").splitlines()[2] == '    return.tags  ["a", "b"]  →  ["a", "c"]'
    assert compare.show(r, "f", "return.nope").splitlines()[2] == "    return.nope  —  →  —"
    assert compare.show(r, "f", "y").splitlines()[1:] == ["  1 identical call(s): #1"]


def test_show_caps_the_path_column_so_one_long_path_does_not_push_the_rest():
    assert compare.PATH_COLUMN == 40
    long_key = "k" * 70
    base = {"f": [compare.Call(1, {"d": {long_key: 1, "s": 1}}, 0, end=2)]}
    head = {"f": [compare.Call(1, {"d": {long_key: 2, "s": 2}}, 0, end=2)]}
    lines = compare.show(report(base, head), "f").splitlines()
    assert lines[2] == f"    d.{long_key}  1  →  2"
    assert lines[3] == "    d.s" + " " * (40 - 3) + "  1  →  2"


def test_load_keeps_the_driving_test_of_each_call(tmp_path: Path):
    calls = compare.load(write(tmp_path / "t.jsonl", {**enter("f", n=1), "test": "t.py::a"}, exit_("f", 1),
                               enter("f", n=2), exit_("f", 2)))
    assert [c.test for c in calls["f"]] == ["t.py::a", None]


def test_show_labels_call_groups_with_the_tests_that_drove_them():
    base = {"f": [compare.Call(1, {"x": 1}, 4, end=2, test="t.py::a"), compare.Call(3, {"x": 1}, 4, end=4, test="t.py::b"),
                  compare.Call(5, {"x": 1}, 4, end=6, test="t.py::c"), compare.Call(7, {"x": 1}, 9, end=8, test="t.py::d")]}
    head = {"f": [compare.Call(1, {"x": 1}, 5, end=2, test="t.py::a"), compare.Call(3, {"x": 1}, 5, end=4, test="t.py::b"),
                  compare.Call(5, {"x": 1}, 5, end=6, test="t.py::c"), compare.Call(7, {"x": 1}, 9, end=8)]}
    lines = compare.show(report(base, head), "f").splitlines()
    assert lines[1] == "  calls #1-#3 (×3)  ← t.py::a, t.py::b, +1 more"
    assert lines[3] == "  1 identical call(s): #4"
    untested = {"f": [compare.Call(1, {"x": 1}, 4, end=2)]}
    assert compare.show(report(untested, {"f": [compare.Call(1, {"x": 1}, 5, end=2)]}), "f").splitlines()[1] == "  calls #1"


def test_show_call_prints_one_call_whole_and_marks_the_differing_rows():
    base = {"f": [compare.Call(1, {"x": 1, "cfg": obj("Cfg", a=1)}, obj("Out", ok=True), end=2, test="t.py::a")]}
    head = {"f": [compare.Call(1, {"x": 1, "cfg": obj("Cfg", a=1, n=True)}, obj("Out", ok=False), end=2, test="t.py::a")]}
    r = report(base, head)
    assert compare.show_call(r, "f", 0).splitlines() == [
        "f  call #1  ← t.py::a",
        "    x          1",
        "    cfg.a      1",
        "    cfg.n      —  →  true   *",
        "    return.ok  true  →  false   *"]
    assert compare.show_call(r, "f", 0, "cfg.n").splitlines()[1] == "    cfg.n  —  →  true   *"
    assert compare.show_call(r, "f", 0, "return.ok").splitlines()[1] == "    return.ok  true  →  false   *"
    assert compare.show_call(r, "f", 0, "cfg").splitlines()[1:] == ["    cfg.a  1", "    cfg.n  —  →  true   *"]
    assert compare.show_call(r, "f", 0, "nope").splitlines()[1] == "    nope: not an argument of this call"
    assert compare.show_call(r, "f", 3) == "f  has no call #4"
    only_head = compare.Report(["f"], {}, head)
    assert compare.show_call(only_head, "f", 0).splitlines()[1].split() == ["x", "—", "→", "1", "*"]


def test_show_call_caps_the_rows_and_says_how_to_narrow():
    wide = {f"k{i}": i for i in range(70)}
    r = compare.Report(["lib.py:f"], {"lib.py:f": [compare.Call(1, {"d": wide}, 0, end=2)]},
                       {"lib.py:f": [compare.Call(1, {"d": wide}, 0, end=2)]})
    lines = compare.show_call(r, "lib.py:f", 0).splitlines()
    assert compare.MAX_CALL_ROWS == 60 and len(lines) == 62
    assert lines[-1] == "    … +11 more leaves; narrow with f#1/path"


def test_pretty_renders_markers_objects_and_containers_short():
    assert compare.pretty({"type": "pathlib.PosixPath"}) == "<pathlib.PosixPath>"
    assert compare.pretty({"type": "str", "len": 69, "sha256": "b1a22781e3e6fc48"}) == "str[69]#b1a22781"
    assert compare.pretty({"type": "numpy.ndarray", "shape": [2, 2], "dtype": "float32", "sha256": "abcdef0123"}) \
        == "numpy.ndarray[2, 2] float32#abcdef01"
    assert compare.pretty(obj("flowdiff.lsp.ServerConfig", a=1, p={"type": "P"})) == "ServerConfig(a=1, p=<P>)"
    assert compare.pretty([1, "s", obj("T")]) == '[1, "s", T()]' and compare.pretty({"k": None}) == "{k: null}"
    assert compare.pretty(compare.MISSING) == "—" and compare.pretty(2.5) == "2.5"


def test_flatten_folds_objects_expands_lists_of_objects_and_keeps_leaf_lists():
    value = obj("Client", config=obj("Cfg", langs=["py", "cpp"], n=1), root={"type": "P"},
                nodes=[obj("Node", name="a"), obj("Node", name="b")], empty=obj("E"), table={})
    assert compare.flatten(value, "client") == [
        ("client.config.langs", ["py", "cpp"]), ("client.config.n", 1), ("client.root", {"type": "P"}),
        ("client.nodes[0].name", "a"), ("client.nodes[1].name", "b"), ("client.empty", {"type": "E"}),
        ("client.table", {})]
    assert compare.flatten([[1, 2], [3]], "m") == [("m[0]", [1, 2]), ("m[1]", [3])]
    assert compare.flatten(compare.MISSING, "x") == [("x", compare.MISSING)]


def test_descend_follows_fields_and_indices_and_stops_at_missing():
    value = obj("Cfg", tags=["a", {"k": 7}], inner=obj("In", n=1))
    assert compare.descend(value, ".tags[1].k") == 7 and compare.descend(value, ".inner.n") == 1
    assert compare.descend(value, ".tags[5]") is compare.MISSING and compare.descend(value, ".zzz.k") is compare.MISSING
    assert compare.descend(compare.MISSING, ".a") is compare.MISSING and compare.descend(3, "") == 3


def test_brief_cuts_at_sixty_characters_inclusive():
    assert compare.BRIEF_LIMIT == 60
    exact = "x" * 58
    assert compare.brief(exact) == json.dumps(exact) and len(json.dumps(exact)) == 60
    cut = compare.brief("x" * 59)
    assert len(cut) == 60 and cut.endswith("…") and cut.startswith('"xxx')


def test_describe_and_show_use_brief_values_but_a_named_argument_is_whole():
    big = {"type": "Thing", "fields": {str(i): i for i in range(30)}}
    base = {"f": [compare.Call(1, {"obj": big}, 1, end=2)]}
    head = {"f": [compare.Call(1, {"obj": 0}, 1, end=2)]}
    d = report(base, head).divergences("f")[0]
    assert d.describe().startswith("obj Thing(0=0, 1=1") and d.describe().endswith("… → 0")
    assert len(d.describe()) < 80
    table = compare.show(report(base, head), "f")
    assert "…" in table and "29=29" not in table
    whole = compare.show(report(base, head), "f", "obj")
    assert "Thing(0=0, 1=1" in whole and "29=29)  →  0" in whole and whole.splitlines()[1] == "  calls #1"


def test_resolve_accepts_the_short_name_or_the_full_frame():
    r = report({}, {}, frames=("lib.py:f", "pkg/m.py:f", "lib.py:g"))
    assert compare.resolve(r, "f") == ["lib.py:f", "pkg/m.py:f"]
    assert compare.resolve(r, "lib.py:g") == ["lib.py:g"] and compare.resolve(r, "nope") == []
    assert compare.short("pkg/m.py:f") == "f" and compare.short("f") == "f"
