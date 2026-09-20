# ADR 0001 — First production transport (M0)

- **Status:** accepted for the *offline* slice; the transport itself is **NOT LIVE TESTED**
- **Date:** 2026-09-19
- **Decision owner:** project owner (this ADR records the decision, it does not execute the experiment)
- **Related plan sections:** 7 (DSH-first adaptation), 18 (M0), 3 (reuse decisions)

## Context

The controller needs exactly one production transport between itself and a native
Harness. The plan's M0 prefers:

```text
HFlow -> acpx custom agent -> native DSH ACP profile -> native DSH agent
```

and names the official DSH headless mode as the single fallback, never both at once.

M0 requires a real compatibility experiment on the target machine. That experiment
was **not** run in this work round, so this ADR cannot certify interop.

## What was actually observed in this environment (2026-09-19)

Read-only checks only. No global configuration was modified, no plugin installed, no
profile booted, no credentials read, no model called.

| Item | Observed |
|---|---|
| Python | 3.14.7 (`C:\Users\16097\AppData\Local\Programs\Python\Python314\python.exe`) |
| Git | 2.55.0.windows.5 |
| DSH CLI | 0.1.5-rc.1 (`C:\Users\16097\AppData\Roaming\npm\dsh.cmd`) |
| Node | v24.19.0 |
| `acpx` | **not installed** — no `acpx`/`acpx.cmd` on PATH, no `acpx` package in the global npm root, no `openclaw/*` directory under `node_modules` |
| DSH profiles present | `headless`, `web` under `C:\Users\16097\.dsh\profiles` |
| DSH `acp` profile | **not present** — `dsh --profile acp` has nothing to boot on this machine |
| DSH ACP package present in the installed CLI | yes, as a dependency bundle (`@deepseek-ai/dsh-acp`), documented as `pnpm dsh --profile acp` |
| DSH headless surface | present and self-documenting: `dsh --profile headless --help` works, exit 0, no model call |

Two consequences follow directly:

1. The preferred path cannot be probed here: acpx is the client and it is absent, and
   the local profile list has no `acp` profile for the documented server command.
   Absence of a *profile* is not proof that ACP is unusable — a profile can be created
   — but creating one is a change to the user's DSH home, which this round must not do.
2. The fallback surface at least answers on this machine, which is a probe, not a
   compatibility result: no task was submitted and no session was created.

## Decision

1. **Intended primary transport remains `acpx -> native DSH ACP`.** It is not replaced
   because of an experiment result — no experiment has been run. It stays the plan.
2. **The first version will implement exactly one production Driver.** The M1/M2 code in
   this repository therefore ships *no* production Driver at all: only
   `drivers/fake.py`. `drivers/selected.py` refuses to run and names the missing
   precondition. This keeps the "one production transport, no silent fallback" rule
   true by construction rather than by discipline.
3. **Fallback selection is deferred to the M0 experiment.** If, in that experiment,
   acpx ↔ local DSH ACP fails a *necessary* contract, the first version switches to the
   official DSH headless surface (one task per process, exit code plus error signals,
   `final` events may appear in failed turns) and this ADR is superseded. Writing both
   is not an option.
4. **No capability is claimed.** Every capability in
   `drivers/selected.py::local_probe` is `documented` or `unknown`; none is `probed`,
   `enforced`, or `live_tested`. `hflow doctor` prints `NOT_LIVE_TESTED`.

## M0 exit criteria still owed (not met by this round)

The experiment must record, at minimum: actual launch entry point; stdout protocol
purity; `initialize` and capability response; one fresh session; one task; termination
semantics; model configuration; a permission request; a Chinese/space path; no orphaned
process on Windows after close; and error output versus exit-code consistency.
Budget: at most 2 extra agent turns; anything else stays on a mock ACP server.

## Consequences

- Until M0 passes, every HFlow behaviour in this repository is proven only against a
  fake driver. Passing tests say nothing about acpx, DSH, or any model.
- The Driver seam is small enough (`probe`/`start`/`cancel`/`reconcile`) that adding the
  real transport does not touch `controller.py`, `contracts.py`, or project acceptance.
- If the transport turns out to need a capability the controller cannot enforce
  (for example a hard turn ceiling inside one invocation), the honest outcome is to
  record it as `best_effort` rather than to claim a strict cost bound.
