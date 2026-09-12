"""python_plan, cpp_plan and the run loop with every external step faked: no interpreter, no docker."""
import json
from pathlib import Path

from flowdiff import changes, cli, container, graph, harness_cpp, harness_py, play

from fake_client import FakeClient, chain_symbols, symbol


def flow_of(tmp_path: Path, entry_name="scale", tests=("tests/test_lib.py::test_scale",), language="python"):
    syms = chain_symbols(tmp_path, "entry", "scale", "clamp")
    for s in syms.values():
        object.__setattr__(s, "path", tmp_path / ("lib.py" if language == "python" else "lib.cpp"))
    nodes = {n: graph.node_of(s, "body" if n == "scale" else "unchanged") for n, s in syms.items()}
    entry = nodes[entry_name] if entry_name else None
    frames = [nodes["scale"], nodes["clamp"]]
    return graph.Flow([nodes["scale"]], entry, frames, [graph.Edge(nodes["scale"].id, nodes["clamp"].id)],
                      entry is None, list(tests), language), nodes


def args_for(root: Path, *extra: str):
    return cli.build_play_parser().parse_args(["--repo", str(root), *extra])


def test_python_plan_uses_the_harness_when_it_is_complete(tmp_path: Path, monkeypatch):
    flow, nodes = flow_of(tmp_path)
    (tmp_path / "lib.py").write_text("")
    monkeypatch.setattr(harness_py, "build", lambda *a: harness_py.Harness("SRC", True, ("w1",), "lib.py:scale"))
    monkeypatch.setattr(play.env, "python_interpreter", lambda root: Path("/py"))
    seen = {}
    monkeypatch.setattr(play, "run_harness", lambda *a: seen.setdefault("harness", a) and None)
    monkeypatch.setattr(play, "test_delta", lambda *a: ["  d"])
    out = tmp_path / "run"
    out.mkdir()
    plan = play.python_plan(FakeClient(tmp_path, {}, {}), graph.Graph(), flow, tmp_path, tmp_path / "base", out, 1, 9.0)
    assert (out / "flow1.py").read_text() == "SRC"
    assert plan.frames == ["lib.py:scale", "lib.py:clamp"] and plan.driver == "lib.py:scale" and plan.warnings == ["w1"]
    assert plan.note is None and plan.stop is None
    assert plan.drive("base", tmp_path / "base") is None
    assert seen["harness"] == (Path("/py"), out / "flow1.py", tmp_path / "base", "base", out / "flow1.base.jsonl", 9.0)
    assert plan.delta() == ["  d"]


def test_python_plan_falls_back_to_shared_tests_then_stops(tmp_path: Path, monkeypatch):
    flow, nodes = flow_of(tmp_path)
    monkeypatch.setattr(harness_py, "build", lambda *a: harness_py.Harness("SRC", False))
    monkeypatch.setattr(play.env, "python_interpreter", lambda root: Path("/py"))
    monkeypatch.setattr(play, "shared_tests", lambda base, root, tests: (["tests/test_lib.py::test_scale"], ["tests/test_lib.py"]))
    seen = {}
    monkeypatch.setattr(play, "run_traced_tests", lambda *a: seen.setdefault("traced", a) and None)
    out = tmp_path / "run"
    out.mkdir()
    plan = play.python_plan(FakeClient(tmp_path, {}, {}), graph.Graph(), flow, tmp_path, tmp_path / "base", out, 2, 9.0)
    assert plan.note == "driven by 1 covering test(s) present on both sides" and plan.driver is None
    assert plan.warnings == ["the driving tests changed in this diff (tests/test_lib.py); a divergence may reflect the inputs rather than the code"]
    assert plan.drive("head", tmp_path) is None
    assert seen["traced"][:4] == (Path("/py"), tmp_path, ["tests/test_lib.py::test_scale"], plan.frames)
    monkeypatch.setattr(play, "shared_tests", lambda base, root, tests: ([], []))
    stopped = play.python_plan(FakeClient(tmp_path, {}, {}), graph.Graph(), flow, tmp_path, tmp_path / "base", out, 3, 9.0)
    assert stopped.drive is None and stopped.stop.startswith("no call site with literal arguments and no covering test")
    assert f"fill the slots in {out / 'flow3.py'}" in stopped.stop
    flow.tests.clear()
    assert play.python_plan(FakeClient(tmp_path, {}, {}), graph.Graph(), flow, tmp_path, tmp_path / "base", out, 4, 9.0).delta is None


class FakeCpp:
    def __init__(self, monkeypatch, exes=None, literal=None, device=()):
        self.calls: list = []
        self.exes = exes if exes is not None else {"tests/test_lib.cpp": "builddir/tests/test_lib"}
        self.literal = literal
        monkeypatch.setattr(harness_cpp, "test_executables", lambda tree, files, workdir=None: dict(self.exes))
        monkeypatch.setattr(harness_cpp, "harvest", lambda client, root, entry: self.literal)
        monkeypatch.setattr(harness_cpp, "build_harness", self.build_harness)
        monkeypatch.setattr(harness_cpp, "trace_tests", self.trace_tests)
        monkeypatch.setattr(harness_cpp, "run_executables", lambda ctr, tree, exes, t: {e: ("PASS" if tree.name != "base" else "FAIL") for e in exes})
        monkeypatch.setattr(harness_cpp, "linked_variant", lambda ctr, tree, exes: (list(device), "linked: x (device build)" if device else "linked: no device libraries (host build)"))
        monkeypatch.setattr(container, "build", self.build)
        self.build_failure = None
        self.harness_failure = None

    def build(self, ctr, tree, timeout):
        self.calls.append(("build", tree))
        return self.build_failure

    def build_harness(self, ctr, tree, source, call, out_rel, timeout):
        self.calls.append(("harness", tree, source, call, out_rel))
        return self.harness_failure or (f"{out_rel}/harness", "")

    def trace_tests(self, ctr, tree, exes, frames, tests, out, timeout):
        self.calls.append(("trace", tree, exes, frames, tests))
        out.write_text(json.dumps({"seq": 1, "event": "enter", "frame": "lib.cpp:scale", "args": {"v": 4}}) + "\n")
        return None


def test_cpp_plan_prefers_the_literal_harness_and_builds_the_base_once(tmp_path: Path, monkeypatch):
    flow, nodes = flow_of(tmp_path, tests=("tests/test_lib.cpp::test_scale",), language="cpp")
    fake = FakeCpp(monkeypatch, literal=["4", "true"])
    out = tmp_path / "run"
    out.mkdir()
    base = tmp_path / ".flowdiff" / "base"
    (base / ".flowdiff" / "run").mkdir(parents=True)
    ctr = container.Container("img", "/src")
    plan = play.cpp_plan(ctr, FakeClient(tmp_path, {}, {}), flow, tmp_path, base, out, 1, 9.0, 99.0)
    assert plan.driver == "lib.cpp:scale" and plan.note is None and plan.stop is None
    assert plan.drive("head", tmp_path) is None and plan.drive("base", base) is None
    assert plan.drive("base", base) is None
    assert [c for c in fake.calls if c[0] == "build"] == [("build", base)]
    harnesses = [c for c in fake.calls if c[0] == "harness"]
    assert harnesses[0][1:] == (tmp_path, "lib.cpp", "scale(4, true)", ".flowdiff/run/harness1")
    traces = [c for c in fake.calls if c[0] == "trace"]
    assert traces[0][2] == [".flowdiff/run/harness1/harness"] and traces[0][3] == {"lib.cpp:scale": "scale", "lib.cpp:clamp": "clamp"}
    assert traces[0][4] == {"test_scale": "tests/test_lib.cpp::test_scale", "test_test_scale": "tests/test_lib.cpp::test_scale"}
    assert (out / "flow1.head.jsonl").read_text().startswith('{"seq": 1') and (out / "flow1.base.jsonl").exists()
    assert plan.trailer == ["linked: no device libraries (host build)"]
    assert plan.delta() == ["  builddir/tests/test_lib  FAIL→PASS"]


def test_cpp_plan_falls_back_to_test_executables_and_reports_each_failure(tmp_path: Path, monkeypatch):
    flow, nodes = flow_of(tmp_path, tests=("tests/test_lib.cpp::test_scale",), language="cpp")
    fake = FakeCpp(monkeypatch, literal=None)
    out = tmp_path / "run"
    out.mkdir()
    base = tmp_path / ".flowdiff" / "base"
    base.mkdir(parents=True)
    ctr = container.Container("img", "/src")
    plan = play.cpp_plan(ctr, FakeClient(tmp_path, {}, {}), flow, tmp_path, base, out, 2, 9.0, 99.0)
    assert plan.note == "driven by 1 covering test executable(s) under gdb" and plan.driver is None
    assert plan.drive("head", tmp_path) is None
    assert [c for c in fake.calls if c[0] == "trace"][0][2] == ["builddir/tests/test_lib"]
    fake.exes = {}
    assert plan.drive("base", base) == "base: none of the covering test executables exist on this side"
    fake.build_failure = "ninja: broken"
    other = tmp_path / ".flowdiff" / "other"
    other.mkdir()
    assert plan.drive("base", other) == "ninja: broken"
    literal_plan = play.cpp_plan(ctr, FakeClient(tmp_path, {}, {}), flow_of(tmp_path, language="cpp")[0], tmp_path, base, out, 3, 9.0, 99.0)
    fake.literal = ["1"]
    fake.harness_failure = "harness compile failed"
    literal_plan = play.cpp_plan(ctr, FakeClient(tmp_path, {}, {}), flow, tmp_path, base, out, 3, 9.0, 99.0)
    assert literal_plan.drive("head", tmp_path) == "head: harness compile failed"


def test_cpp_plan_stops_without_a_driver_or_with_a_device_library(tmp_path: Path, monkeypatch):
    flow, nodes = flow_of(tmp_path, tests=(), language="cpp")
    FakeCpp(monkeypatch, exes={}, literal=None)
    ctr = container.Container("img", "/src")
    plan = play.cpp_plan(ctr, FakeClient(tmp_path, {}, {}), flow, tmp_path, tmp_path / "b", tmp_path, 1, 9.0, 99.0)
    assert plan.stop.startswith("no call site with literal arguments and no built test executable") and plan.drive is None
    flow, nodes = flow_of(tmp_path, tests=("tests/test_lib.cpp::t",), language="cpp")
    FakeCpp(monkeypatch, literal=None, device=("libcuda.so",))
    plan = play.cpp_plan(ctr, FakeClient(tmp_path, {}, {}), flow, tmp_path, tmp_path / "b", tmp_path, 1, 9.0, 99.0)
    assert plan.stop == "linked: x (device build); these frames need the device: clamp, scale"
    assert plan.trailer == ["linked: x (device build)"]
    two_body, _ = flow_of(tmp_path, entry_name=None, tests=("tests/test_lib.cpp::t",), language="cpp")
    FakeCpp(monkeypatch, literal=["9"])
    plan = play.cpp_plan(ctr, FakeClient(tmp_path, {}, {}), two_body, tmp_path, tmp_path / "b", tmp_path, 1, 9.0, 99.0)
    assert plan.driver is None and plan.note == "driven by 1 covering test executable(s) under gdb"


def fake_analyse(monkeypatch, root: Path, flows, language="python", ctr=None):
    def analyse(args, visit=None):
        analysis = cli.Analysis(root, changes.Revisions(root, "HEAD", None), list(flows), ["w0"], ctr)
        if visit is not None:
            visit(FakeClient(root, {}, {}, language=language), graph.Graph(), list(flows), analysis)
        return analysis
    monkeypatch.setattr(cli, "analyse", analyse)
    monkeypatch.setattr(play.worktree, "base_worktree", lambda root, ref, clean: root / ".flowdiff" / "base")
    monkeypatch.setattr(play.render, "render_graph", lambda f: "GRAPH")


def written_plan(root: Path, frames, base_ret, head_ret, **kw):
    def drive(side, tree):
        out = root / ".flowdiff" / "run" / f"flow1.{side}.jsonl"
        ret = base_ret if side == "base" else head_ret
        out.write_text(json.dumps({"seq": 1, "event": "enter", "frame": frames[0], "args": {"v": 4}}) + "\n"
                       + json.dumps({"seq": 2, "event": "exit", "frame": frames[0], "return": ret}) + "\n")
        return None
    return play.Plan(frames, drive=drive, **kw)


def test_run_drives_both_sides_prints_the_verdict_and_writes_the_index(repo: Path, monkeypatch, capsys):
    flow, nodes = flow_of(repo)
    fake_analyse(monkeypatch, repo, [flow])
    plan = written_plan(repo, ["lib.py:scale", "lib.py:clamp"], 8, 12, driver="lib.py:scale", warnings=["w1"],
                        trailer=["linked: host"], delta=lambda: ["  t  PASS→FAIL"])
    monkeypatch.setattr(play, "python_plan", lambda *a: plan)
    assert play.run(args_for(repo, "--depth", "1")) == 0
    captured = capsys.readouterr()
    assert "flow 1/1\nGRAPH\n" in captured.out and "1 of 1 frames differ: scale (return); origin scale\nlinked: host\n" in captured.out
    assert "lib.py:scale  base 1 call(s), head 1 call(s)\n  calls #1\n    return  8  →  12\n" in captured.out
    assert captured.out.rstrip().endswith("  t  PASS→FAIL")
    assert captured.err == "warning: w0\nwarning: w1\n"
    index = json.loads((repo / ".flowdiff" / "run" / "index.json").read_text())
    assert index[0]["driver"] == "lib.py:scale" and index[0]["names"] == {"lib.py:scale": "scale", "lib.py:clamp": "clamp"}
    assert play.run(args_for(repo, "--fail-on-diff")) == 3
    same = written_plan(repo, ["lib.py:scale", "lib.py:clamp"], 8, 8)
    monkeypatch.setattr(play, "python_plan", lambda *a: same)
    assert play.run(args_for(repo, "--fail-on-diff")) == 0


def test_run_prints_the_note_for_test_driven_flows_with_an_entry(repo: Path, monkeypatch, capsys):
    flow, nodes = flow_of(repo)
    fake_analyse(monkeypatch, repo, [flow])
    plan = written_plan(repo, ["lib.py:scale", "lib.py:clamp"], 8, 8, note="driven by 2 covering test(s) present on both sides")
    monkeypatch.setattr(play, "python_plan", lambda *a: plan)
    assert play.run(args_for(repo)) == 0
    out = capsys.readouterr().out
    assert "no call site with literal arguments; driven by 2 covering test(s) present on both sides\n" in out
    two_body, _ = flow_of(repo, entry_name=None)
    fake_analyse(monkeypatch, repo, [two_body])
    assert play.run(args_for(repo)) == 0
    out = capsys.readouterr().out
    assert "no call site with literal arguments;" not in out and "; driven by 2 covering test(s) present on both sides" in out


def test_run_stops_on_a_plan_that_cannot_drive_and_fails_on_a_drive_failure(repo: Path, monkeypatch, capsys):
    flow, nodes = flow_of(repo)
    fake_analyse(monkeypatch, repo, [flow])
    monkeypatch.setattr(play, "python_plan", lambda *a: play.Plan(["lib.py:scale"], stop="fill the slots in x"))
    assert play.run(args_for(repo)) == 2
    assert "fill the slots in x\n" in capsys.readouterr().out
    monkeypatch.setattr(play, "python_plan", lambda *a: play.Plan(["lib.py:scale"], drive=lambda side, tree: f"{side}: boom"))
    assert play.run(args_for(repo)) == 1
    assert capsys.readouterr().err.startswith("base: boom")
    monkeypatch.setattr(play, "python_plan", lambda *a: play.Plan(["lib.py:scale"]))
    assert play.run(args_for(repo)) == 2
    assert "nothing can drive this flow" in capsys.readouterr().out


def test_run_skips_removed_flows_and_languages_without_a_plan(repo: Path, monkeypatch, capsys):
    flow, nodes = flow_of(repo, language="cpp")
    gone = graph.Flow([graph.node_of(symbol("old", repo / "a.py", 30), "removed")], None, [], [], True, [])
    fake_analyse(monkeypatch, repo, [flow, gone], language="cpp", ctr=None)
    assert play.run(args_for(repo)) == 2
    captured = capsys.readouterr()
    assert "1 symbols removed, nothing to enter from: old-" in captured.out
    assert "warning: cpp flows outside a container are analysed only" in captured.err


def test_run_routes_cpp_flows_to_cpp_plan_when_a_container_exists(repo: Path, monkeypatch, capsys):
    flow, nodes = flow_of(repo, tests=("tests/test_lib.cpp::t",), language="cpp")
    ctr = container.Container("img", "/src")
    fake_analyse(monkeypatch, repo, [flow], language="cpp", ctr=ctr)
    seen = {}

    def cpp_plan(c, client, f, root, base, out_dir, index, timeout, build_timeout, no_build=False):
        seen["args"] = (c, root, base, index, timeout, build_timeout)
        return written_plan(root, ["lib.cpp:scale", "lib.cpp:clamp"], 1, 2)
    monkeypatch.setattr(play, "cpp_plan", cpp_plan)
    assert play.run(args_for(repo, "--run-timeout", "7", "--build-timeout", "8")) == 0
    assert seen["args"] == (ctr, repo, repo / ".flowdiff" / "base", 1, 7.0, 8.0)
    assert "1 of 1 frames differ: scale (return); origin scale" in capsys.readouterr().out


MAIN_TU = "namespace { int seed = 1; }\nint scale(int v, bool b) { return b ? v * seed : v; }\nint main() { return 0; }\n"


def test_an_entry_whose_translation_unit_defines_main_is_driven_by_its_test_executables(tmp_path: Path, monkeypatch):
    flow, nodes = flow_of(tmp_path, tests=("tests/test_lib.cpp::test_scale",), language="cpp")
    (tmp_path / "lib.cpp").write_text(MAIN_TU)
    fake = FakeCpp(monkeypatch, literal=["4", "true"])
    out = tmp_path / "run"
    out.mkdir()
    plan = play.cpp_plan(container.Container("img", "/src"), FakeClient(tmp_path, {}, {}), flow, tmp_path,
                         tmp_path / ".flowdiff" / "base", out, 1, 9.0, 99.0)
    assert plan.stop is None and plan.driver is None
    assert plan.note == "driven by 1 covering test executable(s) under gdb"
    assert plan.warnings == ["lib.cpp defines main; driven by its test executables instead of a literal harness"]
    assert plan.drive("head", tmp_path) is None
    assert [c for c in fake.calls if c[0] == "harness"] == []


def test_an_entry_with_main_and_no_test_executable_names_the_file_and_the_way_in(tmp_path: Path, monkeypatch):
    flow, nodes = flow_of(tmp_path, tests=(), language="cpp")
    (tmp_path / "lib.cpp").write_text(MAIN_TU)
    FakeCpp(monkeypatch, exes={}, literal=["4", "true"])
    out = tmp_path / "run"
    out.mkdir()
    plan = play.cpp_plan(container.Container("img", "/src"), FakeClient(tmp_path, {}, {}), flow, tmp_path,
                         tmp_path / ".flowdiff" / "base", out, 1, 9.0, 99.0)
    assert plan.stop == ("lib.cpp defines main, so the literal harness cannot include it, and no built test "
                         "executable reaches this flow; a covering test is the way into this entry")
    assert plan.drive is None


def test_a_translation_unit_without_main_still_gets_the_literal_harness(tmp_path: Path, monkeypatch):
    flow, nodes = flow_of(tmp_path, tests=("tests/test_lib.cpp::test_scale",), language="cpp")
    (tmp_path / "lib.cpp").write_text(MAIN_TU.replace("int main() { return 0; }", "int mainline() { return 0; }"))
    fake = FakeCpp(monkeypatch, literal=["4", "true"])
    out = tmp_path / "run"
    out.mkdir()
    plan = play.cpp_plan(container.Container("img", "/src"), FakeClient(tmp_path, {}, {}), flow, tmp_path,
                         tmp_path / ".flowdiff" / "base", out, 1, 9.0, 99.0)
    assert plan.driver == "lib.cpp:scale" and plan.warnings == []
