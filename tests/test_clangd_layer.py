"""The layer's clangd answers callHierarchy/outgoingCalls, against a real image and a real project.

Opt-in: it builds an image and reaches apt.llvm.org, so CI does not run it.
Enable with FLOWDIFF_DOCKER_TESTS=1; FLOWDIFF_LAYER_BASE overrides the base image.
"""
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from flowdiff import container, lsp

PROJECT = """\
int helper(int v) { return v * 2; }

int caller(int v) { return helper(v) + 1; }
"""

WORKDIR = "/src"
BASE = os.environ.get("FLOWDIFF_LAYER_BASE", "ubuntu:24.04")

needs_docker = pytest.mark.skipif(
    os.environ.get("FLOWDIFF_DOCKER_TESTS") != "1" or shutil.which("docker") is None,
    reason="set FLOWDIFF_DOCKER_TESTS=1 to build the layer and reach apt.llvm.org")


def layer_image() -> str:
    tag = "flowdiff/layer-test:dev"
    with tempfile.TemporaryDirectory(prefix="flowdiff-layer-test-") as tmp:
        (Path(tmp) / "Dockerfile").write_text(container.layer_recipe(BASE, "sha256:test"))
        subprocess.run(["docker", "build", "-q", "-t", tag, tmp], check=True, capture_output=True, text=True)
    return tag


def project(tree: Path) -> Path:
    src = tree / "a.cpp"
    src.write_text(PROJECT)
    (tree / "compile_commands.json").write_text(json.dumps([
        {"directory": WORKDIR, "file": f"{WORKDIR}/a.cpp", "command": "c++ -std=c++17 -c a.cpp"}]))
    return src


def installed_clangd_version(image: str) -> str:
    return subprocess.run(["docker", "run", "--rm", image, "dpkg-query", "-W", "-f=${Version}",
                           f"clangd-{container.CLANGD_VERSION}"], check=True, capture_output=True, text=True).stdout.strip()


@needs_docker
def test_the_layer_installs_the_exact_pinned_clangd_version():
    """A wrong or GC'd CLANGD_PIN falls through to an unpinned same-major install (still passing the
    other layer test), so only checking dpkg's own record of what actually landed catches a stale pin."""
    assert installed_clangd_version(layer_image()) == container.CLANGD_PIN


@needs_docker
def test_a_pin_apt_llvm_org_does_not_have_still_installs_clangd(monkeypatch):
    """Proves the "||" actually falls through at build time, not just that the fallback text appears
    somewhere in the Dockerfile: a pin apt.llvm.org has never served must not abort the whole install."""
    monkeypatch.setattr(container, "CLANGD_PIN", "1:0.0.0~nonexistent-1~exp1")
    tag = "flowdiff/layer-test-badpin:dev"
    with tempfile.TemporaryDirectory(prefix="flowdiff-layer-test-badpin-") as tmp:
        (Path(tmp) / "Dockerfile").write_text(container.layer_recipe(BASE, "sha256:test-badpin"))
        subprocess.run(["docker", "build", "-q", "-t", tag, tmp], check=True, capture_output=True, text=True)
    installed = installed_clangd_version(tag)
    assert installed and installed != "1:0.0.0~nonexistent-1~exp1"


@needs_docker
def test_the_layers_clangd_answers_outgoing_calls_with_the_callee(tmp_path: Path):
    image = layer_image()
    src = project(tmp_path)
    config = lsp.ServerConfig("cpp", "clangd", ("--background-index", f"--compile-commands-dir={WORKDIR}"),
                              lsp.CPP_EXTENSIONS, "cpp",
                              command_prefix=("docker", "run", "--rm", "-i", "-v", f"{tmp_path}:{WORKDIR}",
                                              "-w", WORKDIR, image),
                              uri_map=(lsp.uri_of(tmp_path), "file://" + WORKDIR))
    client = lsp.LspClient(config, tmp_path, timeout=120.0)
    try:
        client.open(src, PROJECT)
        client.wait_for_index(120.0)
        items = client.prepare_call_hierarchy(src, lsp.Position(2, 4))
        assert len(items) == 1 and items[0]["name"] == "caller"
        calls = client.outgoing_calls(items[0])
        assert client.unsupported == set(), "the layer's clangd has no outgoingCalls"
        assert [c["to"]["name"] for c in calls] == ["helper"], "outgoingCalls answered, but empty"
    finally:
        client.close()
