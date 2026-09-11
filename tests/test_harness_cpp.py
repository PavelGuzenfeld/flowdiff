import json
import subprocess
from pathlib import Path

import pytest

from flowdiff import container, graph, harness_cpp, trace_gdb

from fake_client import symbol


def test_frame_id_and_gdb_frames_use_tree_relative_paths_and_full_names(tmp_path: Path):
    node = graph.node_of(symbol("NvmmTransform::crop_and_scale", tmp_path / "gst" / "t.cpp", 5), "body")
    slot = graph.Node("slot:x", "->x", tmp_path / "gst" / "t.cpp", 0, 0, "slot")
    gone = graph.node_of(symbol("old", tmp_path / "gst" / "t.cpp", 9), "removed")
    assert harness_cpp.frame_id(tmp_path, node) == "gst/t.cpp:NvmmTransform::crop_and_scale"
    flow = graph.Flow([node], node, [node, slot, gone], [], False, [])
    assert harness_cpp.gdb_frames(tmp_path, flow) == {"gst/t.cpp:NvmmTransform::crop_and_scale": "NvmmTransform::crop_and_scale"}


def write_db(tree: Path, entries: list[dict]) -> None:
    (tree / "builddir").mkdir(parents=True, exist_ok=True)
    (tree / "builddir" / "compile_commands.json").write_text(json.dumps(entries))


def test_test_executables_follow_meson_outputs_and_require_the_binary(tmp_path: Path):
    build = tmp_path / "builddir"
    write_db(tmp_path, [
        {"directory": "/src/builddir", "file": "../tests/test_a.cpp", "output": "tests/test_a.p/test_a.cpp.o"},
        {"directory": "/src/builddir", "file": "../tests/test_b.cpp", "output": "tests/test_b.p/test_b.cpp.o"},
        {"directory": "/src/builddir", "file": "../gst/x.cpp", "output": "gst/libx.so.p/x.cpp.o"},
        {"directory": "/src/builddir", "file": "../tests/test_c.cpp"},
        {"directory": "/src/builddir", "file": "/src/tests/test_d.cpp", "output": "tests/test_d.p/test_d.cpp.o"},
        {"directory": "/src/builddir", "file": "/elsewhere/test_e.cpp", "output": "tests/test_e.p/test_e.cpp.o"}])
    (build / "tests").mkdir()
    for exe in ("test_a", "test_d", "test_e"):
        (build / "tests" / exe).write_text("")
    wanted = ["tests/test_a.cpp", "tests/test_b.cpp", "tests/test_c.cpp", "tests/test_d.cpp", "test_e.cpp"]
    assert harness_cpp.test_executables(tmp_path, wanted, "/src") == {
        "tests/test_a.cpp": "builddir/tests/test_a", "tests/test_d.cpp": "builddir/tests/test_d"}
    assert harness_cpp.test_executables(tmp_path, wanted) == {"tests/test_a.cpp": "builddir/tests/test_a"}
    assert harness_cpp.test_executables(tmp_path / "nowhere", ["tests/test_a.cpp"]) == {}
    (build / "compile_commands.json").write_text("nonsense")
    assert harness_cpp.compile_db(tmp_path) == []


@pytest.mark.parametrize("text,expected", [
    ("    auto r = NvmmTransform::crop_and_scale(4, -1.5e3, true, nullptr, \"s\", 'c');\n", ["4", " -1.5e3", " true", " nullptr", ' "s"', " 'c'"]),
    ("crop_and_scale(src, dst, crop);\n", None),
    ("crop_and_scale(f(1), 2);\n", None),
    ("crop_and_scale();\n", []),
    ("crop_and_scale(0x1F, 10u, 2.0f);\n", ["0x1F", " 10u", " 2.0f"]),
    ("other(1, 2);\n", None),
    ("crop_and_scale(1,\n", None),
])
def test_literal_call_lifts_only_literal_arguments(text, expected):
    assert harness_cpp.literal_call(text, 0, "NvmmTransform::crop_and_scale") == expected


def test_literal_call_off_the_end_of_the_text_is_none():
    assert harness_cpp.literal_call("x();\n", 5, "x") is None


def test_split_arguments_respects_nesting_and_quotes():
    assert harness_cpp.split_arguments('a, f(b, c), "x,y", {1, 2}') == ["a", " f(b, c)", ' "x,y"', " {1, 2}"]
    assert harness_cpp.split_arguments("") == [] and harness_cpp.split_arguments("a") == ["a"]
    assert harness_cpp.split_arguments("f(a") is None and harness_cpp.split_arguments("a)") is None


def test_test_symbols_map_the_function_and_the_test_macro_name():
    assert harness_cpp.test_symbols(["tests/t.cpp::test_crop", "tests/u.cpp::<module>", "tests/v.cpp::bare"]) == {
        "test_crop": "tests/t.cpp::test_crop", "test_test_crop": "tests/t.cpp::test_crop",
        "bare": "tests/v.cpp::bare", "test_bare": "tests/v.cpp::bare"}


def test_gdb_script_embeds_frames_tests_and_paths_and_is_valid_python():
    text = trace_gdb.script("/src", {"gst/t.cpp:f": "f"}, {"test_f": "tests/t.cpp::test_f"}, "/src/.flowdiff/run/x.jsonl")
    assert "TREE = '/src'" in text and "'gst/t.cpp:f': 'f'" in text and "'test_f': 'tests/t.cpp::test_f'" in text
    assert "OUT = open('/src/.flowdiff/run/x.jsonl'" in text and "set breakpoint pending on" in text
    assert 'key.split(":", 1)[0]' in text
    compile(text, "trace.py", "exec")
    assert trace_gdb.gdb_command("/s.py", "/src/builddir/t", ["--x"]) == ["gdb", "-batch", "-q", "-nx", "-x", "/s.py", "--args", "/src/builddir/t", "--x"]
    assert trace_gdb.read_records('noise\n{"seq": 1}\n[x]\n{"seq": 2}\n') == [{"seq": 1}, {"seq": 2}]


class FakeRun:
    def __init__(self, tmp_path: Path):
        self.calls: list[list[str]] = []
        self.tmp_path = tmp_path
        self.fail_for: set[str] = set()
        self.hang_for: set[str] = set()

    def __call__(self, cmd, **kw):
        self.calls.append(cmd)
        exe = cmd[-1]
        if any(h in " ".join(cmd) for h in self.hang_for):
            raise subprocess.TimeoutExpired(cmd, kw.get("timeout", 0))
        if "gdb" in cmd:
            script = Path(cmd[cmd.index("-x") + 1].replace("/src", str(self.tmp_path)))
            out = script.read_text().split("OUT = open('")[1].split("'")[0].replace("/src", str(self.tmp_path))
            if exe not in self.fail_for:
                Path(out).write_text(json.dumps({"seq": 1, "event": "enter", "frame": "gst/t.cpp:f", "args": {"x": 1}, "exe": exe}) + "\n")
            return subprocess.CompletedProcess(cmd, 0, "", "")
        if "readelf" in cmd:
            return subprocess.CompletedProcess(cmd, 0, " 0x1 (NEEDED) Shared library: [libnvbufsurface.so]\n" if "dev" in exe else " 0x1 (NEEDED) Shared library: [libc.so.6]\n", "")
        return subprocess.CompletedProcess(cmd, 1 if exe.endswith("failing") else 0, "", "")


def test_trace_tests_runs_each_executable_under_gdb_and_merges_the_traces(tmp_path: Path, monkeypatch):
    fake = FakeRun(tmp_path)
    monkeypatch.setattr(harness_cpp.container.subprocess, "run", fake)
    ctr = container.Container("img", "/src")
    out = tmp_path / ".flowdiff" / "run" / "flow1.jsonl"
    out.parent.mkdir(parents=True)
    assert harness_cpp.trace_tests(ctr, tmp_path, ["builddir/tests/a", "builddir/tests/b"], {"gst/t.cpp:f": "f"}, {}, out, 30) is None
    records = trace_gdb.read_records(out.read_text())
    assert [r["exe"] for r in records] == ["/src/builddir/tests/a", "/src/builddir/tests/b"]
    gdb_calls = [c for c in fake.calls if "gdb" in c]
    assert gdb_calls[0][-1] == "/src/builddir/tests/a" and "-batch" in gdb_calls[0]
    assert (tmp_path / ".flowdiff" / "run" / "gdb0.py").read_text().count("set breakpoint pending on") == 1


def test_trace_tests_reports_a_missing_trace_and_a_hang(tmp_path: Path, monkeypatch):
    fake = FakeRun(tmp_path)
    fake.fail_for.add("/src/builddir/tests/a")
    monkeypatch.setattr(harness_cpp.container.subprocess, "run", fake)
    ctr = container.Container("img", "/src")
    out = tmp_path / ".flowdiff" / "run" / "flow1.jsonl"
    out.parent.mkdir(parents=True)
    failure = harness_cpp.trace_tests(ctr, tmp_path, ["builddir/tests/a"], {}, {}, out, 30)
    assert failure and failure.startswith("builddir/tests/a: gdb wrote no trace")
    fake.hang_for.add("tests/b")
    failure = harness_cpp.trace_tests(ctr, tmp_path, ["builddir/tests/b"], {}, {}, out, 30)
    assert failure and failure.startswith("builddir/tests/b: gdb did not finish")


def test_run_executables_and_linked_variant(tmp_path: Path, monkeypatch):
    fake = FakeRun(tmp_path)
    monkeypatch.setattr(harness_cpp.container.subprocess, "run", fake)
    ctr = container.Container("img", "/src")
    assert harness_cpp.run_executables(ctr, tmp_path, ["builddir/tests/ok", "builddir/tests/failing"], 5) == {
        "builddir/tests/ok": "PASS", "builddir/tests/failing": "FAIL"}
    fake.hang_for.add("hangs")
    assert harness_cpp.run_executables(ctr, tmp_path, ["builddir/tests/hangs"], 5) == {"builddir/tests/hangs": "TIMEOUT"}
    assert harness_cpp.linked_variant(ctr, tmp_path, ["builddir/tests/ok"]) == ([], "linked: no device libraries (host build)")
    assert harness_cpp.linked_variant(ctr, tmp_path, ["builddir/tests/dev"]) == (["libnvbufsurface.so"], "linked: libnvbufsurface.so (device build)")
