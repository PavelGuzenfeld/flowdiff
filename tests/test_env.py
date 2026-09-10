import os
import sys
from pathlib import Path

from flowdiff import env


def make_python(venv: Path) -> Path:
    (venv / "bin").mkdir(parents=True)
    (venv / "bin" / "python").write_text("")
    return venv / "bin" / "python"


def test_active_virtualenv_wins(tmp_path: Path, monkeypatch):
    active = make_python(tmp_path / "active")
    make_python(tmp_path / "root" / ".venv")
    monkeypatch.setenv("VIRTUAL_ENV", str(tmp_path / "active"))
    assert env.python_interpreter(tmp_path / "root") == active


def test_active_virtualenv_without_a_python_is_ignored(tmp_path: Path, monkeypatch):
    local = make_python(tmp_path / "root" / ".venv")
    monkeypatch.setenv("VIRTUAL_ENV", str(tmp_path / "empty"))
    assert env.python_interpreter(tmp_path / "root") == local


def test_project_venv_is_used_when_nothing_is_active(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)
    local = make_python(tmp_path / ".venv")
    assert env.python_interpreter(tmp_path) == local


def test_poetry_environment_is_asked_for_when_a_lock_exists(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)
    (tmp_path / "poetry.lock").write_text("")
    calls = []

    class Done:
        returncode = 0
        stdout = "/envs/proj-py3.12\n"

    monkeypatch.setattr(env.subprocess, "run", lambda cmd, **kw: calls.append((cmd, kw["cwd"])) or Done())
    assert env.python_interpreter(tmp_path) == Path("/envs/proj-py3.12/bin/python")
    assert calls == [(["poetry", "env", "info", "--path"], tmp_path)]


def test_poetry_failure_falls_back_to_the_running_interpreter(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)
    (tmp_path / "poetry.lock").write_text("")

    class Failed:
        returncode = 1
        stdout = ""

    monkeypatch.setattr(env.subprocess, "run", lambda *a, **k: Failed())
    assert env.python_interpreter(tmp_path) == Path(sys.executable)


def test_no_project_environment_means_the_running_interpreter(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)
    assert env.python_interpreter(tmp_path) == Path(sys.executable)


def test_harness_env_points_pythonpath_at_the_tree_first(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("PYTHONPATH", "/elsewhere")
    e = env.harness_env(tmp_path, "base", tmp_path / "out.jsonl")
    assert e["PYTHONPATH"] == f"{tmp_path}{os.pathsep}/elsewhere"
    assert e["PYTHONHASHSEED"] == "0" and e["PYTHONDONTWRITEBYTECODE"] == "1"
    assert e["FLOWDIFF_TREE"] == str(tmp_path) and e["FLOWDIFF_SIDE"] == "base"
    assert e["FLOWDIFF_OUT"] == str(tmp_path / "out.jsonl")
    assert Path(e["FLOWDIFF_TOOL"]) == env.TOOL_ROOT and (env.TOOL_ROOT / "flowdiff" / "trace_py.py").exists()


def test_harness_env_without_a_trace_sets_no_flowdiff_variables(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("PYTHONPATH", raising=False)
    e = env.harness_env(tmp_path)
    assert e["PYTHONPATH"] == str(tmp_path)
    assert not [k for k in e if k.startswith("FLOWDIFF_")]
