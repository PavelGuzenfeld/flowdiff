"""Scratch state under .flowdiff/ and the persistent base worktree it holds (decisions 25, 26)."""
from __future__ import annotations

from pathlib import Path

from .changes import git

SCRATCH = ".flowdiff"


def scratch_dir(root: Path) -> Path:
    scratch = root / SCRATCH
    scratch.mkdir(exist_ok=True)
    ignore = scratch / ".gitignore"
    if not ignore.exists():
        ignore.write_text("*\n")
    return scratch


def base_worktree(root: Path, ref: str, clean: bool = False) -> Path:
    base = scratch_dir(root) / "base"
    # Resolve in the main repo: HEAD inside the base worktree is the base's own HEAD.
    sha = git(root, "rev-parse", "--verify", f"{ref}^{{commit}}").strip()
    if clean and base.exists():
        git(root, "worktree", "remove", "--force", str(base))
    git(root, "worktree", "prune")
    if base.exists():
        git(base, "checkout", "-q", "--detach", sha)
    else:
        git(root, "worktree", "add", "-q", "--detach", str(base), sha)
    return base
