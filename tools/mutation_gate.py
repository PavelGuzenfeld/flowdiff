"""Run mutmut per touched module, in parallel throwaway copies, and fail below a score threshold."""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
import time
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


STATUS_CLASSES = ("killed", "timeout", "suspicious", "survived", "skipped", "untested")
# A mutant that hangs the suite is caught, not missed: mutmut's own legend counts a timeout
# as killed. Reading junitxml instead reports those as errors, which understates the score.
KILLING_CLASSES = ("killed", "timeout", "suspicious")

# A protocol conversation with an external process, replaced by a fake in these modules' own tests:
# the score measures the fake's fidelity, not the tests'. Reasoning and measured scores in #34.
SCORE_EXEMPT = frozenset({"flowdiff/lsp.py", "flowdiff/container.py", "flowdiff/harness_cpp.py"})


def status_counts(workdir: Path) -> dict[str, int]:
    counts = {}
    for name in STATUS_CLASSES:
        out = mutmut_in(workdir, "result-ids", name).stdout
        counts[name] = len(out.split())
    return counts


def survivor_report(workdir: Path, limit: int) -> str:
    ids = mutmut_in(workdir, "result-ids", "survived").stdout.split()
    return "\n".join(mutmut_in(workdir, "show", mid).stdout for mid in ids[:limit])


class Result:
    def __init__(self, module: str, counts: dict[str, int], survivors: str,
                 seconds: float, log: str = ""):
        self.module = module
        self.exempt = module in SCORE_EXEMPT
        self.counts = counts
        self.survivors = survivors
        self.seconds = seconds
        self.log = log

    @property
    def killed(self) -> int:
        return sum(self.counts.get(name, 0) for name in KILLING_CLASSES)

    @property
    def total(self) -> int:
        return self.killed + self.counts.get("survived", 0)

    def detail(self) -> str:
        return " ".join(f"{name}={self.counts[name]}" for name in STATUS_CLASSES
                        if self.counts.get(name))

    def score(self) -> float:
        return 100.0 * self.killed / self.total if self.total else 0.0

    def passed(self, threshold: float) -> bool:
        """No mutants means mutmut never ran; that is a broken gate, never a pass."""
        return self.total > 0 and (self.exempt or self.score() >= threshold)

    def line(self, threshold: float) -> str:
        if self.total == 0:
            return f"{self.module:<24} {'no mutants generated — mutmut failed':<30} {self.seconds:5.0f}s  ERROR"
        verdict = "exempt (#34)" if self.exempt else (
            "ok" if self.score() >= threshold else "BELOW THRESHOLD")
        return (f"{self.module:<24} {self.killed:>4}/{self.total:<4} {self.score():6.1f}%  "
                f"{self.seconds:5.0f}s  {verdict:<16} {self.detail()}")


def measure(module: str, threshold: float, survivor_limit: int) -> Result:
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="flowdiff-mutants-") as tmp:
        workdir = Path(tmp)
        isolated_copy(workdir)
        runner = f"{sys.executable} -m pytest -x -q -p no:cacheprovider {tests_for(module)}"
        run = mutmut_in(workdir, "run", "--paths-to-mutate", module, "--tests-dir", "tests",
                        "--runner", runner, "--no-progress",
                        "--disable-mutation-types", DISABLED_MUTATIONS)
        counts = status_counts(workdir)
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
