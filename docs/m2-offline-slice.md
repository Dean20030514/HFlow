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
```

Covered: the sample really fails at the base commit; the slice produces a frozen candidate
with a real commit and tree plus a receipt; a "change" that does not fix anything blocks
delivery while keeping the candidate; editing the worktree after acceptance shows up as
drift; and a dirty target repository is left untouched (same porcelain output, same stash
list, same commit count).

## Known gaps

- Worktrees are created beside the repository and **not** garbage-collected automatically;
  a failed candidate is intentionally kept, so an operator (or a later `hflow clean`) must
  remove them.
- Integration and publishing are not implemented: `LOCAL_CANDIDATE` is the only delivery
  state this path can reach.
- The check runner still executes in the worktree with the current process user's rights.
  Git isolation is a *workspace* boundary, not a sandbox.
- No real Harness has run this path end to end; the Fake Driver proves the controller, not
  DSH. A live M2 task needs its own authorization and budget.
