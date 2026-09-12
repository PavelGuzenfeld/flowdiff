"""Scratch state under .flowdiff/ and the persistent base worktree it holds (decisions 25, 26)."""
from __future__ import annotations

import subprocess
from pathlib import Path

from .changes import git

SCRATCH = ".flowdiff"
POINTER = "last-run"


class WorktreeError(RuntimeError):
    pass


def scratch_dir(root: Path) -> Path:
    scratch = root / SCRATCH
    scratch.mkdir(exist_ok=True)
    ignore = scratch / ".gitignore"
    if not ignore.exists():
        ignore.write_text("*\n")
    return scratch


def remember_run(repo: Path, tree: Path) -> None:
    """An A..B range records in its head worktree, which show and keep have no way to name."""
    pointer = scratch_dir(repo) / POINTER
    if tree == repo:
        pointer.unlink(missing_ok=True)
    else:
        pointer.write_text(f"{tree}\n")


def last_run_tree(repo: Path) -> Path:
    pointer = repo / SCRATCH / POINTER
    if not pointer.exists():
        return repo
    tree = Path(pointer.read_text().strip())
    if not (tree / SCRATCH).is_dir():
        raise WorktreeError(f"{tree}: the worktree holding the last play is gone; run flowdiff play again")
    return tree


def head_worktree(root: Path, ref: str) -> Path:
    """The head of an A..B range, checked out under a directory named like the project so image conventions hold."""
    return base_worktree(root, ref, name=f"head/{root.name}")


def base_worktree(root: Path, ref: str, clean: bool = False, name: str = "base") -> Path:
    base = scratch_dir(root) / name
    # Resolve in the main repo: HEAD inside the base worktree is the base's own HEAD.
    sha = git(root, "rev-parse", "--verify", f"{ref}^{{commit}}").strip()
    if clean and base.exists():
        git(root, "worktree", "remove", "--force", str(base))
    git(root, "worktree", "prune")
    if base.exists():
        git(base, "checkout", "-q", "--detach", sha)
    else:
        git(root, "worktree", "add", "-q", "--detach", str(base), sha)
    populate_submodules(root, base)
    return base


def submodule_names(tree: Path) -> list[str]:
    if not (tree / ".gitmodules").exists():
        return []
    keys = git(tree, "config", "-f", ".gitmodules", "--name-only", "--get-regexp", r"\.path$")
    return [key[len("submodule."):-len(".path")] for key in keys.split()]


def populate_submodules(root: Path, tree: Path) -> None:
    """A worktree leaves submodule directories empty and the build fails much later for the wrong reason.
    Cloning each from the superproject's own module store needs no network and moves no checkout of its own."""
    names = submodule_names(tree)
    if not names:
        return
    common = (root / git(root, "rev-parse", "--git-common-dir").strip()).resolve()
    local = [arg for name in names if (stored := common / "modules" / name).is_dir()
             for arg in ("-c", f"submodule.{name}.url={stored}")]
    try:
        git(tree, "-c", "protocol.file.allow=always", *local, "submodule", "update", "--init")
    except subprocess.CalledProcessError as err:
        raise WorktreeError(f"{tree}: submodule update failed\n{(err.stderr or '').strip()}")
