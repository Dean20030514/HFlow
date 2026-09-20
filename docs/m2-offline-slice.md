# M2 offline delivery slice

This is the first HFlow path that produces a **deliverable** rather than a transport
result, and it is deliberately offline: a Fake Driver performs a small, pre-declared change
inside an isolated Git worktree, and the controller issues a local delivery receipt bound to
the frozen candidate.

```text
fixed base commit
  -> isolated Git worktree (beside the repository, never inside it)
  -> the driver applies one small change limited to the declared write scope
  -> the controller freezes the candidate as a real Git commit
  -> the approved check runs against that frozen candidate
  -> local delivery receipt (LOCAL_CANDIDATE), evidence bound to the candidate
```

Nothing is merged, pushed or published. No real model is called. No reviewer is started.

## Two candidate identities, never conflated

| Identity | Source | Detects |
|---|---|---|
| `candidate.git_commit` / `git_tree` | real Git objects, created by `git commit` in the worktree | exactly which candidate was verified |
| `candidate.fingerprint` | content hash over the TaskSpec's write scope | drift after verification |

Both appear in the receipt. A run that does not use a worktree has an empty `git_commit`
and carries a limitation saying so, instead of presenting a fingerprint as a Git object.

## What the code guarantees

- **The user's checkout is never written to.** Everything happens in a detached worktree
  created from the base commit. Uncommitted user changes are recorded as a note and left
  exactly as they were - never stashed, reset, or committed.
- **Only declared paths enter the candidate.** `freeze_candidate` stages the TaskSpec's
  `write_allow` entries individually; there is no `git add -A`, so a stray log or credential
  file in the worktree cannot be collected by accident.
- **Changes outside the declared scope block the run** before any acceptance, using the
  same manifest diff as the M1 slice.
- **Verification runs on the frozen candidate**, in its worktree. If the candidate changes
  afterwards, the recorded evidence no longer applies and acceptance refuses.
- **A failed check keeps the candidate.** The worktree and its commit survive for
  inspection; nothing is cleaned up or auto-repaired.
- **Nothing is delivered without a passing check.** `ACCEPTED` still means "checked
  candidate frozen", and delivery stays `LOCAL_CANDIDATE`.

## Running it

```sh
python -m pytest -q tests/test_m2_slice.py        # 5 tests, real Git repos in temp dirs
python -m pytest -q tests/test_cli_m2_cleanup.py  # 15 tests: the same flow through `hflow`
python examples/m2_cli_demo.py                    # the whole flow, printed
```

Covered: the sample really fails at the base commit; the slice produces a frozen candidate
with a real commit and tree plus a receipt; a "change" that does not fix anything blocks
delivery while keeping the candidate; editing the worktree after acceptance shows up as
drift; and a dirty target repository is left untouched (same porcelain output, same stash
list, same commit count).

Through the CLI, additionally: `status`/`report` read the same facts with no process started,
`--driver acpx-dsh` refuses before any credential or workspace, preview changes nothing (no
ref created, no file removed), `--apply` removes only that run's worktree while the candidate
ref and receipt survive, repeated apply is idempotent, and a `MISSING` path is never reported
as success. The dangerous cases - active execution, unfrozen changes, HEAD drift, an ignored
`.env`, a foreign path - are all refused with the scene preserved.

## Candidate retention

A commit SHA in a report is an identifier, not a retention policy: a detached worktree's HEAD
alone does not keep an object alive. Freezing therefore also creates

```text
refs/hflow/candidates/<run-id>/<attempt-id>
```

via `git update-ref` with an empty old value (create-if-absent). An existing ref pointing at
the same candidate is reused; anything else is refused, so a user's ref is never overwritten.
`clean` uses that ref as its "the delivery outlives the workspace" check, and never creates
one during a preview. The ref is not an acceptance mark - failed candidates are kept too.

## Known gaps

- Worktrees are created beside the repository and are **not** garbage-collected
  automatically. `clean` releases one run's workspace on request; a kept failed candidate
  stays until an operator releases it deliberately.
- Integration and publishing are not implemented: `LOCAL_CANDIDATE` is the only delivery
  state this path can reach.
- The check runner still executes in the worktree with the current process user's rights.
  Git isolation is a *workspace* boundary, not a sandbox.
- A linked worktree does write shared Git metadata in the source repository (objects and
  `worktrees/` administration). The guarantee is about the user's HEAD, index, working files,
  stash and branches - not "the `.git` directory is never written".
- No real Harness has run this path end to end; the Fake Driver proves the controller, not
  DSH. A live M2 task needs its own authorization and budget.

## Porcelain parsing note

`parse_status_z` reads `git status --porcelain=v1 -z --untracked-files=all --ignored` and
never trims a record. An earlier report described a "`line[3:]` off-by-one" bug; that
description was wrong - `record[3:]` is correct for an untrimmed `XY<space><path>` record.
What actually corrupted paths was trimming the record first (`.strip()` eats the leading space
of an unstaged ` M` record) and then slicing at a fixed offset. The parser now uses `-z`
records, keeps rename/copy's two paths together, refuses unmerged and submodule records
instead of guessing, and is covered by a test that exercises those states against a real
repository rather than fixed strings.
