# flowdiff

Turn a code change into a runnable, comparable flow. Given a working tree and a base revision,
flowdiff finds the functions the diff touched, builds the call graph around them with the language
server, picks the stable frame the flow is entered from, runs that flow on both revisions, and tells
you which frame first produced a different value. `git diff` answers "what did I type"; this answers
"what did it do".

Python and C++. Python runs natively in the project's own interpreter. C++ runs inside the project's
dev image with clangd and gdb layered on top, so nothing has to be installed on the host beyond docker.

## Install

    git clone https://github.com/PavelGuzenfeld/flowdiff
    cd flowdiff && python3 -m venv .venv && .venv/bin/pip install -e .[dev]
    sudo apt install git libgraph-easy-perl clangd
    cargo install ast-grep            # or a release binary on PATH
    npm install -g pyright            # Python projects
    # C++ projects: docker, and a dev image named <repo>:dev, or pass --image

`flowdiff` reads the working tree against HEAD; `flowdiff <ref>` reads HEAD against `<ref>`. Exit 0
rendered or ran, 1 the tool itself failed, 2 nothing to show or nothing could drive the flow, 3
values diverged under `--fail-on-diff`. `--split` draws the base revision's graph beside the head's,
from a second language-server session rooted at the base worktree.

## A Python flow, on this repository's own history

Pull request #14 taught covering-test discovery to open the test files first, because pyright answers
references only for open documents. Replay it from a clean clone:

    git worktree add /tmp/pr14 a51ed26 && cd /tmp/pr14
    ~/flowdiff/.venv/bin/flowdiff play f48fcbc

    flow 1/1
    ┌────────────────┐     ┌──────────────────┐
    │ comment_spans~ │ ──> │   scan_kinds+    │ <─────────────────────────────┐
    └────────────────┘     └──────────────────┘                               │
    ┌────────────────┐     ┌──────────────────┐     ┌─────────────────┐     ┌───────────────┐
    │     main~      │ ──> │      flows~      │ ──> │ covering_tests~ │ ──> │ import_lines+ │
    └────────────────┘     └──────────────────┘     └─────────────────┘     └───────────────┘
      │                      │
      ∨                      └───────────────────┐
    ┌────────────────┐     ┌──────────────────┐  │
    │  server_for~   │ ──> │  ServerConfig~   │  │
    └────────────────┘     └──────────────────┘  │
                           ┌──────────────────┐  │
                           │ open_test_files+ │ <┘
                           └──────────────────┘
    no stable entry within 9 frames of comment_spans~, scan_kinds+, main~, … (+3); driven by 25 covering test(s) present on both sides
    covered by 34 test(s)
    3 of 9 frames differ: covering_tests (client.config.references_need_open, client.config.import_kinds, return); flows (…, return[0].tests); server_for (return.references_need_open, return.import_kinds); origin server_for
      9 ABSENT→PASS, 25 PASS→PASS
    warning: the driving tests changed in this diff (tests/test_changes.py, …); a divergence may reflect the inputs rather than the code

No function in this repository is called with literal arguments, so the covering tests drove the flow
on both revisions. Nine frames changed textually; three produced different values. Drill into one:

    ~/flowdiff/.venv/bin/flowdiff show covering_tests

    flowdiff/graph.py:covering_tests  base 5 call(s), head 5 call(s)
      calls #1  ← tests/test_cli_integration.py::test_covering_tests_are_reported_when_a_test_reaches_the_flow
        client.config.references_need_open  —  →  true
        client.config.import_kinds          —  →  ["import_statement", "import_from_statement"]
        return                              []  →  ["tests/test_lib.py::test_scale"]
      calls #2-#5 (×4)  ← tests/test_graph_build.py::test_covering_tests_falls_back_to_module_scope, …, +2 more
        client.config.references_need_open  —  →  false
        client.config.import_kinds          —  →  []

The one call whose result moved is the pyright-shaped client with a frame in `lib.py`: the test that
found nothing now finds `test_scale`. That is the bug the pull request fixed, recovered from the run
rather than read from the diff. `show covering_tests#1` prints that call whole; `show
covering_tests#1/client.config` narrows it to one subtree; `flowdiff keep` writes the recorded calls
of the frame the harness drove as a test in the repository's own dialect.

## A C++ flow, in a container

In a checkout of `gst-nvmm-cpp` with its dev image built (`docker build -f docker/Dockerfile.dev -t
gst-nvmm-cpp:dev .`), make `crop_and_scale` also set the destination crop:

    sed -i 's/^    params.src_crop = src_crop;$/    params.src_crop = src_crop;\n    params.dst_crop = src_crop;/' gst/common/nvmm_transform.cpp
    ~/flowdiff/.venv/bin/flowdiff play --depth 1

    building the working tree in flowdiff/gst-nvmm-cpp:dev
    building .flowdiff/base in flowdiff/gst-nvmm-cpp:dev
    flow 1/1
    ┌────────────────────────────────┐     ┌───────────┐
    │ NvmmTransform::crop_and_scale~ │ ──> │ transform │
    └────────────────────────────────┘     └───────────┘
    entry NvmmTransform::crop_and_scale  (2 frames, 1 changed: NvmmTransform::crop_and_scale~)
    covered by 7 test(s)
    no call site with literal arguments; driven by 1 covering test executable(s) under gdb
    1 of 2 frames differ: transform (params.dst_crop.x, params.dst_crop.y, params.dst_crop.width, +1 more); origin transform
    linked: no device libraries (host build)
    gst/common/nvmm_transform.cpp:transform  base 10 call(s), head 10 call(s)
      calls #2  ← tests/test_nvmm_transform.cpp::test_crop_and_scale
        params.dst_crop.x       0  →  100
        params.dst_crop.y       0  →  100
        params.dst_crop.width   0  →  800
        params.dst_crop.height  0  →  600
      9 identical call(s): #1,#3-#10
      builddir/tests/test_nvmm_transform  PASS→PASS
    warning: clangd has no callHierarchy/outgoingCalls; callees come from call expressions in the changed bodies

The first run builds both revisions inside `flowdiff/gst-nvmm-cpp:dev`, a layer over the project's
image with gdb and clangd; later runs take a few seconds. `crop_and_scale` itself returned the same
thing on both sides; the value that moved is an argument it passes to `transform`, and the test that
exercised it is named. `linked: no device libraries (host build)` says this ran against the project's
host mock of NvBufSurface, not the device, so an identical result here is not a device result.

When a function is called somewhere with literal arguments, flowdiff writes a harness instead:
it includes the entry's translation unit so statics are callable, borrows the unit's flags from the
compile database, links the target's sibling objects and the libraries the built artefact needs,
and runs that under gdb on both sides.

## Design

Goal, non-goals and every numbered decision live in issue #7; open questions, risks and rejected
alternatives in #8. Amendments are edits to those bodies with a comment saying what changed.
