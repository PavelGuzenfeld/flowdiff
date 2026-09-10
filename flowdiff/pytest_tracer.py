"""pytest plugin: trace the flow frames while the covering tests run, so the tests are the harness.

Loaded with -p flowdiff.pytest_tracer inside the project's interpreter (Python 3.8 compatible);
tree, frames and output path arrive in the FLOWDIFF_* environment set by env.harness_env.
"""
from __future__ import annotations

import json
import os
from typing import Any, Optional

from .trace_py import Tracer

_tracer: Optional[Tracer] = None


def pytest_sessionstart(session: Any) -> None:
    global _tracer
    tracer = Tracer(os.environ["FLOWDIFF_TREE"], json.loads(os.environ["FLOWDIFF_FRAMES"]),
                    os.environ["FLOWDIFF_OUT"])
    tracer.start()
    _tracer = tracer


def pytest_sessionfinish(session: Any, exitstatus: int) -> None:
    global _tracer
    if _tracer is not None:
        _tracer.stop()
        _tracer = None
