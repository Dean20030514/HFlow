# HFlow

A small deterministic controller for harness-agnostic agentic development tasks.
The controller owns admission, budget, evidence, and delivery accounting; a native
Harness (DSH first) owns reasoning and tools.

**Status: M1 offline vertical slice.** The only implemented driver is a fake one. No
real Harness has been connected, no model has been called, and no compatibility claim
is made. See `docs/adr/0001-transport.md`.

## What works today

```text
read TaskSpec -> deterministic admission -> SQLite create-or-reuse run
  -> transactional budget reservation -> driver invocation (fake)
  -> verification evidence over a frozen candidate -> independent review verdict
  -> controller-generated ResultReceipt -> status / report
```

## Install and test

Python 3.12+ (developed on 3.14.7). Runtime dependency: `pydantic>=2.12,<3`.
Development dependency: `pytest>=9.0,<10`.

```sh
python -m pytest -q            # 49 passed, 1 skipped (see below)
python -m pip install -e .     # optional: installs the `hflow` console script
```

Tests need no model, no network, and no DSH: they run against `drivers/fake.py`.
The single skip is `test_path_resolution_refuses_junctions_and_absolute_paths`, which
needs the ability to create a directory link; where that is unavailable the junction
leg is skipped while the `..` and absolute-path legs still run.

## Commands

```sh
hflow doctor   --json                     # read-only environment probe, no model calls
hflow run      --task examples/task.json --project-root . --driver fake --json
hflow status   R-xxxxxxxxxx               # pure SQLite read, zero model calls
hflow report   R-xxxxxxxxxx --json        # receipt + evidence, zero model calls
hflow resume   R-xxxxxxxxxx               # reconcile an interrupted attempt; never re-dispatches
hflow cancel   R-xxxxxxxxxx
hflow schema                              # generated JSON Schema for every contract
```

Runtime data (SQLite, evidence) goes to `%LOCALAPPDATA%\HFlow` on Windows or
`$XDG_DATA_HOME/hflow` elsewhere; override with `--data-dir` or `HFLOW_DATA_DIR`.
It never lands inside a project checkout.

Exit codes: `0` accepted, `2` refused at admission, `3` blocked after dispatch, `4` usage.

Without installing, run through the module path:

```sh
python -c "import sys; sys.path.insert(0,'src'); from hflow.cli import main; raise SystemExit(main())" run --task examples/task.json --project-root . --driver fake
```

## Data ownership (three kinds, kept apart)

| Kind | Location | Notes |
|---|---|---|
| Project contract | `<repo>/.hflow/project.json` | approved checks, deny paths, limits; versioned with the project |
| Machine binding | not implemented yet | will hold executable paths, transport, profile, capability record |
| Runtime data | platform data dir | SQLite plus evidence references; outside the repo |

`contracts.py` is the single definition of every structure; JSON Schema is generated
from it (`hflow schema`). There is no second hand-written schema to drift.

## Non-negotiables in the code

- The controller generates `ResultReceipt`; a worker cannot report `ACCEPTED`.
- Budget is reserved in the same transaction that records the dispatch.
- Identical TaskSpec does not buy a second worker turn. Note what that means: the reply
  is the **historical** run, and `status`/`report` print a `candidate` line saying whether
  the scoped files still match the fingerprint that run was accepted at. A historical
  `ACCEPTED` is never presented as verification of the current working tree.
- Implementer and reviewer are separate driver processes with separate reserved turns;
  `run` reports `implementer_invocations` and `reviewer_invocations` separately. Neither
  number is a model-request count, and no field claims to know billed usage.
- An unknown outcome blocks and never auto-retries.
- Verification is bound to a candidate fingerprint and a checks digest.
- `status`, `report` and `doctor` make no model calls.

## Not implemented (do not assume otherwise)

Production DSH/acpx driver (M0 experiment not run), Git worktree isolation and snapshot,
strong read-only sandbox, cross-process cancellation with child-process-tree proof,
repair cycle, integration/publish delivery, reuse-research automation, teams and native
subagents, real billing observation, metrics against a direct-DSH baseline.
