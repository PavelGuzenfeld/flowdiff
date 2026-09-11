"""In-process tracer for the Python harness: one JSON line per frame entry and exit (decisions 27, 29, 30).

Frames are named "relative/path.py:function". sys.monitoring on 3.12+, sys.settrace before that;
both emit the same records. Runs inside the project's interpreter: Python 3.8 compatible.
"""
from __future__ import annotations

import inspect
import json
import os
import sys
from typing import Any, Iterable, Optional

from . import summarise


def frame_key(tree: str, filename: str, name: str) -> Optional[str]:
    real = os.path.realpath(filename)
    if not real.startswith(tree + os.sep):
        return None
    return os.path.relpath(real, tree) + ":" + name


class Tracer:
    def __init__(self, tree: str, frames: Iterable[str], out_path: str):
        self.tree = os.path.realpath(tree)
        self.wanted = set(frames)
        self.out = open(out_path, "w")
        self.seq = 0
        # Set by the pytest plugin to the running test's node id; recorded on every event.
        self.context: Optional[str] = None
        self._tool = None
        self._previous_trace: Any = None
        summarise.load_project_summariser(self.tree, os.environ.get("FLOWDIFF_SCRATCH"))

    def key_of(self, code: Any) -> Optional[str]:
        key = frame_key(self.tree, code.co_filename, code.co_name)
        return key if key in self.wanted else None

    def record(self, event: str, key: str, payload: dict) -> None:
        self.seq += 1
        line = {"seq": self.seq, "event": event, "frame": key}
        if self.context is not None:
            line["test"] = self.context
        line.update(payload)
        self.out.write(json.dumps(line, default=repr) + "\n")

    def on_enter(self, key: str, frame: Any) -> None:
        info = inspect.getargvalues(frame)
        args = {n: summarise.summarise(info.locals[n]) for n in info.args if n in info.locals}
        if info.varargs and info.varargs in info.locals:
            args["*" + info.varargs] = summarise.summarise(info.locals[info.varargs])
        if info.keywords and info.keywords in info.locals:
            args["**" + info.keywords] = summarise.summarise(info.locals[info.keywords])
        self.record("enter", key, {"args": args})

    def on_return(self, key: str, value: Any) -> None:
        self.record("exit", key, {"return": summarise.summarise(value)})

    def on_raise(self, key: str, exc: BaseException) -> None:
        self.record("exit", key, {"raises": type(exc).__name__})

    def start(self) -> None:
        monitoring = getattr(sys, "monitoring", None)
        if monitoring is None:
            self._previous_trace = sys.gettrace()
            sys.settrace(self._settrace)
            return
        self._tool = monitoring.PROFILER_ID
        monitoring.use_tool_id(self._tool, "flowdiff")
        monitoring.register_callback(self._tool, monitoring.events.PY_START, self._py_start)
        monitoring.register_callback(self._tool, monitoring.events.PY_RETURN, self._py_return)
        monitoring.register_callback(self._tool, monitoring.events.PY_UNWIND, self._py_unwind)
        monitoring.set_events(self._tool, monitoring.events.PY_START | monitoring.events.PY_RETURN
                              | monitoring.events.PY_UNWIND)

    def stop(self) -> None:
        if self._tool is not None:
            sys.monitoring.set_events(self._tool, 0)
            sys.monitoring.free_tool_id(self._tool)
            self._tool = None
        else:
            sys.settrace(self._previous_trace)
        self.out.close()

    def _py_start(self, code: Any, offset: int) -> Any:
        key = self.key_of(code)
        if key is None:
            return sys.monitoring.DISABLE
        self.on_enter(key, sys._getframe(1))

    def _py_return(self, code: Any, offset: int, value: Any) -> Any:
        key = self.key_of(code)
        if key is None:
            return sys.monitoring.DISABLE
        self.on_return(key, value)

    def _py_unwind(self, code: Any, offset: int, exc: BaseException) -> None:
        # PY_UNWIND is not a local event: returning DISABLE here is a ValueError, unlike PY_START/PY_RETURN.
        key = self.key_of(code)
        if key is not None:
            self.on_raise(key, exc)

    def _settrace(self, frame: Any, event: str, arg: Any) -> Any:
        if event != "call":
            return None
        key = self.key_of(frame.f_code)
        if key is None:
            return None
        self.on_enter(key, frame)
        pending: list = []

        def local(frame: Any, event: str, arg: Any) -> Any:
            if event == "exception":
                pending[:] = [arg[1]]
            elif event == "return":
                # A frame leaving by exception reports return None; the pending exception says so.
                if arg is None and pending:
                    self.on_raise(key, pending[0])
                else:
                    self.on_return(key, arg)
            return local
        return local


class trace:
    def __init__(self, tree: str, frames: Iterable[str], out_path: str):
        self.tracer = Tracer(tree, frames, out_path)

    def __enter__(self) -> Tracer:
        self.tracer.start()
        return self.tracer

    def __exit__(self, *exc: Any) -> None:
        self.tracer.stop()
