from pathlib import Path

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


def test_clean_recreates_the_worktree(repo: Path):
    base = worktree.base_worktree(repo, "HEAD")
    (base / "stale.txt").write_text("left over from a build")
    assert worktree.base_worktree(repo, "HEAD") == base and (base / "stale.txt").exists()
    worktree.base_worktree(repo, "HEAD", clean=True)
    assert not (base / "stale.txt").exists() and (base / "a.py").exists()
