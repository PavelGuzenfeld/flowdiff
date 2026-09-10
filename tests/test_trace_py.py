import importlib.util
import json
import sys
from pathlib import Path

import pytest

from flowdiff import trace_py

LIB = """\
def g(x):
    return x * 2


def f(x, *rest, **opts):
    return g(x) + 1


def h(x):
    raise ValueError(x)


def untraced():
    return f(1)


def boom():
    raise KeyError("outside the flow")


def catcher():
    try:
        boom()
    except KeyError:
        return g(5)
"""


@pytest.fixture
def lib(tmp_path: Path):
    (tmp_path / "lib.py").write_text(LIB)
    spec = importlib.util.spec_from_file_location("trace_lib", tmp_path / "lib.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def records(path: Path) -> list[dict]:
    return [json.loads(l) for l in path.read_text().splitlines()]


def run(tmp_path: Path, lib, frames, action):
    out = tmp_path / "trace.jsonl"
    with trace_py.trace(str(tmp_path), frames, str(out)) as tracer:
        action(lib)
    assert tracer.out.closed
    return records(out)


@pytest.fixture(params=["monitoring", "settrace"])
def backend(request, monkeypatch):
    if request.param == "settrace":
        monkeypatch.delattr(sys, "monitoring")
    return request.param


def test_enter_and_exit_records_nest_in_call_order(tmp_path: Path, lib, backend):
    recs = run(tmp_path, lib, ["lib.py:f", "lib.py:g"], lambda m: m.f(3))
    assert [(r["seq"], r["event"], r["frame"]) for r in recs] == [
        (1, "enter", "lib.py:f"), (2, "enter", "lib.py:g"), (3, "exit", "lib.py:g"), (4, "exit", "lib.py:f")]
    assert recs[0]["args"] == {"x": 3, "*rest": [], "**opts": {}}
    assert recs[1]["args"] == {"x": 3}
    assert recs[2]["return"] == 6 and recs[3]["return"] == 7


def test_only_wanted_frames_are_recorded(tmp_path: Path, lib, backend):
    recs = run(tmp_path, lib, ["lib.py:g"], lambda m: m.untraced())
    assert [r["frame"] for r in recs] == ["lib.py:g", "lib.py:g"]


def test_an_exception_leaves_a_raises_record(tmp_path: Path, lib, backend):
    def action(m):
        with pytest.raises(ValueError):
            m.h("bad")
    recs = run(tmp_path, lib, ["lib.py:h"], action)
    assert recs == [{"seq": 1, "event": "enter", "frame": "lib.py:h", "args": {"x": "bad"}},
                    {"seq": 2, "event": "exit", "frame": "lib.py:h", "raises": "ValueError"}]


def test_an_exception_outside_the_flow_neither_records_nor_breaks_the_trace(tmp_path: Path, lib, backend):
    recs = run(tmp_path, lib, ["lib.py:g"], lambda m: m.catcher())
    assert [(r["event"], r["frame"]) for r in recs] == [("enter", "lib.py:g"), ("exit", "lib.py:g")]
    assert recs[1]["return"] == 10


def test_varargs_and_keywords_are_named_with_their_stars(tmp_path: Path, lib, backend):
    recs = run(tmp_path, lib, ["lib.py:f"], lambda m: m.f(1, 2, 3, k="v"))
    assert recs[0]["args"] == {"x": 1, "*rest": [2, 3], "**opts": {"k": "v"}}


def test_settrace_fallback_restores_the_previous_trace_function(tmp_path: Path, lib, monkeypatch):
    monkeypatch.delattr(sys, "monitoring")
    previous = lambda frame, event, arg: None
    sys.settrace(previous)
    try:
        run(tmp_path, lib, ["lib.py:g"], lambda m: m.g(1))
        assert sys.gettrace() is previous
    finally:
        sys.settrace(None)


def test_monitoring_tool_is_released_after_the_run(tmp_path: Path, lib):
    run(tmp_path, lib, ["lib.py:g"], lambda m: m.g(1))
    assert sys.monitoring.get_tool(sys.monitoring.PROFILER_ID) is None


def test_frame_key_is_relative_to_the_tree_or_none(tmp_path: Path):
    tree = str(tmp_path.resolve())
    assert trace_py.frame_key(tree, str(tmp_path / "pkg" / "m.py"), "f") == "pkg/m.py:f"
    assert trace_py.frame_key(tree, "/elsewhere/m.py", "f") is None
    assert trace_py.frame_key(tree, str(tmp_path) + "x/m.py", "f") is None


def test_unserialisable_values_fall_back_to_repr(tmp_path: Path, lib, backend):
    (tmp_path / "obj.py").write_text("class Weird:\n    __slots__ = ()\n\n\ndef make():\n    return Weird()\n")
    spec = importlib.util.spec_from_file_location("trace_obj", tmp_path / "obj.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    recs = run(tmp_path, module, ["obj.py:make"], lambda m: m.make())
    assert recs[1]["return"] == {"type": "trace_obj.Weird"}
