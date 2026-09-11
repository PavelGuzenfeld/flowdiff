"""The container half of the environment: a flowdiff layer over the project's dev image, a run wrapper that
mounts a tree where the compile database expects it, and the project's own build (decisions 25, 44, 45)."""
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
DEVICE_LIBS = ("libnvbufsurface", "libcuda", "libcudart", "libnvinfer")


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


@dataclass(frozen=True)
class Container:
    image: str
    workdir: str

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
    return Container(image, image_field(base, "{{.Config.WorkingDir}}") or "/src")


def find_compile_db(root: Path) -> Path | None:
    candidates = [root, *sorted(root.glob("build*"))]
    found = [d / "compile_commands.json" for d in candidates if (d / "compile_commands.json").is_file()]
    return max(found, key=lambda p: p.stat().st_mtime) if found else None


def needs_container(root: Path) -> bool:
    db = find_compile_db(root)
    if db is None:
        return True
    try:
        entries = json.loads(db.read_text())
    except (OSError, ValueError):
        return True
    return not entries or not Path(entries[0].get("directory", "")).is_dir()


def build(container: Container, tree: Path, timeout: float) -> str | None:
    """Configure once, then the project's own incremental build; the error text on failure, else None."""
    build_dir = tree / BUILD_DIR
    if (tree / "meson.build").is_file():
        steps = ([] if (build_dir / "build.ninja").is_file()
                 else [["meson", "setup", BUILD_DIR, "-Dbuildtype=debug"]]) + [["ninja", "-C", BUILD_DIR]]
    elif (tree / "CMakeLists.txt").is_file():
        steps = ([] if (build_dir / "CMakeCache.txt").is_file()
                 else [["cmake", "-S", ".", "-B", BUILD_DIR, "-DCMAKE_BUILD_TYPE=Debug",
                        "-DCMAKE_EXPORT_COMPILE_COMMANDS=ON"]]) + [["cmake", "--build", BUILD_DIR]]
    else:
        return f"{tree}: neither meson.build nor CMakeLists.txt; no build system to run"
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
