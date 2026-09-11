"""Pair the base and head traces frame by frame; the verdict names the first divergence (decisions 36, 38)."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

MISSING = object()
BRIEF_LIMIT = 60


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


@dataclass(frozen=True)
class Divergence:
    frame: str
    index: int
    field: str
    base: Any
    head: Any
    at: int

    def describe(self) -> str:
        if self.base is MISSING:
            return f"call #{self.index + 1} only on head"
        if self.head is MISSING:
            return f"call #{self.index + 1} only on base"
        return f"{self.field} {brief(self.base)} → {brief(self.head)}"


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
                if (key in b or key in h) and b.get(key, MISSING) != h.get(key, MISSING):
                    out.append(Divergence(frame, i, key, b.get(key), h.get(key), head[i].observed_at(key)))
        return out

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
    return f"{len(differing)} of {len(traced)} frames differ; origin {short(origin.frame)}: {origin.describe()}"


def short(frame: str) -> str:
    return frame.rsplit(":", 1)[-1]


def resolve(report: Report, query: str) -> list[str]:
    """`name`, `path:name`, or `name/arg`; the arg part is stripped by the caller."""
    return [f for f in report.frames if f == query or short(f) == query]


def show(report: Report, frame: str, arg: str | None = None) -> str:
    """Values are cut to BRIEF_LIMIT unless one argument is asked for by name; that one is printed whole."""
    render = json.dumps if arg is not None else brief
    base, head = report.base.get(frame, []), report.head.get(frame, [])
    lines = [f"{frame}  base {len(base)} call(s), head {len(head)} call(s)"]
    for i in range(max(len(base), len(head))):
        b = base[i].values() if i < len(base) else {}
        h = head[i].values() if i < len(head) else {}
        keys = [k for k in b if k not in ("return", "raises")] + [k for k in h if k not in b and k not in ("return", "raises")]
        keys += [k for k in ("raises", "return") if k in b or k in h]
        if arg is not None:
            keys = [k for k in keys if k == arg]
        for k in keys:
            bv = render(b[k]) if k in b else "—"
            hv = render(h[k]) if k in h else "—"
            mark = "" if bv == hv else "  *"
            lines.append(f"  #{i + 1:<3} {k:<14} {bv:<28} {hv}{mark}")
    return "\n".join(lines)
