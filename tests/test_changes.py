import shutil
from pathlib import Path

import pytest

from flowdiff import changes
from flowdiff.changes import Hunk, Revisions, classify, diff_hunks
from flowdiff.lsp import Position, Range, Symbol

from conftest import SOURCE, git


def sym(name: str, kind: int, first: int, last: int, detail: str = "", path: str = "/a.py") -> Symbol:
    return Symbol(name, kind, detail, Range(Position(first, 0), Position(last, 999)),
                  Range(Position(first, 4), Position(first, 4 + len(name))), Path(path))


def test_modified_line_becomes_hunk(repo: Path):
    (repo / "a.py").write_text(SOURCE.replace("x + 1", "x + 2"))
    hunks = diff_hunks(Revisions(repo, "HEAD", None))
    assert hunks == [Hunk(Path("a.py"), 2, 1, 2, 1)]


def test_untracked_file_is_one_whole_file_hunk(repo: Path):
    (repo / "b.py").write_text("a = 1\nb = 2\nc = 3\n")
    hunks = diff_hunks(Revisions(repo, "HEAD", None))
    assert Hunk(Path("b.py"), 0, 0, 1, 3) in hunks


def test_deleted_file_keeps_its_path(repo: Path):
    git(repo, "rm", "-q", "a.py")
    hunks = diff_hunks(Revisions(repo, "HEAD", None))
    assert hunks and hunks[0].path == Path("a.py") and hunks[0].new_lines == 0


def test_ref_mode_compares_two_revisions(repo: Path):
    (repo / "a.py").write_text(SOURCE + "\n\ndef h():\n    return 0\n")
    git(repo, "commit", "-q", "-am", "add h")
    revs = Revisions(repo, "HEAD~1", "HEAD")
    assert revs.diff_args() == ["HEAD~1", "HEAD"]
    assert "def h" in revs.read_head(Path("a.py")) and "def h" not in revs.read_base(Path("a.py"))
    assert diff_hunks(revs)[0].path == Path("a.py")


def test_missing_base_file_reads_empty(repo: Path):
    assert Revisions(repo, "HEAD", None).read_base(Path("nope.py")) == ""


def test_hunk_spans_are_zero_based_and_inclusive():
    assert Hunk(Path("x"), 10, 3, 12, 0).old_span() == (9, 11)
    assert Hunk(Path("x"), 10, 3, 12, 0).new_span() == (11, 11)
    assert Hunk(Path("x"), 0, 0, 1, 5).new_span() == (0, 4)


@pytest.mark.skipif(shutil.which("ast-grep") is None, reason="ast-grep not installed")
def test_normalized_ignores_comments_and_whitespace():
    commented = "def f():\n    # note\n    return  1  # trailing\n"
    plain = "def f():\n    return 1\n"
    assert changes.normalized("python", ".py", commented) == changes.normalized("python", ".py", plain)
    assert changes.normalized("python", ".py", "def f():\n    return 2\n") != changes.normalized("python", ".py", plain)


def test_comment_spans_empty_text_is_empty():
    assert changes.comment_spans("python", ".py", "   \n") == []


def test_signature_text_prefers_detail_then_selection_line():
    with_detail = sym("f", 12, 0, 2, detail="(x: int) -> int")
    assert changes.signature_text(with_detail, "def f(x):\n") == "(x: int) -> int"
    assert changes.signature_text(sym("f", 12, 0, 2), "def f( x ):\n    pass\n") == "deff(x):"


@pytest.fixture
def no_ast_grep(monkeypatch):
    monkeypatch.setattr(changes, "comment_spans", lambda *a: [])


def test_classify_added_body_signature_and_untouched(no_ast_grep):
    base_text = "def f(x):\n    return x\n\ndef g(y):\n    return y\n\ndef k():\n    return 0\n"
    head_text = "def f(x):\n    return x + 1\n\ndef g(y, z):\n    return y\n\ndef k():\n    return 0\n\ndef h():\n    pass\n"
    base = [sym("f", 12, 0, 1, "(x)"), sym("g", 12, 3, 4, "(y)"), sym("k", 12, 6, 7, "()")]
    head = [sym("f", 12, 0, 1, "(x)"), sym("g", 12, 3, 4, "(y, z)"), sym("k", 12, 6, 7, "()"), sym("h", 12, 9, 10, "()")]
    hunks = [Hunk(Path("a.py"), 2, 1, 2, 1), Hunk(Path("a.py"), 4, 1, 4, 1), Hunk(Path("a.py"), 9, 0, 10, 2)]
    result = {c.symbol.name: c.status for c in classify(head, base, hunks, head_text, base_text, "python", ".py")}
    assert result == {"f": "body", "g": "signature", "h": "added"}


def test_classify_drops_formatting_only_change(no_ast_grep):
    base_text = "def f(x):\n    return x+1\n"
    head_text = "def f(x):\n    return x + 1\n"
    hunks = [Hunk(Path("a.py"), 2, 1, 2, 1)]
    assert classify([sym("f", 12, 0, 1)], [sym("f", 12, 0, 1)], hunks, head_text, base_text, "python", ".py") == []


def test_classify_reports_removed_symbol(no_ast_grep):
    base_text = "def f():\n    pass\n\ndef gone():\n    pass\n"
    head_text = "def f():\n    pass\n"
    hunks = [Hunk(Path("a.py"), 3, 3, 2, 0)]
    result = classify([sym("f", 12, 0, 1)], [sym("f", 12, 0, 1), sym("gone", 12, 3, 4)], hunks, head_text, base_text, "python", ".py")
    assert [(c.symbol.name, c.status, c.marker) for c in result] == [("gone", "removed", "-")]
