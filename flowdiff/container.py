"""The container half of the environment: the compile database as the source of truth for where the tree
is mounted, a flowdiff layer over the project's dev image, a run wrapper, and the project's own build
(decisions 25, 44, 45)."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

LAYER_LABEL = "flowdiff.base"
LAYER_DOCKERFILE = """\
FROM {base}
RUN apt-get update && apt-get install -y --no-install-recommends gdb clangd binutils \\
    && rm -rf /var/lib/apt/lists/*
LABEL {label}={digest}
"""
BUILD_DIR = "builddir"
COLCON_BUILD_DIR = "build"
DEVICE_LIBS = ("libnvbufsurface", "libcuda", "libcudart", "libnvinfer")
SCRATCH = ".flowdiff"


class ContainerError(RuntimeError):
    pass


def docker(*args: str, check: bool = True, **kwargs) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(["docker", *args], capture_output=True, text=True, **kwargs)
    if check and proc.returncode != 0:
        raise ContainerError(f"docker {args[0]}: {proc.stderr.strip()[-2000:]}")
    return proc


def image_exists(tag: str) -> bool:
    return docker("image", "inspect", tag, check=False).returncode == 0


def image_field(tag: str, template: str) -> str:
    return docker("image", "inspect", "--format", template, tag).stdout.strip()


def project_image(root: Path, explicit: str | None) -> str | None:
    """--image, else <repo name>:dev when such an image exists locally (the convention of the demo target)."""
    if explicit:
        return explicit
    tag = f"{root.name}:dev"
    return tag if image_exists(tag) else None


def layered_image(root: Path, base: str) -> str:
    """flowdiff/<project>:dev, rebuilt only when the base image's digest changed (decision 44)."""
    digest = image_field(base, "{{.Id}}")
    tag = f"flowdiff/{root.name}:dev"
    if image_exists(tag) and image_field(tag, '{{index .Config.Labels "%s"}}' % LAYER_LABEL) == digest:
        return tag
    with tempfile.TemporaryDirectory(prefix="flowdiff-layer-") as tmp:
        (Path(tmp) / "Dockerfile").write_text(LAYER_DOCKERFILE.format(base=base, label=LAYER_LABEL, digest=digest))
        docker("build", "-q", "-t", tag, tmp)
    return tag


def compile_databases(tree: Path) -> list[Path]:
    """Every compile_commands.json a build could have left: per package first (colcon writes one per
    package under build/<pkg>), then the top-level build directories, then the tree root."""
    per_package = sorted(p for d in tree.glob("build*") for p in d.glob("*/compile_commands.json") if p.is_file())
    top = sorted(d / "compile_commands.json" for d in tree.glob("build*") if (d / "compile_commands.json").is_file())
    root = [tree / "compile_commands.json"] if (tree / "compile_commands.json").is_file() else []
    return per_package + top + root


def compile_database(tree: Path) -> Path | None:
    """One database for the whole tree: the single one found, or all of them merged under .flowdiff with
    the first entry per (directory, file) kept, so a stale top-level copy never shadows a package's own."""
    found = compile_databases(tree)
    if not found:
        return None
    if len(found) == 1:
        return found[0]
    seen: set[tuple[str, str]] = set()
    merged: list[dict] = []
    for path in found:
        try:
            entries = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        for entry in entries:
            key = (entry.get("directory", ""), entry.get("file", ""))
            if key not in seen:
                seen.add(key)
                merged.append(entry)
    scratch = tree / SCRATCH
    scratch.mkdir(exist_ok=True)
    ignore = scratch / ".gitignore"
    if not ignore.exists():
        ignore.write_text("*\n")
    target = scratch / "compile_commands.json"
    text = json.dumps(merged, indent=1)
    if not target.exists() or target.read_text() != text:
        target.write_text(text)
    return target


def entries_of(db: Path | None) -> list[dict]:
    if db is None:
        return []
    try:
        return json.loads(db.read_text())
    except (OSError, ValueError):
        return []


def mount_point(tree: Path, db: Path, entries: list[dict]) -> str | None:
    """Where the tree was mounted when the database was written: an entry's directory ends with the
    database's own tree-relative directory, and what precedes it is the mount (decision 44)."""
    rel_dir = db.parent.relative_to(tree).as_posix()
    for entry in entries:
        directory = entry.get("directory", "")
        if rel_dir == ".":
            if directory and not Path(directory).is_dir():
                return directory
            continue
        if directory.endswith("/" + rel_dir):
            return directory[: -len(rel_dir) - 1] or "/"
    for path in compile_databases(tree):
        rel = path.parent.relative_to(tree).as_posix()
        for entry in entries_of(path):
            directory = entry.get("directory", "")
            if directory.endswith("/" + rel):
                return directory[: -len(rel) - 1] or "/"
    return None


@dataclass(frozen=True)
class Container:
    image: str
    workdir: str
    # Where clangd finds compile_commands.json inside the container; the meson default when unset.
    db_dir: str = ""

    @property
    def compile_commands_dir(self) -> str:
        return self.db_dir or f"{self.workdir}/{BUILD_DIR}"

    def command(self, tree: Path, args: list[str], interactive: bool = False,
                mounts: list[tuple[Path, str]] = [], cwd: str | None = None) -> list[str]:
        flags = ["--rm", "-i"] if interactive else ["--rm"]
        volumes = [f"{tree.resolve()}:{self.workdir}"] + [f"{host.resolve()}:{self.workdir}/{rel}" for host, rel in mounts]
        return ["docker", "run", *flags, "--user", f"{os.getuid()}:{os.getgid()}", "-e", "HOME=/tmp",
                *[flag for v in volumes for flag in ("-v", v)], "-w", cwd or self.workdir, self.image, *args]

    def clangd_command(self, tree: Path, scratch: Path) -> list[str]:
        """clangd keeps its index under <project>/.cache; a mount points that at the scratch dir instead."""
        cache = scratch / "clangd-cache"
        cache.mkdir(parents=True, exist_ok=True)
        return self.command(tree, [], interactive=True, mounts=[(cache, ".cache")])

    def run(self, tree: Path, args: list[str], timeout: float | None = None,
            cwd: str | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run(self.command(tree, args, cwd=cwd), capture_output=True, text=True, timeout=timeout)

    def path(self, tree: Path, host: Path) -> str:
        return self.workdir + "/" + host.resolve().relative_to(tree.resolve()).as_posix()

    def host_path(self, tree: Path, inside: str) -> Path:
        """A container path back on the host, when it is under the mount."""
        if inside == self.workdir:
            return tree
        if inside.startswith(self.workdir + "/"):
            return tree / inside[len(self.workdir) + 1:]
        return Path(inside)


def db_dir_of(tree: Path, db: Path, mount: str) -> str:
    return mount + "/" + db.parent.relative_to(tree).as_posix() if db.parent != tree else mount


def detect(root: Path, explicit: str | None) -> Container | None:
    """Container mode when the compile database is absent or points at a directory the host does not have."""
    if not needs_container(root):
        return None
    if shutil.which("docker") is None:
        raise ContainerError("docker not found on PATH, and the compile database is not usable on this host")
    base = project_image(root, explicit)
    if base is None:
        raise ContainerError(f"no dev image for {root.name}: pass --image, or build {root.name}:dev")
    image = layered_image(root, base)
    db = compile_database(root)
    mount = (mount_point(root, db, entries_of(db)) if db is not None else None) \
        or image_field(base, "{{.Config.WorkingDir}}") or "/src"
    db_dir = db_dir_of(root, db, mount) if db is not None else f"{mount}/{BUILD_DIR}"
    return Container(image, mount, db_dir)


def find_compile_db(root: Path) -> Path | None:
    return compile_database(root)


def needs_container(root: Path) -> bool:
    entries = entries_of(compile_database(root))
    return not entries or not Path(entries[0].get("directory", "")).is_dir()


def build_system(tree: Path) -> str | None:
    if (tree / "meson.build").is_file():
        return "meson"
    if (tree / "CMakeLists.txt").is_file():
        return "cmake"
    if any(tree.glob("*/package.xml")) or any(tree.glob("src/*/package.xml")):
        return "colcon"
    return None


def build(container: Container, tree: Path, timeout: float) -> str | None:
    """Configure once, then the project's own incremental build; the error text on failure, else None."""
    build_dir = tree / BUILD_DIR
    system = build_system(tree)
    if system == "meson":
        steps = ([] if (build_dir / "build.ninja").is_file()
                 else [["meson", "setup", BUILD_DIR, "-Dbuildtype=debug"]]) + [["ninja", "-C", BUILD_DIR]]
    elif system == "cmake":
        steps = ([] if (build_dir / "CMakeCache.txt").is_file()
                 else [["cmake", "-S", ".", "-B", BUILD_DIR, "-DCMAKE_BUILD_TYPE=Debug",
                        "-DCMAKE_EXPORT_COMPILE_COMMANDS=ON"]]) + [["cmake", "--build", BUILD_DIR]]
    elif system == "colcon":
        steps = [["colcon", "build", "--symlink-install", "--cmake-args", "-DCMAKE_EXPORT_COMPILE_COMMANDS=ON"]]
    else:
        return f"{tree}: no meson.build, CMakeLists.txt or package.xml; no build system to run"
    for step in steps:
        try:
            proc = container.run(tree, step, timeout=timeout)
        except subprocess.TimeoutExpired:
            return f"{' '.join(step)}: timed out after {timeout:.0f}s"
        if proc.returncode != 0:
            return f"{' '.join(step)} failed:\n{(proc.stdout.rstrip() + chr(10) + proc.stderr).strip()[-3000:]}"
    return None


def needed_libraries(container: Container, tree: Path, artefact: str) -> list[str]:
    """NEEDED entries of a built artefact, by readelf inside the container (decision 22)."""
    proc = container.run(tree, ["readelf", "-d", artefact])
    return [line.split("[", 1)[1].rstrip("]").strip() for line in proc.stdout.splitlines()
            if "(NEEDED)" in line and "[" in line]


def device_libraries(libraries: list[str]) -> list[str]:
    return sorted({lib for lib in libraries if any(lib.startswith(d) for d in DEVICE_LIBS)})
