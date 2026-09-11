import json
from pathlib import Path

from flowdiff import cli, compare, keep


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
    assert keep.cases("lib", "scale", calls) == [
        ("lib.scale(4)", "8", True), ("lib.scale(5)", '{"type": "P", "fields": {"a": 1}}', False)]
    many = [compare.Call(i, {"v": i}, i, end=i) for i in range(20)]
    assert len(keep.cases("lib", "f", many)) == keep.MAX_CASES == 12


def test_emit_pytest_dialect():
    found = [("lib.scale(4)", "8", True), ("lib.scale(5)", '{"type": "P"}', False)]
    assert keep.emit("lib", "scale", found, "pytest") == (
        '"""Golden flow test written by flowdiff keep; rerun keep after a deliberate behaviour change."""\n'
        "import lib\nfrom flowdiff.summarise import summarise\n\n\n"
        "def test_flow_scale_1():\n    assert lib.scale(4) == 8\n\n\n"
        "def test_flow_scale_2():\n    assert summarise(lib.scale(5)) == {\"type\": \"P\"}\n")


def test_emit_unittest_and_plain_dialects():
    found = [("lib.scale(4)", "8", True)]
    assert keep.emit("lib", "scale", found, "unittest") == (
        keep.HEADER + "import unittest\nimport lib\n\n\n"
        "class FlowScale(unittest.TestCase):\n    def test_1(self):\n        self.assertEqual(lib.scale(4), 8)\n")
    assert keep.emit("lib", "scale", found, "plain") == keep.HEADER + "import lib\n\n\nassert lib.scale(4) == 8\n"
    assert compile(keep.emit("lib", "scale", found, "unittest"), "t", "exec")
    assert compile(keep.emit("lib", "snake_name", found, "pytest"), "t", "exec")


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
