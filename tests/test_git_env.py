"""Intent: #83 — git exports GIT_DIR, GIT_INDEX_FILE and friends into every hook
it runs, and they override `-C`. Inherited, every git call flowdiff makes is
silently redirected at whichever repo invoked it: the tool reads the wrong
repository's diff, and the test fixtures stage into the commit being written."""

import ast
from pathlib import Path

import pytest

from conftest import SOURCE, git
from flowdiff.changes import Revisions


@pytest.fixture
def other_repo(tmp_path_factory) -> Path:
    """A repo that is not the one under test — what a hook would point us at."""
    path = tmp_path_factory.mktemp("invoking-repo")
    git(path, "init", "-q", "-b", "main")
    return path


def test_an_inherited_git_dir_does_not_redirect_us_to_another_repo(
    repo, other_repo, monkeypatch
):
    monkeypatch.setenv("GIT_DIR", str(other_repo / ".git"))
    resolved = Path(git(repo, "rev-parse", "--absolute-git-dir").strip())
    assert resolved.resolve() == (repo / ".git").resolve()


def test_an_inherited_index_does_not_capture_what_we_stage(
    repo, other_repo, monkeypatch
):
    # The damaging half: under a pre-commit hook the inherited index is the
    # commit being written, so this staged the file into that commit.
    monkeypatch.setenv("GIT_INDEX_FILE", str(other_repo / ".git" / "index"))
    (repo / "new.py").write_text("x = 1\n")

    git(repo, "add", "new.py")

    assert "new.py" in git(repo, "ls-files").split()
    assert git(other_repo, "ls-files").strip() == ""


def test_an_inherited_work_tree_does_not_move_what_we_read(
    repo, other_repo, monkeypatch
):
    monkeypatch.setenv("GIT_WORK_TREE", str(other_repo))
    assert Path(git(repo, "rev-parse", "--show-toplevel").strip()).resolve() == (
        repo.resolve()
    )


def test_a_repo_built_under_a_polluted_environment_is_still_its_own_repo(
    tmp_path, other_repo, monkeypatch
):
    """The real ordering. Every other test here pollutes the environment after
    its fixtures have run; a pre-commit hook pollutes it before pytest starts, so
    it is the fixture's own init/add/commit that gets redirected."""
    monkeypatch.setenv("GIT_DIR", str(other_repo / ".git"))
    monkeypatch.setenv("GIT_INDEX_FILE", str(other_repo / ".git" / "index"))

    git(tmp_path, "init", "-q", "-b", "main")
    git(tmp_path, "config", "user.email", "tests@example.invalid")
    git(tmp_path, "config", "user.name", "tests")
    (tmp_path / "a.py").write_text("x = 1\n")
    git(tmp_path, "add", "a.py")
    git(tmp_path, "commit", "-q", "-m", "init")

    assert git(tmp_path, "ls-files").split() == ["a.py"]
    assert git(other_repo, "ls-files").strip() == ""
    assert git(other_repo, "rev-list", "--count", "--all").strip() == "0"


def test_an_inherited_git_dir_does_not_change_which_revision_we_read(
    repo, other_repo, monkeypatch
):
    """The other half of #83: git show reads objects, so only GIT_DIR can move it.
    Asserting the right content, never emptiness — git_show turns a failure into ""."""
    git(other_repo, "config", "user.email", "tests@example.invalid")
    git(other_repo, "config", "user.name", "tests")
    (other_repo / "a.py").write_text("def f(x):\n    return 'the invoking repo'\n")
    git(other_repo, "add", "a.py")
    git(other_repo, "commit", "-q", "-m", "init")
    monkeypatch.setenv("GIT_DIR", str(other_repo / ".git"))

    assert Revisions(repo, "HEAD", None).read_base(Path("a.py")) == SOURCE


def test_the_scrubbed_set_is_read_from_git_rather_than_hardcoded():
    import subprocess

    from flowdiff.changes import _repo_scoped_vars

    named = subprocess.run(["git", "rev-parse", "--local-env-vars"],  # scrub-exempt: the oracle
                           capture_output=True, text=True, check=True).stdout.split()
    # Equality, not a subset: a hardcoded handful satisfies a subset bound and
    # then quietly goes stale as git grows the list.
    assert _repo_scoped_vars() == frozenset(named)
    assert len(named) > 8


def test_a_probe_that_fails_falls_back_rather_than_scrubbing_nothing(monkeypatch):
    """Asking git is the primary source, so a failed ask must not degrade to an
    empty set: that passes GIT_DIR through and reinstates the bug."""
    import subprocess

    from flowdiff import changes

    monkeypatch.setattr(changes.subprocess, "run",
                        lambda *a, **k: subprocess.CompletedProcess([], 128, "", ""))
    monkeypatch.setenv("GIT_DIR", "/somewhere/else/.git")
    monkeypatch.setenv("GIT_INDEX_FILE", "/somewhere/else/.git/index")
    changes._repo_scoped_vars.cache_clear()
    try:
        # The names, not REPO_SCOPED_FALLBACK: comparing the constant to itself
        # would let it shrink to {"GIT_DIR"} and still pass the damaging half.
        assert changes._repo_scoped_vars() == {"GIT_DIR", "GIT_INDEX_FILE", "GIT_WORK_TREE"}
        assert {"GIT_DIR", "GIT_INDEX_FILE"}.isdisjoint(changes._env())
    finally:
        changes._repo_scoped_vars.cache_clear()


def test_only_the_repo_scoped_variables_are_dropped(monkeypatch):
    from flowdiff.changes import _env

    monkeypatch.setenv("FLOWDIFF_CANARY", "kept")
    monkeypatch.setenv("GIT_DIR", "/somewhere/else/.git")
    # Not repo-scoped, and dropping it would change who a commit is attributed to.
    monkeypatch.setenv("GIT_AUTHOR_NAME", "kept too")

    env = _env()

    assert "GIT_DIR" not in env
    assert env["GIT_AUTHOR_NAME"] == "kept too"
    assert env["FLOWDIFF_CANARY"] == "kept"
    assert "PATH" in env  # or git stops being findable at all


SPAWNERS = frozenset({"run", "Popen", "call", "check_call", "check_output"})
EXEMPT = "scrub-exempt"


def _unscrubbed_git_calls(path: Path) -> list[int]:
    """Lines spawning git with a literal argv. An argv assembled elsewhere stays
    invisible; that needs dataflow, and a name is not evidence of a git call."""
    lines = path.read_text(encoding="utf-8").splitlines()
    spawned = []
    for node in ast.walk(ast.parse("\n".join(lines))):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        argv = node.args[0]
        if isinstance(argv, ast.List | ast.Tuple) and argv.elts:
            argv = argv.elts[0]
        is_git = isinstance(argv, ast.Constant) and isinstance(argv.value, str) and (
            argv.value == "git" or argv.value.startswith("git "))
        if name in SPAWNERS and is_git and not any(
                EXEMPT in line for line in lines[node.lineno - 1:node.end_lineno]):
            spawned.append(node.lineno)
    return spawned


def test_nothing_else_shells_out_to_git_around_the_helper():
    root = Path(__file__).resolve().parents[1]
    offenders = {
        str(p.relative_to(root)): lines
        for folder in ("flowdiff", "tests", "tools")
        for p in sorted((root / folder).rglob("*.py"))
        if p.name != "changes.py" and (lines := _unscrubbed_git_calls(p))
    }
    assert offenders == {}
