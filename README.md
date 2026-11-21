# Blast Radius entry

For each patch against sqlglot at `eb11aec`, pick the tests that can catch it and
split them over workers.

## How it works

**Maps of the pinned commit (built once, committed in `maps/`).**
`tools/trace_plugin.py` runs every sqlglot test alone in its own process and records:

- the source lines it executes, kept apart by when they run: while modules load,
  inside functions during the test, in class and module bodies loaded during the
  test, and while pickling work for pool workers;
- the same lines per fixture case, for tests that walk a fixture file;
- the files it opens and folders it lists;
- the expression classes it creates and the dialect classes it uses.

`tools/build_map.py` folds the traces into `maps/map.bin`. `tools/build_refs.py`
indexes where class-level and module-level names are read. `tools/family_map.py`
lists the source files each dialect is built from. `tools/measure_costs.py` times
each test in the in-process runner. Nothing in the maps comes from any patch.

**Candidates (`br/candidates.py`).** A patch is applied in memory and both versions
are parsed. Statements whose syntax is unchanged are ignored, so comment and layout
edits select nothing. For each changed statement:

- inside a function or lambda: the tests that execute it;
- a class or module body: the tests that create that expression class, or that read
  the changed name, or that use a dialect built from that file;
- a new statement: the tests that run its enclosing function;
- a file some test reads, or a folder some test lists: those tests;
- a file that no longer parses or loads: every test that loads it.

**Verification (`br/child.py`, `br/execute.py`).** Candidates are run against the
patched code in forked children, with the patch served from memory. Tests that walk a
fixture file only run the cases that meet the change. `br/policy.py` stops running
as soon as selecting the remaining candidates unrun is cheaper than running them,
and never runs a test that costs more to run than to select. Tests that fail are
selected; candidates that were not run are selected unless the evidence says they pass.

**Packing (`br/schedule.py`).** Every worker count from 1 to 8 is tried with a
longest-first split improved by moves and swaps. Idle time on the less busy workers
is then filled with left-out candidates at no cost.

## Rebuild the maps

    docker build -f tools/Dockerfile.trace -t br-trace .
    # trace:   tools/trace_all.py tests.json out/ 4     (inside br-trace, sqlglot mounted)
    # costs:   python -m tools.measure_costs tests.json maps/costs.arm64.json
    # fold:    python tools/build_map.py tests.json out/ maps/map.bin maps/costs.arm64.json maps/families.json
    # names:   python tools/build_refs.py <sqlglot checkout> maps/refs.bin

## Run locally

    ./run_sample.sh path/to/sample            # the scorer's limits, linux/amd64
    ./dev_run.sh path/to/sample               # repo mounted, for development

`tools/harness/` makes synthetic patches and labels them by running the whole suite;
it is used only to measure this selector and nothing from it ships in the image.

## Third-party code

No code is borrowed. The image installs duckdb, numpy and pandas from PyPI because
sqlglot's own tests import them, and downloads sqlglot from its repository.
