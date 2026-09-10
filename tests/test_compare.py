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
    assert fields == [("raises", None, "ValueError"), ("return", 4, None)]


def test_a_call_present_on_one_side_only_is_a_divergence_in_its_own_right():
    base = {"f": [compare.Call(1, {"x": 1}, 2, end=2)]}
    head = {"f": [compare.Call(1, {"x": 1}, 2, end=2), compare.Call(3, {"x": 5}, 6, end=4)]}
    only = report(base, head).divergences("f")
    assert len(only) == 1 and only[0].describe() == "call #2 only on head" and only[0].at == 3
    reverse = report(head, base).divergences("f")
    assert reverse[0].describe() == "call #2 only on base" and reverse[0].at == 3


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
        == "2 of 2 frames differ; origin g: return 2 → 3"


def test_show_lays_out_base_and_head_columns_and_marks_differences():
    base = {"f": [compare.Call(1, {"x": 1, "y": "a"}, 4, end=2)]}
    head = {"f": [compare.Call(1, {"x": 1}, 6, end=2), compare.Call(3, {"x": 2}, 8, end=4)]}
    out = compare.show(report(base, head), "f")
    lines = out.splitlines()
    assert lines[0] == "f  base 1 call(s), head 2 call(s)"
    assert lines[1].split() == ["#1", "x", "1", "1"]
    assert lines[2].split() == ["#1", "y", '"a"', "—", "*"]
    assert lines[3].split() == ["#1", "return", "4", "6", "*"]
    assert lines[4].split() == ["#2", "x", "—", "2", "*"]
    assert lines[5].split() == ["#2", "return", "—", "8", "*"]


def test_show_can_narrow_to_one_argument_and_keeps_head_only_arguments():
    base = {"f": [compare.Call(1, {"x": 1}, 4, end=2)]}
    head = {"f": [compare.Call(1, {"x": 1, "z": 0}, 4, end=2)]}
    assert compare.show(report(base, head), "f", "x").splitlines()[1:] == ["  #1   x              1                            1"]
    assert compare.show(report(base, head), "f", "z").splitlines()[1].split() == ["#1", "z", "—", "0", "*"]


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
    assert d.describe().startswith("obj {\"type\": \"Thing\"") and d.describe().endswith("… → 0")
    assert len(d.describe()) < 80
    table = compare.show(report(base, head), "f")
    assert "…" in table and json.dumps(big) not in table
    whole = compare.show(report(base, head), "f", "obj")
    assert json.dumps(big) in whole


def test_resolve_accepts_the_short_name_or_the_full_frame():
    r = report({}, {}, frames=("lib.py:f", "pkg/m.py:f", "lib.py:g"))
    assert compare.resolve(r, "f") == ["lib.py:f", "pkg/m.py:f"]
    assert compare.resolve(r, "lib.py:g") == ["lib.py:g"] and compare.resolve(r, "nope") == []
    assert compare.short("pkg/m.py:f") == "f" and compare.short("f") == "f"
