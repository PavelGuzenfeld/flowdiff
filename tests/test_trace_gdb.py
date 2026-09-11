import importlib


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
