from pathlib import Path

import pytest

from flowdiff import cli

from conftest import SOURCE


@pytest.fixture
def tools_present(monkeypatch):
    monkeypatch.setattr(cli.shutil, "which", lambda name: f"/usr/bin/{name}")


def test_exit_codes_are_the_documented_values():
    """Literals, not the constants: comparing a constant to itself asserts nothing."""
    assert (cli.EXIT_OK, cli.EXIT_TOOL_ERROR, cli.EXIT_NOTHING, cli.EXIT_DIFF) == (0, 1, 2, 3)


def test_verbs_dispatch_to_play_with_their_own_parsers(monkeypatch):
    from flowdiff import play
    seen = {}
    monkeypatch.setattr(play, "run", lambda args: seen.setdefault("play", args) and 7)
    monkeypatch.setattr(play, "show", lambda args: seen.setdefault("show", args) and 8)
    assert cli.main(["play", "--depth", "2", "--fail-on-diff", "--clean-base"]) == 7
    assert (seen["play"].depth, seen["play"].fail_on_diff, seen["play"].clean_base) == (2, True, True)
    assert cli.main(["show", "scale/return"]) == 8
    assert seen["show"].frame == "scale/return"


def test_play_parser_defaults():
    args = cli.build_play_parser().parse_args([])
    assert (args.depth, args.clean_base, args.fail_on_diff, args.run_timeout) == (0, False, False, 300.0)
    assert (args.ref, args.hops, args.no_tests) == (None, 3, False)


def test_missing_tool_is_exit_1(monkeypatch, capsys, repo: Path):
    monkeypatch.setattr(cli.shutil, "which", lambda name: None if name == "graph-easy" else "/usr/bin/x")
    assert cli.main(["--repo", str(repo)]) == 1
    assert "graph-easy" in capsys.readouterr().err


def test_missing_tool_names_every_absent_binary(monkeypatch, capsys, repo: Path):
    monkeypatch.setattr(cli.shutil, "which", lambda name: None)
    assert cli.main(["--repo", str(repo)]) == 1
    err = capsys.readouterr().err
    assert all(binary in err for binary in cli.REQUIRED_BINARIES)
    assert "apt install libgraph-easy-perl" in err


def test_not_a_repo_is_exit_1(tools_present, tmp_path: Path, capsys):
    assert cli.main(["--repo", str(tmp_path)]) == 1
    assert str(tmp_path) in capsys.readouterr().err


def test_clean_tree_is_exit_2(tools_present, repo: Path, capsys):
    assert cli.main(["--repo", str(repo)]) == 2
    out = capsys.readouterr().out
    assert "no changes" in out and "working tree" in out


def test_clean_tree_against_a_ref_names_both_revisions(tools_present, repo: Path, capsys):
    assert cli.main(["--repo", str(repo), "HEAD"]) == 2
    out = capsys.readouterr().out
    assert "HEAD" in out and "working tree" not in out


def test_only_test_file_changes_are_exit_2(tools_present, repo: Path, capsys):
    (repo / "tests").mkdir()
    (repo / "tests" / "test_a.py").write_text("from a import f\n\n\ndef test_f():\n    assert f(1) == 2\n")
    assert cli.main(["--repo", str(repo)]) == 2
    assert "only test files changed" in capsys.readouterr().out
    (repo / "notes.txt").write_text("x\n")
    assert cli.main(["--repo", str(repo)]) == 2
    assert "no changed files in a supported language" in capsys.readouterr().out


def test_unsupported_language_is_exit_2(tools_present, repo: Path, capsys):
    (repo / "notes.txt").write_text("hello\n")
    assert cli.main(["--repo", str(repo)]) == 2
    assert "supported language" in capsys.readouterr().out


def test_missing_language_server_is_exit_1(monkeypatch, repo: Path, capsys):
    monkeypatch.setattr(cli.shutil, "which", lambda name: None if name == "pyright-langserver" else "/usr/bin/x")
    (repo / "a.py").write_text(SOURCE.replace("x + 1", "x + 2"))
    assert cli.main(["--repo", str(repo)]) == 1
    err = capsys.readouterr().err
    assert "pyright-langserver" in err and "npm install -g pyright" in err


def test_parser_defaults():
    args = cli.build_parser().parse_args([])
    assert (args.ref, args.hops, args.timeout) == (None, 3, 60.0)
    assert args.no_tests is False and args.full is False
    assert args.repo == Path.cwd()


def test_parser_accepts_a_base_ref_and_overrides():
    args = cli.build_parser().parse_args(["origin/main", "--hops", "5", "--full", "--no-tests"])
    assert (args.ref, args.hops) == ("origin/main", 5)
    assert args.no_tests is True and args.full is True
