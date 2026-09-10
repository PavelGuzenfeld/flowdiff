import shutil
import subprocess
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


def test_new_file_in_ref_mode_has_zero_old_span(repo: Path):
    (repo / "b.py").write_text("x = 1\n")
    git(repo, "add", "b.py")
    git(repo, "commit", "-q", "-m", "add b")
    hunks = diff_hunks(Revisions(repo, "HEAD~1", "HEAD"))
    assert hunks == [Hunk(Path("b.py"), 0, 0, 1, 1)]


def test_git_failures_raise(repo: Path):
    with pytest.raises(subprocess.CalledProcessError):
        changes.git(repo, "definitely-not-a-git-verb")


def test_markers_cover_every_status():
    s = sym("f", 12, 0, 1)
    assert [changes.ChangedSymbol(s, st).marker for st in ("body", "signature", "added", "removed")] == ["~", "~", "+", "-"]


class FakeClient:
    """Answers document_symbols from whatever text was opened last; tracks kinds seen."""

    def __init__(self, symbols_by_text: dict[str, list[Symbol]]):
        self.symbols_by_text = symbols_by_text
        self.opened: list[tuple[Path, str]] = []

    def open(self, path: Path, text: str) -> None:
        self.opened.append((path, text))

    def document_symbols(self, path: Path) -> list[Symbol]:
        return self.symbols_by_text[self.opened[-1][1]]


def test_changed_symbols_opens_base_then_head_and_filters_kinds(repo: Path, no_ast_grep):
    head_text = SOURCE.replace("x + 1", "x + 2")
    (repo / "a.py").write_text(head_text)
    variable = sym("CONST", 13, 0, 0)
    f = sym("f", 12, 0, 1, "(x)")
    g = sym("g", 12, 4, 5, "(y)")
    client = FakeClient({SOURCE: [f, g, variable], head_text: [f, g, variable]})
    revs = Revisions(repo, "HEAD", None)
    result = changes.changed_symbols(client, revs, diff_hunks(revs), "python")  # type: ignore[arg-type]
    assert [(c.symbol.name, c.status) for c in result] == [("f", "body")]
    assert [t for _, t in client.opened] == [SOURCE, head_text]
    assert client.opened[0][0] == (repo / "a.py").resolve()


def test_changed_symbols_groups_hunks_per_file(repo: Path, no_ast_grep):
    (repo / "a.py").write_text(SOURCE.replace("x + 1", "x + 2"))
    (repo / "b.py").write_text("def h():\n    return 0\n")
    f = sym("f", 12, 0, 1, "(x)")
    h = sym("h", 12, 0, 1, "()", path=str(repo / "b.py"))
    client = FakeClient({SOURCE: [f], SOURCE.replace("x + 1", "x + 2"): [f], "": [], "def h():\n    return 0\n": [h]})
    revs = Revisions(repo, "HEAD", None)
    result = changes.changed_symbols(client, revs, diff_hunks(revs), "python")  # type: ignore[arg-type]
    assert sorted((c.symbol.name, c.status) for c in result) == [("f", "body"), ("h", "added")]


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


def test_comment_spans_empty_when_ast_grep_fails(monkeypatch):
    class Broken:
        returncode = 2
        stdout = ""
    monkeypatch.setattr(changes.subprocess, "run", lambda *a, **k: Broken())
    assert changes.comment_spans("python", ".py", "x = 1  # c\n") == []


@pytest.mark.skipif(shutil.which("ast-grep") is None, reason="ast-grep not installed")
def test_comment_spans_are_character_offsets_not_bytes():
    text = "s = 'é'  # c\n"
    spans = changes.comment_spans("python", ".py", text)
    assert [text[a:b] for a, b in spans] == ["# c"]


def test_signature_text_prefers_detail_then_selection_line():
    with_detail = sym("f", 12, 0, 2, detail="(x: int) -> int")
    assert changes.signature_text(with_detail, "def f(x):\n") == "(x: int) -> int"
    assert changes.signature_text(sym("f", 12, 0, 2), "def f( x ):\n    pass\n") == "deff(x):"
    assert changes.signature_text(sym("f", 12, 5, 6), "def f():\n") == ""


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
