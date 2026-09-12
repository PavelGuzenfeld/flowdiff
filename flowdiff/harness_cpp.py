"""The C++ side of play: which built test executables reach the flow, how to build a literal harness
against the entry's translation unit, and how to run either under gdb in the container so the flow's
frames are traced (decisions 18 to 24, 28, 39, 41, 42).

Two build layouts are understood. Meson keeps a target's objects under <target>.p/ next to the artefact
and names the object in the compile database's output field. CMake (and colcon, which drives CMake per
package) keeps them under <build dir>/CMakeFiles/<target>.dir/ with no output field, and puts the
executable or lib<target>.so in that build dir."""
from __future__ import annotations

import re
import shlex
from pathlib import Path

from . import container, trace_gdb, worktree
from .graph import Flow, Node

LITERAL = re.compile(r"""^(?:[-+]?(?:0[xX][0-9a-fA-F]+|\d+(?:\.\d*)?(?:[eE][-+]?\d+)?[uUlLfF]*)|true|false|nullptr|NULL|'(?:\\.|[^'\\])'|"(?:\\.|[^"\\])*")$""")


def frame_id(root: Path, node: Node) -> str:
    return f"{node.path.resolve().relative_to(root.resolve()).as_posix()}:{node.name}"


def compile_db(tree: Path) -> list[dict]:
    return container.entries_of(container.compile_database(tree))


def host_dir(tree: Path, entry: dict, workdir: str | None) -> Path:
    """The entry's directory on the host: the mount prefix translated when there is one."""
    directory = entry.get("directory", "")
    if workdir and (directory == workdir or directory.startswith(workdir + "/")):
        return tree / directory[len(workdir):].lstrip("/")
    return Path(directory) if directory else tree


def source_of(tree: Path, entry: dict, workdir: str | None) -> Path:
    """The entry's source as a host path: relative files resolve against the entry's (translated) directory."""
    file = Path(entry["file"])
    if not file.is_absolute():
        return (host_dir(tree, entry, workdir) / file).resolve()
    if workdir and file.as_posix().startswith(workdir + "/"):
        return (tree / file.as_posix()[len(workdir) + 1:]).resolve()
    return file.resolve()


def target_of(tree: Path, entry: dict, workdir: str | None) -> tuple[Path, str] | None:
    """(objects directory on the host, target name) for the entry's target, in either layout."""
    output = entry.get("output", "")
    directory = host_dir(tree, entry, workdir)
    if ".p/" in output:
        prefix = output.split(".p/")[0]
        return directory / f"{prefix}.p", Path(prefix).name
    stem = Path(entry["file"]).name + ".o"
    for objects in sorted((directory / "CMakeFiles").glob("*.dir")):
        if any(o.name == stem for o in objects.rglob("*.o")):
            return objects, objects.name[: -len(".dir")]
    return None


def output_flag(entry: dict) -> str:
    """CMake names the object after -o in the command rather than in an output field."""
    words = shlex.split(entry["command"]) if "command" in entry else list(entry.get("arguments", []))
    for i, word in enumerate(words):
        if word == "-o" and i + 1 < len(words):
            return words[i + 1]
    return ""


def object_of(tree: Path, entry: dict, workdir: str | None) -> Path | None:
    """The object this entry produced; the name the entry itself gives, since two targets can compile one
    source and only this entry's own object dates this entry's compile."""
    named = entry.get("output") or output_flag(entry)
    if not named:
        return None
    candidate = host_dir(tree, entry, workdir) / named
    return candidate if candidate.is_file() else None


# A changed header has no compile entry of its own, so only translation units can be dated this way.
TU_SUFFIXES = frozenset({".c", ".cc", ".cpp", ".cxx", ".cu"})


def unusable_after_build(tree: Path, sources: list[Path], workdir: str | None) -> str | None:
    """Why a failed build's artefacts cannot carry the analysis, or None when they can. A build that broke
    elsewhere still answers the question; one that left an object older than its source answers it wrongly."""
    db = container.compile_database(tree)
    if db is None:
        return "no compile database was written"
    by_source: dict[str, dict] = {}
    for entry in container.entries_of(db):
        try:
            by_source.setdefault(source_of(tree, entry, workdir).relative_to(tree.resolve()).as_posix(), entry)
        except ValueError:
            continue
    for rel in sorted({s.as_posix() for s in sources if s.suffix in TU_SUFFIXES}):
        entry = by_source.get(rel)
        if entry is None:
            return f"{rel} is not in the compile database"
        obj = object_of(tree, entry, workdir)
        if obj is None:
            return f"{rel} has no object in the build tree"
        if obj.stat().st_mtime < (tree / rel).stat().st_mtime:
            return f"{obj.relative_to(tree).as_posix()} is older than {rel}"
    return None


def artefact_of(objects: Path, target: str) -> Path | None:
    """The built artefact for a target: meson's sits beside the .p dir under its own name, CMake's is the
    executable or lib<target>.so in the build directory."""
    build = objects.parent.parent if objects.parent.name == "CMakeFiles" else objects.parent
    for candidate in (build / target, build / f"lib{target}.so"):
        if candidate.is_file():
            return candidate
    for candidate in build.glob(f"lib{target}.so*"):
        if candidate.is_file():
            return candidate
    return None


def test_executables(tree: Path, test_files: list[str], workdir: str | None = None) -> dict[str, str]:
    """test file (tree-relative) -> executable (tree-relative), through the compile database; only when the
    executable exists in the build tree."""
    found: dict[str, str] = {}
    for entry in compile_db(tree):
        try:
            rel = source_of(tree, entry, workdir).relative_to(tree.resolve()).as_posix()
        except ValueError:
            continue
        if rel not in test_files or rel in found:
            continue
        target = target_of(tree, entry, workdir)
        if target is None:
            continue
        exe = artefact_of(*target)
        if exe is not None and ".so" not in exe.suffixes:
            found[rel] = exe.resolve().relative_to(tree.resolve()).as_posix()
    return found


def harvest(client, root: Path, entry: Node) -> list[str] | None:
    """Literal arguments from the entry's call sites, test files first (decision 18); None when none has them."""
    from .lsp import Position
    from .graph import is_test_path
    refs = client.references(entry.path, Position(entry.line, entry.col))
    sites = sorted(((loc.path, loc.range.start.line) for loc in refs),
                   key=lambda s: (not is_test_path(s[0], root), str(s[0]), s[1]))
    for path, line in sites:
        args = literal_call(path.read_text(encoding="utf-8", errors="replace"), line, entry.name)
        if args is not None:
            return [a.strip() for a in args]
    return None


def literal_call(text: str, line: int, name: str) -> list[str] | None:
    """Arguments of the call to `name` on 0-based `line` when every one is a literal, else None."""
    short = name.rsplit("::", 1)[-1]
    lines = text.splitlines()
    if line >= len(lines):
        return None
    for match in re.finditer(r"(?<![\w])" + re.escape(short) + r"\s*\(", lines[line]):
        inner = balanced(lines[line][match.end():])
        if inner is None:
            return None
        args = split_arguments(inner)
        if args is not None and all(LITERAL.match(a.strip()) for a in args):
            return args
    return None


def balanced(text: str) -> str | None:
    """The text up to the parenthesis closing an already-open one, or None when it never closes."""
    depth, quote = 1, None
    for i, ch in enumerate(text):
        if quote:
            if ch == quote and text[i - 1] != "\\":
                quote = None
        elif ch in "\"'":
            quote = ch
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return text[:i]
    return None


def split_arguments(text: str) -> list[str] | None:
    """Top-level comma split; None when parentheses do not balance on this line."""
    out, depth, current, quote = [], 0, "", None
    for ch in text:
        if quote:
            current += ch
            if ch == quote:
                quote = None
            continue
        if ch in "\"'":
            quote = ch
        elif ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
            if depth < 0:
                return None
        elif ch == "," and depth == 0:
            out.append(current)
            current = ""
            continue
        current += ch
    if depth != 0:
        return None
    return [a for a in out + [current] if a.strip()] if (out or current.strip()) else []


def gdb_frames(root: Path, flow: Flow) -> dict[str, str]:
    return {frame_id(root, f): f.name for f in flow.frames if f.status not in ("slot", "removed")}


def test_symbols(tests: list[str]) -> dict[str, str]:
    """Function names whose start marks a test's calls: the id's function, and the TEST(name) macro's test_name."""
    out: dict[str, str] = {}
    for test in tests:
        _, _, name = test.partition("::")
        if name and name != "<module>":
            out[name] = test
            out[f"test_{name}"] = test
    return out


def trace_tests(ctr: container.Container, tree: Path, executables: list[str], frames: dict[str, str],
                tests: dict[str, str], out: Path, timeout: float) -> str | None:
    """Run every executable under gdb in `tree`, appending to `out` (a path under the tree); the failure text or None."""
    scratch = worktree.scratch_dir(tree) / "run"
    scratch.mkdir(exist_ok=True)
    out.unlink(missing_ok=True)
    out.touch()
    for i, exe in enumerate(executables):
        part = scratch / f"gdb{i}.jsonl"
        part.unlink(missing_ok=True)
        script_path = scratch / f"gdb{i}.py"
        script_path.write_text(trace_gdb.script(ctr.workdir, frames, tests, ctr.path(tree, part)))
        cmd = trace_gdb.gdb_command(ctr.path(tree, script_path), f"{ctr.workdir}/{exe}")
        try:
            proc = ctr.run(tree, cmd, timeout=timeout)
        except Exception as exc:  # TimeoutExpired or docker failure
            return f"{exe}: gdb did not finish: {exc}"
        if not part.exists():
            return f"{exe}: gdb wrote no trace\n{(proc.stdout + proc.stderr).strip()[-2000:]}"
        with out.open("a") as merged:
            merged.write(part.read_text())
    return None


def compile_entry(tree: Path, source_rel: str, workdir: str | None) -> dict | None:
    for entry in compile_db(tree):
        try:
            if source_of(tree, entry, workdir).relative_to(tree.resolve()).as_posix() == source_rel:
                return entry
        except ValueError:
            continue
    return None


DROP_WITH_VALUE = {"-o", "-MQ", "-MF", "-MT"}
DROP = {"-c", "-MD", "-MMD"}


def borrowed_flags(entry: dict) -> list[str]:
    """The TU's own compile command without its output, dependency and source arguments (decision 21)."""
    words = shlex.split(entry["command"]) if "command" in entry else list(entry.get("arguments", []))
    out: list[str] = []
    skip = False
    for word in words[1:]:
        if skip:
            skip = False
            continue
        if word in DROP_WITH_VALUE:
            skip = True
        elif word in DROP or word == entry["file"]:
            continue
        else:
            out.append(word)
    return out + ["-g", "-O0", "-fno-inline"]


def harness_source(tu: str, call: str) -> str:
    """The TU is included, not linked, so its statics are callable (decision 20)."""
    return f'#include "{tu}"\n\nint main() {{\n    (void)({call});\n    return 0;\n}}\n'


def link_inputs(ctr: container.Container, tree: Path, entry: dict) -> tuple[list[str], list[str]] | None:
    """Sibling objects of the entry's target (its own object excluded, the harness includes that TU) and the
    linker flags for every library the built artefact NEEDs: internal ones by path, external ones by -l (22).
    Paths are container paths."""
    target = target_of(tree, entry, ctr.workdir)
    if target is None:
        return None
    objects, name = target
    own = Path(entry["file"]).name + ".o"
    siblings = sorted(ctr.path(tree, o) for o in objects.rglob("*.o") if o.name != own)
    artefact = artefact_of(objects, name)
    if artefact is None:
        return siblings, []
    flags: list[str] = []
    rpaths: list[str] = []
    build_roots = [d for d in tree.glob("build*") if d.is_dir()]
    for lib in container.needed_libraries(ctr, tree, ctr.path(tree, artefact)):
        internal = [p for d in build_roots for p in d.rglob(lib) if p.is_file()]
        if internal:
            flags.append(ctr.path(tree, internal[0]))
            rpaths.append(f"-Wl,-rpath,{ctr.path(tree, internal[0].parent)}")
        else:
            flags.append(f"-l{re.sub(r'^lib|\.so(\.\d+)*$', '', lib)}")
    return siblings, flags + sorted(set(rpaths))


def build_harness(ctr: container.Container, tree: Path, source_rel: str, call: str, out_dir_rel: str,
                  timeout: float) -> str | tuple[str, str]:
    """Compile and link the harness inside the container from the TU's own build directory; the
    executable's tree-relative path, or the error text."""
    entry = compile_entry(tree, source_rel, ctr.workdir)
    if entry is None:
        return f"{source_rel}: not in the compile database"
    inputs = link_inputs(ctr, tree, entry)
    if inputs is None:
        return f"{source_rel}: its compile entry names no build target"
    siblings, libs = inputs
    harness_dir = tree / out_dir_rel
    harness_dir.mkdir(parents=True, exist_ok=True)
    (harness_dir / "harness.cpp").write_text(harness_source(f"{ctr.workdir}/{source_rel}", call))
    harness = f"{ctr.workdir}/{out_dir_rel}/harness"
    compiler = shlex.split(entry["command"])[0] if "command" in entry else (entry.get("arguments") or ["c++"])[0]
    steps = [[compiler, *borrowed_flags(entry), "-o", f"{harness}.o", "-c", f"{harness}.cpp"],
             ["c++", f"{harness}.o", *siblings, *libs, "-pthread", "-o", harness]]
    for step in steps:
        try:
            proc = ctr.run(tree, step, timeout=timeout, cwd=entry.get("directory"))
        except Exception as exc:
            return f"harness: {exc}"
        if proc.returncode != 0:
            return f"harness {'link' if step is steps[1] else 'compile'} failed:\n{(proc.stdout + proc.stderr).strip()[-3000:]}"
    return (f"{out_dir_rel}/harness", "")


def run_executables(ctr: container.Container, tree: Path, executables: list[str], timeout: float) -> dict[str, str]:
    results = {}
    for exe in executables:
        try:
            proc = ctr.run(tree, [f"{ctr.workdir}/{exe}"], timeout=timeout)
            results[exe] = "PASS" if proc.returncode == 0 else "FAIL"
        except Exception:
            results[exe] = "TIMEOUT"
    return results


def linked_variant(ctr: container.Container, tree: Path, executables: list[str]) -> tuple[list[str], str]:
    """Device libraries the executables link, and the verdict line naming the variant (decisions 41, 42)."""
    libs: list[str] = []
    for exe in executables:
        libs += container.needed_libraries(ctr, tree, f"{ctr.workdir}/{exe}")
    device = container.device_libraries(libs)
    if device:
        return device, f"linked: {', '.join(device)} (device build)"
    return [], "linked: no device libraries (host build)"
