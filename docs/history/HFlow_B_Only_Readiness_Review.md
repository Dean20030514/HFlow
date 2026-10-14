# HFlow: retain A evidence, finish a focused B readiness review

## Status and execution boundary

The implementation report names `988a531`, `7c8b051`, and `27c54ae`, reports A as a successful live local forced stop, and reports B as a client-configuration startup failure before a DSH session or prompt. These are reported facts: this brief's author has not inspected the local commits, approval records, or raw evidence and has not rerun the project tests.

**New live authorization: none.** This brief is not approval to execute either A or B, to create an approved authorization artifact, or to broaden permissions. Existing B accounting remains used 1/2; the unused reviewer slot is not an implementer retry and does not authorize continuation of a stopped pipeline.

The next deliverable is a small readiness result using existing code and tests, not another framework. Preserve A's scoped evidence. Do not repeat it merely because a later commit has a different SHA. Do not change transport, install tools, build a signing platform, or expand cleanup, process supervision, or billing features.

## 1. Correct the report using existing evidence

For A, clarify the row `extra dispatches / late ACCEPTED = 1 / no`. If one is the total task count, state total=1 and additional=0. If there was actually an additional task beyond the permitted one, retain that as an authorization violation rather than relabeling it. Use existing records; no live work is needed.

For B, distinguish:

- one authorization allowance consumed under the policy in force at the time;
- known client startup/configuration rejection;
- no DSH session/prompt dispatched, according to the reported evidence;
- no candidate, formal verification, or review;
- no authority to repurpose the unused reviewer allowance.

Do not rewrite the historical accounting or replace old observations with results from `27c54ae`. If the stored state is a coarse `outcome_unknown`, retain it as history and explain the known pre-prompt failure in the existing reason/notes fields. Do not route a known startup failure into session recovery or automatic retry.

Record the actual SHA and relevant binding for A and the actual SHA/binding for the failed B. The approved starting baseline and each tested execution baseline are different facts. A task base commit is also distinct from the HFlow runner commit.

## 2. Treat the authorization artifact as data, not proof of authorship

Keep the useful binding, role allocation, and transactional consumption work. However, a literal `provided_by: user` checks only a field value. A model can produce the same bytes as a user. A test rejecting `provided_by: agent` does not establish resistance to an artifact falsely claiming `provided_by: user`.

Read the existing artifact-ingestion path and identify where approval provenance is established independently of the submitted artifact. Check whether the executor can create/import a fresh authorization ID with a matching task and reset allowance. A SQL constraint prevents overspending an existing record through its guarded path; it does not authenticate the creator of a new record.

Use whichever of these descriptions matches the actual implementation:

**Trusted-local, user-attested operation:** a human independently confirms/creates the approval through the existing manual procedure. The artifact records that decision and bounds consumption. The executor is trusted not to forge approvals or modify the controller/database. State explicitly that same-user forgery is not technically prevented. This can be proposed for the disposable supervised trial; it is not an anti-forgery certification or permission to proceed without human confirmation.

**Enforced separation:** an already-existing approval issuer or protected approval store is outside the executing agent's writable and invocable authority. Verify that minting, altering, or substituting an artifact is rejected by that boundary. If no such boundary exists, do not claim this property. If it is a hard user requirement, keep live execution blocked rather than solve it with another string field.

A second CLI, a separate file under the same writable account, a non-inherited environment variable, or a signing key available to the executor does not by itself establish the required separation. Do not build a bespoke signing/identity service for this M2 test.

Narrow the wording of existing tests to what they actually prove. Where a trusted registration route already exists, reuse it for a focused matching-but-unregistered artifact test. Where no such route exists, document the trust assumption instead of adding a fake provenance test.

## 3. Bind permission choices to the actual approved role and task

acpx v0.17.1 documents:

- `defaultPermissions`: `approve-all`, `approve-reads`, or `deny-all`;
- `nonInteractivePermissions`: `deny` or `fail`;
- `approve-all`: automatically approves tool permission requests, not merely file writes;
- CLI policy and configuration are different surfaces; project settings override home settings, and CLI flags override configuration.

The named `permissionPolicy` is documented for the embedded runtime and a per-tool policy is available through specific CLI flags. It is not thereby a valid top-level key in the CLI JSON configuration. Use only the selected installed client's verified interface.

`HFLOW_ALLOW_WRITES=1` may be a local request/configuration switch. It must not independently grant authority or override a role/task's approved permissions. A disposable worktree also does not grant authority by itself.

Before a new B is proposed, show the effective settings separately for implementer and reviewer. Ensure the reviewer does not inherit implementer `approve-all` through a shared environment or config. Derive its settings from its own role binding. Keep review isolation accurately labeled as prompt-only/audit-only unless stronger enforcement is actually proven.

Broad auto-approval must be disclosed as broad tool approval. If the proposed synthetic implementation requires it, it needs an explicit human decision covering that limitation. Do not describe permission to modify one source file as automatic approval of unrestricted tool requests. Do not broaden permissions automatically after a denial.

acpx permission mediation and DSH native tool restrictions are distinct. acpx documentation explicitly warns that its filesystem checks do not confine arbitrary shell commands or hostile same-user processes. Do not claim a sandbox from worktree, environment flags, or config values.

## 4. Validate the exact effective configuration, not only JSON parsing

Reuse `real_client_checks.py` and the existing mock. The new config checks are useful; do not repeat them if their current evidence already covers the exact generated configuration and invocation below.

For each role, verify the selected installed client's resolved view using the same generated non-secret config, home, cwd, explicit custom agent and relevant command flags as the eventual task. Include the actual worktree/project overrides. A check of only the generated home file is insufficient if another layer wins at launch.

Keep preflight credential-free and explicitly mock/metadata-only. Do not run an arbitrary supplied agent command as a purported free config check. Avoid logging auth values or unredacted environment data.

If not already covered, extend the existing mock with one deterministic permission request. Observe the client's permission decision for the implementer and reviewer policies. This proves the client's policy mapping, not DSH native enforcement. Do not introduce another mock framework or a new ACP transport.

Assert the expected effective settings and the absence of unsupported keys in HFlow's narrow generated configuration. A parse success alone does not prove every unknown key was rejected or every supplied setting took effect. Do not duplicate all of acpx's schema.

Freeze or fingerprint the validated non-secret effective settings and verify they still match at dispatch. Existing binding/detail fields are sufficient. An environment variable must not change the permission mode after approval/preflight without detection. Check only relevant drift; do not re-certify all features for every metadata change.

Use the existing authorization and budget transactions. Do not create a second budget store. Verify an invalid effective config or unauthorized permission escalation is rejected before credentials, a model submission, or a new allowance claim. This prospective preflight rule does not refund the already-consumed historical B attempt.

## 5. Preserve A with its actual scope

Compare the small diff between A's actual tested runtime and the proposed B runtime. Version strings alone are not the evidence; relevant launcher, Job, process cleanup and protocol lifetime behavior matter.

If the changes are limited to configuration mapping and audit records without altering the tested process-stop path, preserve the original A result with its original binding and document why it remains applicable to the proposed B path. A did not test every permission mode or every possible tool process; do not enlarge its claim.

If actual process containment/lifecycle behavior changed materially, report that affected prerequisite. Do not spend a new A task or announce equivalence automatically. No unrelated full regression or stop stress test is required.

## 6. B-only execution remains a separate human decision

After the focused readiness checks pass, the next live proposal is B only: a new approval for at most one implementer invocation and, only after valid candidate verification, one independent reviewer invocation. Preserve the old B record and its unused but unusable continuation slot; do not top it up, refund it, or repurpose it.

Bind a new approval to the actual reviewed HFlow runtime/binding, existing prepared task, full base commit `5526040ee01672e321c10c4b98bff9d79c30cbe4`, task and fixed-test digests, role allocations and disclosed effective permissions. Do not have the model manufacture human approval or merely change the authorization ID.

Retain the failed B workspace/evidence. A newly approved attempt should use an independently identified managed run/workspace at the unchanged base. Do not overwrite the old worktree, regenerate a different defect, or provide the implementer with a prepared patch.

The existing M2 acceptance chain remains unchanged: real implementation, frozen candidate, fixed deterministic checks, fresh-session real review, controller-owned acceptance, and local candidate retention. No fake verdict, model call to repair result formatting, extra implementer, automatic repair cycle, or post-failure reviewer dispatch is allowed.

No Codex, planner, subagents, global changes, automatic merge/push/release/clean, or unattended execution. Credentials use only the previously permitted reference and controlled injection. The current report is not a new authorization.

## 7. Finish with one short readiness result

Report only: actual reviewed SHA/binding; A scope and corrected dispatch count; honest authorization trust model; implementer/reviewer effective permission modes; existing or new zero-model evidence covering those modes; and whether B is ready for a new human decision or has one specific blocker.

Already-correct code needs no new commit. Necessary fixes and focused tests may use a single commit such as `fix(auth): align approval and permission enforcement with the declared trust model`. Do not label a documentation-only correction as anti-forgery enforcement.

**Stop condition:** finish this focused review without any new live dispatch. Do not develop an identity platform or resume transmission research. The remaining live goal is the prepared B candidate, not another successful setup demo.

## Sources and evidence boundary

Local facts above come from the implementation report, not an independent source audit. Primary references checked for this review:

- acpx v0.17.1 configuration: https://github.com/openclaw/acpx/blob/v0.17.1/docs/config.md
- acpx v0.17.1 permissions: https://github.com/openclaw/acpx/blob/v0.17.1/docs/permissions.md
- JSON Schema const (a value constraint): https://json-schema.org/understanding-json-schema/reference/const
- MITRE CWE-807 (untrusted inputs in security decisions): https://cwe.mitre.org/data/definitions/807.html
- OWASP Authorization Cheat Sheet: https://cheatsheetseries.owasp.org/cheatsheets/Authorization_Cheat_Sheet.html
- Existing `HFlow_M2_Controlled_Live_Acceptance.md`: separate A/B allowances, role allocation, stop-on-failure and unchanged M2 acceptance criteria.
