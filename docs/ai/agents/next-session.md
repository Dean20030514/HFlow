# Next-session handoff — 2026-10-07 (after batch J)

## User decision (unchanged)

> 先完成“小任务从授权到集成、异常后可恢复”的真实闭环，再扩展 Team，最接近实际可用。

## What batch J delivered (offline only)

Batch J was offline hardening for the first live small task. It followed the 2026-10-07 GitHub
survey (ADR 0001 addendum; no transport change; acpx stays pinned at 0.17.1).

- **Credential refusal** (user ruling 2026-10-07, "refuse before dispatch"): a real launch is
  refused with `no_credential_source` when `DEEPSEEK_API_KEY` is not among the child's variable
  names and the bound `DSH_HOME` has neither `.credentials.yaml` nor `.env`. The files are checked
  by stat only. The check runs at admission, before anything is reserved (exit 2), and again at
  the spawn gate. It checks presence only, not validity.
- **`resume` owner check**: an `outcome_unknown` run whose v6 owner may still be alive is not
  reconciled. Nothing is written and the exit code is 5. `resume --legacy-owner-gone --attest` can
  close the open entries of a pre-v6 ended run; the attestation is written atomically with the
  closure.
- **Process lookup**: `main()` sets `NoDefaultCurrentDirectoryInExePath=1`, so HFlow's own `git`
  and check launches never run a program from the current directory. `doctor` finds programs on
  absolute PATH entries only and never executes `dsh`. It also gains readiness lines:
  integrate/Git, acpx entry, the Desktop shim hint, and credentials.
- **Inert git**: `safe.bareRepository=explicit` is set; a graft file is neutralised; inherited
  `GIT_CONFIG`, `GIT_ATTR_SOURCE`, `GIT_SHALLOW_FILE`, `GIT_*_PATHSPECS`, diff and discovery
  variables are dropped. `GIT_CEILING_DIRECTORIES` is deliberately kept.
- **Integrate**: the next-step commands it prints carry `--data-dir` and are quoted for PowerShell.
- **Command forms**: `python -m hflow` works. Ctrl+C exits 130. Text output is `key: value`
  lines; `--json` output is unchanged except that `doctor --json` changed shape.
- **Tests**: one CLI-only end-to-end test of `prepare` → `run` → `integrate` (including the
  hand-off and `reconcile`); real-process fault drills (kill before/after `update-ref`, kill
  mid-run then `resume` + `ledger settle`); and a regression test for acpx #770, where the agent
  dies mid-turn and the client exits 1 with no error envelope, giving `OUTCOME_UNKNOWN`.

## Next step: the first live small task (needs its own explicit approval and budget)

The live budget is still **0**. Run this checklist only after the user approves this specific
task (AGENTS rule 10).

1. Use a shell where the following holds:
   - The DSH Desktop shim directory
     `%LOCALAPPDATA%\Programs\DeepSeek Harness\resources\runtime\cli\bin` is prepended to `PATH`.
   - `DEEPSEEK_API_KEY` is set in that shell, or `DSH_HOME` is bound to a home that holds
     `.credentials.yaml` and boots headless. Never create a home from nothing (DSH #7978).
   - `HFLOW_ALLOW_WRITES=1` is set.
   - acpx 0.17.1 is found via `HFLOW_ACPX_CLI` or `.probe/acpx`.
2. `python tools/m0_probe/real_client_checks.py all`. This is model-free.
3. `python -m hflow doctor --profile <id>`. Every readiness line must be usable, and credentials
   must not say "would be REFUSED".
4. `prepare --json` with a small task whose checks finish in seconds. Keep the turn well under
   DSH's reported 300 s stream timeout. Copy `authorization.binding` into the authorization file,
   with the user's own approval text.
5. `run` with identical flags. If it ends `ACCEPTED/LOCAL_CANDIDATE`, run `integrate prepare`,
   then `apply` (or the hand-off plus `reconcile`), then `status`/`report`, then `clean --apply`.
6. If anything is interrupted: press Ctrl+C in the owner's own terminal, not `cancel` from another
   one. Then `resume` (exit 5 means the owner may still be alive; wait), then `ledger settle`.
   Never re-run the same authorization.
7. After the run, check the worktree root with `icacls`, because of the reported DSH Windows
   sandbox ACE/Low-label leftovers (#8312). If `clean` refuses, record the refusal; never force it.

Only after that real evidence comes Team/DAG work. Before parallel tasks:

- DSH #1485: concurrent instances sharing one `DSH_HOME` corrupt workspace session membership.
  Use per-run homes seeded from a booting profile, or serialize.
- Integrate does not re-compare the shared Git metadata against the source run's snapshot. Today
  the worker is gone by then, but with parallel runs another worker could plant a filter or
  merge driver before `integrate prepare`.
- Prior art collected 2026-10-07 for that phase: mergetrain's train/bisect landing, codex's
  spawn-slot reservations, per-task homes, and synchronous fan-out/join.

Do not rewrite historical evidence or reactivate the archived `workflow` installation.
