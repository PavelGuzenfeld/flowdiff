import json
from pathlib import Path

from flowdiff import cli, compare, keep, worktree


def test_dialect_detection_prefers_pytest_then_unittest_then_plain(tmp_path: Path):
    tests = tmp_path / "tests"
    assert keep.detect_dialect(tests) == "plain"
    tests.mkdir()
    (tests / "test_a.py").write_text("import unittest\n\n\nclass T(unittest.TestCase):\n    pass\n")
    assert keep.detect_dialect(tests) == "unittest"
    (tests / "sub").mkdir()
    (tests / "sub" / "test_b.py").write_text("from pytest import raises\n")
    assert keep.detect_dialect(tests) == "pytest"
    (tests / "test_a.py").write_text("x = 'import pytest'\n")
    (tests / "sub" / "test_b.py").write_text("# import pytest\n    def test_method(self):\n        pass\n")
    assert keep.detect_dialect(tests) == "plain"
    (tests / "test_c.py").write_text("from lib import f\n\n\ndef test_f():\n    assert f(1) == 2\n")
    assert keep.detect_dialect(tests) == "pytest"


def test_test_dir_prefers_an_existing_test_directory(tmp_path: Path):
    assert keep.TEST_DIR_PRIORITY == ("tests", "test", "spec", "specs")
    assert keep.test_dir_of(tmp_path) == tmp_path / "tests"
    (tmp_path / "specs").mkdir()
    assert keep.test_dir_of(tmp_path) == tmp_path / "specs"
    (tmp_path / "spec").mkdir()
    assert keep.test_dir_of(tmp_path) == tmp_path / "spec"
    (tmp_path / "test").mkdir()
    assert keep.test_dir_of(tmp_path) == tmp_path / "test"
    (tmp_path / "tests").mkdir()
    assert keep.test_dir_of(tmp_path) == tmp_path / "tests"


def test_literal_reproduces_json_values_and_refuses_stand_ins():
    assert keep.literal(4) == "4" and keep.literal(True) == "True" and keep.literal(None) == "None"
    assert keep.literal("s") == "'s'" and keep.literal([1, "a"]) == "[1, 'a']"
    assert keep.literal({"k": [1, {"n": None}]}) == "{'k': [1, {'n': None}]}"
    assert keep.literal({"type": "pathlib.PosixPath"}) is None
    assert keep.literal([1, {"type": "str", "len": 90, "sha256": "ab"}]) is None
    assert keep.literal({"k": {"type": "T", "fields": {}}}) is None


def test_call_source_orders_positional_then_star_arguments():
    assert keep.call_source("lib", "f", {"x": 1, "y": "a"}) == "lib.f(1, 'a')"
    assert keep.call_source("pkg.m", "f", {"x": 1, "*rest": [2, 3], "**opts": {"k": "v"}}) \
        == "pkg.m.f(1, *[2, 3], **{'k': 'v'})"
    assert keep.call_source("lib", "f", {"x": {"type": "Obj", "fields": {}}}) is None


def test_cases_keep_distinct_literal_calls_and_fall_back_to_summaries_for_returns():
    calls = [compare.Call(1, {"v": 4}, 8, end=2), compare.Call(3, {"v": 4}, 8, end=4),
             compare.Call(5, {"v": 5}, {"type": "P", "fields": {"a": 1}}, end=6),
             compare.Call(7, {"v": {"type": "Obj", "fields": {}}}, 1, end=8),
             compare.Call(9, {"v": 6}, raises="ValueError", end=10), compare.Call(11, {"v": 7})]
    assert keep.cases("lib", "scale", calls) == ([
        keep.Case("lib.scale(4)", (), (("lib.scale(4)", "8", True),)),
        keep.Case("lib.scale(5)", (), (("lib.scale(5)", '{"type": "P", "fields": {"a": 1}}', False),))], [])
    many = [compare.Call(i, {"v": i}, i, end=i) for i in range(20)]
    assert len(keep.cases("lib", "f", many)[0]) == keep.MAX_CASES == 12


def test_emit_pytest_dialect():
    found = [keep.Case("lib.scale(4)", (), (("lib.scale(4)", "8", True),)),
             keep.Case("lib.scale(5)", (), (("lib.scale(5)", '{"type": "P"}', False),))]
    assert keep.emit("lib", "scale", found, "pytest") == (
        '"""Golden flow test written by flowdiff keep; rerun keep after a deliberate behaviour change."""\n'
        "import lib\nfrom flowdiff.summarise import summarise\n\n\n"
        "def test_flow_scale_1():\n    assert lib.scale(4) == 8\n\n\n"
        "def test_flow_scale_2():\n    assert summarise(lib.scale(5)) == {\"type\": \"P\"}\n")


def test_emit_unittest_and_plain_dialects():
    found = [keep.Case("lib.scale(4)", (), (("lib.scale(4)", "8", True),))]
    assert keep.emit("lib", "scale", found, "unittest") == (
        keep.HEADER + "import unittest\nimport lib\n\n\n"
        "class FlowScale(unittest.TestCase):\n    def test_1(self):\n        self.assertEqual(lib.scale(4), 8)\n")
    assert keep.emit("lib", "scale", found, "plain") == keep.HEADER + "import lib\n\n\nassert lib.scale(4) == 8\n"
    assert compile(keep.emit("lib", "scale", found, "unittest"), "t", "exec")
    assert compile(keep.emit("lib", "snake_name", found, "pytest"), "t", "exec")


def test_cpp_dialect_detection_order(tmp_path: Path):
    tests = tmp_path / "tests"
    assert keep.detect_cpp_dialect(tests) == ("plain", "#include <cassert>")
    tests.mkdir()
    (tests / "t_doc.cpp").write_text('#include "doctest.h"\n')
    assert keep.detect_cpp_dialect(tests) == ("doctest", "#include <doctest/doctest.h>")
    (tests / "t_catch.cpp").write_text("#include <catch2/catch_test_macros.hpp>\n")
    assert keep.detect_cpp_dialect(tests)[0] == "catch2"
    (tests / "t_g.cpp").write_text("#include <gtest/gtest.h>\n")
    assert keep.detect_cpp_dialect(tests) == ("gtest", "#include <gtest/gtest.h>")
    (tests / "t_local.cpp").write_text('#include "harness/test_harness.h"\nTEST(x) {}\n')
    assert keep.detect_cpp_dialect(tests) == ("harness", '#include "harness/test_harness.h"')


def test_cpp_literals_and_cases():
    assert [keep.cpp_literal(v) for v in (True, 3, 2.5, "s", None)] == ["true", "3", "2.5", '"s"', "nullptr"]
    assert keep.cpp_literal({"type": "T", "fields": {}}) is None and keep.cpp_literal([1]) is None
    calls = [compare.Call(1, {"x": 4, "y": True}, {"type": "R", "fields": {"ok": True, "code": 0, "detail": {"type": "P"}}}, end=2),
             compare.Call(3, {"x": 4, "y": True}, {"type": "R", "fields": {"ok": True, "code": 0}}, end=4),
             compare.Call(5, {"x": 7}, 9, end=6),
             compare.Call(7, {"x": {"type": "Obj", "fields": {}}}, 1, end=8),
             compare.Call(9, {"x": 1}, {"type": "Opaque"}, end=10),
             compare.Call(11, {"x": 1}, raises="unwound", end=12)]
    assert keep.cpp_cases("ns::f", calls) == ([
        keep.Case("ns::f(4, true)", (), (("result.ok", "true", True), ("result.code", "0", True))),
        keep.Case("ns::f(7)", (), (("result", "9", True),))], [])


def test_emit_cpp_in_the_local_harness_dialect_and_plain():
    found = [keep.Case("ns::f(4, true)", (), (("result.ok", "true", True), ("result.code", "0", True))),
             keep.Case("ns::f(7)", (), (("result", "9", True),))]
    text = keep.emit_cpp("f.hpp", "ns::f", found, "harness", '#include "test_harness.h"')
    assert text.splitlines()[:3] == ["// Golden flow test written by flowdiff keep; rerun keep after a deliberate behaviour change.",
                                     '#include "f.hpp"', '#include "test_harness.h"']
    assert "TEST(flow_f_1) {\n    auto result = ns::f(4, true);\n    ASSERT_EQ(result.ok, true);\n    ASSERT_EQ(result.code, 0);\n}" in text
    assert "TEST(flow_f_2) {\n    auto result = ns::f(7);\n    ASSERT_EQ(result, 9);\n}" in text
    plain = keep.emit_cpp("f.hpp", "ns::f", found[1:], "plain", "#include <cassert>")
    assert "static void flow_f_1() {\n    auto result = ns::f(7);\n    assert(result == 9);\n}" in plain
    assert plain.rstrip().endswith("int main() {\n    flow_f_1();\n    return 0;\n}")
    gtest = keep.emit_cpp("f.hpp", "ns::f", found[1:], "gtest", "#include <gtest/gtest.h>")
    assert "TEST(FlowKeep, flow_f_1) {" in gtest and "EXPECT_EQ(result, 9);" in gtest


def test_keep_writes_a_cpp_test_from_gdb_traces(repo: Path, capsys):
    run = repo / ".flowdiff" / "run"
    run.mkdir(parents=True)
    (repo / "gst").mkdir()
    (repo / "gst" / "t.cpp").write_text("")
    (repo / "gst" / "t.hpp").write_text("")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_x.cpp").write_text('#include "test_harness.h"\n')
    (run / "h.jsonl").write_text("".join(json.dumps(r) + "\n" for r in [
        {"seq": 1, "event": "enter", "frame": "gst/t.cpp:ns::scale", "args": {"v": 4}},
        {"seq": 2, "event": "exit", "frame": "gst/t.cpp:ns::scale", "return": {"type": "R", "fields": {"ok": True}}}]))
    (run / "index.json").write_text(json.dumps([{"flow": 1, "frames": ["gst/t.cpp:ns::scale"], "base": str(run / "b.jsonl"),
                                                 "head": str(run / "h.jsonl"), "driver": None,
                                                 "names": {"gst/t.cpp:ns::scale": "geo::ns::scale"}}]))
    assert cli.main(["keep", "scale", "--repo", str(repo)]) == 0
    assert capsys.readouterr().out == "tests/flow_scale.cpp: 1 case(s), harness dialect; add it to the build to run it\n"
    written = (repo / "tests" / "flow_scale.cpp").read_text()
    assert '#include "t.hpp"' in written and "ASSERT_EQ(result.ok, true);" in written
    assert "auto result = geo::ns::scale(4);" in written
    (run / "h.jsonl").write_text(json.dumps({"seq": 1, "event": "enter", "frame": "gst/t.cpp:ns::scale",
                                             "args": {"v": {"type": "Buf", "fields": {}}}}) + "\n")
    assert cli.main(["keep", "scale", "--repo", str(repo)]) == 2
    assert "no recorded call has scalar arguments" in capsys.readouterr().err


def recorded(repo: Path, driver: str | None) -> None:
    run = repo / ".flowdiff" / "run"
    run.mkdir(parents=True)
    (run / "h.jsonl").write_text("".join(json.dumps(r) + "\n" for r in [
        {"seq": 1, "event": "enter", "frame": "a.py:g", "args": {"y": 3}},
        {"seq": 2, "event": "enter", "frame": "a.py:f", "args": {"x": 3}},
        {"seq": 3, "event": "exit", "frame": "a.py:f", "return": 4},
        {"seq": 4, "event": "exit", "frame": "a.py:g", "return": 8}]))
    (run / "index.json").write_text(json.dumps([{"flow": 1, "frames": ["a.py:g", "a.py:f"], "base": str(run / "b.jsonl"),
                                                 "head": str(run / "h.jsonl"), "driver": driver}]))


def test_keep_writes_the_driver_by_default_in_the_detected_dialect(repo: Path, capsys):
    recorded(repo, "a.py:g")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_x.py").write_text("import pytest\n")
    assert cli.main(["keep", "--repo", str(repo)]) == 0
    assert capsys.readouterr().out == "tests/flow_g.py: 1 case(s), pytest dialect; register it with your test runner if it needs registering\n"
    assert (repo / "tests" / "flow_g.py").read_text().endswith("def test_flow_g_1():\n    assert a.g(3) == 8\n")


def test_keep_names_a_frame_and_creates_the_test_dir_when_missing(repo: Path, capsys):
    recorded(repo, None)
    assert cli.main(["keep", "f", "--repo", str(repo)]) == 0
    assert "tests/flow_f.py: 1 case(s), plain dialect" in capsys.readouterr().out
    assert (repo / "tests" / "flow_f.py").read_text() == keep.HEADER + "import a\n\n\nassert a.f(3) == 4\n"


def test_keep_error_paths(repo: Path, capsys, tmp_path: Path):
    assert cli.main(["keep", "--repo", str(repo)]) == 2
    assert "run flowdiff play first" in capsys.readouterr().err
    recorded(repo, None)
    assert cli.main(["keep", "--repo", str(repo)]) == 2
    assert "driven by covering tests" in capsys.readouterr().err
    assert cli.main(["keep", "nope", "--repo", str(repo)]) == 2
    assert "not a frame of the last play" in capsys.readouterr().err
    (repo / ".flowdiff" / "run" / "h.jsonl").write_text(json.dumps(
        {"seq": 1, "event": "enter", "frame": "a.py:f", "args": {"x": {"type": "Obj", "fields": {}}}}) + "\n")
    assert cli.main(["keep", "f", "--repo", str(repo)]) == 2
    assert "no recorded call has literal arguments" in capsys.readouterr().err
    assert cli.main(["keep", "--repo", str(tmp_path / "nowhere")]) == 1


def test_keep_reads_a_ranges_recordings_and_writes_the_test_in_the_repo(repo: Path, capsys):
    head = worktree.head_worktree(repo, "HEAD")
    recorded(head, "a.py:g")
    worktree.remember_run(repo, head)
    assert cli.main(["keep", "--repo", str(repo)]) == 0
    assert capsys.readouterr().out.startswith("tests/flow_g.py: 1 case(s)")
    assert (repo / "tests" / "flow_g.py").is_file()
    assert not (head / "tests").exists()


def mutating(args: dict, after: dict, result=0) -> compare.Call:
    return compare.Call(1, args, result, end=2, after=after)


def test_a_mutated_argument_is_bound_to_a_name_and_asserted_after_the_call():
    call = mutating({"items": [1, 2], "n": 3}, {"items": [1, 2, 3]}, result=3)
    found, unpinned = keep.cases("lib", "fill", [call])
    assert unpinned == []
    assert found == [keep.Case("lib.fill(items, 3)", (("items", "[1, 2]"),),
                               (("result", "3", True), ("items", "[1, 2, 3]", True)))]
    assert keep.emit("lib", "fill", found, "pytest") == (
        keep.HEADER + "import lib\n\n\n"
        "def test_flow_fill_1():\n    items = [1, 2]\n    result = lib.fill(items, 3)\n"
        "    assert result == 3\n    assert items == [1, 2, 3]\n")


def test_an_argument_the_body_left_alone_is_not_bound():
    call = mutating({"items": [1, 2]}, {"items": [1, 2]}, result=2)
    found, unpinned = keep.cases("lib", "size", [call])
    assert found == [keep.Case("lib.size([1, 2])", (), (("lib.size([1, 2])", "2", True),))] and unpinned == []


def test_a_mutated_argument_no_literal_can_stand_in_for_is_named_instead():
    call = mutating({"sink": {"type": "Writer", "fields": {}}, "n": 1},
                    {"sink": {"type": "Writer", "fields": {"count": 1}}})
    found, unpinned = keep.cases("lib", "emit", [call])
    assert found == [] and unpinned == ["sink"]
    assert keep.unpinned_note(unpinned) == "; mutated but not asserted: sink"
    assert keep.unpinned_note([]) == ""


def test_a_mutated_argument_whose_name_the_test_cannot_introduce_is_not_bound():
    for arg in ("lib", "result", "class", "*rest"):
        call = mutating({arg: [1]}, {arg: [1, 2]})
        found, unpinned = keep.cases("lib", "fill", [call])
        assert unpinned == [arg.lstrip("*")], arg
    assert keep.bindable("lib", "items") and not keep.bindable("pkg.lib", "pkg")


def test_bound_cases_in_the_unittest_and_plain_dialects():
    found = [keep.Case("lib.fill(items, 3)", (("items", "[1, 2]"),),
                       (("result", "3", True), ("items", "[1, 2, 3]", True)))]
    assert keep.emit("lib", "fill", found, "unittest") == (
        keep.HEADER + "import unittest\nimport lib\n\n\n"
        "class FlowFill(unittest.TestCase):\n    def test_1(self):\n        items = [1, 2]\n"
        "        result = lib.fill(items, 3)\n        self.assertEqual(result, 3)\n"
        "        self.assertEqual(items, [1, 2, 3])\n")
    assert keep.emit("lib", "fill", found, "plain") == (
        keep.HEADER + "import lib\n\n\nitems = [1, 2]\nresult = lib.fill(items, 3)\n"
        "assert result == 3\nassert items == [1, 2, 3]\n")
    for dialect in ("pytest", "unittest", "plain"):
        assert compile(keep.emit("lib", "fill", found, dialect), "t", "exec")


def test_a_mutated_scalar_reference_is_bound_and_asserted_in_cpp():
    call = compare.Call(1, {"v": 4, "n": 2}, 8, end=2, after={"v": 7})
    found, unpinned = keep.cpp_cases("ns::scale", [call])
    assert unpinned == []
    assert found == [keep.Case("ns::scale(v, 2)", (("v", "4"),), (("result", "8", True), ("v", "7", True)))]
    text = keep.emit_cpp("f.hpp", "ns::scale", found, "harness", '#include "test_harness.h"')
    assert ("TEST(flow_scale_1) {\n    auto v = 4;\n    auto result = ns::scale(v, 2);\n"
            "    ASSERT_EQ(result, 8);\n    ASSERT_EQ(v, 7);\n}") in text


def test_a_mutated_cpp_argument_that_is_not_scalar_is_named_instead():
    call = compare.Call(1, {"out": {"type": "R", "fields": {"ok": False}}}, 0, end=2,
                        after={"out": {"type": "R", "fields": {"ok": True}}})
    found, unpinned = keep.cpp_cases("ns::fill", [call])
    assert found == [] and unpinned == ["out"]
