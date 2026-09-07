"""End-to-end: the real CLI over a real git repo and a real language server."""
import shutil
from pathlib import Path

import pytest

from flowdiff import cli

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


@pytest.mark.xfail(reason="pyright reports references only for files it has open; flowdiff "
                          "never opens the test files, so cross-file discovery misses them",
                   strict=True)
def test_covering_tests_are_reported_when_a_test_reaches_the_flow(project: Path, capsys):
    tests_dir = project / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_lib.py").write_text("from lib import scale\n\n\ndef test_scale():\n    assert scale(1) == 2\n")
    git(project, "add", "tests")
    git(project, "commit", "-q", "-m", "add test")
    (project / "lib.py").write_text(BEFORE.replace("* 2", "* 3"))
    assert cli.main(["--repo", str(project)]) == 0
    out = capsys.readouterr().out
    assert "tests/test_lib.py::test_scale" in out
    assert "covered by 1 test(s)" in out


def test_comment_only_change_reports_nothing_to_show(project: Path, capsys):
    (project / "lib.py").write_text(BEFORE.replace("def scale(value):", "def scale(value):  # a note"))
    assert cli.main(["--repo", str(project), "--no-tests"]) == 2
    assert "no symbols" in capsys.readouterr().out
