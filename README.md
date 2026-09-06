# flowdiff

A diff that shows what a change *does*, not what you typed. flowdiff finds the
symbols a change touched, connects them through the language server's call graph,
picks the nearest entry point whose signature is the same on both sides, and renders
that flow with the changed frames marked. Later milestones generate a harness that
drives the entry point on the base revision and the working tree and diff the trace.

The design, every settled decision and the milestone plan are in
[docs/plan.md](docs/plan.md).

Python 3.12, standard library only. External tools: `git`, `ast-grep`, `graph-easy`,
and a language server per language — `clangd` for C and C++, `pyright-langserver`
for Python.

```
flowdiff            # working tree vs HEAD
flowdiff origin/main
```
