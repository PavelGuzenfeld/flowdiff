"""The plugin inside a real pytest process, launched the way play launches it."""
import subprocess
import sys
from pathlib import Path

from flowdiff import compare, env


def project(tmp_path: Path) -> Path:
    (tmp_path / "lib.py").write_text("def scale(v):\n    return v * 2\n\n\ndef entry(v):\n    return scale(v)\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_lib.py").write_text(
        "from lib import entry, scale\n\n\ndef test_entry():\n    assert entry(3) == 6\n\n\n"
        "def test_scale_fails():\n    assert scale(1) == 0\n")
    return tmp_path


def run_pytest(tree: Path, out: Path, *ids: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
                           "-p", "flowdiff.pytest_tracer", *ids], cwd=tree, capture_output=True, text=True,
                          env=env.harness_env(tree, "head", out, ["lib.py:scale"]))


def test_plugin_traces_the_named_frames_across_every_collected_test(tmp_path: Path):
    tree = project(tmp_path)
    out = tmp_path / "trace.jsonl"
    proc = run_pytest(tree, out, "tests/test_lib.py::test_entry", "tests/test_lib.py::test_scale_fails")
    assert proc.returncode == 1, proc.stdout + proc.stderr
    calls = compare.load(out)["lib.py:scale"]
    assert [(c.args, c.result) for c in calls] == [({"v": 3}, 6), ({"v": 1}, 2)]
    assert "lib.py:entry" not in compare.load(out)


def test_plugin_writes_an_empty_trace_when_no_test_reaches_a_frame(tmp_path: Path):
    tree = project(tmp_path)
    (tree / "tests" / "test_other.py").write_text("def test_nothing():\n    assert True\n")
    out = tmp_path / "trace.jsonl"
    proc = run_pytest(tree, out, "tests/test_other.py")
    assert proc.returncode == 0 and out.exists() and compare.load(out) == {}
