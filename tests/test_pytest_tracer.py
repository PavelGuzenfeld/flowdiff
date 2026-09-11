"""The plugin's hooks in this process, and the plugin inside a real pytest process launched the way play launches it."""
import importlib
import subprocess
import sys
from pathlib import Path

import pytest

from flowdiff import compare, env, pytest_tracer


@pytest.fixture
def released():
    yield
    if sys.monitoring.get_tool(sys.monitoring.PROFILER_ID) is not None:
        sys.monitoring.set_events(sys.monitoring.PROFILER_ID, 0)
        sys.monitoring.free_tool_id(sys.monitoring.PROFILER_ID)
    pytest_tracer._tracer = None


def test_session_hooks_start_then_stop_the_tracer_and_release_the_tool(tmp_path: Path, monkeypatch, released):
    out = tmp_path / "trace.jsonl"
    monkeypatch.setenv("FLOWDIFF_TREE", str(tmp_path))
    monkeypatch.setenv("FLOWDIFF_FRAMES", '["lib.py:f"]')
    monkeypatch.setenv("FLOWDIFF_OUT", str(out))
    pytest_tracer.pytest_sessionstart(None)
    started = pytest_tracer._tracer
    assert started is not None and started.wanted == {"lib.py:f"} and not started.out.closed
    assert sys.monitoring.get_tool(sys.monitoring.PROFILER_ID) == "flowdiff"
    pytest_tracer.pytest_sessionfinish(None, 0)
    assert started.out.closed and pytest_tracer._tracer is None
    assert sys.monitoring.get_tool(sys.monitoring.PROFILER_ID) is None


def test_logstart_and_logfinish_set_and_clear_the_context(tmp_path: Path, monkeypatch, released):
    monkeypatch.setenv("FLOWDIFF_TREE", str(tmp_path))
    monkeypatch.setenv("FLOWDIFF_FRAMES", "[]")
    monkeypatch.setenv("FLOWDIFF_OUT", str(tmp_path / "t.jsonl"))
    pytest_tracer.pytest_runtest_logstart("t.py::early", None)
    pytest_tracer.pytest_sessionstart(None)
    assert pytest_tracer._tracer is not None and pytest_tracer._tracer.context is None
    pytest_tracer.pytest_runtest_logstart("t.py::a", None)
    assert pytest_tracer._tracer.context == "t.py::a"
    pytest_tracer.pytest_runtest_logfinish("t.py::a", None)
    assert pytest_tracer._tracer.context is None
    pytest_tracer.pytest_sessionfinish(None, 0)


def test_a_fresh_plugin_has_no_tracer_and_finishing_without_a_start_does_nothing(released):
    fresh = importlib.reload(pytest_tracer)
    assert fresh._tracer is None
    fresh.pytest_sessionfinish(None, 0)
    assert fresh._tracer is None


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
    assert [c.test for c in calls] == ["tests/test_lib.py::test_entry", "tests/test_lib.py::test_scale_fails"]
    assert "lib.py:entry" not in compare.load(out)


def test_plugin_writes_an_empty_trace_when_no_test_reaches_a_frame(tmp_path: Path):
    tree = project(tmp_path)
    (tree / "tests" / "test_other.py").write_text("def test_nothing():\n    assert True\n")
    out = tmp_path / "trace.jsonl"
    proc = run_pytest(tree, out, "tests/test_other.py")
    assert proc.returncode == 0 and out.exists() and compare.load(out) == {}
