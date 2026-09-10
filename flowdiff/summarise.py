"""Values as JSON: scalars verbatim, sized things as shape, hash and stats (decisions 31 to 33).

Imported by the harness inside the project's interpreter, so this module stays compatible with
Python 3.8 and imports nothing from the rest of flowdiff.
"""
from __future__ import annotations

import hashlib
import importlib.util
import math
import os
from typing import Any, Callable, Optional

INLINE_LIMIT = 64
MAX_DEPTH = 4

_project: Optional[Callable[[str, Any], Optional[dict]]] = None
# The base worktree and the working tree differ in prefix only, so strings under either compare as
# <tree>/...; the scratch dir holds the base tree and sits in the working tree, so the longest prefix wins.
_prefixes: list = []


def load_project_summariser(tree: str, scratch: Optional[str] = None) -> None:
    global _project, _prefixes
    _prefixes = [(os.path.realpath(tree), "<tree>")]
    if scratch:
        _prefixes.append((os.path.realpath(scratch), "<scratch>"))
    _prefixes.sort(key=lambda p: -len(p[0]))
    path = os.path.join(tree, ".flowdiff", "summarisers.py")
    if not os.path.exists(path):
        _project = None
        return
    spec = importlib.util.spec_from_file_location("flowdiff_project_summarisers", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    _project = getattr(module, "summarise", None)


def type_name(value: Any) -> str:
    cls = type(value)
    return cls.__qualname__ if cls.__module__ == "builtins" else cls.__module__ + "." + cls.__qualname__


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:16]


def stats(items: list) -> dict:
    numbers = [x for x in items if isinstance(x, (int, float)) and not isinstance(x, bool)]
    if not numbers or len(numbers) != len(items):
        return {}
    return {"min": min(numbers), "max": max(numbers), "mean": sum(numbers) / len(numbers)}


def sized(value: Any, items: list) -> dict:
    out = {"type": type_name(value), "len": len(items), "sha256": digest(repr(items).encode())}
    out.update(stats(items))
    return out


def summarise(value: Any, depth: int = 0) -> Any:
    if _project is not None:
        custom = _project(type_name(value), value)
        if custom is not None:
            return custom
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else repr(value)
    if isinstance(value, str):
        for prefix, label in _prefixes:
            if value.startswith(prefix + os.sep):
                value = label + value[len(prefix):]
                break
        return value if len(value) <= INLINE_LIMIT else {"type": "str", "len": len(value),
                                                          "sha256": digest(value.encode("utf-8", "replace"))}
    if isinstance(value, (bytes, bytearray)):
        return repr(bytes(value)) if len(value) <= INLINE_LIMIT else {"type": type_name(value), "len": len(value),
                                                                      "sha256": digest(bytes(value))}
    if depth >= MAX_DEPTH:
        return {"type": type_name(value)}
    if isinstance(value, (set, frozenset)):
        value = sorted(value, key=repr)
    if isinstance(value, (list, tuple)):
        items = list(value)
        return [summarise(v, depth + 1) for v in items] if len(items) <= INLINE_LIMIT else sized(value, items)
    if isinstance(value, dict):
        if len(value) <= INLINE_LIMIT:
            return {str(k): summarise(v, depth + 1) for k, v in value.items()}
        return sized(value, list(value.values()))
    if hasattr(value, "shape") and hasattr(value, "tobytes") and hasattr(value, "dtype"):
        return array_summary(value)
    fields = getattr(value, "__dict__", None)
    if isinstance(fields, dict) and len(fields) <= INLINE_LIMIT:
        return {"type": type_name(value), "fields": {k: summarise(v, depth + 1) for k, v in fields.items()}}
    return {"type": type_name(value)}


def array_summary(array: Any) -> dict:
    out = {"type": type_name(array), "shape": list(array.shape), "dtype": str(array.dtype),
           "sha256": digest(array.tobytes())}
    try:
        out.update({"min": float(array.min()), "max": float(array.max()), "mean": float(array.mean())})
    except (TypeError, ValueError):
        pass
    return out
