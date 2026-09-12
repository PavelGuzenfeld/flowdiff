import importlib
import shutil
import subprocess
from pathlib import Path

import pytest


def trace_gdb():
    """Imported inside each test: a module that fails to import must fail the test, not the collection."""
    return importlib.import_module("flowdiff.trace_gdb")


def test_gdb_script_embeds_frames_tests_and_paths_and_is_valid_python():
    text = trace_gdb().script("/src", {"gst/t.cpp:f": "f"}, {"test_f": "tests/t.cpp::test_f"}, "/src/.flowdiff/run/x.jsonl")
    assert "TREE = '/src'" in text and "'gst/t.cpp:f': 'f'" in text and "'test_f': 'tests/t.cpp::test_f'" in text
    assert "OUT = open('/src/.flowdiff/run/x.jsonl'" in text and "set breakpoint pending on" in text
    assert 'key.split(":", 1)[0]' in text and "FinishBreakpoint" in text and "INLINE_LIMIT = 64" in text
    compile(text, "trace.py", "exec")


def test_gdb_command_runs_the_script_in_batch_mode_without_the_users_init():
    assert trace_gdb().gdb_command("/s.py", "/src/builddir/t", ["--x"]) == \
        ["gdb", "-batch", "-q", "-nx", "-x", "/s.py", "--args", "/src/builddir/t", "--x"]
    assert trace_gdb().gdb_command("/s.py", "/t")[-2:] == ["--args", "/t"]


def test_read_records_keeps_only_json_object_lines():
    assert trace_gdb().read_records('noise\n{"seq": 1}\n[x]\n{"seq": 2}\n') == [{"seq": 1}, {"seq": 2}]
    assert trace_gdb().read_records("") == []


PROGRAM = """\
struct Box { int width; int height; };

int grow(int& v, int by) { v += by; return v; }

void resize(Box* b, int w) { b->width = w; }

int peek(int v) { return v; }

int main() {
    int n = 4;
    grow(n, 3);
    Box b{1, 2};
    resize(&b, 9);
    peek(7);
    return 0;
}
"""

gdb_and_gpp = pytest.mark.skipif(shutil.which("gdb") is None or shutil.which("g++") is None,
                                 reason="the gdb tracer needs gdb and g++")


def traced(tmp_path: Path, frames: dict[str, str]) -> list[dict]:
    source = tmp_path / "t.cpp"
    source.write_text(PROGRAM)
    exe = tmp_path / "t"
    subprocess.run(["g++", "-g", "-O0", str(source), "-o", str(exe)], check=True)
    out = tmp_path / "trace.jsonl"
    script = tmp_path / "trace_script.py"
    script.write_text(trace_gdb().script(str(tmp_path), frames, {}, str(out)))
    subprocess.run(trace_gdb().gdb_command(str(script), str(exe)), capture_output=True, text=True, timeout=120)
    return trace_gdb().read_records(out.read_text()) if out.exists() else []


@gdb_and_gpp
def test_a_reference_argument_written_by_the_callee_is_recorded_again_at_exit(tmp_path: Path):
    recs = traced(tmp_path, {"t.cpp:grow": "grow"})
    assert [r["event"] for r in recs] == ["enter", "exit"]
    assert recs[0]["args"] == {"v": 4, "by": 3}
    assert recs[1]["after"] == {"v": 7} and recs[1]["return"] == 7


@gdb_and_gpp
def test_a_pointer_argument_is_recorded_through_the_pointer(tmp_path: Path):
    recs = traced(tmp_path, {"t.cpp:resize": "resize"})
    assert recs[0]["args"]["b"]["fields"] == {"width": 1, "height": 2}
    assert recs[1]["after"]["b"]["fields"] == {"width": 9, "height": 2}


@gdb_and_gpp
def test_a_by_value_argument_is_not_watched(tmp_path: Path):
    recs = traced(tmp_path, {"t.cpp:peek": "peek"})
    assert recs[0]["args"] == {"v": 7} and "after" not in recs[1]
