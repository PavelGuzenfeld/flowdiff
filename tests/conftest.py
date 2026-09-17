from pathlib import Path

import pytest

# The same helper the tool uses, not a copy of it. A second implementation is a
# second place to forget that git exports GIT_DIR into hooks (#83).
from flowdiff.changes import git

SOURCE = "def f(x):\n    return x + 1\n\n\ndef g(y):\n    return f(y) * 2\n"


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    git(tmp_path, "init", "-q", "-b", "main")
    git(tmp_path, "config", "user.email", "tests@example.invalid")
    git(tmp_path, "config", "user.name", "tests")
    (tmp_path / "a.py").write_text(SOURCE)
    git(tmp_path, "add", "a.py")
    git(tmp_path, "commit", "-q", "-m", "init")
    return tmp_path
