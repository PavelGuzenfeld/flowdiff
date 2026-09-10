import subprocess
from pathlib import Path

import pytest

SOURCE = "def f(x):\n    return x + 1\n\n\ndef g(y):\n    return f(y) * 2\n"


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True, text=True).stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    git(tmp_path, "init", "-q", "-b", "main")
    git(tmp_path, "config", "user.email", "tests@example.invalid")
    git(tmp_path, "config", "user.name", "tests")
    (tmp_path / "a.py").write_text(SOURCE)
    git(tmp_path, "add", "a.py")
    git(tmp_path, "commit", "-q", "-m", "init")
    return tmp_path
