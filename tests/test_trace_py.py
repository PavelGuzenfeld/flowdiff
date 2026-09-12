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


def test_tree_and_scratch_paths_in_values_are_normalised(tmp_path: Path, lib, backend, monkeypatch):
    monkeypatch.setenv("FLOWDIFF_SCRATCH", str(tmp_path / ".flowdiff"))
    recs = run(tmp_path, lib, ["lib.py:g"], lambda m: m.g(str(tmp_path / ".flowdiff" / "run" / "x")))
    assert recs[0]["args"] == {"x": "<scratch>/run/x"}
    recs = run(tmp_path, lib, ["lib.py:g"], lambda m: m.g(str(tmp_path / "data")))
    assert recs[0]["args"] == {"x": "<tree>/data"}


def test_context_is_recorded_on_every_event_while_set(tmp_path: Path, lib, backend):
    out = tmp_path / "trace.jsonl"
    with trace_py.trace(str(tmp_path), ["lib.py:g"], str(out)) as tracer:
        lib.g(1)
        tracer.context = "tests/t.py::test_a"
        lib.g(2)
        tracer.context = None
        lib.g(3)
    recs = records(out)
    assert [r.get("test") for r in recs] == [None, None, "tests/t.py::test_a", "tests/t.py::test_a", None, None]


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


MUTATORS = """\
def fill(items, n):
    items.append(n)
    return len(items)


def rebind(items):
    items = [99]
    return items[0]


def both(left, right):
    left["k"] = 1
    return right


def untouched(items):
    return len(items)


def blows_up(items):
    items.append(1)
    raise ValueError("late")
"""


@pytest.fixture
def mut(tmp_path: Path):
    (tmp_path / "lib.py").write_text(MUTATORS)
    spec = importlib.util.spec_from_file_location("mut_lib", tmp_path / "lib.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_a_mutated_argument_is_recorded_again_at_exit(tmp_path: Path, mut, backend):
    recs = run(tmp_path, mut, ["lib.py:fill"], lambda m: m.fill([1, 2], 3))
    assert recs[0]["args"] == {"items": [1, 2], "n": 3}
    assert recs[1]["after"] == {"items": [1, 2, 3]} and recs[1]["return"] == 3


def test_a_rebound_parameter_is_not_reported_as_mutated(tmp_path: Path, mut, backend):
    recs = run(tmp_path, mut, ["lib.py:rebind"], lambda m: m.rebind([1, 2]))
    assert "after" not in recs[1]


def test_an_argument_aliased_by_another_is_not_watched(tmp_path: Path, mut, backend):
    shared = {"k": 0}
    recs = run(tmp_path, mut, ["lib.py:both"], lambda m: m.both(shared, shared))
    assert "after" not in recs[1]
    recs = run(tmp_path, mut, ["lib.py:both"], lambda m: m.both({"k": 0}, {"other": 0}))
    assert recs[1]["after"] == {"left": {"k": 1}, "right": {"other": 0}}


def test_an_immutable_argument_is_never_summarised_twice(tmp_path: Path, lib, backend):
    recs = run(tmp_path, lib, ["lib.py:g"], lambda m: m.g(3))
    assert "after" not in recs[1]


def test_a_frame_that_raises_still_records_what_it_wrote(tmp_path: Path, mut, backend):
    def action(m):
        with pytest.raises(ValueError):
            m.blows_up([])
    recs = run(tmp_path, mut, ["lib.py:blows_up"], action)
    assert recs[1]["raises"] == "ValueError" and recs[1]["after"] == {"items": [1]}


def test_the_watch_table_does_not_outlive_the_run(tmp_path: Path, mut, backend):
    out = tmp_path / "trace.jsonl"
    with trace_py.trace(str(tmp_path), ["lib.py:fill"], str(out)) as tracer:
        mut.fill([1], 2)
    assert tracer.watched == {}
