import shutil
from pathlib import Path

import pytest

from flowdiff import worktree

from conftest import SOURCE, git


def test_scratch_dir_is_invisible_to_git_status(repo: Path):
    scratch = worktree.scratch_dir(repo)
    (scratch / "junk.txt").write_text("x")
    assert scratch == repo / ".flowdiff"
    assert (scratch / ".gitignore").read_text() == "*\n"
    assert git(repo, "status", "--short") == ""


def test_scratch_dir_keeps_an_existing_gitignore(repo: Path):
    (repo / ".flowdiff").mkdir()
    (repo / ".flowdiff" / ".gitignore").write_text("*\n!keep\n")
    worktree.scratch_dir(repo)
    assert (repo / ".flowdiff" / ".gitignore").read_text() == "*\n!keep\n"


def commit_change(repo: Path, text: str) -> str:
    (repo / "a.py").write_text(text)
    git(repo, "commit", "-q", "-am", "change")
    return git(repo, "rev-parse", "HEAD").strip()


def test_base_worktree_checks_out_the_ref_detached_under_scratch(repo: Path):
    first = git(repo, "rev-parse", "HEAD").strip()
    commit_change(repo, SOURCE.replace("x + 1", "x + 2"))
    base = worktree.base_worktree(repo, "HEAD~1")
    assert base == repo / ".flowdiff" / "base"
    assert (base / "a.py").read_text() == SOURCE
    assert git(base, "rev-parse", "HEAD").strip() == first
    assert git(base, "rev-parse", "--abbrev-ref", "HEAD").strip() == "HEAD"
    assert git(repo, "status", "--short") == ""


def test_head_worktree_keeps_the_project_name_for_its_directory(repo: Path):
    first = git(repo, "rev-parse", "HEAD").strip()
    commit_change(repo, SOURCE.replace("x + 1", "x + 2"))
    head = worktree.head_worktree(repo, "HEAD~1")
    assert head == repo / ".flowdiff" / "head" / repo.name and head.name == repo.name
    assert git(head, "rev-parse", "HEAD").strip() == first and (head / "a.py").read_text() == SOURCE
    assert worktree.base_worktree(repo, "HEAD") == repo / ".flowdiff" / "base"
    assert git(repo, "status", "--short") == ""


def test_base_worktree_is_reused_and_moved_to_the_new_ref(repo: Path):
    worktree.base_worktree(repo, "HEAD")
    second = commit_change(repo, SOURCE.replace("x + 1", "x + 2"))
    base = worktree.base_worktree(repo, "HEAD")
    assert git(base, "rev-parse", "HEAD").strip() == second
    assert (base / "a.py").read_text().count("x + 2") == 1
    assert len([l for l in git(repo, "worktree", "list").splitlines() if ".flowdiff/base" in l]) == 1


def test_head_means_the_main_repo_head_not_the_base_worktrees(repo: Path):
    first = git(repo, "rev-parse", "HEAD").strip()
    second = commit_change(repo, SOURCE.replace("x + 1", "x + 2"))
    worktree.base_worktree(repo, "HEAD~1")
    assert git(repo / ".flowdiff" / "base", "rev-parse", "HEAD").strip() == first
    base = worktree.base_worktree(repo, "HEAD")
    assert git(base, "rev-parse", "HEAD").strip() == second


def superproject(repo: Path, tmp_path: Path) -> Path:
    """Adds a submodule at sub, pinned to v2 at HEAD~1 and v1 at HEAD; returns its origin."""
    origin = tmp_path / "sub-origin"
    origin.mkdir()
    git(origin, "init", "-q", "-b", "main")
    git(origin, "config", "user.email", "tests@example.invalid")
    git(origin, "config", "user.name", "tests")
    (origin / "f.txt").write_text("v1\n")
    git(origin, "add", "f.txt")
    git(origin, "commit", "-q", "-m", "v1")
    (origin / "f.txt").write_text("v2\n")
    git(origin, "commit", "-q", "-am", "v2")
    git(repo, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(origin), "sub")
    git(repo, "commit", "-q", "-m", "sub at v2")
    git(repo / "sub", "checkout", "-q", "HEAD~1")
    git(repo, "commit", "-q", "-am", "sub at v1")
    return origin


def test_a_worktrees_submodule_holds_the_commit_that_revision_pinned(repo: Path, tmp_path_factory):
    superproject(repo, tmp_path_factory.mktemp("origin"))
    base = worktree.base_worktree(repo, "HEAD~1")
    assert (base / "sub" / "f.txt").read_text() == "v2\n"


def test_submodules_are_populated_without_reaching_their_configured_remote(repo: Path, tmp_path_factory):
    origin = superproject(repo, tmp_path_factory.mktemp("origin"))
    shutil.rmtree(origin)
    base = worktree.base_worktree(repo, "HEAD~1")
    assert (base / "sub" / "f.txt").read_text() == "v2\n"


def test_populating_a_worktree_leaves_the_main_trees_submodule_where_it_was(repo: Path, tmp_path_factory):
    superproject(repo, tmp_path_factory.mktemp("origin"))
    worktree.base_worktree(repo, "HEAD~1")
    assert (repo / "sub" / "f.txt").read_text() == "v1\n"
    assert git(repo, "status", "--short") == ""


def test_a_reused_base_worktree_moves_its_submodule_to_the_new_ref(repo: Path, tmp_path_factory):
    superproject(repo, tmp_path_factory.mktemp("origin"))
    worktree.base_worktree(repo, "HEAD~1")
    base = worktree.base_worktree(repo, "HEAD")
    assert (base / "sub" / "f.txt").read_text() == "v1\n"


def test_a_repo_without_gitmodules_names_no_submodules(repo: Path):
    assert worktree.submodule_names(repo) == []


def test_gitmodules_names_every_submodule_of_the_revision(repo: Path, tmp_path_factory):
    superproject(repo, tmp_path_factory.mktemp("origin"))
    assert worktree.submodule_names(repo) == ["sub"]


def test_an_unpopulatable_submodule_stops_the_run_with_gits_own_reason(repo: Path, tmp_path_factory):
    origin = superproject(repo, tmp_path_factory.mktemp("origin"))
    shutil.rmtree(repo / ".git" / "modules" / "sub")
    shutil.rmtree(origin)
    with pytest.raises(worktree.WorktreeError) as err:
        worktree.base_worktree(repo, "HEAD")
    message = str(err.value)
    assert message.startswith(f"{repo / '.flowdiff' / 'base'}: submodule update failed\n")
    assert "fatal: clone of" in message and str(origin) in message


def test_clean_recreates_the_worktree(repo: Path):
    base = worktree.base_worktree(repo, "HEAD")
    (base / "stale.txt").write_text("left over from a build")
    assert worktree.base_worktree(repo, "HEAD") == base and (base / "stale.txt").exists()
    worktree.base_worktree(repo, "HEAD", clean=True)
    assert not (base / "stale.txt").exists() and (base / "a.py").exists()
