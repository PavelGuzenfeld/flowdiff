"""End-to-end play and show: real git worktree, real pyright, the project's own interpreter."""
import shutil
from pathlib import Path

import pytest

from flowdiff import cli

from conftest import git
from test_cli_integration import BEFORE, project  # noqa: F401  (fixture)

pytestmark = pytest.mark.skipif(
    shutil.which("pyright-langserver") is None or shutil.which("graph-easy") is None,
    reason="needs pyright-langserver and graph-easy")

TEST_WITH_LITERAL = "from lib import scale\n\n\ndef test_scale():\n    assert scale(4) == 8\n"


@pytest.fixture
def played(project: Path) -> Path:
    (project / "tests").mkdir()
    (project / "tests" / "test_lib.py").write_text(TEST_WITH_LITERAL)
    git(project, "add", "tests")
    git(project, "commit", "-q", "-m", "add test")
    (project / "lib.py").write_text(BEFORE.replace("* 2", "* 3"))
    return project


def test_play_runs_both_sides_and_names_the_origin_frame(played: Path, capsys):
    assert cli.main(["play", "--repo", str(played)]) == 0
    out = capsys.readouterr().out
    assert "scale~" in out and "clamp" in out
    assert "1 of 2 frames differ: scale (return); origin scale" in out
    assert "tests/test_lib.py::test_scale  PASS→FAIL" in out
    assert "#1" not in out
    assert git(played, "status", "--short").split() == ["M", "lib.py"]


def test_show_after_play_differs_exactly_where_the_edit_differs(played: Path, capsys):
    assert cli.main(["play", "--repo", str(played), "--no-tests"]) == 0
    capsys.readouterr()
    assert cli.main(["show", "scale", "--repo", str(played)]) == 0
    assert capsys.readouterr().out.splitlines() == ["lib.py:scale  base 1 call(s), head 1 call(s)",
                                                    "  calls #1", "    return  8  →  12"]
    assert cli.main(["show", "clamp", "--repo", str(played)]) == 0
    assert capsys.readouterr().out.splitlines() == ["lib.py:clamp  base 1 call(s), head 1 call(s)",
                                                    "  1 identical call(s): #1"]
    assert cli.main(["show", "clamp/ceiling", "--repo", str(played)]) == 0
    assert capsys.readouterr().out.splitlines()[1:] == ["  calls #1", "    ceiling  100  →  100"]
    assert cli.main(["show", "scale#1", "--repo", str(played)]) == 0
    assert capsys.readouterr().out.splitlines() == ["lib.py:scale  call #1", "    value   4  →  4",
                                                    "    return  8  →  12   *"]


def test_play_depth_prints_values_inline_and_fail_on_diff_exits_3(played: Path, capsys):
    assert cli.main(["play", "--repo", str(played), "--no-tests", "--depth", "1", "--fail-on-diff"]) == 3
    out = capsys.readouterr().out
    assert "lib.py:scale  base 1 call(s), head 1 call(s)" in out and "lib.py:clamp  base" not in out


def test_play_without_a_literal_call_site_stops_with_the_slot_path(project: Path, capsys):
    (project / "lib.py").write_text(BEFORE.replace("* 2", "* 3"))
    assert cli.main(["play", "--repo", str(project), "--no-tests"]) == 2
    out = capsys.readouterr().out
    harness = project / ".flowdiff" / "run" / "flow1.py"
    assert f"fill the slots in {harness}" in out
    assert "lib.scale(value=<value>)" in harness.read_text()
    assert not (project / ".flowdiff" / "run" / "index.json").exists()


def test_play_falls_back_to_the_covering_tests_when_nothing_lifts(project: Path, capsys):
    (project / "tests").mkdir()
    (project / "tests" / "test_lib.py").write_text(
        "from lib import scale\n\nFOUR = 4\n\n\ndef test_scale():\n    assert scale(FOUR) == 8\n")
    git(project, "add", "tests")
    git(project, "commit", "-q", "-m", "add test")
    (project / "lib.py").write_text(BEFORE.replace("* 2", "* 3"))
    assert cli.main(["play", "--repo", str(project)]) == 0
    captured = capsys.readouterr()
    assert "no call site with literal arguments; driven by 1 covering test(s) present on both sides" in captured.out
    assert "1 of 2 frames differ: scale (return); origin scale" in captured.out
    assert "tests/test_lib.py::test_scale  PASS→FAIL" in captured.out
    assert "driving tests changed" not in captured.err
    assert cli.main(["show", "scale", "--repo", str(project)]) == 0
    assert capsys.readouterr().out.splitlines()[1] == "  calls #1  ← tests/test_lib.py::test_scale"
    (project / "tests" / "test_lib.py").write_text(
        "from lib import scale\n\nFOUR = 5\n\n\ndef test_scale():\n    assert scale(FOUR) == 8\n")
    assert cli.main(["play", "--repo", str(project)]) == 0
    captured = capsys.readouterr()
    assert "2 of 2 frames differ: scale (value, return); clamp (value, return); origin scale" in captured.out
    assert "warning: the driving tests changed in this diff (tests/test_lib.py); a divergence may reflect " \
           "the inputs rather than the code" in captured.err
    assert cli.main(["play", "--repo", str(project), "--no-tests"]) == 2
    assert "no covering test present on both sides; fill the slots" in capsys.readouterr().out


def test_an_extracted_helper_is_a_new_frame_not_a_divergence(played: Path, capsys):
    (played / "lib.py").write_text(BEFORE.replace("def scale(value):\n    return clamp(value, 100) * 2",
                                                  "def double(v):\n    return v * 2\n\n\n"
                                                  "def scale(value):\n    return double(clamp(value, 100))"))
    assert cli.main(["play", "--repo", str(played), "--no-tests"]) == 0
    out = capsys.readouterr().out
    assert "double+" in out and "identical: 3 frames traced, no value differs" in out
    assert cli.main(["show", "double", "--repo", str(played)]) == 0
    assert capsys.readouterr().out.startswith("lib.py:double  base 0 call(s), head 1 call(s)")


def test_play_identical_behaviour_says_so(played: Path, capsys):
    (played / "lib.py").write_text(BEFORE.replace("clamp(value, 100) * 2", "2 * clamp(value, 100)"))
    assert cli.main(["play", "--repo", str(played), "--no-tests", "--fail-on-diff"]) == 0
    assert "identical: 2 frames traced, no value differs" in capsys.readouterr().out


def test_play_drives_the_flow_from_a_caller_when_the_entry_has_no_literal_site(project: Path, capsys):
    (project / "tests").mkdir()
    (project / "tests" / "test_lib.py").write_text("from lib import entry\n\n\ndef test_entry():\n    assert entry(3) == 6\n")
    git(project, "add", "tests")
    git(project, "commit", "-q", "-m", "add test")
    (project / "lib.py").write_text(BEFORE.replace("* 2", "* 3"))
    assert cli.main(["play", "--repo", str(project), "--no-tests"]) == 0
    captured = capsys.readouterr()
    assert "1 of 2 frames differ: scale (return); origin scale" in captured.out
    assert "warning: scale: driven from entry, the nearest caller with a literal call site" in captured.err
    assert "lib.entry(3)" in (project / ".flowdiff" / "run" / "flow1.py").read_text()


def test_play_reuses_the_base_worktree_and_clean_base_rebuilds_it(played: Path, capsys):
    assert cli.main(["play", "--repo", str(played), "--no-tests"]) == 0
    base = played / ".flowdiff" / "base"
    (base / "stale.txt").write_text("")
    assert cli.main(["play", "--repo", str(played), "--no-tests"]) == 0 and (base / "stale.txt").exists()
    assert cli.main(["play", "--repo", str(played), "--no-tests", "--clean-base"]) == 0
    assert not (base / "stale.txt").exists()
