# flowdiff

A CLI that turns a code change into a runnable, comparable *flow* rather than a
patch. It finds the symbols a change touched, connects them through the LSP call
graph, picks the nearest entry point whose signature is stable on both sides,
generates the smallest harness that drives it, runs that harness against the base
revision and the working tree, and reports what diverged and where.

Designed 2026-09-06. Nothing implemented yet.

## Goal

Answer three questions about an in-progress edit, without reading the diff:

1. What is the flow that changed — which frames, connected how, entered where.
2. Did behaviour actually move, and at which frame did it first diverge.
3. Can that be locked into a test in this repo's own style.

Targets a mid-edit loop (working tree vs HEAD), not only PR review.

## Non-goals

- Input synthesis. Arguments are harvested from real call sites or the harness is
  left incomplete and the run stops. The tool never invents a value.
- Slicing C++ into standalone translation units. Both sides are built by the
  project's own build system.
- Remote/device execution in v1. Device-bound flows are refused with an explanation.
- Replacing the diff. `git diff` still answers "what did I type"; this answers
  "what did it do".

## Decisions

1. Name is `flowdiff`; repo is `github.com/PavelGuzenfeld/flowdiff`, MIT.
2. The repo is a public surface from the first commit: no internal project names,
   no internal email addresses, prose not bullet lists in PR bodies, no AI
   attribution or `Signed-off-by` trailers.
3. `gst-nvmm-cpp` is the C++ demo target; flowdiff's own test suite is the Python
   demo target (self-hosting).
4. v1 ships C++ and Python together, not C++ alone.
5. The tool is Python 3.12, standard library only. External binaries — `git`,
   `clangd`, `pyright-langserver`, `ast-grep`, `gdb`, `graph-easy`, `docker` — are
   checked at startup and reported with an install hint when missing.
6. Verbs are `flowdiff [ref]` (analyse and render, no build), `flowdiff play [ref]`
   (build, run, diff), `flowdiff show <frame>[/<arg>]` (drill into recorded values),
   `flowdiff keep <flow>` (emit a permanent test).
7. Default base is HEAD, compared against the working tree including staged and
   unstaged changes. `flowdiff <ref>` compares HEAD's tree against `<ref>`.
8. Changed symbols are the intersection of `git diff -U0` hunk ranges with LSP
   `textDocument/documentSymbol` ranges, from the same language server session that
   builds the call graph.
9. A hunk whose enclosing symbol is textually identical after stripping comment
   nodes and collapsing whitespace is dropped as formatting-only.
10. A symbol is marked as *signature drift* when its `documentSymbol` detail or
    selection-range text differs between base and head.
11. The call graph is built from LSP `callHierarchy/incomingCalls` and
    `outgoingCalls`, bounded to 3 hops from any changed symbol.
12. Framework dispatch edges that are assignments rather than calls are added by
    ast-grep hint rules, one file per framework, starting with
    `hints/gobject.yml` matching `$K->$SLOT = $FN`. A match is only a candidate:
    the edge is added when `$FN` resolves through LSP `textDocument/definition` to a
    function definition, so field-to-field assignments never become edges. Such
    edges are tagged `registered` and rendered dashed.
13. A frame with no incoming calls whose only references are address-taken emits a
    `no caller found — add a hint rule?` warning rather than being silently treated
    as a root.
14. One flow is one connected component of changed symbols in that graph.
15. A flow's entry point is the nearest common caller whose signature is identical
    in base and head.
16. When no such frame exists within the hop bound, the flow gets two
    revision-specific harness bodies emitting one shared observables record. Each
    body's arguments are harvested independently from that revision's own call
    sites — decision 18 run twice — so nothing is invented. When the two sides
    harvest from different call sites the verdict carries a warning that a
    divergence may reflect the inputs rather than the code.
17. A flow's frames are every symbol on a path from the entry to a changed symbol,
    plus the direct callees of those symbols.
18. Entry arguments are harvested from LSP `textDocument/references` on the entry,
    ranked with call sites inside the test directory first, production callers second.
19. When no call site yields liftable arguments, the harness is written with empty
    argument slots, the path is printed, and the run stops with exit code 2.
20. The C++ harness `#include`s the translation unit that defines the entry point,
    so `static` functions are callable.
21. The C++ harness is compiled with the defining TU's own command from
    `compile_commands.json`, with `-g -O0 -fno-inline` appended.
22. The C++ harness links the sibling object files of the entry's build target —
    never the target's own library — plus every library in that target's built
    artefact's `NEEDED` list (`readelf -d`), which covers internal and external
    dependencies alike for any build system. `nm` is used only to find which built
    target defines the entry symbol.
23. No project build file is ever edited. The harness is not a build target.
24. One harness source is compiled twice: once against the base worktree, once
    against the working tree — except under decision 16, where each revision
    compiles its own body.
25. `.flowdiff/base` is a persistent git worktree with its own build directory,
    reused across runs and rebuilt incrementally by the project's own build command.
    `--clean-base` resets it.
26. `.flowdiff/` contains a `.gitignore` holding `*`, so the tool's scratch state
    never appears in the host repo's status.
27. The trace records every frame the flow touches — arguments on entry and the
    return value on exit.
28. C++ frames are traced by a generated `gdb -batch` Python script that breakpoints
    each flow frame, reads arguments from the frame block, and `finish`es to capture
    the return.
29. Python frames are traced in-process by `sys.monitoring` on 3.12 and above, with
    a `sys.settrace` fallback filtered by code object.
30. Both tracers emit the same JSON-lines schema, one record per frame entry and
    exit, so rendering and `show` are language-agnostic.
31. Scalars and small structs are recorded verbatim. Anything with a size — buffers,
    images, arrays, strings over 64 bytes — is recorded as shape, byte hash, and
    min/max/mean. Raw bytes are written only under `--dump`.
32. Type-aware summarisers ship for `GstBuffer*`, `GstCaps`, `nvmm::img::Image<T>`,
    `std::vector`/`std::array`/`std::string`, numpy arrays, and `bytes`/`str`, with a
    generic size-plus-hash fallback.
33. A project may add `.flowdiff/summarisers.py` exposing
    `summarise(type_name, value) -> dict | None`, loaded by both tracers.
34. Frames that the debugger cannot locate because they were inlined are reported as
    `inlined — not traced` and counted in the summary; the tool does not rebuild the
    project to defeat inlining.
35. `play` prints only the merged flow graph and a one-line verdict. No values.
36. Values are reached with `flowdiff show <frame>` and `flowdiff show <frame>/<arg>`,
    read from the trace file on disk. `play --depth N` unfolds N layers inline for
    non-interactive logs.
37. The graph is one merged `graph-easy --as boxart` rendering of the union of both
    flows, with `~` changed, `+` added, `-` removed markers and dashed registered
    edges. `--split` prints base and head side by side.
38. The verdict line names the count of differing frames and the origin frame — the
    first frame along the flow where a value diverged.
39. Tests that reach any flow frame are found by LSP references from the test
    directory, built and run on both sides, and reported per test as PASS→PASS,
    PASS→FAIL or FAIL→PASS. `--no-tests` skips this.
40. Exit codes: 0 both sides ran and a diff was printed; 1 the tool itself failed;
    2 no harness ran — no flows, empty argument slots, or device-bound frames. The
    absence of covering tests never changes the exit code; the verdict line reports
    it. `--fail-on-diff` makes divergence exit non-zero.
41. Before compiling, the libraries actually linked by a flow's frames are checked
    against a device deny-list (`libnvbufsurface`, `libcuda`, `libcudart`,
    `libnvinfer`). A hit prints the graph and covering tests, names the offending
    frames, and exits 2.
42. The check in decision 41 reads the built artefact, not the source, so a project
    that substitutes a host mock at configure time passes it. The scan's own result
    is reported in the verdict — `linked: no device libraries (host build)` or
    `linked: libnvbufsurface (device build)` — so an "identical" result on a mock is
    never mistaken for device truth.
43. `--via ssh://…` for remote execution on a device is deferred to v2.
44. C++ steps run in a `flowdiff/<project>:dev` image layered on the project's own
    dev image with `clangd`, `gdb` and the tool added, cached by the base image
    digest, mounting the repo at the path the compile database already expects.
45. Container mode is selected automatically when the compile database's `directory`
    does not exist on the host; otherwise the tool runs natively. `--image` forces it.
46. Python steps run natively with the project's interpreter, detected in order:
    `$VIRTUAL_ENV`, `.venv/`, uv or poetry metadata, then the interpreter running
    flowdiff itself. `PYTHONPATH` is pointed at the base worktree or the working
    tree.
47. `keep` writes `tests/flow_<entry>.<ext>` as a golden test asserting the recorded
    return and mutated arguments, buffers asserted by hash. Registering it with the
    build system is the user's commit, not the tool's.
48. `keep` detects the repo's test dialect by scanning the test directory in priority
    order: local harness header, gtest, Catch2, doctest for C++; pytest then unittest
    for Python; plain asserts when nothing matches.
49. Implementation order is the language-agnostic spine first, then the Python half
    end-to-end, then the C++ half, then `keep`.

## Open questions

- Whether clangd reports usable `incomingCalls` at all for a GObject vfunc was never
  measured; a spike was written and not run. Decision 12 makes the answer
  non-blocking, but the hop bound and the hint rule's coverage are unvalidated.
- The 3-hop bound (decision 11) and the deny-list contents (decision 41) are starting
  values, to be tuned once real flows run.
- Whether the `NEEDED`-based link resolution of decision 22 holds when the entry's
  target is a static archive, which has no `NEEDED` list; the consumer executable's
  list would have to stand in for it.
- PyPI packaging and the binary's distribution are unaddressed; v1 is a git clone.

### Resolved during planning

The C++ demo looked blocked by decision 41: the crop flow through
`NvBufSurfTransform` is device-bound, so the example that motivated this design
appeared to be one the tool would refuse. Checking the built artefact settled it —
`gst-nvmm-cpp` ships `gst/common/nvbufsurface_mock.h` and meson selects it whenever
the real NvBufSurface is absent, so on an x86 host `libgstnvmmconvert.so` links only
gst, glib and `libnvmm_common`. `readelf -d` shows no device library at all. The crop
demo runs on x86 against the project's own mock, and it still exercises everything M3
exists to build: a `.cpp` with statics, sibling-object linking, and the container
layer. Decision 42 exists so the mock is named in the verdict rather than hidden.

This is not the rejected "stub the device calls" alternative below. That one had
flowdiff generating stubs; here the project made the substitution itself, at configure
time, for its own test suite, and flowdiff simply plays the build it is handed.

## Steps

**M1 — spine** (~885 lines). No language-specific code. Ends with `flowdiff` (no
`play`) rendering a correct marked graph for a real change in this repo.

| | | |
|---|---|---|
| `cli.py` | verbs, exit codes, binary checks | ~120 |
| `lsp.py` | JSON-RPC client, per-language server config, the six requests used | ~190 |
| `changes.py` | hunk parsing, symbol intersection, formatting-only drop, drift | ~150 |
| `graph.py` | bidirectional walk, hint edges, components, entry selection | ~230 |
| `render.py` | merged graph, markers, verdict line, `show` rendering | ~180 |
| `hints/gobject.yml` | the one framework rule v1 ships | ~15 |

**M2 — Python end-to-end** (~610 lines). No container, no compile, no linking. Ends
with flowdiff self-hosting: a change to `graph.py` produces a played flow through its
own code.

| | | |
|---|---|---|
| `worktree.py` | persistent base worktree, checkout, path selection | ~90 |
| `env.py` (venv half) | interpreter detection | ~60 |
| `harness_py.py` | harness generation, argument harvest | ~130 |
| `trace_py.py` | `sys.monitoring` + fallback, JSONL emit | ~160 |
| `summarise.py` | built-ins, project hook loading | ~170 |

**M3 — C++ end-to-end** (~720 lines). Every hard risk lives here. Ends with the
`gst-nvmm-cpp` demo flow played inside the container layer.

| | | |
|---|---|---|
| `env.py` (container half) | root probe, layer build, run wrapper, deny-list | ~170 |
| `harness_cpp.py` | include-the-TU generation, flag borrow, link resolution | ~260 |
| `trace_gdb.py` | generated gdb script, breakpoints, summarisers, JSONL | ~220 |
| build hook in `worktree.py` | incremental base rebuild | ~70 |

**M4 — keep** (~160 lines). Dialect detection and golden-test emission.

**M5 — demo and docs** (~370 lines). README with one worked C++ flow and one
self-hosted Python flow, plus flowdiff's own test suite.

Roughly 2,750 lines total. This is a substantial build, not a weekend script — the
milestone boundaries are the places to stop and reassess.

## Risks and rejected alternatives

**Risks**

- *Empty argument slots are the common case.* If most entry points have no call site
  with liftable literals, decision 19 fires constantly and the tool exits 2 more often
  than it produces a diff. M2 will show this early and cheaply; if it bites, the fix is
  a better ranking heuristic, not synthesis.
- *A mock-backed run can report a false "identical".* A change that only differs on
  real hardware looks unchanged when played against a host mock. Decision 42 makes the
  variant visible in the verdict, but visible is not the same as safe — the honest
  answer for such a change is still a device run, which is v2.
- *Finding the entry's target is heuristic.* Decision 22 locates it by `nm` over the
  built artefacts, which can pick the wrong one when a symbol is defined in several.
- *Breakpointing statics inside an included TU.* The mangled name gdb sees for a
  `static` function pulled in through `#include` may not match what the call graph
  reported. Unproven until M3.
- *The include trick has failure modes.* A TU with its own `main()`, non-idempotent
  static initialisers, or an anonymous-namespace collision with the harness will not
  compile. Each needs a guard and a clear error.
- *graph-easy is Perl and slow past roughly 60 nodes.* The hop bound should keep flows
  well under that, but a wide component could be unpleasant. Its layouter has a hard
  timeout (`--timeout=N`, default 240 s); the tool passes a short one and falls back
  to an indented call tree when the layout aborts, so a wide flow degrades rather
  than hangs.

**Rejected, with reasons, so they are not quietly reverted**

- *Slicing both revisions into `before::`/`after::` namespaces in one binary.* Truly
  isolated and a single run, but it breaks on the first static, global, or GObject type
  — which is every element in the demo target.
- *Type-driven input synthesis.* Always produces a runnable harness, and on this
  codebase a zero-sized buffer takes the early-return path, so both sides agree and the
  tool reports "identical" for a change that did something. A false negative is worse
  than a refusal.
- *flowdiff generating stubs for device calls so device-bound flows run on x86.* The
  diff would then verify flowdiff's own stub. Distinct from a project that ships its
  own host mock and selects it at configure time, which decisions 41 and 42 accept.
- *`-Dstatic=` or visibility tricks to export static entry points.* Breaks any TU with
  static locals or static members.
- *`LD_PRELOAD` interposition for tracing.* Only sees exported dynamic symbols; every
  vfunc in the demo target is invisible to it.
- *`-finstrument-functions`.* Gives which frames ran, not their values — the diff
  degrades to "did the path change".
- *Source instrumentation of the included TU with ast-grep.* Only reaches frames in
  that one TU; anything in another translation unit or a prebuilt library goes dark.
- *Rebuilding affected TUs at `-O0` to defeat inlining.* ODR and duplicate-symbol
  trouble across the library boundary, which is exactly what decision 22 avoids.
- *Synthesising the head-side argument when signatures drift.* Rejected in favour of
  decision 16's per-revision harvest; inventing the new parameter's value is the one
  thing the Non-goals forbid.
- *Stash-based base builds.* Fastest incremental path, but it mutates the tree being
  edited and a crash mid-run leaves the user's work in a stash.
- *Fresh worktree per run.* Correct and stateless, but minutes per invocation defeats
  the mid-edit default of decision 7.
- *Interactive curses drill-down.* Not pipeable, not usable in CI, and a second
  rendering path to keep in sync.
- *`typer` and `rich`.* Nicer output, but every project's container layer would then
  need a network-enabled pip install.
