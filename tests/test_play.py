import json
import os
import stat
from pathlib import Path

from flowdiff import cli, compare, play

FAKE_INTERPRETER = """\
#!/usr/bin/env python3
import os, sys, time
target = sys.argv[-1]
if "sleep" in target:
    time.sleep(5)
if "FLOWDIFF_OUT" in os.environ:
    with open(os.environ["FLOWDIFF_OUT"], "w") as f:
        f.write('{"seq": 1, "event": "enter", "frame": "lib.py:f", "args": {"x": 1}}\\n')
        f.write('{"seq": 2, "event": "exit", "frame": "lib.py:f", "return": %s}\\n' % os.environ["FLOWDIFF_SIDE"].__len__())
    with open(os.environ["FLOWDIFF_OUT"] + ".argv", "w") as f:
        f.write(" ".join(sys.argv[1:]))
if target.endswith(".py"):
    sys.exit(int(os.environ.get("FAKE_EXIT", "0")))
sys.stderr.write("boom\\n")
sys.exit({"pass": 0, "fail": 1}.get(target.split("::")[-1], 4))
"""


def fake_interpreter(tmp_path: Path) -> Path:
    path = tmp_path / "fakepy"
    path.write_text(FAKE_INTERPRETER)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def with_tests(tmp_path: Path, *names: str) -> Path:
    (tmp_path / "t.py").write_text("".join(f"def {n}():\n    pass\n\n\n" for n in names))
    return tmp_path


def test_run_tests_maps_pytest_exit_codes(tmp_path: Path):
    py = fake_interpreter(with_tests(tmp_path, "pass", "fail", "odd"))
    tests = ["t.py::pass", "t.py::fail", "t.py::odd", "t.py::gone", "u.py::pass"]
    assert play.run_tests(py, tmp_path, tests, 10) == {"t.py::pass": "PASS", "t.py::fail": "FAIL",
                                                      "t.py::odd": "ERROR(4)", "t.py::gone": "ABSENT",
                                                      "u.py::pass": "ABSENT"}


def test_run_tests_reports_a_timeout(tmp_path: Path):
    py = fake_interpreter(with_tests(tmp_path, "sleep"))
    assert play.run_tests(py, tmp_path, ["t.py::sleep"], 0.3) == {"t.py::sleep": "TIMEOUT"}


def test_present_needs_the_file_and_the_def_unless_module_level(tmp_path: Path):
    with_tests(tmp_path, "test_a")
    assert play.present(tmp_path, "t.py::test_a") and play.present(tmp_path, "t.py::<module>")
    assert play.present(tmp_path, "t.py") and not play.present(tmp_path, "t.py::test_b")
    assert not play.present(tmp_path, "missing.py::<module>")
    assert play.node_ids(tmp_path, ["t.py::<module>", "t.py::test_a", "t.py::test_b"]) == ["t.py", "t.py::test_a"]


def test_shared_tests_keeps_both_sided_tests_and_names_changed_files(tmp_path: Path):
    base, root = tmp_path / "base", tmp_path / "root"
    for tree in (base, root):
        (tree / "tests").mkdir(parents=True)
        (tree / "tests" / "test_same.py").write_text("def test_a():\n    pass\n")
    (base / "tests" / "test_edited.py").write_text("def test_b():\n    pass\n")
    (root / "tests" / "test_edited.py").write_text("def test_b():\n    assert 1\n")
    (root / "tests" / "test_new.py").write_text("def test_c():\n    pass\n")
    tests = ["tests/test_same.py::test_a", "tests/test_edited.py::test_b", "tests/test_new.py::test_c",
             "tests/test_same.py::test_gone"]
    shared, changed = play.shared_tests(base, root, tests)
    assert shared == ["tests/test_same.py::test_a", "tests/test_edited.py::test_b"]
    assert changed == ["tests/test_edited.py"]


def test_run_traced_tests_passes_the_plugin_and_accepts_failing_tests(tmp_path: Path):
    py = fake_interpreter(with_tests(tmp_path, "pass", "fail", "odd", "sleep"))
    out = tmp_path / "h.jsonl"
    assert play.run_traced_tests(py, tmp_path, ["t.py::fail", "t.py::<module>"], ["lib.py:f"], "head", out, 10) is None
    assert compare.load(out)["lib.py:f"][0].result == 4
    assert (tmp_path / "h.jsonl.argv").read_text() == "-m pytest -q -p no:cacheprovider -p flowdiff.pytest_tracer t.py::fail t.py"
    assert play.run_traced_tests(py, tmp_path, ["t.py::odd"], [], "base", out, 10).startswith("base: pytest exited 4")
    assert play.run_traced_tests(py, tmp_path, ["t.py::gone"], [], "base", out, 10) \
        == "base: none of the covering tests exist on this side"
    assert play.run_traced_tests(py, tmp_path, ["t.py::sleep"], [], "base", out, 0.3) \
        == "base: covering tests timed out after 0s"


def test_test_delta_prints_before_and_after_per_test(tmp_path: Path, monkeypatch):
    seen = []
    monkeypatch.setattr(play, "run_tests", lambda i, tree, tests, t: seen.append(tree) or
                        {tests[0]: "PASS" if tree.name == "base" else "FAIL"})
    lines = play.test_delta(Path("py"), tmp_path / "base", tmp_path / "root", ["t.py::a"], 1)
    assert lines == ["  t.py::a  PASS→FAIL"] and seen == [tmp_path / "base", tmp_path / "root"]


def test_run_harness_writes_the_trace_for_the_side(tmp_path: Path):
    py = fake_interpreter(tmp_path)
    out = tmp_path / "head.jsonl"
    out.write_text("stale\n")
    assert play.run_harness(py, tmp_path / "h.py", tmp_path, "head", out, 10) is None
    assert compare.load(out)["lib.py:f"][0].result == 4


def test_run_harness_reports_exit_code_with_stderr_and_timeouts(tmp_path: Path, monkeypatch):
    py = fake_interpreter(tmp_path)
    monkeypatch.setenv("FAKE_EXIT", "3")
    failure = play.run_harness(py, tmp_path / "h.py", tmp_path, "base", tmp_path / "b.jsonl", 10)
    assert failure and failure.startswith("base: harness exited 3")
    monkeypatch.delenv("FAKE_EXIT")
    assert play.run_harness(py, tmp_path / "sleep.py", tmp_path, "base", tmp_path / "b.jsonl", 0.3) \
        == "base: harness timed out after 0s"


def test_show_without_a_recording_is_exit_2(repo: Path, capsys):
    assert cli.main(["show", "f", "--repo", str(repo)]) == 2
    assert "run flowdiff play first" in capsys.readouterr().err


def test_show_outside_a_repo_is_exit_1(tmp_path: Path, capsys):
    assert cli.main(["show", "f", "--repo", str(tmp_path)]) == 1


def recorded(repo: Path) -> None:
    run = repo / ".flowdiff" / "run"
    run.mkdir(parents=True)
    (run / "b.jsonl").write_text(json.dumps({"seq": 1, "event": "enter", "frame": "lib.py:f", "args": {"x": 1}}) + "\n"
                                 + json.dumps({"seq": 2, "event": "exit", "frame": "lib.py:f", "return": 2}) + "\n")
    (run / "h.jsonl").write_text(json.dumps({"seq": 1, "event": "enter", "frame": "lib.py:f", "args": {"x": 1}}) + "\n"
                                 + json.dumps({"seq": 2, "event": "exit", "frame": "lib.py:f", "return": 3}) + "\n")
    (run / "index.json").write_text(json.dumps([{"flow": 1, "frames": ["lib.py:f", "lib.py:g"],
                                                 "base": str(run / "b.jsonl"), "head": str(run / "h.jsonl")}]))


def test_show_prints_the_frame_and_narrows_by_argument(repo: Path, capsys):
    recorded(repo)
    assert cli.main(["show", "f", "--repo", str(repo)]) == 0
    out = capsys.readouterr().out
    assert out.startswith("lib.py:f  base 1 call(s), head 1 call(s)\n") and "return" in out and "*" in out
    assert cli.main(["show", "lib.py:f/x", "--repo", str(repo)]) == 0
    out = capsys.readouterr().out
    assert "x" in out and "return" not in out


def test_show_of_an_unknown_frame_is_exit_2(repo: Path, capsys):
    recorded(repo)
    assert cli.main(["show", "nope", "--repo", str(repo)]) == 2
    assert "nope: not a frame of the last play" in capsys.readouterr().err
    assert cli.main(["show", "g", "--repo", str(repo)]) == 0
    assert capsys.readouterr().out.startswith("lib.py:g  base 0 call(s), head 0 call(s)")


def test_run_dir_lives_under_the_ignored_scratch_dir(repo: Path):
    assert play.run_dir(repo) == repo / ".flowdiff" / "run"
    assert (repo / ".flowdiff" / ".gitignore").read_text() == "*\n"
    assert os.path.isdir(repo / ".flowdiff" / "run")
