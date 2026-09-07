"""Run mutmut per touched module, in parallel throwaway copies, and fail below a score threshold."""
from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

PACKAGE = Path("flowdiff")
# Rewording help text, log lines and error messages is not a defect; pinning that prose in a
# test would lock wording rather than behaviour.
DISABLED_MUTATIONS = "string,fstring"
_LOCAL_MUTMUT = Path(sys.executable).with_name("mutmut")
MUTMUT = str(_LOCAL_MUTMUT) if _LOCAL_MUTMUT.exists() else "mutmut"


def touched_modules(base: str) -> list[str]:
    out = subprocess.run(["git", "diff", "--name-only", f"{base}...HEAD", "--", f"{PACKAGE}/*.py"],
                         check=True, capture_output=True, text=True).stdout
    return sorted(p for p in out.split() if Path(p).is_file() and Path(p).name != "__init__.py")


def tests_for(module: str) -> str:
    """A module's own test files, so a mutant does not pay for the whole suite."""
    stem = Path(module).stem
    matches = sorted(str(p) for p in Path("tests").glob(f"test_{stem}*.py"))
    return " ".join(matches) if matches else "tests"


def isolated_copy(dest: Path) -> None:
    """mutmut rewrites sources in place, so it only ever runs on a throwaway copy."""
    listing = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard"],
                             check=True, capture_output=True, text=True)
    for name in listing.stdout.split():
        target = dest / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(name, target)


def mutmut_in(workdir: Path, *args: str, check: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run([MUTMUT, *args], check=check, capture_output=True, text=True, cwd=workdir)


def scores(report: ET.Element) -> dict[str, tuple[int, int]]:
    """path -> (killed, total); a testcase with a failure or error child is a surviving mutant."""
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
        if not line.strip() or not re.fullmatch(r"[\d,\s-]+", line.strip()):
            continue
        for part in line.replace(" ", "").split(","):
            lo, _, hi = part.partition("-")
            if lo.isdigit():
                ids.extend(range(int(lo), int(hi or lo) + 1))
    return ids


def survivor_report(workdir: Path, limit: int) -> str:
    results = mutmut_in(workdir, "results").stdout
    shown = [mutmut_in(workdir, "show", str(mid)).stdout for mid in survivor_ids(results)[:limit]]
    return "\n".join([results, *shown])


class Result:
    def __init__(self, module: str, counts: dict[str, tuple[int, int]], survivors: str,
                 seconds: float, log: str = ""):
        self.module = module
        self.counts = counts
        self.survivors = survivors
        self.seconds = seconds
        self.log = log

    @property
    def killed(self) -> int:
        return sum(k for k, _ in self.counts.values())

    @property
    def total(self) -> int:
        return sum(t for _, t in self.counts.values())

    def score(self) -> float:
        return 100.0 * self.killed / self.total if self.total else 0.0

    def passed(self, threshold: float) -> bool:
        """No mutants means mutmut never ran; that is a broken gate, never a pass."""
        return self.total > 0 and self.score() >= threshold

    def line(self, threshold: float) -> str:
        if self.total == 0:
            return f"{self.module:<24} {'no mutants generated — mutmut failed':<28} {self.seconds:5.0f}s  ERROR"
        verdict = "ok" if self.passed(threshold) else "BELOW THRESHOLD"
        return f"{self.module:<24} {self.killed:>4}/{self.total:<4} {self.score():6.1f}%  {self.seconds:5.0f}s  {verdict}"


def measure(module: str, threshold: float, survivor_limit: int) -> Result:
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="flowdiff-mutants-") as tmp:
        workdir = Path(tmp)
        isolated_copy(workdir)
        runner = f"{sys.executable} -m pytest -x -q -p no:cacheprovider {tests_for(module)}"
        run = mutmut_in(workdir, "run", "--paths-to-mutate", module, "--tests-dir", "tests",
                        "--runner", runner, "--no-progress",
                        "--disable-mutation-types", DISABLED_MUTATIONS)
        counts = scores(ET.fromstring(mutmut_in(workdir, "junitxml", check=True).stdout))
        elapsed = time.monotonic() - started
        result = Result(module, counts, "", elapsed, log=(run.stdout + run.stderr).strip())
        if result.total and not result.passed(threshold):
            result.survivors = survivor_report(workdir, survivor_limit)
        return result


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--base", default="origin/main")
    ap.add_argument("--threshold", type=float, default=80.0)
    ap.add_argument("--paths", nargs="*", help="override the touched-module detection")
    ap.add_argument("--jobs", type=int, default=4, help="modules measured concurrently")
    ap.add_argument("--survivors", type=int, default=40, help="surviving mutants to print per module")
    args = ap.parse_args(argv)

    paths = args.paths or touched_modules(args.base)
    if not paths:
        print("mutation gate: no package modules touched")
        return 0

    print(f"mutating {len(paths)} module(s) with {min(args.jobs, len(paths))} job(s); "
          f"threshold {args.threshold:.0f}%", flush=True)
    for module in paths:
        print(f"  {module} against {tests_for(module)}", flush=True)

    results: list[Result] = []
    with ThreadPoolExecutor(max_workers=max(1, min(args.jobs, len(paths)))) as pool:
        futures = {pool.submit(measure, m, args.threshold, args.survivors): m for m in paths}
        for future in as_completed(futures):
            result = future.result()
            results.append(result)
            print(result.line(args.threshold), flush=True)

    failed = [r for r in results if not r.passed(args.threshold)]
    for r in failed:
        if r.total == 0:
            print(f"\n=== mutmut produced no mutants for {r.module} ===\n{r.log[-3000:]}")
        else:
            print(f"\n=== surviving mutants in {r.module} ===\n{r.survivors}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
