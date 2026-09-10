"""The project's own interpreter and the environment a Python harness runs in (decision 46)."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

TOOL_ROOT = Path(__file__).resolve().parent.parent


def python_interpreter(root: Path) -> Path:
    active = os.environ.get("VIRTUAL_ENV")
    if active and (Path(active) / "bin" / "python").exists():
        return Path(active) / "bin" / "python"
    if (root / ".venv" / "bin" / "python").exists():
        return root / ".venv" / "bin" / "python"
    if (root / "poetry.lock").exists():
        out = subprocess.run(["poetry", "env", "info", "--path"], cwd=root, capture_output=True, text=True)
        if out.returncode == 0 and out.stdout.strip():
            return Path(out.stdout.strip()) / "bin" / "python"
    return Path(sys.executable)


def harness_env(tree: Path, side: str = "", trace_out: Path | None = None,
                frames: list[str] | None = None) -> dict[str, str]:
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
    return env
