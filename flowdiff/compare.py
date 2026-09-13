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
    test: str | None = None
    # Watched arguments summarised again at exit; only a caller-visible mutation makes one differ.
    after: dict[str, Any] = field(default_factory=dict)
    thread: int | None = None

    def mutated(self) -> dict[str, Any]:
        return {n: v for n, v in self.after.items() if n in self.args and v != self.args[n]}

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
            call = Call(rec["seq"], rec.get("args", {}), test=rec.get("test"), thread=rec.get("thread"))
            calls.setdefault(frame, []).append(call)
            open_calls.setdefault(frame, []).append(call)
        elif open_calls.get(frame):
            call = open_calls[frame].pop()
            call.end = rec["seq"]
            call.after = rec.get("after", {})
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


def within_tolerance(base: Any, head: Any, abs_tol: float | None, rel_tol: float | None) -> bool:
    """At least one side is a float within the stated budget (decision: no tolerance without a stated reason,
    so exact unless a budget is given, and scoped to floats — two plain ints stay exact); bool is not numeric
    here even though it subclasses int. Either budget alone is enough; both widen the bound, they never narrow it."""
    if abs_tol is None and rel_tol is None:
        return False
    if isinstance(base, bool) or isinstance(head, bool):
        return False
    if not isinstance(base, (int, float)) or not isinstance(head, (int, float)):
        return False
    if not isinstance(base, float) and not isinstance(head, float):
        return False
    bound = max(abs_tol or 0.0, (rel_tol or 0.0) * max(abs(base), abs(head)))
    return abs(head - base) <= bound


def is_marker(value: Any) -> bool:
    """A summariser stand-in for something not recorded whole: {"type": T} alone, or with len/shape and a hash."""
    return isinstance(value, dict) and "type" in value and "fields" not in value


def is_volatile(value: Any) -> bool:
    """A marker for something the summariser knows varies by itself between runs: a timestamp, a uuid, an address."""
    return isinstance(value, dict) and value.get("volatile") is True


def pretty(value: Any) -> str:
    if value is MISSING:
        return "—"
    if is_marker(value):
        if is_volatile(value):
            return f"<{value['type']} volatile>"
        digest = f"#{value['sha256'][:8]}" if "sha256" in value else ""
        if "shape" in value:
            return f"{value['type']}{value['shape']} {value.get('dtype', '')}{digest}".rstrip()
        if "len" in value:
            return f"{value['type']}[{value['len']}]{digest}"
        return f"<{value['type']}>"
    if is_object(value):
        return f"{value['type'].rsplit('.', 1)[-1]}(" + ", ".join(f"{k}={pretty(v)}" for k, v in value["fields"].items()) + ")"
    if isinstance(value, list):
        return "[" + ", ".join(pretty(v) for v in value) + "]"
    if isinstance(value, dict):
        return "{" + ", ".join(f"{k}: {pretty(v)}" for k, v in value.items()) + "}"
    return json.dumps(value)


def value_text(value: Any, whole: bool = False) -> str:
    text = pretty(value)
    return text if whole or len(text) <= BRIEF_LIMIT else text[:BRIEF_LIMIT - 1] + "…"


def flatten(value: Any, path: str = "") -> list[tuple[str, Any]]:
    """Every leaf of a summarised value with its path; objects' fields are folded, lists of leaves stay one leaf."""
    if is_object(value):
        return flatten(value["fields"], path) if value["fields"] else [(path, {"type": value["type"]})]
    if isinstance(value, dict) and not is_marker(value) and value:
        return [leaf for k, v in value.items() for leaf in flatten(v, f"{path}.{k}" if path else k)]
    if isinstance(value, list) and any(is_object(v) or isinstance(v, list) or (isinstance(v, dict) and not is_marker(v))
                                       for v in value):
        return [leaf for i, v in enumerate(value) for leaf in flatten(v, f"{path}[{i}]")]
    return [(path, value)]


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
    # Opt-in numeric budget (decisions on floats): unset means exact, as everywhere else.
    float_tol: float | None = None
    float_rtol: float | None = None

    def _leaf_divergences(self, frame: str) -> list[Divergence]:
        """Every differing leaf for a frame, volatile ones included; divergences()/volatile() split by is_volatile.
        A float leaf within the stated tolerance is not a divergence, even though `show` still lists it (leaves()
        itself stays exact) so a drift that stays inside the budget is visible rather than erased."""
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
                            for path, lb, lh in leaves(b.get(key, MISSING), h.get(key, MISSING), key)
                            if not within_tolerance(lb, lh, self.float_tol, self.float_rtol)]
        return out

    def divergences(self, frame: str) -> list[Divergence]:
        """Timed by the head trace where the call exists there, so the origin is the first value that differed."""
        return [d for d in self._leaf_divergences(frame) if not is_volatile(d.base) and not is_volatile(d.head)]

    def volatile(self, frame: str) -> list[Divergence]:
        """Leaves divergences() drops because one side is a marker for something known to vary on its own."""
        return [d for d in self._leaf_divergences(frame) if is_volatile(d.base) or is_volatile(d.head)]

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

    def unstable(self, frame: str) -> bool:
        """A frame entered from more than one thread, on either side: recorded call order there is not
        something the trace can trust — scheduling, not the diff, may have picked it."""
        return any(len({c.thread for c in calls if c.thread is not None}) > 1
                  for calls in (self.base.get(frame, []), self.head.get(frame, [])))


def thread_warnings(report: Report) -> list[str]:
    """One line per traced frame entered from more than one thread: the recorded call order there is not
    something the diff can trust, since scheduling rather than the code may have picked it."""
    return [f"{short(f)}: entered from more than one thread; call order is not stable"
           for f in report.traced() if report.unstable(f)]


def volatility_note(report: Report) -> str:
    """Names leaves divergences() excluded as volatile, deduped, so a quiet verdict is not mistaken for untouched."""
    excluded: list[tuple[str, str]] = []
    for f in report.traced():
        for d in report.volatile(f):
            pair = (f, d.field)
            if pair not in excluded:
                excluded.append(pair)
    if not excluded:
        return ""
    shown = ", ".join(f"{short(f)}.{path}" for f, path in excluded[:3])
    more = f", +{len(excluded) - 3} more" if len(excluded) > 3 else ""
    return f"; excluded as volatile: {shown}{more}"


def tolerance_note(report: Report) -> str:
    if report.float_tol is None and report.float_rtol is None:
        return ""
    parts = [f"{kind}={value:g}" for kind, value in (("abs", report.float_tol), ("rel", report.float_rtol))
            if value is not None]
    return f"  (float tolerance {', '.join(parts)})"


def verdict(report: Report) -> str:
    traced = report.traced()
    if not traced:
        return "no flow frame was reached: the harness ran but traced nothing"
    note = tolerance_note(report) + volatility_note(report)
    differing = report.differing()
    if not differing:
        return f"identical: {len(traced)} frames traced, no value differs{note}"
    origin = report.origin()
    assert origin is not None
    where = "; ".join(f"{short(f)} ({report.changed_paths(f)})" for f in differing)
    return f"{len(differing)} of {len(traced)} frames differ: {where}; origin {short(origin.frame)}{note}"


def short(frame: str) -> str:
    """The symbol without its path: the part after the path's colon, so C++ Class::method survives whole."""
    return frame.split(":", 1)[1] if ":" in frame else frame


def resolve(report: Report, query: str) -> list[str]:
    """A frame id, its symbol, or the symbol's last :: component; the /arg part is stripped by the caller."""
    return [f for f in report.frames if query in (f, short(f), short(f).rsplit("::", 1)[-1])]


def call_rows(b: dict[str, Any], h: dict[str, Any], path: str | None) -> list[tuple[str, str, str, bool]]:
    """Differing leaves of one call, or every leaf under `path` printed whole when a path is asked for.

    The fourth element marks a leaf volatile: one side's value is known to vary on its own between runs.
    """
    keys = [k for k in b if k not in ("return", "raises")] + [k for k in h if k not in b and k not in ("return", "raises")]
    keys += [k for k in ("raises", "return") if k in b or k in h]
    rows = []
    for key in keys:
        if path is None:
            rows += [(p, value_text(lb), value_text(lh), is_volatile(lb) or is_volatile(lh))
                     for p, lb, lh in leaves(b.get(key, MISSING), h.get(key, MISSING), key)]
        elif path == key or path.startswith(key + ".") or path.startswith(key + "["):
            bv, hv = descend(b.get(key, MISSING), path[len(key):]), descend(h.get(key, MISSING), path[len(key):])
            rows.append((path, pretty(bv), pretty(hv), is_volatile(bv) or is_volatile(hv)))
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


MAX_TESTS_IN_LABEL = 2


def tests_of(report: Report, frame: str, indices: list[int]) -> str:
    """The tests that drove these calls, from either side, or empty when no test drove them."""
    names: list[str] = []
    for i in indices:
        for side in (report.head, report.base):
            calls = side.get(frame, [])
            test = calls[i].test if i < len(calls) else None
            if test and test not in names:
                names.append(test)
    if not names:
        return ""
    shown = ", ".join(names[:MAX_TESTS_IN_LABEL])
    return "  ← " + shown + (f", +{len(names) - MAX_TESTS_IN_LABEL} more" if len(names) > MAX_TESTS_IN_LABEL else "")


def table(rows: list[tuple[str, str, str, bool]]) -> list[str]:
    width = min(max(len(r[0]) for r in rows), PATH_COLUMN)
    return [f"    {p:<{width}}  {bv}  →  {hv}" + ("  (volatile)" if volatile else "")
            for p, bv, hv, volatile in rows]


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
        count = f" (×{len(indices)})" if len(indices) > 1 else ""
        lines.append(f"  calls {call_labels(indices)}{count}{tests_of(report, frame, indices)}")
        lines += table(list(rows))
    if identical:
        lines.append(f"  {len(identical)} identical call(s): {call_labels(identical)}")
    return "\n".join(lines)


MAX_CALL_ROWS = 60


def show_call(report: Report, frame: str, index: int, path: str | None = None) -> str:
    """One call as a flat leaf table: identical leaves as one value, differing ones as base → head *."""
    base, head = report.base.get(frame, []), report.head.get(frame, [])
    if index >= max(len(base), len(head)):
        return f"{frame}  has no call #{index + 1}"
    b = base[index].values() if index < len(base) else {}
    h = head[index].values() if index < len(head) else {}
    keys = [k for k in b if k not in ("return", "raises")] + [k for k in h if k not in b and k not in ("return", "raises")]
    keys += [k for k in ("raises", "return") if k in b or k in h]
    rows: list[tuple[str, str]] = []
    for key in keys:
        if path is not None and not (path == key or path.startswith(key + ".") or path.startswith(key + "[")):
            continue
        rest = path[len(key):] if path is not None else ""
        bl = dict(flatten(descend(b.get(key, MISSING), rest), path or key))
        hl = dict(flatten(descend(h.get(key, MISSING), rest), path or key))
        for leaf in list(hl) + [p for p in bl if p not in hl]:
            bv, hv = bl.get(leaf, MISSING), hl.get(leaf, MISSING)
            if bv == hv:
                text = pretty(bv)
            elif is_volatile(bv) or is_volatile(hv):
                text = f"{pretty(bv)}  →  {pretty(hv)}   (volatile)"
            else:
                text = f"{pretty(bv)}  →  {pretty(hv)}   *"
            rows.append((leaf, text))
    lines = [f"{frame}  call #{index + 1}{tests_of(report, frame, [index])}"]
    if not rows:
        return "\n".join(lines + [f"    {path}: not an argument of this call"])
    width = min(max(len(r[0]) for r in rows), PATH_COLUMN)
    lines += [f"    {p:<{width}}  {text}" for p, text in rows[:MAX_CALL_ROWS]]
    if len(rows) > MAX_CALL_ROWS:
        lines.append(f"    … +{len(rows) - MAX_CALL_ROWS} more leaves; narrow with {short(frame)}#{index + 1}/path")
    return "\n".join(lines)
