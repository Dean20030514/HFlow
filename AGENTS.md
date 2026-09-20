# AGENTS.md — HFlow working notes

Short navigation for an agent (or human) editing this repository. It intentionally does
not restate the design; read the plan and `docs/architecture.md` when the task needs them.

## Where things are

| Concern | File |
|---|---|
| Every data contract (single source of truth) | `src/hflow/contracts.py` |
| State machine, budget, acceptance, receipt, cancel intent | `src/hflow/controller.py` |
| SQLite schema, transactions, compare-and-set | `src/hflow/store.py` |
| Admission rules (scope, reuse, budget, risk) | `src/hflow/admission.py` |
| Approved checks and evidence | `src/hflow/verify.py` |
| Scope containment and candidate fingerprints | `src/hflow/workspace.py` |
| The one production driver (acpx → DSH ACP) | `src/hflow/drivers/acpx_dsh.py` |
| Windows Job Object process boundary | `src/hflow/drivers/winjob.py` |
| Neutral event projection from the client stream | `src/hflow/drivers/acp_events.py` |
| Offline driver (tests and development) | `src/hflow/drivers/fake.py` |
| Which driver is production | `src/hflow/drivers/selected.py` |
| Transport decision and its evidence | `docs/adr/0001-transport.md`, `docs/m0-results.md` |

## Rules that must not be relaxed to make a test pass

1. Budget is reserved before dispatch, in the same transaction as the attempt row.
2. Only the controller writes `ResultReceipt` / task state.
3. Evidence carries the candidate fingerprint and checks digest; stale evidence is
   refused, never reused.
4. A late result for an old attempt changes nothing.
5. `OUTCOME_UNKNOWN` blocks; `resume` reconciles and does not re-dispatch.
6. `status`, `report`, `doctor` never call a model.
7. Capabilities are `documented` / `probed` / `enforced` / `unsupported` / `unknown`.
   Never upgrade one without a recorded observation, and never call an offline fake
   result a compatibility proof.
8. A stop is only `confirmed_stopped` when the managed process boundary confirms it, and a
   forced stop is never reported as a successful protocol cancellation. An unconfirmed stop
   blocks; it never re-dispatches.
9. The agent launch argv carries only the launcher path and fixed flags. Task text, nonce,
   user content and credentials never go into a command line - they travel through stdin or
   a controlled file.
10. No live model task runs without an explicit new budget from the user. The M0 allowance
    is spent; a new probe file or directory does not create new allowance.

## Working style for this repository

- Add a test next to the behaviour, not a document describing it.
- If a behaviour is not implemented, say so in `README.md` under "Not implemented"
  instead of half-claiming it in code comments.
- Do not add a dependency to avoid writing twenty lines of standard library.
- Do not create empty module trees for future milestones.
- Run `python -m pytest -q` before reporting progress; report real output.

## Useful commands

```sh
python -m pytest -q
python -m pytest -q tests/test_controller.py -k budget
python -m pip install -e ".[dev]"
```
