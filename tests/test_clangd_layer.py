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
