from pathlib import Path

import pytest

from flowdiff import changes, cli

from conftest import SOURCE, git


@pytest.fixture
def tools_present(monkeypatch):
    monkeypatch.setattr(cli.shutil, "which", lambda name: f"/usr/bin/{name}")


def test_exit_codes_are_the_documented_values():
    """Literals, not the constants: comparing a constant to itself asserts nothing."""
    assert (cli.EXIT_OK, cli.EXIT_TOOL_ERROR, cli.EXIT_NOTHING, cli.EXIT_DIFF) == (0, 1, 2, 3)


def test_verbs_dispatch_to_play_with_their_own_parsers(monkeypatch):
    from flowdiff import play
    seen = {}
    monkeypatch.setattr(play, "run", lambda args: seen.setdefault("play", args) and 7)
    monkeypatch.setattr(play, "show", lambda args: seen.setdefault("show", args) and 8)
    assert cli.main(["play", "--depth", "2", "--fail-on-diff", "--clean-base"]) == 7
    assert (seen["play"].depth, seen["play"].fail_on_diff, seen["play"].clean_base) == (2, True, True)
    assert cli.main(["show", "scale/return"]) == 8
    assert seen["show"].frame == "scale/return"
    from flowdiff import keep
    monkeypatch.setattr(keep, "run", lambda args: seen.setdefault("keep", args) and 9)
    assert cli.main(["keep"]) == 9 and seen["keep"].frame is None
    assert cli.build_keep_parser().parse_args(["scale"]).frame == "scale"


def test_play_parser_defaults():
    args = cli.build_play_parser().parse_args([])
    assert (args.depth, args.clean_base, args.fail_on_diff, args.run_timeout) == (0, False, False, 300.0)
    assert (args.ref, args.hops, args.no_tests) == (None, 3, False)


def test_missing_tool_is_exit_1(monkeypatch, capsys, repo: Path):
    monkeypatch.setattr(cli.shutil, "which", lambda name: None if name == "graph-easy" else "/usr/bin/x")
    assert cli.main(["--repo", str(repo)]) == 1
    assert "graph-easy" in capsys.readouterr().err


def test_missing_tool_names_every_absent_binary(monkeypatch, capsys, repo: Path):
    monkeypatch.setattr(cli.shutil, "which", lambda name: None)
    assert cli.main(["--repo", str(repo)]) == 1
    err = capsys.readouterr().err
    assert all(binary in err for binary in cli.REQUIRED_BINARIES)
    assert "apt install libgraph-easy-perl" in err


def test_not_a_repo_is_exit_1(tools_present, tmp_path: Path, capsys):
    assert cli.main(["--repo", str(tmp_path)]) == 1
    assert str(tmp_path) in capsys.readouterr().err


def test_clean_tree_is_exit_2(tools_present, repo: Path, capsys):
    assert cli.main(["--repo", str(repo)]) == 2
    out = capsys.readouterr().out
    assert "no changes" in out and "working tree" in out


def test_unknown_ref_is_exit_1_with_gits_reason(tools_present, repo: Path, capsys):
    assert cli.main(["--repo", str(repo), "origin/main"]) == 1
    err = capsys.readouterr().err
    assert err.startswith("git diff origin/main: ambiguous argument 'origin/main': unknown revision")
    assert "Traceback" not in err and "git <command>" not in err
    assert cli.main(["--repo", str(repo), "HEAD..nope"]) == 1
    assert capsys.readouterr().err.startswith("git diff HEAD: Needed a single revision")


def test_clean_tree_against_a_ref_names_both_revisions(tools_present, repo: Path, capsys):
    assert cli.main(["--repo", str(repo), "HEAD"]) == 2
    out = capsys.readouterr().out
    assert "HEAD" in out and "working tree" not in out
    assert not (repo / ".flowdiff" / "head").exists()


def test_only_test_file_changes_are_exit_2(tools_present, repo: Path, capsys):
    (repo / "tests").mkdir()
    (repo / "tests" / "test_a.py").write_text("from a import f\n\n\ndef test_f():\n    assert f(1) == 2\n")
    assert cli.main(["--repo", str(repo)]) == 2
    assert "only test files changed" in capsys.readouterr().out
    (repo / "notes.txt").write_text("x\n")
    assert cli.main(["--repo", str(repo)]) == 2
    assert "no changed files in a supported language" in capsys.readouterr().out


def test_unsupported_language_is_exit_2(tools_present, repo: Path, capsys):
    (repo / "notes.txt").write_text("hello\n")
    assert cli.main(["--repo", str(repo)]) == 2
    assert "supported language" in capsys.readouterr().out


def test_missing_language_server_is_exit_1(monkeypatch, repo: Path, capsys):
    monkeypatch.setattr(cli.shutil, "which", lambda name: None if name == "pyright-langserver" else "/usr/bin/x")
    (repo / "a.py").write_text(SOURCE.replace("x + 1", "x + 2"))
    assert cli.main(["--repo", str(repo)]) == 1
    err = capsys.readouterr().err
    assert "pyright-langserver" in err and "npm install -g pyright" in err


def test_parser_defaults():
    args = cli.build_parser().parse_args([])
    assert (args.ref, args.hops, args.timeout) == (None, 3, 60.0)
    assert args.no_tests is False and args.full is False and args.list_tests is False and args.split is False
    assert args.repo == Path.cwd()


def test_main_reads_sys_argv_after_the_program_name(monkeypatch):
    from flowdiff import keep
    seen = {}
    monkeypatch.setattr(keep, "run", lambda args: seen.setdefault("frame", args.frame) and 4)
    monkeypatch.setattr(cli.sys, "argv", ["flowdiff", "keep", "scale"])
    assert cli.main() == 4 and seen["frame"] == "scale"


def test_common_parser_pins_the_container_defaults():
    args = cli.build_parser().parse_args([])
    assert args.image is None and args.build_timeout == 1800.0


class FakeBaseClient:
    def __init__(self, cpp=False):
        self.waited = []
        self.closed = False
        self.root = Path("/base")

    def wait_for_index(self, timeout):
        self.waited.append(timeout)
        return True

    def close(self):
        self.closed = True


def split_fixture(monkeypatch, tmp_path: Path, language="python", changed_names=("scale",), base_names=("scale",)):
    from flowdiff import graph, lsp, worktree
    from fake_client import FakeClient, symbol
    root, base = tmp_path / "root", tmp_path / ".flowdiff" / "base"
    root.mkdir()
    monkeypatch.setattr(worktree, "base_worktree", lambda r, ref: base)
    suffix = ".py" if language == "python" else ".cpp"
    config = lsp.ServerConfig(language, "srv", (), frozenset({suffix}), language)
    monkeypatch.setattr(lsp, "server_for", lambda p, r, c=None: config)
    fake = FakeBaseClient()
    monkeypatch.setattr(lsp, "LspClient", lambda cfg, r, timeout: fake)
    head_syms = {n: symbol(n, root / f"m{suffix}", i * 10) for i, n in enumerate(changed_names)}
    base_syms = [changes.ChangedSymbol(symbol(n, base / f"m{suffix}", i * 10), "body") for i, n in enumerate(base_names)]
    monkeypatch.setattr(changes, "base_symbols", lambda client, r, b, ch: base_syms)
    monkeypatch.setattr(graph, "build_graph", lambda client, ch, hops: graph.Graph())
    base_flows = [graph.Flow([graph.node_of(c.symbol, "body")], None, [graph.node_of(c.symbol, "body")], [], True, [])
                  for c in base_syms]
    monkeypatch.setattr(graph, "flows", lambda client, g, ch, hops, with_tests: base_flows)
    changed = [changes.ChangedSymbol(s, "body") for s in head_syms.values()]
    head_flows = [graph.Flow([graph.node_of(s, "body")], None, [graph.node_of(s, "body")], [], True, [], language)
                  for s in head_syms.values()]
    analysis = cli.Analysis(root, changes.Revisions(root, "HEAD", None))
    args = cli.build_parser().parse_args(["--split", "--timeout", "3", "--build-timeout", "9"])
    return analysis, config, changed, head_flows, args, fake


def test_base_side_pairs_flows_by_changed_names_and_closes_the_client(monkeypatch, tmp_path: Path):
    analysis, config, changed, flows, args, fake = split_fixture(monkeypatch, tmp_path, changed_names=("scale", "other"),
                                                                base_names=("other",))
    paired = cli.base_side(analysis, config, changed, flows, args)
    assert [[n.name for n in f.changed] for f in paired] == [[], ["other"]]
    assert paired[0].two_body is True and paired[0].frames == [] and paired[0].language == "python"
    assert fake.closed and fake.waited == []


def test_base_side_waits_for_the_index_and_builds_the_base_for_cpp(monkeypatch, tmp_path: Path):
    from flowdiff import container
    analysis, config, changed, flows, args, fake = split_fixture(monkeypatch, tmp_path, language="cpp")
    analysis.container = container.Container("img", "/src")
    built = []
    monkeypatch.setattr(container, "build", lambda ctr, tree, timeout: built.append((tree, timeout)) or None)
    assert len(cli.base_side(analysis, config, changed, flows, args)) == 1
    assert built == [(tmp_path / ".flowdiff" / "base", 9.0)] and fake.waited == [9.0]
    monkeypatch.setattr(container, "build", lambda ctr, tree, timeout: "ninja failed:\ndetail")
    assert cli.base_side(analysis, config, changed, flows, args) == []
    assert analysis.warnings == ["base graph skipped: ninja failed:"]


def test_base_side_is_empty_without_a_server_or_matching_symbols(monkeypatch, tmp_path: Path):
    from flowdiff import lsp
    analysis, config, changed, flows, args, fake = split_fixture(monkeypatch, tmp_path, base_names=())
    assert cli.base_side(analysis, config, changed, flows, args) == [] and fake.closed
    monkeypatch.setattr(lsp, "server_for", lambda p, r, c=None: None)
    assert cli.base_side(analysis, config, changed, flows, args) == []


def test_analyse_builds_in_the_container_for_cpp_hunks_and_reports_detection_failures(monkeypatch, repo: Path, capsys):
    from dataclasses import replace
    from flowdiff import container, graph, lsp
    from fake_client import FakeClient, symbol
    (repo / "pkg").mkdir()
    (repo / "pkg" / "package.xml").write_text("<package><name>pkg_lib</name></package>")
    (repo / "pkg" / "a.cpp").write_text("int f(int x) {\n    return x + 1;\n}\n")
    git(repo, "add", "pkg")
    git(repo, "commit", "-q", "-m", "cpp")
    (repo / "pkg" / "a.cpp").write_text("int f(int x) {\n    return x + 2;\n}\n")
    monkeypatch.setattr(cli.shutil, "which", lambda name: "/usr/bin/x")
    ctr = container.Container("flowdiff/repo:dev", "/src")
    monkeypatch.setattr(container, "detect", lambda root, image: ctr)
    built = []
    monkeypatch.setattr(container, "build", lambda c, tree, timeout: built.append((c, tree, timeout)) or None)
    f = symbol("f", repo / "pkg" / "a.cpp", 0, last=2)
    monkeypatch.setattr(lsp, "LspClient", lambda cfg, root, timeout: FakeClient(root, {"f": f}, {}, language="cpp"))
    monkeypatch.setattr(changes, "comment_spans", lambda *a: [])
    args = cli.build_parser().parse_args(["--repo", str(repo), "--no-tests"])
    analysis = cli.analyse(args)
    assert isinstance(analysis, cli.Analysis) and analysis.container == replace(ctr, packages=("pkg_lib",))
    assert built == [(analysis.container, repo, 1800.0)] and [n.name for fl in analysis.flows for n in fl.changed] == ["f"]
    assert "building the working tree in flowdiff/repo:dev" in capsys.readouterr().err
    monkeypatch.setattr(container, "build", lambda c, tree, timeout: "meson setup failed")
    assert cli.analyse(args) == 1 and "meson setup failed" in capsys.readouterr().err
    no_build = cli.build_parser().parse_args(["--repo", str(repo), "--no-tests", "--no-build"])
    assert isinstance(cli.analyse(no_build), cli.Analysis) and "building" not in capsys.readouterr().err
    monkeypatch.setattr(container, "detect", lambda root, image: None)
    assert isinstance(cli.analyse(args), cli.Analysis) and "building" not in capsys.readouterr().err

    def refuse(root, image):
        raise container.ContainerError("no dev image for repo")
    monkeypatch.setattr(container, "detect", refuse)
    assert cli.analyse(args) == 1 and "container: no dev image for repo" in capsys.readouterr().err


def test_render_all_draws_base_beside_head_when_base_flows_exist(monkeypatch, capsys, tmp_path: Path):
    from flowdiff import graph, render
    monkeypatch.setattr(render, "render_graph", lambda f: "HEAD-GRAPH" if f.frames and f.frames[0].name == "l" else "BASE-GRAPH")
    live = graph.Node("l", "l", tmp_path / "m.py", 5, 0, "body")
    old = graph.Node("o", "o", tmp_path / "m.py", 5, 0, "body")
    flows = [graph.Flow([live], live, [live], [], False, []), graph.Flow([live], live, [live], [], False, [])]
    bases = [graph.Flow([old], old, [old], [], False, []), graph.Flow([], None, [], [], True, [])]
    analysis = cli.Analysis(tmp_path, changes.Revisions(tmp_path, "HEAD", None), flows, [], None, bases)
    cli.render_all(analysis, full=False)
    out = capsys.readouterr().out
    assert "flow 1/2\nbase         │   head\nBASE-GRAPH   │   HEAD-GRAPH\nentry l  (1 frames, 1 changed: l~)\nno covering tests\n" in out
    second = out.split("flow 2/2\n", 1)[1].splitlines()
    assert second[0].split("│") == ["base" + " " * 22 + "   ", "   head"]
    assert second[1].split("│") == ["(not in the base revision)   ", "   HEAD-GRAPH"]
    assert not out.endswith("\n\n")
    flows[0].tests.append("tests/t.py::t")
    cli.render_all(analysis, full=False, list_tests=True)
    assert "covered by 1 test(s)\n  tests/t.py::t\n" in capsys.readouterr().out


def test_render_all_numbers_only_live_flows_and_collapses_removed_ones(monkeypatch, capsys, tmp_path: Path):
    from flowdiff import graph, render
    monkeypatch.setattr(render, "render_graph", lambda f: "GRAPH")
    gone = graph.Node("g", "g", tmp_path / "m.py", 0, 0, "removed")
    live = graph.Node("l", "l", tmp_path / "m.py", 5, 0, "body")
    flows = [graph.Flow([gone], None, [gone], [], True, []),
             graph.Flow([live], live, [live], [], False, ["tests/t.py::t"]),
             graph.Flow([gone], None, [gone], [], True, [])]
    analysis = cli.Analysis(tmp_path, changes.Revisions(tmp_path, "HEAD", None), flows, ["w1"])
    cli.render_all(analysis, full=False)
    captured = capsys.readouterr()
    assert captured.out.startswith("flow 1/1\nGRAPH\n")
    assert captured.out.rstrip().endswith("\n\n2 symbols removed, nothing to enter from: g-, g-")
    assert "tests/t.py::t" not in captured.out and captured.err == "warning: w1\n"
    cli.render_all(cli.Analysis(tmp_path, analysis.revs, flows[:1]), full=False)
    assert capsys.readouterr().out == "1 symbols removed, nothing to enter from: g-\n"


def test_repeated_warnings_collapse_to_one_line_with_a_count(capsys, tmp_path: Path):
    analysis = cli.Analysis(tmp_path, changes.Revisions(tmp_path, "HEAD", None), [], ["w1", "w2", "w1"])
    cli.render_all(analysis, full=False)
    assert capsys.readouterr().err == "warning: w1 (×2)\nwarning: w2\n"


def test_revisions_read_a_ref_or_a_range(tmp_path: Path):
    R = changes.Revisions
    assert cli.revisions(tmp_path, None) == R(tmp_path, "HEAD", None)
    assert cli.revisions(tmp_path, "v1") == R(tmp_path, "v1", "HEAD")
    assert cli.revisions(tmp_path, "v1..v2") == R(tmp_path, "v1", "v2")
    assert cli.revisions(tmp_path, "..v2") == R(tmp_path, "HEAD", "v2")
    assert cli.revisions(tmp_path, "v1..") == R(tmp_path, "v1", "HEAD")


def test_a_range_is_analysed_from_a_worktree_at_its_head(tools_present, monkeypatch, repo: Path):
    from flowdiff import lsp
    from fake_client import FakeClient, symbol
    second = SOURCE.replace("x + 1", "x + 2")
    (repo / "a.py").write_text(second)
    git(repo, "commit", "-q", "-am", "f changes")
    (repo / "a.py").write_text(second.replace("* 2", "* 3"))
    git(repo, "commit", "-q", "-am", "g changes")
    (repo / "a.py").write_text(second.replace("* 2", "* 4"))
    monkeypatch.setattr(lsp, "LspClient",
                        lambda cfg, root, timeout: FakeClient(root, {"f": symbol("f", root / "a.py", 0, last=2),
                                                                     "g": symbol("g", root / "a.py", 4, last=6)}, {}))
    monkeypatch.setattr(changes, "comment_spans", lambda *a: [])
    analysis = cli.analyse(cli.build_parser().parse_args(["--repo", str(repo), "--no-tests", "HEAD~2..HEAD~1"]))
    assert isinstance(analysis, cli.Analysis)
    head = repo / ".flowdiff" / "head" / repo.name
    shas = [git(repo, "rev-parse", r).strip() for r in ("HEAD~2", "HEAD~1")]
    assert analysis.root == head and analysis.revs == changes.Revisions(head, *shas)
    assert (head / "a.py").read_text() == second
    assert [n.name for fl in analysis.flows for n in fl.changed] == ["f"]
    assert cli.analyse(cli.build_parser().parse_args(["--repo", str(repo), "HEAD..HEAD"])) == 2
    assert (head / "a.py").read_text() == second.replace("* 2", "* 3")


def test_parser_accepts_a_base_ref_and_overrides():
    args = cli.build_parser().parse_args(["origin/main", "--hops", "5", "--full", "--no-tests"])
    assert (args.ref, args.hops) == ("origin/main", 5)
    assert args.no_tests is True and args.full is True
