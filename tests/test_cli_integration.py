"""End-to-end: the real CLI over a real git repo and a real language server."""
import shutil
from pathlib import Path

import pytest

from flowdiff import cli, graph

from conftest import git

pytestmark = pytest.mark.skipif(
    shutil.which("pyright-langserver") is None or shutil.which("graph-easy") is None,
    reason="needs pyright-langserver and graph-easy")

BEFORE = """\
def clamp(value, ceiling):
    return min(value, ceiling)


def scale(value):
    return clamp(value, 100) * 2


def entry(value):
    return scale(value)
"""


@pytest.fixture
def project(tmp_path: Path) -> Path:
    git(tmp_path, "init", "-q", "-b", "main")
    git(tmp_path, "config", "user.email", "tests@example.invalid")
    git(tmp_path, "config", "user.name", "tests")
    (tmp_path / "lib.py").write_text(BEFORE)
    git(tmp_path, "add", "lib.py")
    git(tmp_path, "commit", "-q", "-m", "init")
    return tmp_path


def test_body_change_renders_a_flow_entered_above_it(project: Path, capsys):
    (project / "lib.py").write_text(BEFORE.replace("clamp(value, 100)", "clamp(value, 50)"))
    assert cli.main(["--repo", str(project), "--no-tests"]) == 0
    out = capsys.readouterr().out
    assert "flow 1/1" in out
    assert "scale~" in out
    assert "entry" in out and "clamp" in out
    assert "no covering tests" in out


def test_signature_change_enters_from_the_stable_caller(project: Path, capsys):
    changed = BEFORE.replace("def clamp(value, ceiling):", "def clamp(value, ceiling, floor=0):")
    (project / "lib.py").write_text(changed)
    assert cli.main(["--repo", str(project), "--no-tests"]) == 0
    out = capsys.readouterr().out
    assert "clamp~" in out
    assert "entry scale" in out


def test_covering_tests_are_reported_when_a_test_reaches_the_flow(project: Path, capsys):
    tests_dir = project / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_lib.py").write_text("from lib import scale\n\n\ndef test_scale():\n    assert scale(1) == 2\n")
    git(project, "add", "tests")
    git(project, "commit", "-q", "-m", "add test")
    (project / "lib.py").write_text(BEFORE.replace("* 2", "* 3"))
    assert cli.main(["--repo", str(project)]) == 0
    out = capsys.readouterr().out
    assert "covered by 1 test(s)" in out and "tests/test_lib.py::test_scale" not in out
    assert cli.main(["--repo", str(project), "--list-tests"]) == 0
    assert "covered by 1 test(s)\n  tests/test_lib.py::test_scale" in capsys.readouterr().out


def test_test_file_cap_is_reported_as_a_warning(project: Path, capsys, monkeypatch):
    monkeypatch.setattr(graph, "MAX_TEST_FILES_OPENED", 1)
    (project / "test_one.py").write_text("from lib import scale\n")
    (project / "test_two.py").write_text("from lib import scale\n")
    (project / "lib.py").write_text(BEFORE.replace("* 2", "* 3"))
    assert cli.main(["--repo", str(project)]) == 0
    assert "warning: 2 test files; references searched in the first 1" in capsys.readouterr().err


def test_changed_tests_are_covering_tests_not_frames(project: Path, capsys):
    (project / "tests").mkdir()
    (project / "tests" / "test_lib.py").write_text("from lib import scale\n\n\ndef test_scale():\n    assert scale(1) == 3\n")
    (project / "lib.py").write_text(BEFORE.replace("* 2", "* 3"))
    assert cli.main(["--repo", str(project), "--list-tests"]) == 0
    captured = capsys.readouterr()
    assert "entry scale  (2 frames, 1 changed: scale~)" in captured.out
    assert "test_scale~" not in captured.out and "test_scale+" not in captured.out
    assert "tests/test_lib.py::test_scale" in captured.out
    assert "no caller found" not in captured.err


def test_split_draws_the_base_revisions_graph_beside_the_heads(project: Path, capsys):
    changed = BEFORE.replace("def scale(value):\n    return clamp(value, 100) * 2",
                             "def scale(value):\n    return double(clamp(value, 100))\n\n\ndef double(v):\n    return v * 2")
    (project / "lib.py").write_text(changed)
    assert cli.main(["--repo", str(project), "--no-tests", "--split"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("flow 1/1\nbase") and graph_module_gutter() in out
    paired = [line.split(graph_module_gutter(), 1) for line in out.splitlines() if graph_module_gutter() in line]
    left, right = [p[0] for p in paired], [p[1] for p in paired]
    assert any("scale~" in l for l in left) and any("clamp" in l for l in left) and not any("double" in l for l in left)
    assert any("double+" in r for r in right) and any("scale~" in r for r in right)
    assert "entry scale  (3 frames, 2 changed: scale~, double+)" in out


def graph_module_gutter() -> str:
    from flowdiff import render
    return render.GUTTER


def test_comment_only_change_reports_nothing_to_show(project: Path, capsys):
    (project / "lib.py").write_text(BEFORE.replace("def scale(value):", "def scale(value):  # a note"))
    assert cli.main(["--repo", str(project), "--no-tests"]) == 2
    assert "no symbols" in capsys.readouterr().out
