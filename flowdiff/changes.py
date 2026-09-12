"""Changed symbols: git diff -U0 hunks intersected with LSP documentSymbol ranges."""
from __future__ import annotations

import json
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .lsp import TRACKED_KINDS, LspClient, Symbol

HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


@dataclass(frozen=True)
class Hunk:
    path: Path
    old_start: int
    old_lines: int
    new_start: int
    new_lines: int

    def old_span(self) -> tuple[int, int]:
        return self.old_start - 1, self.old_start - 1 + max(self.old_lines - 1, 0)

    def new_span(self) -> tuple[int, int]:
        return self.new_start - 1, self.new_start - 1 + max(self.new_lines - 1, 0)


@dataclass(frozen=True)
class Revisions:
    """base and head as git tree-ish names; head None means the working tree."""
    repo: Path
    base: str
    head: str | None

    def read_base(self, path: Path) -> str:
        return git_show(self.repo, self.base, path)

    def read_head(self, path: Path) -> str:
        if self.head is None:
            return (self.repo / path).read_text(encoding="utf-8", errors="replace")
        return git_show(self.repo, self.head, path)

    def diff_args(self) -> list[str]:
        return [self.base] if self.head is None else [self.base, self.head]


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True, text=True).stdout


def git_show(repo: Path, rev: str, path: Path) -> str:
    result = subprocess.run(["git", "-C", str(repo), "show", f"{rev}:{path.as_posix()}"],
                            capture_output=True, text=True)
    return result.stdout if result.returncode == 0 else ""


def diff_hunks(revs: Revisions) -> list[Hunk]:
    out = git(revs.repo, "diff", "-U0", "--no-color", "--no-ext-diff", "--no-renames", *revs.diff_args())
    hunks: list[Hunk] = []
    path: Path | None = None
    for line in out.splitlines():
        if line.startswith("--- ") and line[4:] != "/dev/null":
            path = Path(line[4:].removeprefix("a/"))
        elif line.startswith("+++ ") and line[4:] != "/dev/null":
            path = Path(line[4:].removeprefix("b/"))
        elif line.startswith("@@") and path is not None:
            m = HUNK_RE.match(line)
            if m:
                hunks.append(Hunk(path, int(m[1]), int(m[2] or 1), int(m[3]), int(m[4] or 1)))
    if revs.head is None:
        hunks += untracked_hunks(revs.repo)
    return hunks


def untracked_hunks(repo: Path) -> list[Hunk]:
    """git diff never lists untracked files; a new file is one hunk covering all of it."""
    hunks = []
    for name in git(repo, "ls-files", "--others", "--exclude-standard").splitlines():
        path = Path(name)
        try:
            count = len((repo / path).read_text(encoding="utf-8", errors="replace").splitlines())
        except (OSError, IsADirectoryError):
            continue
        hunks.append(Hunk(path, 0, 0, 1, count))
    return hunks


@dataclass(frozen=True)
class ChangedSymbol:
    symbol: Symbol
    status: str  # body | signature | added | removed

    @property
    def marker(self) -> str:
        return {"body": "~", "signature": "~", "added": "+", "removed": "-"}[self.status]


def changed_symbols(client: LspClient, revs: Revisions, hunks: list[Hunk],
                    ast_grep_language: str) -> list[ChangedSymbol]:
    by_path: dict[Path, list[Hunk]] = {}
    for h in hunks:
        by_path.setdefault(h.path, []).append(h)

    result: list[ChangedSymbol] = []
    for rel, file_hunks in by_path.items():
        abs_path = (revs.repo / rel).resolve()
        base_text = revs.read_base(rel)
        head_text = revs.read_head(rel)

        client.open(abs_path, base_text)
        base_syms = [s for s in client.document_symbols(abs_path) if s.kind in TRACKED_KINDS]
        client.open(abs_path, head_text)
        head_syms = [s for s in client.document_symbols(abs_path) if s.kind in TRACKED_KINDS]
        result += classify(head_syms, base_syms, file_hunks, head_text, base_text,
                           ast_grep_language, rel.suffix)
    return result


def base_symbols(client: LspClient, root: Path, base_tree: Path, changed: list[ChangedSymbol]) -> list[ChangedSymbol]:
    """The changed symbols as they exist in the base worktree, matched by name and kind in the same file,
    so a graph can be built around them on that revision (decision 37). Added symbols have no base side."""
    result: list[ChangedSymbol] = []
    cache: dict[Path, list[Symbol]] = {}
    for c in changed:
        if c.status == "added":
            continue
        path = (base_tree / c.symbol.path.resolve().relative_to(root.resolve())).resolve()
        if path not in cache:
            if not path.is_file():
                cache[path] = []
                continue
            client.open(path, path.read_text(encoding="utf-8", errors="replace"))
            cache[path] = client.document_symbols(path)
        match = next((s for s in cache[path] if (s.name, s.kind) == (c.symbol.name, c.symbol.kind)), None)
        if match is not None:
            result.append(ChangedSymbol(match, c.status))
    return result


def classify(head_syms: list[Symbol], base_syms: list[Symbol], hunks: list[Hunk],
             head_text: str, base_text: str, language: str, suffix: str) -> list[ChangedSymbol]:
    base_by_key = {(s.name, s.kind): s for s in base_syms}
    head_by_key = {(s.name, s.kind): s for s in head_syms}
    result: list[ChangedSymbol] = []

    for sym in head_syms:
        if not any(sym.range.overlaps_lines(*h.new_span()) for h in hunks):
            continue
        base = base_by_key.get((sym.name, sym.kind))
        if base is None:
            result.append(ChangedSymbol(sym, "added"))
            continue
        head_norm = normalized(language, suffix, sym.range.slice(head_text))
        base_norm = normalized(language, suffix, base.range.slice(base_text))
        if head_norm == base_norm:
            continue
        drifted = signature_text(sym, head_text) != signature_text(base, base_text)
        result.append(ChangedSymbol(sym, "signature" if drifted else "body"))

    for sym in base_syms:
        if (sym.name, sym.kind) in head_by_key:
            continue
        if any(sym.range.overlaps_lines(*h.old_span()) for h in hunks):
            result.append(ChangedSymbol(sym, "removed"))
    return result


def signature_text(sym: Symbol, text: str) -> str:
    if sym.detail:
        return sym.detail
    lines = text.splitlines()
    line = lines[sym.selection.start.line] if sym.selection.start.line < len(lines) else ""
    return "".join(line.split())


def normalized(language: str, suffix: str, text: str) -> str:
    """Comment nodes stripped, whitespace collapsed; two spellings of the same code compare equal."""
    for start, end in sorted(comment_spans(language, suffix, text), reverse=True):
        text = text[:start] + text[end:]
    return "".join(text.split())


def comment_spans(language: str, suffix: str, text: str) -> list[tuple[int, int]]:
    encoded = text.encode()
    spans = []
    for m in scan_kinds(language, suffix, text, ("comment",)):
        b = m["range"]["byteOffset"]
        spans.append((len(encoded[:b["start"]].decode()), len(encoded[:b["end"]].decode())))
    return spans


def scan_kinds(language: str, suffix: str, text: str, kinds: tuple[str, ...]) -> list[dict[str, Any]]:
    if not kinds:
        return []
    rule = f"id: kinds\nlanguage: {language}\nrule:\n  any:\n" + "".join(f"    - kind: {k}\n" for k in kinds)
    return scan_rule(rule, suffix, text)


def scan_rule(rule: str, suffix: str, text: str) -> list[dict[str, Any]]:
    if not text.strip():
        return []
    with tempfile.NamedTemporaryFile("w", suffix=suffix, delete_on_close=False) as f:
        f.write(text)
        f.close()
        out = subprocess.run(["ast-grep", "scan", "--inline-rules", rule, "--json=compact", f.name],
                             capture_output=True, text=True)
    if out.returncode not in (0, 1) or not out.stdout.strip():
        return []
    return json.loads(out.stdout)
