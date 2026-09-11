import json
import os
import subprocess
from pathlib import Path

import pytest

from flowdiff import container


class FakeDocker:
    """Answers docker subcommands from a script; records every invocation."""

    def __init__(self, images: dict[str, dict], build_ok: bool = True):
        self.images = images
        self.calls: list[list[str]] = []
        self.build_ok = build_ok

    def __call__(self, cmd, **kwargs):
        self.calls.append(cmd)
        args = cmd[1:]
        if args[:2] == ["image", "inspect"]:
            fmt = args[args.index("--format") + 1] if "--format" in args else None
            tag = args[-1]
            if tag not in self.images:
                return subprocess.CompletedProcess(cmd, 1, "", f"No such image: {tag}")
            image = self.images[tag]
            if fmt == "{{.Id}}":
                out = image["id"]
            elif fmt == "{{.Config.WorkingDir}}":
                out = image.get("workdir", "")
            elif fmt and "Labels" in fmt:
                out = image.get("labels", {}).get("flowdiff.base", "")
            else:
                out = "ok"
            return subprocess.CompletedProcess(cmd, 0, out + "\n", "")
        if args[0] == "build":
            if not self.build_ok:
                return subprocess.CompletedProcess(cmd, 1, "", "build failed")
            tag = args[args.index("-t") + 1]
            dockerfile = (Path(args[-1]) / "Dockerfile").read_text()
            digest = dockerfile.rsplit("=", 1)[1].strip()
            self.images[tag] = {"id": "sha256:layer", "labels": {"flowdiff.base": digest}}
            return subprocess.CompletedProcess(cmd, 0, "sha256:layer\n", "")
        if args[0] == "run":
            return subprocess.CompletedProcess(cmd, 0, "", "")
        raise AssertionError(f"unexpected docker call {cmd}")


@pytest.fixture
def fake(monkeypatch):
    fake = FakeDocker({"proj:dev": {"id": "sha256:base1", "workdir": "/src"}})
    monkeypatch.setattr(container.subprocess, "run", fake)
    return fake


def test_project_image_prefers_explicit_then_repo_name_convention(fake, tmp_path: Path):
    root = tmp_path / "proj"
    assert container.project_image(root, "custom:tag") == "custom:tag"
    assert container.project_image(root, None) == "proj:dev"
    assert container.project_image(tmp_path / "other", None) is None


def test_layered_image_is_built_once_per_base_digest(fake, tmp_path: Path):
    root = tmp_path / "proj"
    assert container.layered_image(root, "proj:dev") == "flowdiff/proj:dev"
    builds = [c for c in fake.calls if c[1] == "build"]
    assert len(builds) == 1 and "flowdiff/proj:dev" in builds[0]
    assert container.layered_image(root, "proj:dev") == "flowdiff/proj:dev"
    assert len([c for c in fake.calls if c[1] == "build"]) == 1
    fake.images["proj:dev"]["id"] = "sha256:base2"
    container.layered_image(root, "proj:dev")
    assert len([c for c in fake.calls if c[1] == "build"]) == 2
    assert fake.images["flowdiff/proj:dev"]["labels"]["flowdiff.base"] == "sha256:base2"


def test_layer_dockerfile_adds_the_debugger_and_language_server_and_labels_the_base():
    text = container.LAYER_DOCKERFILE.format(base="proj:dev", label=container.LAYER_LABEL, digest="sha256:x")
    assert text.startswith("FROM proj:dev\n") and "gdb" in text and "clangd" in text and "binutils" in text
    assert text.rstrip().endswith("LABEL flowdiff.base=sha256:x")


def test_docker_failures_raise_with_the_subcommand(fake):
    fake.build_ok = False
    with pytest.raises(container.ContainerError, match="docker build: build failed"):
        container.docker("build", "-q", "-t", "x", "/nowhere")
    assert container.docker("image", "inspect", "missing:tag", check=False).returncode == 1


def test_container_command_mounts_the_tree_at_the_workdir_as_the_calling_user(tmp_path: Path):
    c = container.Container("flowdiff/proj:dev", "/src")
    cmd = c.command(tmp_path, ["ninja", "-C", "builddir"])
    assert cmd == ["docker", "run", "--rm", "--user", f"{os.getuid()}:{os.getgid()}", "-e", "HOME=/tmp",
                   "-v", f"{tmp_path.resolve()}:/src", "-w", "/src", "flowdiff/proj:dev", "ninja", "-C", "builddir"]
    assert c.command(tmp_path, ["clangd"], interactive=True)[2:4] == ["--rm", "-i"]
    assert c.path(tmp_path, tmp_path / "gst" / "a.cpp") == "/src/gst/a.cpp"
    clangd = c.clangd_command(tmp_path, tmp_path / ".flowdiff")
    assert (tmp_path / ".flowdiff" / "clangd-cache").is_dir()
    assert clangd[-1] == "flowdiff/proj:dev" and "-i" in clangd
    assert f"{(tmp_path / '.flowdiff' / 'clangd-cache').resolve()}:/src/.cache" in clangd


def test_needs_container_when_the_database_is_absent_or_points_off_host(tmp_path: Path):
    assert container.needs_container(tmp_path)
    build = tmp_path / "builddir"
    build.mkdir()
    db = build / "compile_commands.json"
    db.write_text(json.dumps([{"directory": "/src/builddir", "command": "c++", "file": "a.cpp"}]))
    assert container.needs_container(tmp_path)
    db.write_text(json.dumps([{"directory": str(build), "command": "c++", "file": "a.cpp"}]))
    assert not container.needs_container(tmp_path)
    db.write_text("[]")
    assert container.needs_container(tmp_path)
    db.write_text("not json")
    assert container.needs_container(tmp_path)


def test_detect_returns_none_on_a_usable_host_database(tmp_path: Path, fake):
    build = tmp_path / "builddir"
    build.mkdir()
    (build / "compile_commands.json").write_text(json.dumps([{"directory": str(build), "command": "c++", "file": "a.cpp"}]))
    assert container.detect(tmp_path, None) is None


def test_detect_builds_the_layer_and_reads_the_workdir(tmp_path: Path, fake, monkeypatch):
    root = tmp_path / "proj"
    root.mkdir()
    monkeypatch.setattr(container.shutil, "which", lambda name: "/usr/bin/docker")
    c = container.detect(root, None)
    assert (c.image, c.workdir, c.compile_commands_dir) == ("flowdiff/proj:dev", "/src", "/src/builddir")
    fake.images["bare:dev"] = {"id": "sha256:b"}
    assert container.detect(root, "bare:dev").workdir == "/src"
    (root / "build" / "pkg").mkdir(parents=True)
    (root / "build" / "pkg" / "compile_commands.json").write_text(json.dumps([{"directory": "/ws/build/pkg", "file": "/ws/pkg/a.cpp", "command": "c++"}]))
    c = container.detect(root, None)
    assert (c.workdir, c.compile_commands_dir) == ("/ws", "/ws/build/pkg")
    with pytest.raises(container.ContainerError, match="no dev image for other"):
        container.detect(tmp_path / "other", None)
    monkeypatch.setattr(container.shutil, "which", lambda name: None)
    with pytest.raises(container.ContainerError, match="docker not found"):
        container.detect(root, None)


def test_build_configures_once_then_runs_the_incremental_build(tmp_path: Path, fake):
    c = container.Container("img", "/src")
    (tmp_path / "meson.build").write_text("")
    assert container.build(c, tmp_path, 10) is None
    runs = [cmd[cmd.index("img") + 1:] for cmd in fake.calls if cmd[1] == "run"]
    assert runs == [["meson", "setup", "builddir", "-Dbuildtype=debug"], ["ninja", "-C", "builddir"]]
    (tmp_path / "builddir").mkdir()
    (tmp_path / "builddir" / "build.ninja").write_text("")
    fake.calls.clear()
    assert container.build(c, tmp_path, 10) is None
    assert [cmd[cmd.index("img") + 1:] for cmd in fake.calls] == [["ninja", "-C", "builddir"]]


def test_build_falls_back_to_cmake_and_reports_missing_build_systems(tmp_path: Path, fake):
    c = container.Container("img", "/src")
    assert container.build(c, tmp_path, 10) == f"{tmp_path}: no meson.build, CMakeLists.txt or package.xml; no build system to run"
    (tmp_path / "CMakeLists.txt").write_text("")
    assert container.build(c, tmp_path, 10) is None
    runs = [cmd[cmd.index("img") + 1:] for cmd in fake.calls if cmd[1] == "run"]
    assert runs[0][:4] == ["cmake", "-S", ".", "-B"] and "-DCMAKE_EXPORT_COMPILE_COMMANDS=ON" in runs[0]
    assert runs[1] == ["cmake", "--build", "builddir"]


def test_build_runs_colcon_for_a_workspace_of_packages(tmp_path: Path, fake):
    c = container.Container("img", "/ws")
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "package.xml").write_text("<package/>")
    assert container.build_system(tmp_path) == "colcon"
    assert container.build(c, tmp_path, 10) is None
    runs = [cmd[cmd.index("img") + 1:] for cmd in fake.calls if cmd[1] == "run"]
    assert runs == [["colcon", "build", "--symlink-install", "--cmake-args", "-DCMAKE_EXPORT_COMPILE_COMMANDS=ON"]]
    fake.calls.clear()
    scoped = container.Container("img", "/ws", packages=("logger", "nav_control"))
    assert container.build(scoped, tmp_path, 10) is None
    runs = [cmd[cmd.index("img") + 1:] for cmd in fake.calls if cmd[1] == "run"]
    assert runs == [["colcon", "build", "--symlink-install", "--packages-up-to", "logger", "nav_control",
                     "--cmake-args", "-DCMAKE_EXPORT_COMPILE_COMMANDS=ON"]]
    (tmp_path / "CMakeLists.txt").write_text("")
    assert container.build_system(tmp_path) == "cmake"
    (tmp_path / "meson.build").write_text("")
    assert container.build_system(tmp_path) == "meson"
    nested = tmp_path / "ws"
    (nested / "src" / "p").mkdir(parents=True)
    (nested / "src" / "p" / "package.xml").write_text("")
    assert container.build_system(nested) == "colcon" and container.build_system(tmp_path / "empty") is None


def test_packages_are_named_by_their_manifest_not_their_directory(tmp_path: Path):
    tree = tmp_path / "ws"
    (tree / "mc_dir" / "src").mkdir(parents=True)
    (tree / "mc_dir" / "package.xml").write_text('<?xml version="1.0"?><package format="3"><name> nav_control </name></package>')
    (tree / "outer" / "inner").mkdir(parents=True)
    (tree / "outer" / "package.xml").write_text("<package><name>outer_pkg</name></package>")
    (tree / "outer" / "inner" / "package.xml").write_text("<package><name>inner_pkg</name></package>")
    (tree / "src" / "logger" / "include").mkdir(parents=True)
    (tree / "src" / "logger" / "package.xml").write_text("<package><name>logger</name></package>")
    (tree / "nameless").mkdir()
    (tree / "nameless" / "package.xml").write_text("<package/>")
    (tree / "package.xml").write_text("<package><name>root_is_not_a_package</name></package>")
    (tmp_path / "package.xml").write_text("<package><name>above_the_tree</name></package>")
    files = [Path("mc_dir/src/a.cpp"), Path("mc_dir/src/b.cpp"), Path("outer/inner/n.cpp"), Path("src/logger/include/l.hpp"),
             Path("nameless/x.cpp"), Path("scripts/tool.cpp")]
    assert container.packages_of(tree, files) == ("inner_pkg", "logger", "nav_control")
    assert container.packages_of(tree, []) == ()


def test_compile_databases_are_merged_per_package_first(tmp_path: Path):
    assert container.compile_database(tmp_path) is None
    top = tmp_path / "build" / "compile_commands.json"
    pkg = tmp_path / "build" / "pkg" / "compile_commands.json"
    pkg.parent.mkdir(parents=True)
    pkg.write_text(json.dumps([{"directory": "/ws/build/pkg", "file": "/ws/pkg/a.cpp", "command": "fresh"}]))
    assert container.compile_database(tmp_path) == pkg
    top.write_text(json.dumps([{"directory": "/ws/build/pkg", "file": "/ws/pkg/a.cpp", "command": "stale"},
                               {"directory": "/ws/build/other", "file": "/ws/other/b.cpp", "command": "b"}]))
    merged = container.compile_database(tmp_path)
    assert merged == tmp_path / ".flowdiff" / "compile_commands.json"
    entries = json.loads(merged.read_text())
    assert [(e["file"], e["command"]) for e in entries] == [("/ws/pkg/a.cpp", "fresh"), ("/ws/other/b.cpp", "b")]
    assert (tmp_path / ".flowdiff" / ".gitignore").read_text() == "*\n"
    assert merged.read_text() == json.dumps(entries, indent=1)
    stamp = merged.stat().st_mtime_ns
    container.compile_database(tmp_path)
    assert merged.stat().st_mtime_ns == stamp
    top.write_text("broken")
    (tmp_path / "compile_commands.json").write_text(json.dumps([{"directory": "/ws", "file": "/ws/c.cpp", "command": "c"}]))
    entries = json.loads(container.compile_database(tmp_path).read_text())
    assert [e["file"] for e in entries] == ["/ws/pkg/a.cpp", "/ws/c.cpp"]
    assert container.entries_of(None) == [] and container.entries_of(top) == []


def test_mount_point_comes_from_the_databases_directory(tmp_path: Path):
    pkg = tmp_path / "build" / "pkg" / "compile_commands.json"
    pkg.parent.mkdir(parents=True)
    entries = [{"directory": "/ws/build/pkg", "file": "/ws/pkg/a.cpp"}]
    assert container.mount_point(tmp_path, pkg, entries) == "/ws"
    assert container.db_dir_of(tmp_path, pkg, "/ws") == "/ws/build/pkg"
    root_db = tmp_path / "compile_commands.json"
    assert container.mount_point(tmp_path, root_db, [{"directory": "/src"}]) == "/src"
    assert container.mount_point(tmp_path, root_db, [{"directory": str(tmp_path)}]) is None
    assert container.mount_point(tmp_path, root_db, [{"directory": str(tmp_path)}, {"directory": "/src"}]) == "/src"
    assert container.db_dir_of(tmp_path, root_db, "/src") == "/src"
    pkg.write_text(json.dumps(entries))
    merged = tmp_path / ".flowdiff" / "compile_commands.json"
    merged.parent.mkdir()
    assert container.mount_point(tmp_path, merged, [{"directory": "/nowhere/x"}]) == "/ws"
    assert container.mount_point(tmp_path, merged, []) == "/ws"
    c = container.Container("img", "/ws", "/ws/.flowdiff")
    assert c.compile_commands_dir == "/ws/.flowdiff"
    assert c.host_path(tmp_path, "/ws/build/pkg") == tmp_path / "build" / "pkg"
    assert c.host_path(tmp_path, "/ws") == tmp_path and c.host_path(tmp_path, "/usr/include") == Path("/usr/include")


def test_needs_container_reads_the_per_package_database(tmp_path: Path):
    pkg = tmp_path / "build" / "pkg" / "compile_commands.json"
    pkg.parent.mkdir(parents=True)
    pkg.write_text(json.dumps([{"directory": "/ws/build/pkg", "file": "a.cpp"}]))
    assert container.needs_container(tmp_path)
    pkg.write_text(json.dumps([{"directory": str(pkg.parent), "file": "a.cpp"}]))
    assert not container.needs_container(tmp_path)


def test_build_reports_a_failing_step_and_a_timeout(tmp_path: Path, monkeypatch):
    c = container.Container("img", "/src")
    (tmp_path / "meson.build").write_text("")

    def failing(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 1, "out", "ninja: error: bad")
    monkeypatch.setattr(container.subprocess, "run", failing)
    assert container.build(c, tmp_path, 10) == "meson setup builddir -Dbuildtype=debug failed:\nout\nninja: error: bad"

    def hanging(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, kw.get("timeout", 0))
    monkeypatch.setattr(container.subprocess, "run", hanging)
    assert container.build(c, tmp_path, 7) == "meson setup builddir -Dbuildtype=debug: timed out after 7s"


def test_needed_libraries_and_the_device_deny_list(tmp_path: Path, monkeypatch):
    readelf = ("Dynamic section at offset 0x1 contains 3 entries:\n"
               " 0x0000000000000001 (NEEDED)             Shared library: [libnvbufsurface.so.1.0.0]\n"
               " 0x0000000000000001 (NEEDED)             Shared library: [libgstreamer-1.0.so.0]\n"
               " 0x000000000000000e (SONAME)             Library soname: [libx.so]\n")
    monkeypatch.setattr(container.subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, readelf, ""))
    libs = container.needed_libraries(container.Container("img", "/src"), tmp_path, "builddir/libx.so")
    assert libs == ["libnvbufsurface.so.1.0.0", "libgstreamer-1.0.so.0"]
    assert container.device_libraries(libs) == ["libnvbufsurface.so.1.0.0"]
    assert container.device_libraries(["libgstreamer-1.0.so.0", "libstdc++.so.6"]) == []
    assert container.DEVICE_LIBS == ("libnvbufsurface", "libcuda", "libcudart", "libnvinfer")
