"""Pair the base and head traces frame by frame; the verdict names the first divergence (decisions 36, 38)."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

MISSING = object()
BRIEF_LIMIT = 60
PATH_COLUMN = 40


def brief(value: Any) -> str:
    text = json.dumps(value)
    return text if len(text) <= BRIEF_LIMIT else text[:BRIEF_LIMIT - 1] + "…"


@dataclass
class Call:
    seq: int
    args: dict[str, Any] = field(default_factory=dict)
    result: Any = MISSING
    raises: str | None = None
    end: int | None = None

    def observed_at(self, field_name: str) -> int:
        return self.end if field_name in ("return", "raises") and self.end is not None else self.seq

    def values(self) -> dict[str, Any]:
        out = dict(self.args)
        if self.raises is not None:
            out["raises"] = self.raises
        elif self.result is not MISSING:
            out["return"] = self.result
        return out


def load(path: Path) -> dict[str, list[Call]]:
    """Entry and exit records folded into one Call per invocation; recursion pairs by nesting."""
    calls: dict[str, list[Call]] = {}
    open_calls: dict[str, list[Call]] = {}
    if not path.exists():
        return calls
    for line in path.read_text().splitlines():
        rec = json.loads(line)
        frame = rec["frame"]
        if rec["event"] == "enter":
            call = Call(rec["seq"], rec.get("args", {}))
            calls.setdefault(frame, []).append(call)
            open_calls.setdefault(frame, []).append(call)
        elif open_calls.get(frame):
            call = open_calls[frame].pop()
            call.end = rec["seq"]
            if "raises" in rec:
                call.raises = rec["raises"]
            else:
                call.result = rec.get("return")
    return calls


def is_object(value: Any) -> bool:
    return isinstance(value, dict) and "type" in value and "fields" in value


def leaves(base: Any, head: Any, path: str = "") -> list[tuple[str, Any, Any]]:
    """Differing leaves of two summarised values as (path, base, head); an object's fields level is folded away."""
    if is_object(base) and is_object(head) and base["type"] == head["type"]:
        return leaves(base["fields"], head["fields"], path)
    if isinstance(base, dict) and isinstance(head, dict) and not is_object(base) and not is_object(head):
        out: list[tuple[str, Any, Any]] = []
        for key in list(base) + [k for k in head if k not in base]:
            sub = f"{path}.{key}" if path else key
            out += leaves(base.get(key, MISSING), head.get(key, MISSING), sub)
        return out
    if isinstance(base, list) and isinstance(head, list) and len(base) == len(head):
        return [leaf for i, (b, h) in enumerate(zip(base, head)) for leaf in leaves(b, h, f"{path}[{i}]")]
    return [] if base == head else [(path, base, head)]


def value_text(value: Any, whole: bool = False) -> str:
    if value is MISSING:
        return "—"
    return json.dumps(value) if whole else brief(value)


@dataclass(frozen=True)
class Divergence:
    frame: str
    index: int
    field: str
    base: Any
    head: Any
    at: int

    def describe(self) -> str:
        if self.field == "call":
            return f"call #{self.index + 1} only on {'head' if self.base is MISSING else 'base'}"
        return f"{self.field} {value_text(self.base)} → {value_text(self.head)}"


@dataclass
class Report:
    frames: list[str]
    base: dict[str, list[Call]]
    head: dict[str, list[Call]]
    # Added and removed frames: calls on one side only are what the marker already says, not a divergence.
    one_sided: set[str] = field(default_factory=set)

    def divergences(self, frame: str) -> list[Divergence]:
        """Timed by the head trace where the call exists there, so the origin is the first value that differed."""
        base, head = self.base.get(frame, []), self.head.get(frame, [])
        out = []
        for i in range(max(len(base), len(head))):
            if i >= len(base) or i >= len(head):
                if frame in self.one_sided:
                    continue
                present = head[i] if i < len(head) else base[i]
                out.append(Divergence(frame, i, "call", MISSING if i >= len(base) else base[i].values(),
                                      MISSING if i >= len(head) else head[i].values(), present.seq))
                continue
            b, h = base[i].values(), head[i].values()
            for key in [k for k in b if k not in ("return", "raises")] + ["raises", "return"]:
                if key in b or key in h:
                    out += [Divergence(frame, i, path, lb, lh, head[i].observed_at(key))
                            for path, lb, lh in leaves(b.get(key, MISSING), h.get(key, MISSING), key)]
        return out

    def changed_paths(self, frame: str, limit: int = 3) -> str:
        paths: list[str] = []
        for d in self.divergences(frame):
            text = d.describe() if d.field == "call" else d.field
            if text not in paths:
                paths.append(text)
        shown = ", ".join(paths[:limit])
        return shown + (f", +{len(paths) - limit} more" if len(paths) > limit else "")

    def traced(self) -> list[str]:
        return [f for f in self.frames if f in self.base or f in self.head]

    def differing(self) -> list[str]:
        return [f for f in self.frames if self.divergences(f)]

    def origin(self) -> Divergence | None:
        found = [d for f in self.differing() for d in self.divergences(f)]
        return min(found, key=lambda d: d.at) if found else None


def verdict(report: Report) -> str:
    traced = report.traced()
    if not traced:
        return "no flow frame was reached: the harness ran but traced nothing"
    differing = report.differing()
    if not differing:
        return f"identical: {len(traced)} frames traced, no value differs"
    origin = report.origin()
    assert origin is not None
    where = "; ".join(f"{short(f)} ({report.changed_paths(f)})" for f in differing)
    return f"{len(differing)} of {len(traced)} frames differ: {where}; origin {short(origin.frame)}"


def short(frame: str) -> str:
    return frame.rsplit(":", 1)[-1]


def resolve(report: Report, query: str) -> list[str]:
    """`name`, `path:name`, or `name/arg`; the arg part is stripped by the caller."""
    return [f for f in report.frames if f == query or short(f) == query]


def call_rows(b: dict[str, Any], h: dict[str, Any], path: str | None) -> list[tuple[str, str, str]]:
    """Differing leaves of one call, or every leaf under `path` printed whole when a path is asked for."""
    keys = [k for k in b if k not in ("return", "raises")] + [k for k in h if k not in b and k not in ("return", "raises")]
    keys += [k for k in ("raises", "return") if k in b or k in h]
    rows = []
    for key in keys:
        if path is None:
            rows += [(p, value_text(lb), value_text(lh)) for p, lb, lh in leaves(b.get(key, MISSING), h.get(key, MISSING), key)]
        elif path == key or path.startswith(key + ".") or path.startswith(key + "["):
            bv, hv = descend(b.get(key, MISSING), path[len(key):]), descend(h.get(key, MISSING), path[len(key):])
            rows.append((path, value_text(bv, whole=True), value_text(hv, whole=True)))
    return rows


def descend(value: Any, rest: str) -> Any:
    """Follow `.field` and `[index]` steps of a leaf path, through an object's fields level."""
    for step in re.findall(r"\.([^.\[]+)|\[(\d+)\]", rest):
        if value is MISSING:
            return MISSING
        if is_object(value):
            value = value["fields"]
        if step[0]:
            value = value.get(step[0], MISSING) if isinstance(value, dict) else MISSING
        else:
            index = int(step[1])
            value = value[index] if isinstance(value, list) and index < len(value) else MISSING
    return value


def call_labels(indices: list[int]) -> str:
    """#1,#3-#6 for calls 0, 2, 3, 4, 5."""
    runs: list[list[int]] = []
    for i in indices:
        if runs and runs[-1][-1] == i - 1:
            runs[-1].append(i)
        else:
            runs.append([i])
    return ",".join(f"#{r[0] + 1}" if len(r) == 1 else f"#{r[0] + 1}-#{r[-1] + 1}" for r in runs)


def show(report: Report, frame: str, path: str | None = None) -> str:
    base, head = report.base.get(frame, []), report.head.get(frame, [])
    lines = [f"{frame}  base {len(base)} call(s), head {len(head)} call(s)"]
    groups: dict[tuple, list[int]] = {}
    for i in range(max(len(base), len(head))):
        b = base[i].values() if i < len(base) else {}
        h = head[i].values() if i < len(head) else {}
        groups.setdefault(tuple(call_rows(b, h, path)), []).append(i)
    identical = groups.pop((), [])
    for rows, indices in groups.items():
        lines.append(f"  calls {call_labels(indices)}" + (f" (×{len(indices)})" if len(indices) > 1 else ""))
        width = min(max(len(r[0]) for r in rows), PATH_COLUMN)
        lines += [f"    {p:<{width}}  {bv}  →  {hv}" for p, bv, hv in rows]
    if identical:
        lines.append(f"  {len(identical)} identical call(s): {call_labels(identical)}")
    return "\n".join(lines)
