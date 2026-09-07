"""Run mutmut over the modules a branch touches and fail below a mutation-score threshold."""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

PACKAGE = Path("flowdiff")


def touched_modules(base: str) -> list[str]:
    out = subprocess.run(["git", "diff", "--name-only", f"{base}...HEAD", "--", f"{PACKAGE}/*.py"],
                         check=True, capture_output=True, text=True).stdout
    return sorted(p for p in out.split() if Path(p).is_file() and Path(p).name != "__init__.py")


def run_mutmut(paths: list[str]) -> ET.Element:
    subprocess.run(["mutmut", "run", "--paths-to-mutate", ",".join(paths), "--tests-dir", "tests",
                    "--runner", f"{sys.executable} -m pytest -x -q tests", "--no-progress"], check=False)
    xml = subprocess.run(["mutmut", "junitxml"], check=True, capture_output=True, text=True).stdout
    return ET.fromstring(xml)


def scores(report: ET.Element) -> dict[str, tuple[int, int]]:
    """path -> (killed, total); a testcase with a failure child is a surviving mutant."""
    per_file: dict[str, list[int]] = {}
    for case in report.iter("testcase"):
        path = case.get("file") or case.get("name", "").split(":")[0]
        killed = case.find("failure") is None and case.find("error") is None
        per_file.setdefault(path, [0, 0])
        per_file[path][0] += int(killed)
        per_file[path][1] += 1
    return {p: (k, t) for p, (k, t) in per_file.items()}


def survivor_ids(results_text: str) -> list[int]:
    """`mutmut results` lists survivors as '1-3, 7, 12'; expand to ids."""
    ids: list[int] = []
    for line in results_text.splitlines():
        if not re.fullmatch(r"[\d,\s-]+", line.strip()) or not line.strip():
            continue
        for part in line.replace(" ", "").split(","):
            lo, _, hi = part.partition("-")
            if lo.isdigit():
                ids.extend(range(int(lo), int(hi or lo) + 1))
    return ids


def show_survivors(limit: int = 60) -> None:
    results = subprocess.run(["mutmut", "results"], capture_output=True, text=True).stdout
    print(results)
    for mid in survivor_ids(results)[:limit]:
        print(subprocess.run(["mutmut", "show", str(mid)], capture_output=True, text=True).stdout)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--base", default="origin/main")
    ap.add_argument("--threshold", type=float, default=80.0)
    ap.add_argument("--paths", nargs="*", help="override the touched-module detection")
    args = ap.parse_args(argv)

    paths = args.paths or touched_modules(args.base)
    if not paths:
        print("mutation gate: no package modules touched")
        return 0
    report = run_mutmut(paths)
    failed = False
    for path, (killed, total) in sorted(scores(report).items()):
        score = 100.0 * killed / total if total else 100.0
        verdict = "ok" if score >= args.threshold else "BELOW THRESHOLD"
        failed |= score < args.threshold
        print(f"{path:<32} {killed:>4}/{total:<4} {score:6.1f}%  {verdict}")
    if failed:
        show_survivors()
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
