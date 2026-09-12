"""A gdb batch script that breakpoints the flow's frames, records arguments on entry and the return value on
exit, and writes the same JSON-lines records trace_py does (decisions 27, 28, 30, 31).

The script text runs inside gdb's Python, which is whatever the container ships (3.8 or later), so it
imports nothing from flowdiff and keeps to plain syntax.
"""
from __future__ import annotations

import json

SCRIPT = r'''
import gdb, json, hashlib, os, sys

TREE = %(tree)r
FRAMES = %(frames)r        # "rel/path.cpp:function" -> function
TESTS = %(tests)r          # function names whose start marks a test's calls
OUT = open(%(out)r, "w")
INLINE_LIMIT = 64
MAX_DEPTH = 3
seq = 0
context = None


def record(event, key, payload):
    global seq
    seq += 1
    line = {"seq": seq, "event": event, "frame": key}
    if context is not None:
        line["test"] = context
    line.update(payload)
    OUT.write(json.dumps(line, default=str) + "\n")
    OUT.flush()


def digest(data):
    return hashlib.sha256(data).hexdigest()[:16]


def type_name(t):
    return str(t.strip_typedefs()) if t.name is None else t.name


def summarise(v, depth=0):
    try:
        t = v.type.strip_typedefs()
        code = t.code
        if code == gdb.TYPE_CODE_REF or code == getattr(gdb, "TYPE_CODE_RVALUE_REF", -1):
            return summarise(v.referenced_value(), depth)
        if code == gdb.TYPE_CODE_BOOL:
            return bool(v)
        if code in (gdb.TYPE_CODE_INT, gdb.TYPE_CODE_CHAR):
            return int(v)
        if code == gdb.TYPE_CODE_FLT:
            return float(v)
        if code == gdb.TYPE_CODE_ENUM:
            return str(v)
        if code == gdb.TYPE_CODE_PTR:
            if int(v) == 0:
                return None
            target = t.target().strip_typedefs()
            if target.code in (gdb.TYPE_CODE_INT, gdb.TYPE_CODE_CHAR) and target.sizeof == 1:
                try:
                    return v.lazy_string(length=INLINE_LIMIT + 1).value().string(length=INLINE_LIMIT + 1)[:INLINE_LIMIT]
                except Exception:
                    return {"type": type_name(t)}
            if depth < MAX_DEPTH and target.code in (gdb.TYPE_CODE_STRUCT, gdb.TYPE_CODE_UNION):
                return summarise(v.dereference(), depth + 1)
            return {"type": type_name(t)}
        if code == gdb.TYPE_CODE_ARRAY:
            low, high = t.range()
            count = high - low + 1
            elem = t.target().strip_typedefs()
            if count <= INLINE_LIMIT and elem.code in (gdb.TYPE_CODE_INT, gdb.TYPE_CODE_FLT, gdb.TYPE_CODE_BOOL, gdb.TYPE_CODE_CHAR):
                return [summarise(v[i], depth + 1) for i in range(low, high + 1)]
            data = bytes(gdb.selected_inferior().read_memory(v.address, t.sizeof)) if v.address else b""
            return {"type": type_name(t), "len": count, "sha256": digest(data)}
        if code in (gdb.TYPE_CODE_STRUCT, gdb.TYPE_CODE_UNION):
            if depth >= MAX_DEPTH:
                return {"type": type_name(t)}
            fields = {}
            for f in t.fields():
                if f.is_base_class or not f.name or f.name.startswith("_vptr"):
                    continue
                if len(fields) >= INLINE_LIMIT:
                    break
                fields[f.name] = summarise(v[f.name], depth + 1)
            return {"type": type_name(t), "fields": fields}
        return {"type": type_name(t)}
    except Exception as exc:
        return {"type": "unreadable", "error": str(exc)[:80]}


def watch_target(v):
    """Only a pointer or reference argument can carry a callee's writes back to the caller."""
    try:
        t = v.type.strip_typedefs()
        if t.code in (gdb.TYPE_CODE_REF, getattr(gdb, "TYPE_CODE_RVALUE_REF", -1)):
            return ("ref", v.referenced_value().address)
        if t.code == gdb.TYPE_CODE_PTR and int(v) != 0:
            return ("ptr", v)
        return None
    except Exception:
        return None


def after_values(watched):
    out = {}
    for name, pair in watched.items():
        kind, v = pair
        try:
            out[name] = summarise(v.dereference() if kind == "ref" else v)
        except Exception:
            continue
    return out


class Exit(gdb.FinishBreakpoint):
    def __init__(self, frame, key, watched):
        super().__init__(frame, internal=True)
        self.key = key
        self.watched = watched

    def stop(self):
        value = self.return_value
        payload = {"return": None if value is None else summarise(value)}
        after = after_values(self.watched)
        if after:
            payload["after"] = after
        record("exit", self.key, payload)
        return False

    def out_of_scope(self):
        record("exit", self.key, {"raises": "unwound"})


class Enter(gdb.Breakpoint):
    def __init__(self, spec, key):
        super().__init__(spec, internal=True)
        self.key = key

    def stop(self):
        frame = gdb.selected_frame()
        args = {}
        watched = {}
        try:
            block = frame.block()
            while block is not None and not block.function:
                block = block.superblock
            if block is not None:
                for sym in block:
                    if sym.is_argument:
                        value = sym.value(frame)
                        args[sym.name] = summarise(value)
                        target = watch_target(value)
                        if target is not None:
                            watched[sym.name] = target
        except Exception as exc:
            args["<unreadable>"] = str(exc)[:80]
        record("enter", self.key, {"args": args})
        try:
            Exit(frame, self.key, watched)
        except Exception:
            pass
        return False


class TestStart(gdb.Breakpoint):
    def __init__(self, name):
        super().__init__(name, internal=True)
        self.name = name

    def stop(self):
        global context
        context = TESTS[self.name]
        return False


gdb.execute("set breakpoint pending on")   # the frames live in shared libraries not loaded before run
for key, function in FRAMES.items():
    source = key.split(":", 1)[0]          # C++ names carry ::, so split at the path's colon
    try:
        Enter("-source %%s -function %%s" %% (os.path.basename(source), function), key)
    except gdb.error as exc:
        record("unresolved", key, {"error": str(exc)})
for name in TESTS:
    try:
        TestStart(name)
    except gdb.error:
        pass

gdb.execute("set pagination off")
gdb.execute("set print pretty off")
gdb.execute("run")
OUT.close()
'''


def script(tree: str, frames: dict[str, str], tests: dict[str, str], out: str) -> str:
    """frames: "rel/path.cpp:name" -> function name to break on; tests: symbol -> test id it marks."""
    return SCRIPT % {"tree": tree, "frames": frames, "tests": tests, "out": out}


def gdb_command(script_path: str, executable: str, args: list[str] = []) -> list[str]:
    return ["gdb", "-batch", "-q", "-nx", "-x", script_path, "--args", executable, *args]


def read_records(text: str) -> list[dict]:
    return [json.loads(line) for line in text.splitlines() if line.startswith("{")]
