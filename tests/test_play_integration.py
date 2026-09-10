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
    assert "1 of 2 frames differ; origin scale: return 8 → 12" in out
    assert "tests/test_lib.py::test_scale  PASS→FAIL" in out
    assert "#1" not in out
    assert git(played, "status", "--short").split() == ["M", "lib.py"]


def test_show_after_play_differs_exactly_where_the_edit_differs(played: Path, capsys):
    assert cli.main(["play", "--repo", str(played), "--no-tests"]) == 0
    capsys.readouterr()
    assert cli.main(["show", "scale", "--repo", str(played)]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == "lib.py:scale  base 1 call(s), head 1 call(s)"
    assert lines[1].split() == ["#1", "value", "4", "4"]
    assert lines[2].split() == ["#1", "return", "8", "12", "*"]
    assert cli.main(["show", "clamp", "--repo", str(played)]) == 0
    clamp = capsys.readouterr().out
    assert "*" not in clamp and "ceiling" in clamp and clamp.count("4") >= 4


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


def test_play_identical_behaviour_says_so(played: Path, capsys):
    (played / "lib.py").write_text(BEFORE.replace("clamp(value, 100) * 2", "2 * clamp(value, 100)"))
    assert cli.main(["play", "--repo", str(played), "--no-tests", "--fail-on-diff"]) == 0
    assert "identical: 2 frames traced, no value differs" in capsys.readouterr().out


def test_play_reuses_the_base_worktree_and_clean_base_rebuilds_it(played: Path, capsys):
    assert cli.main(["play", "--repo", str(played), "--no-tests"]) == 0
    base = played / ".flowdiff" / "base"
    (base / "stale.txt").write_text("")
    assert cli.main(["play", "--repo", str(played), "--no-tests"]) == 0 and (base / "stale.txt").exists()
    assert cli.main(["play", "--repo", str(played), "--no-tests", "--clean-base"]) == 0
    assert not (base / "stale.txt").exists()
