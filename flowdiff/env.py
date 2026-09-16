"""The project's own interpreter and the environment a Python harness runs in (decision 46)."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

TOOL_ROOT = Path(__file__).resolve().parent.parent
UV_QUERY = ["uv", "run", "python", "-c", "import sys; print(sys.executable)"]
POETRY_QUERY = ["poetry", "env", "info", "--path"]


def reported_path(cmd: list[str], root: Path) -> str:
    """Where a package manager says its environment is; empty when it fails or is not installed."""
    try:
        out = subprocess.run(cmd, cwd=root, capture_output=True, text=True)
    except OSError:
        return ""
    return out.stdout.strip() if out.returncode == 0 else ""


def python_interpreter(root: Path) -> tuple[Path, str]:
    """The project's interpreter and the convention that named it (decision 46)."""
    active = os.environ.get("VIRTUAL_ENV")
    if active and (Path(active) / "bin" / "python").exists():
        return Path(active) / "bin" / "python", "$VIRTUAL_ENV"
    if (root / ".venv" / "bin" / "python").exists():
        return root / ".venv" / "bin" / "python", ".venv/"
    # uv answers no report-only query: `uv python find` names the system interpreter and
    # `uv run --no-sync` leaves an unsynced .venv, so asking it syncs the project (issue #74).
    if (root / "uv.lock").exists() and (found := reported_path(UV_QUERY, root)):
        return Path(found), "uv.lock"
    if (root / "poetry.lock").exists() and (found := reported_path(POETRY_QUERY, root)):
        return Path(found) / "bin" / "python", "poetry.lock"
    return Path(sys.executable), "no project environment; flowdiff's own interpreter"


def harness_env(tree: Path, side: str = "", trace_out: Path | None = None, frames: list[str] | None = None,
                freeze: bool = False) -> dict[str, str]:
    env = dict(os.environ)
    path = [str(tree)] + ([str(TOOL_ROOT)] if trace_out is not None else [])
    if env.get("PYTHONPATH"):
        path.append(env["PYTHONPATH"])
    env["PYTHONPATH"] = os.pathsep.join(path)
    # The two runs must not differ in set/dict iteration order alone.
    env["PYTHONHASHSEED"] = "0"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    if trace_out is not None:
        env["FLOWDIFF_TOOL"] = str(TOOL_ROOT)
        env["FLOWDIFF_TREE"] = str(tree)
        env["FLOWDIFF_SIDE"] = side
        env["FLOWDIFF_OUT"] = str(trace_out)
        env["FLOWDIFF_SCRATCH"] = str(trace_out.parent.parent)
        env["FLOWDIFF_FRAMES"] = json.dumps(frames or [])
        if freeze:
            env["FLOWDIFF_FREEZE"] = "1"
    return env
