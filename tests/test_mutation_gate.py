from pathlib import Path

import pytest

from conftest import git
from tools.mutation_gate import SCORE_EXEMPT, Result, isolated_copy, touched_modules
from tools.mutation_gate import tests_for as _tests_for


def result(module: str, killed: int, survived: int) -> Result:
    return Result(module, {"killed": killed, "survived": survived}, "", 1.0)


def test_protocol_module_below_the_floor_is_not_gated_on_its_score() -> None:
    assert result("flowdiff/lsp.py", 1, 3).score() == 25.0
    assert result("flowdiff/lsp.py", 1, 3).passed(80.0)


def test_protocol_module_with_no_mutants_is_still_a_broken_gate() -> None:
    assert not result("flowdiff/lsp.py", 0, 0).passed(80.0)


def test_raising_the_threshold_does_not_re_gate_a_protocol_module() -> None:
    assert result("flowdiff/container.py", 83, 17).passed(95.0)


def test_logic_module_below_the_floor_still_fails() -> None:
    assert not result("flowdiff/graph.py", 79, 21).passed(80.0)


def test_exempt_line_reports_the_measured_score_beside_the_verdict() -> None:
    line = result("flowdiff/harness_cpp.py", 858, 142).line(80.0)
    assert "85.8%" in line
    assert "exempt (#34)" in line
    assert "BELOW THRESHOLD" not in line


def test_every_exempt_module_still_exists() -> None:
    assert all(Path(module).is_file() for module in SCORE_EXEMPT)


def test_the_real_subprocess_integration_file_sorts_after_play_s_other_test_files() -> None:
    """-x must exhaust test_play_plans.py before it ever pays for test_play_integration.py."""
    files = _tests_for("flowdiff/play.py").split()
    assert files.index("tests/test_play_integration.py") == len(files) - 1


@pytest.fixture
def gated_repo(tmp_path_factory) -> Path:
    """A repo shaped like the one the tool runs in: a package module, changed."""
    root = tmp_path_factory.mktemp("gated")
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.email", "tests@example.invalid")
    git(root, "config", "user.name", "tests")
    (root / "flowdiff").mkdir()
    (root / "flowdiff" / "graph.py").write_text("x = 1\n")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "base")
    (root / "flowdiff" / "graph.py").write_text("x = 2\n")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "change")
    return root


@pytest.fixture
def invoking_repo(tmp_path_factory) -> Path:
    """The repo a hook would point git at, sharing no filename with the one above."""
    root = tmp_path_factory.mktemp("invoking")
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.email", "tests@example.invalid")
    git(root, "config", "user.name", "tests")
    (root / "elsewhere.py").write_text("y = 1\n")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "init")
    return root


def test_an_inherited_git_dir_does_not_move_which_modules_count_as_touched(
    gated_repo, invoking_repo, monkeypatch
) -> None:
    monkeypatch.setenv("GIT_DIR", str(invoking_repo / ".git"))
    monkeypatch.chdir(gated_repo)

    assert touched_modules("HEAD~1") == ["flowdiff/graph.py"]


def test_an_inherited_index_does_not_choose_what_gets_copied(
    gated_repo, invoking_repo, tmp_path, monkeypatch
) -> None:
    # ls-files names the files; their contents come from the working tree, so a
    # foreign index names files this tree does not have.
    monkeypatch.setenv("GIT_INDEX_FILE", str(invoking_repo / ".git" / "index"))
    monkeypatch.chdir(gated_repo)
    dest = tmp_path / "copy"
    dest.mkdir()

    isolated_copy(dest)

    assert (dest / "flowdiff" / "graph.py").read_text() == "x = 2\n"
    assert not (dest / "elsewhere.py").exists()
