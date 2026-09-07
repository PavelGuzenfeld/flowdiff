from pathlib import Path

import pytest

from flowdiff import cli

from conftest import SOURCE


@pytest.fixture
def tools_present(monkeypatch):
    monkeypatch.setattr(cli.shutil, "which", lambda name: f"/usr/bin/{name}")


def test_missing_tool_is_exit_1(monkeypatch, capsys, repo: Path):
    monkeypatch.setattr(cli.shutil, "which", lambda name: None if name == "graph-easy" else "/usr/bin/x")
    assert cli.main(["--repo", str(repo)]) == cli.EXIT_TOOL_ERROR
    assert "graph-easy" in capsys.readouterr().err


def test_not_a_repo_is_exit_1(tools_present, tmp_path: Path, capsys):
    assert cli.main(["--repo", str(tmp_path)]) == cli.EXIT_TOOL_ERROR
    assert "not a git repository" in capsys.readouterr().err


def test_clean_tree_is_exit_2(tools_present, repo: Path, capsys):
    assert cli.main(["--repo", str(repo)]) == cli.EXIT_NOTHING
    assert "no changes" in capsys.readouterr().out


def test_unsupported_language_is_exit_2(tools_present, repo: Path, capsys):
    (repo / "notes.txt").write_text("hello\n")
    assert cli.main(["--repo", str(repo)]) == cli.EXIT_NOTHING
    assert "supported language" in capsys.readouterr().out


def test_missing_language_server_is_exit_1(monkeypatch, repo: Path, capsys):
    monkeypatch.setattr(cli.shutil, "which", lambda name: None if name == "pyright-langserver" else "/usr/bin/x")
    (repo / "a.py").write_text(SOURCE.replace("x + 1", "x + 2"))
    assert cli.main(["--repo", str(repo)]) == cli.EXIT_TOOL_ERROR
    assert "pyright-langserver" in capsys.readouterr().err
