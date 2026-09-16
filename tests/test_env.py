import os
import sys
from dataclasses import dataclass
from pathlib import Path

from flowdiff import env


def make_python(venv: Path) -> Path:
    (venv / "bin").mkdir(parents=True)
    (venv / "bin" / "python").write_text("")
    return venv / "bin" / "python"


@dataclass
class Reply:
    stdout: str
    returncode: int = 0


def answers(monkeypatch, stdout: str, returncode: int = 0, calls: list | None = None):
    def run(cmd, **kw):
        if calls is not None:
            calls.append((cmd, kw["cwd"]))
        return Reply(stdout, returncode)

    monkeypatch.setattr(env.subprocess, "run", run)


def test_active_virtualenv_wins(tmp_path: Path, monkeypatch):
    active = make_python(tmp_path / "active")
    make_python(tmp_path / "root" / ".venv")
    monkeypatch.setenv("VIRTUAL_ENV", str(tmp_path / "active"))
    assert env.python_interpreter(tmp_path / "root") == (active, "$VIRTUAL_ENV")


def test_active_virtualenv_without_a_python_is_ignored(tmp_path: Path, monkeypatch):
    local = make_python(tmp_path / "root" / ".venv")
    monkeypatch.setenv("VIRTUAL_ENV", str(tmp_path / "empty"))
    assert env.python_interpreter(tmp_path / "root") == (local, ".venv/")


def test_project_venv_is_used_when_nothing_is_active(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)
    local = make_python(tmp_path / ".venv")
    assert env.python_interpreter(tmp_path) == (local, ".venv/")


def test_uv_environment_is_asked_for_when_a_lock_exists(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)
    (tmp_path / "uv.lock").write_text("")
    calls = []
    answers(monkeypatch, "/envs/uvproj/bin/python\n", calls=calls)
    assert env.python_interpreter(tmp_path) == (Path("/envs/uvproj/bin/python"), "uv.lock")
    assert calls == [(["uv", "run", "python", "-c", "import sys; print(sys.executable)"], tmp_path)]


def test_uv_answers_with_the_interpreter_itself_not_an_environment_root(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)
    (tmp_path / "uv.lock").write_text("")
    answers(monkeypatch, "/envs/uvproj/bin/python\n")
    chosen, _ = env.python_interpreter(tmp_path)
    assert chosen.name == "python" and chosen.parent.name == "bin"


def test_uv_is_preferred_over_poetry_when_both_locks_exist(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)
    (tmp_path / "uv.lock").write_text("")
    (tmp_path / "poetry.lock").write_text("")
    calls = []
    answers(monkeypatch, "/envs/uvproj/bin/python\n", calls=calls)
    assert env.python_interpreter(tmp_path)[1] == "uv.lock"
    assert [cmd[0] for cmd, _ in calls] == ["uv"]


def test_uv_failure_falls_through_to_poetry(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)
    (tmp_path / "uv.lock").write_text("")
    (tmp_path / "poetry.lock").write_text("")
    replies = {"uv": Reply("", 1), "poetry": Reply("/envs/proj-py3.12\n")}
    monkeypatch.setattr(env.subprocess, "run", lambda cmd, **_: replies[cmd[0]])
    assert env.python_interpreter(tmp_path) == (Path("/envs/proj-py3.12/bin/python"), "poetry.lock")


def test_an_uninstalled_manager_falls_through_instead_of_raising(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)
    (tmp_path / "uv.lock").write_text("")

    def absent(cmd, **_):
        raise FileNotFoundError(2, "No such file or directory", cmd[0])

    monkeypatch.setattr(env.subprocess, "run", absent)
    assert env.python_interpreter(tmp_path) == (Path(sys.executable), "no project environment; flowdiff's own interpreter")


def test_poetry_environment_is_asked_for_when_a_lock_exists(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)
    (tmp_path / "poetry.lock").write_text("")
    calls = []
    answers(monkeypatch, "/envs/proj-py3.12\n", calls=calls)
    assert env.python_interpreter(tmp_path) == (Path("/envs/proj-py3.12/bin/python"), "poetry.lock")
    assert calls == [(["poetry", "env", "info", "--path"], tmp_path)]


def test_poetry_failure_falls_back_to_the_running_interpreter(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)
    (tmp_path / "poetry.lock").write_text("")
    answers(monkeypatch, "", returncode=1)
    assert env.python_interpreter(tmp_path) == (Path(sys.executable), "no project environment; flowdiff's own interpreter")


def test_a_manager_that_succeeds_but_says_nothing_falls_through(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)
    (tmp_path / "poetry.lock").write_text("")
    answers(monkeypatch, "  \n", returncode=0)
    assert env.python_interpreter(tmp_path)[0] == Path(sys.executable)


def test_no_project_environment_means_the_running_interpreter(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)
    assert env.python_interpreter(tmp_path) == (Path(sys.executable), "no project environment; flowdiff's own interpreter")


def test_harness_env_points_pythonpath_at_the_tree_first(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("PYTHONPATH", "/elsewhere")
    e = env.harness_env(tmp_path, "base", tmp_path / "out.jsonl", ["lib.py:f"])
    assert e["PYTHONPATH"] == f"{tmp_path}{os.pathsep}{env.TOOL_ROOT}{os.pathsep}/elsewhere"
    assert e["PYTHONHASHSEED"] == "0" and e["PYTHONDONTWRITEBYTECODE"] == "1"
    assert e["FLOWDIFF_TREE"] == str(tmp_path) and e["FLOWDIFF_SIDE"] == "base"
    assert e["FLOWDIFF_OUT"] == str(tmp_path / "out.jsonl") and e["FLOWDIFF_FRAMES"] == '["lib.py:f"]'
    assert e["FLOWDIFF_SCRATCH"] == str(tmp_path.parent)
    assert Path(e["FLOWDIFF_TOOL"]) == env.TOOL_ROOT and (env.TOOL_ROOT / "flowdiff" / "trace_py.py").exists()
    assert env.harness_env(tmp_path, "base", tmp_path / "out.jsonl")["FLOWDIFF_FRAMES"] == "[]"


def test_harness_env_without_a_trace_sets_no_flowdiff_variables(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("PYTHONPATH", raising=False)
    e = env.harness_env(tmp_path)
    assert e["PYTHONPATH"] == str(tmp_path)
    assert not [k for k in e if k.startswith("FLOWDIFF_")]


def test_harness_env_sets_freeze_only_when_asked_and_only_with_a_trace(tmp_path: Path):
    e = env.harness_env(tmp_path, "base", tmp_path / "out.jsonl", freeze=True)
    assert e["FLOWDIFF_FREEZE"] == "1"
    assert "FLOWDIFF_FREEZE" not in env.harness_env(tmp_path, "base", tmp_path / "out.jsonl")
    assert "FLOWDIFF_FREEZE" not in env.harness_env(tmp_path, freeze=True)
