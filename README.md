# HFlow

HFlow runs one coding task through an agent harness under an explicit approval, a budget and the
project's own approved checks, and keeps the evidence. A small deterministic controller owns
admission, budget, attempts, evidence and the receipt; the harness (DSH over acpx today) owns
reasoning and tools. A run works in an isolated Git worktree and ends with a frozen candidate
commit, which `hflow integrate` takes onto a local branch only after you confirm it.

## Status

- Offline-complete: the whole operator loop (init, prepare, approve, run, status, integrate, clean,
  resume, ledger settle) is tested with no model, no network and no DSH.
- One production driver, `acpx-dsh` (acpx 0.17.1 -> DSH 0.2.0-rc.2 over ACP), and the offline
  `fake` driver. Live evidence: three recorded M2 tasks on one machine and one acpx/DSH binding
  (`docs/m2-live-acceptance-result.md`), not a general property of HFlow.
- The first live task through this loop needs its own explicit approval and budget (AGENTS.md
  rule 10). The live budget is **0**; unattended execution is disabled.

## Install

Python 3.12+; Git 2.31+ for worktree runs and 2.40+ for `hflow integrate`. Runtime dependency:
`pydantic` only.

```sh
python -m pip install -e ".[dev]"
python -m pytest -q          # no model, network or DSH; -n 8 runs it on pytest-xdist
```

The editable install provides the `hflow` command that every printed `next:` line uses
(`PYTHONPATH=src python -m hflow` is the same entry point). Tests use their own `HFLOW_DATA_DIR`
(`tests/conftest.py`); a test whose precondition is missing (Windows, `.probe/acpx`, a recorded
ledger) skips itself.

## Try it offline

Both use throwaway directories and the `fake` driver, which reaches no model:

```sh
python examples/m2_cli_demo.py   # temp Git repo -> fake run -> frozen candidate -> clean
```

Or by hand, inside a throwaway Git repository with a pytest suite:

```sh
hflow init project               # .hflow/project.json; checks inferred, or --check ID=COMMAND
hflow init task T-1 --goal "Reject empty text in slug()" --allow src/demo.py
#   answer "reuse" in T-1.task.json (admission refuses until you do), e.g.
#   "reuse": {"status": "not_required", "reason": "a two-line change"}
hflow prepare --task T-1.task.json --allow-writes --plan-out T-1.plan.json
hflow run --plan T-1.plan.json --fake-write-plan fix.json   # {"src/demo.py": "<new text>"}
hflow status                     # the inbox: what needs you, one next: line each
hflow diff --last --stat
hflow integrate <run-id>         # at an interactive terminal (step 8 below)
```

A fake plan needs no approval. `examples/task.json` and `examples/project.json` are samples of the
two documents you write by hand; `hflow schema` prints the JSON Schema of every document.

## First real task

Only for a task the user approved, with its own budget. The full live checklist and the upstream
caveats for choosing a first task are in `docs/ai/agents/next-session.md`.

1. **DSH**: the npm CLI `@deepseek-ai/dsh@0.2.0-rc.2`, installed with `--ignore-scripts` under
   `<data-dir>/dsh/0.2.0-rc.2` (`hflow doctor` prints the `npm install --prefix ...` line). A
   launch looks on PATH, then there. Keep the DSH Desktop shim off PATH (DSH #9626).
2. **Credentials**: `DEEPSEEK_API_KEY` in the shell, or `DSH_HOME` set to a DSH home holding
   `.credentials.yaml`. With neither, a real launch is refused (`no_credential_source`).
3. **acpx 0.17.1**: `HFLOW_ACPX_CLI` (its `dist/cli.js`), else `<data-dir>/m0/acpx`, else this
   checkout's `.probe/acpx` - never PATH. Model-free check:
   `python tools/m0_probe/real_client_checks.py all`.
4. `hflow doctor --profile <id>`: every readiness line usable; credentials not "REFUSED".
5. Once per repository and machine: `hflow init project`, `hflow init profile <id>`. Per task:
   `hflow init task <id> --goal "..." --allow <path> ...`, then answer the reuse decision by hand.
6. In your own terminal: `hflow approve --task <t.json> --profile <id> --allow-writes --run`. It
   writes the plan (`<id>.plan.json` beside the task), shows it with an approval code, reads the
   code and your own approval text, writes the authorization and runs it in the same process. Or
   in two steps: `hflow prepare --task <t.json> --profile <id> --allow-writes --plan-out <p>`, then
   `hflow approve --plan <p> [--run]`. The plan records the write opt-in, so `approve --plan` and
   `run --plan` never read `HFLOW_ALLOW_WRITES`. A changed plan is refused as stale; a plan for the
   npm DSH prepared before this build must be prepared again (the shim's `node` is now bound by
   content).
7. `hflow status`: the inbox, grouped Running / Ready / Attention. `hflow status <run-id>` and
   `hflow report <run-id>` read one run.
8. `hflow integrate <run-id>`: builds one commit on the target (default: the task's base branch),
   checks it in a worktree of its own, shows it and asks for its confirmation code, then moves the
   branch by compare-and-set. If the branch is checked out it prints
   `git -C <repo> merge --ff-only <commit>` for you to run; then run `hflow integrate <run-id>`
   again. Once integrated, the run's worktree is released (as `clean --apply`) unless
   `--keep-worktree`.
9. Interrupted: Ctrl+C in the owner's terminal, not `hflow cancel` from another. Then
   `hflow resume <run-id>` (exit 5: the owner may still be alive, wait), then
   `hflow ledger settle <invocation-id> --attest "<what you know and why>"` for an `unknown` or
   `launch_unknown` entry. Never re-run the same authorization.

`approve` and `integrate <run-id>` read codes only from an interactive console; piped input and a
NUL-device stdin are not one (`integrate <run-id>` then stops at `ready`, exit 0).

## Commands

| Command | What it does |
|---|---|
| `init project` \| `profile <id>` \| `task <id>` | Write one starting file that does not exist yet; never overwrite |
| `doctor [--profile <id>]` | Read-only readiness: Git, acpx, `dsh`, credential sources, writes, ledger, profile |
| `prepare --task <t> [--allow-writes] [--plan-out <p>]` | Zero-model preview of what `run` would do and what an approval must cover |
| `approve --plan <p>` \| `--task <t> ...` `[--run]` | At a terminal: approval code plus your text -> authorization file; `--run` runs it |
| `run --plan <p>` \| `--task <t> ...` | Admit and run one task; a real driver needs an authorization |
| `status [<run-id>]` | No id: the inbox. An id: that run's state, owner and candidate drift |
| `report <run-id>` \| `--last` | Receipt, evidence and the configuration the run executed under |
| `diff <run-id>` \| `--last` `[--stat]` | Base commit to frozen candidate commit (Git identity, not the fingerprint) |
| `runs [--limit N]` | Recent runs with their first `next:` command |
| `integrate <run-id>` | Continue the run's integration from any state; also `integrate prepare` / `apply` / `reconcile` / `show` |
| `clean <run-id> [--apply \| --dry-run \| --reconcile]` | Release a run's worktree: preview, remove, or settle after an interruption |
| `resume <run-id>` | Reconcile once the owner process is provably gone; never re-dispatches |
| `cancel <run-id>` | Stop a run another process is executing; it blocks `outcome_unknown` |
| `ledger settle <invocation-id> --attest "..."` | Close one `unknown` / `launch_unknown` entry by your attestation |
| `schema` | Generated JSON Schema for every data contract |

Only `run` (and `approve --run`) reaches a model. Common flags: `--json`, `--data-dir`.
`--profile` (or `HFLOW_PROFILE`) is the only source of per-role bindings; without it the driver is
`fake`. `prepare`, `approve --task` and `run --task` also take `--root-budget-file` and
`--repair-policy-file` (`docs/operations.md`). Removed, exit 4: `status --last` (use
`report --last`), `runs --needs-you` (use `hflow status`), `--controller-id`, `--receipt-out`,
`--authorization-mode`, `--base-commit` and `--workspace` (the task file sets base and workspace).

## Exit codes

| Code | Meaning |
|---|---|
| `0` | Accepted, or the command did what it was asked |
| `2` | Refused before anything was created: admission, a dispatch precondition, a stale plan, a wrong approval code; `doctor` when a named profile is unusable |
| `3` | Blocked after dispatch (`hflow status <run-id>` says why) |
| `4` | Usage: a bad or removed flag, an unknown run / integration / invocation id, a refused attestation |
| `5` | Not finished, or another process may still be working: another owner's run, a `resume` refused on the owner rule or rolled back, a locked ledger |
| `6` | A stored record or the ledger cannot be read by this build |
| `130` | Ctrl+C: stopped where it was, retried nothing; what was recorded before is authoritative |

`hflow integrate` (every form): `0` ready (prepared, or stopped at `ready` without a terminal) or
integrated; `2` a wrong confirmation code, a refusal or a Git error; `3` a hand-off to a checked-out
branch, `stale`, `interrupted`, `conflict`, `checks_failed`, `failed`, or a ref update that failed
back to `ready`; `4` usage, an unknown run, a bad `--attest`; `5` another process may still be
working on it; `6` an unreadable record.

## Where data lives

| Kind | Location |
|---|---|
| Project contract (checks, deny paths, limits; versioned) | `<repo>/.hflow/project.json` |
| Machine profile (per-role agent, driver, model) | `<data-dir>/profiles/<id>.json` |
| Task, plan, authorization | `./<id>.task.json`; by default the plan beside the task, the authorization beside the plan |
| Ledger and evidence | `<data-dir>/hflow.sqlite`, `<data-dir>/artifacts/` |
| Run worktrees | `<repo>.hflow-worktrees/<run-id>`, beside the repository |
| Candidate and integration refs (kept by `clean`) | `refs/hflow/candidates/...`, `refs/hflow/integrations/...` |
| npm DSH, acpx (found, never installed by HFlow) | `<data-dir>/dsh/0.2.0-rc.2`, `<data-dir>/m0/acpx` |

`<data-dir>` is `%LOCALAPPDATA%\HFlow` on Windows, else `$XDG_DATA_HOME/hflow` (or
`~/.local/share/hflow`); `--data-dir` or `HFLOW_DATA_DIR` overrides it. Runtime data never lands
in a checkout, and a profile is never read from one. `contracts.py` defines every structure except
the authorization artifact (`authorization.py`); `hflow schema` covers both.

## Rules the code keeps

The twelve rules in `AGENTS.md` hold in the code. In operator terms:

- Budget is reserved in the dispatch's own transaction; an unresolved ledger entry blocks its root
  budget, and open entries close only through `resume` or your attestation.
- Only the controller writes the receipt; a verdict comes only from the reviewer's own final
  message. An unknown outcome blocks; `resume` never re-dispatches.
- Evidence binds the candidate fingerprint and checks digest; the frozen candidate is exactly the
  checked change, held to the task's literal write scope.
- An approval binds the effective configuration (profile, agents, models, writes, the resolved
  launch and its entry files' content, the project contract, the base commit SHA).
- No launch starts on what a repository or shell could change (`.acpxrc.json` or a root `.env` in
  the workspace, a `DSH_HOME` inside it, no credential source); task text and credentials never
  reach a command line. `status`, `report` and `doctor` never call a model.

## Not implemented (do not assume otherwise)

- **No sandbox.** Workers and `command` checks run with your rights. Secret-like environment
  variables (names containing `API_KEY`, `TOKEN`, `SECRET`, `PASSWORD`, `CREDENTIAL`, `AUTH`,
  `COOKIE`, ...) other than `DEEPSEEK_API_KEY` are withheld from the agent child (names recorded,
  never values), but there is no filesystem or credential sandbox and no protection against
  another process of the same user. Scope is checked after the fact, not prevented.
- **No cooperative (protocol) cancellation.** A stop is a forced Windows Job Object teardown; off
  Windows a forced stop does not kill even the direct child.
- **No unattended execution**; each live task needs its own approval.
- **No Team, DAG or parallel tasks, no native subagents**: `dependencies` must be empty, and one
  root budget runs one task at a time.
- **No publish delivery or remote integration**: no push, no pull request; `integrated` /
  `published` delivery is refused. `integrate` moves only a local branch nobody has checked out and
  does not resolve conflicts.
- **No billing observation**: billed tokens, cost and quota stay `null`, never `0`.
- **No `hflow repair` command** and no revival of a historical `BLOCKED` run.
- **`hflow init` only scaffolds**, inferring checks only from pytest configuration and a real
  `package.json` test script; no Markdown handoff renderer.
- **No reuse-research automation and no metrics against a direct-DSH baseline.**
- **No project-declared check environment**: `PYTHONPATH`, `NODE_PATH`, `NODE_OPTIONS` are not
  inherited and cannot be declared.
- **No attestation for a run owner that reads `unknown`** (another host, another user or elevated,
  off Windows): its entries and `outcome_unknown` reconcile wait; `--legacy-owner-gone` covers
  only a pre-v6 owner.
- **No upgrade of the pinned acpx** from 0.17.1 to 0.19.x (surveyed, deferred).
- **A prompt answered with a JSON-RPC error is not read as `failed`**; it stays `outcome_unknown`.
- **No structured-output request and no verdict repair.**
- **No per-role binding of DSH's permission or tools mode** (the ambient variables are removed),
  and no parsing of a `.env` (a workspace-root one is refused unread).
- **Launch files are not held open during the spawn** (Windows has no exec-by-handle);
  `node_modules` trees, DSH's own code and `cmd.exe` are bound by path, not by content.

## Limits and what is not verified

Bounded behaviour and unverified points are described in `docs/operations.md`: "Daily commands",
"Before you run anything: `hflow prepare`", "What an approval covers", "Root budgets and the
dispatch ledger", "When a repair may happen", "Reading a blocked run", "Worktree runs: freeze, scope
and receipt", "Releasing a workspace (`clean`)", "Integrating an accepted candidate", "Client launch
hardening", "Model selection", "Stopping a run", "Recovering from a controller crash" and "What is
not safe yet".

## Reading index

- `AGENTS.md`: the rules that must not be relaxed, and where each concern lives in `src/`.
- `docs/operations.md`: the operator guide.
- `docs/architecture.md`: layers, state, enforced properties, "What is deliberately absent",
  "Known boundaries (do not overstate)".
- `docs/adr/0001-transport.md`: the transport decision and the upstream reads; `docs/m0-results.md`:
  the M0 probe record.
- `docs/batch-i-integration-plan.md`: the integration contract.
- `docs/ai/agents/next-session.md`: the next milestone and the full live checklist.

One tool writes a tracked file: `tools/m0_probe/write_results_doc.py` regenerates
`docs/m0-results.md` (hand edits are overwritten). Only the controller writes a receipt or a task
state (AGENTS rule 2); the one-off M2 offline replay that also did was retired after its use
(`docs/m2-review-wire-repair.md`).
