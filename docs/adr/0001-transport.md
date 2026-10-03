# ADR 0001 — First production transport (M0)

- **Status:** decided — `acpx -> official DSH ACP` is the first production transport
- **Date:** 2026-09-19 (M0 probe executed 2026-09-20 local time)
- **Decision owner:** project owner (credentials policy approved explicitly for this probe)
- **Related plan sections:** 7 (DSH-first adaptation), 18 (M0), 3 (reuse decisions)
- **Evidence:** `docs/m0-results.md`, `tools/m0_probe/` (runner, mock, client), probe JSON reports

## Decision fields

```text
preferred_transport          = acpx-dsh-acp
transport_selection          = acpx-dsh-acp      # decided in M0
driver_implementation        = in_progress       # thin Driver exists; not production-certified
basic_live_roundtrip         = passed (reported evidence from M0)
protocol_cancel_for_selected_exec = unsupported  # one-shot exec has no queue owner to reach
forced_local_stop_offline    = passed            # stubborn stub + Job Object teardown
forced_local_stop_live       = not_tested        # see the trial record below
unattended_execution         = disabled
production_transport         = acpx-dsh-acp      # single implementation, no runtime fallback
codex_invocations            = 0
```

"Nothing blocks further development" is true. "Nothing blocks production release" is
**not** true: cooperative cancellation does not exist on this launch path, and the only stop
mechanism proven so far is forced teardown of the managed process boundary - proven offline,
not against DSH.

### Live forced-stop trial (2026-09-20): INCONCLUSIVE - client launch failed

One live top-level task was authorized by the user for baseline `8335cfc` and sent. It
**did not reach the harness**, so the trial is inconclusive and `forced_local_stop_live`
stays `not_tested`. The authorization is consumed; there was no retry, and the M0 ledger is
unchanged.

Two levels are deliberately kept apart:

| Level | Count | Meaning |
|---|---|---|
| authorization gate | 1 consumed | the trial's single slot, spent as designed (startup failures do not refund) |
| real ACP `session/prompt` dispatch | 0 | the harness was never reached, so no model work was possible |

The cause is **known**, not unknown: the driver launched the Node CLI with the **Python**
interpreter (``python -u .../dist/cli.js``), which cannot parse JavaScript. That was a defect
in this repository's driver - a class the offline tests could not catch, because the test
stand-in client is itself a Python script.

What the record shows:

| Fact | Evidence |
|---|---|
| the client never started | the invocation's stdout is 0 bytes; stderr is a Python `SyntaxError` on acpx's `dist/cli.js` |
| the harness never booted | the probe's isolated `DSH_HOME` has no `profiles/` directory at all |
| no model work happened | no `session/prompt` was ever observed (`dispatched=false`); no helper READY |
| the trial's own gates behaved | no `helper_ready.json`, no extra dispatch, no late `ACCEPTED`, no orphan processes |

**Fix (`581c2ff`)**: the interpreter is selected from the entry-point kind (Node for
`.js`/`.mjs`/`.cjs`, Python for `.py`, direct execution otherwise), covered by a regression
test. **Real-client evidence after the fix (`tools/m0_probe/real_client_checks.py`, both
PASS, zero model calls, no credential, no real DSH):**

| Check | What it ran | Result |
|---|---|---|
| `version` | the installed acpx through the driver's own argv/boundary/drain code: `node .../dist/cli.js --version` | rc=0, reported `0.17.1`, process reaped, boundary empty |
| `mock` | the real acpx client against the project's existing mock ACP agent, full one-shot `exec` (structured argv, task stdin, Job, event drain) | `stopReason` observed, nonce echoed, client gone, no unparsed lines |

The mechanism itself is still only offline-proven: a self-check run of the same stop probe
(stand-in client) reached the full stop sequence - helper started by the agent,
`IsProcessInJob` true for *this* invocation's job, 3 processes in the boundary, forced stop
confirmed in 2.02s, heartbeat stopped, no late acceptance. A stand-in client replaces DSH, so
that run is INCONCLUSIVE by construction rather than a live pass.

A second live task would need a new explicit authorization, and only after the zero-model
checks for the current combination hold.

## Context

The controller needs exactly one production transport to a native Harness. The plan's M0
prefers `acpx -> native DSH ACP` and names the official DSH headless surface as the single
fallback, never both at once. This ADR records what the experiment actually produced and
separates that from the decision.

## Facts observed on this machine

Read-only local checks plus one bounded live probe. No global install, no PATH change, no
edit to `~/.dsh` or `~/.acpx`, no plugin change, no DSH source build, no global config
write. All probe state lives in the gitignored `.probe/` scratch directory.

| Item | Observed |
|---|---|
| Python / Node / Git | 3.14.7 / v24.19.0 / 2.55.0.windows.5 |
| DSH CLI | 0.1.5-rc.1 at `C:\Users\16097\AppData\Roaming\npm\dsh.CMD` |
| `@deepseek-ai/dsh-acp` in the installed package | present (0.1.5-rc.2) |
| Shipped profile templates in the installed launcher | `acp`, `web`, `headless`, `sdk`, `sdk-minimal` (`dsh-app-boot` `PROFILE_TEMPLATES`) |
| `acpx` | not installed before this probe; **0.17.1** installed **project-locally** at `.probe/acpx` (MIT, `engines.node >=22.13.0`, `bin: dist/cli.js`), lockfile kept |
| acpx home override | none. The bundle reads no `ACPX_HOME`; `~/.acpx` is resolved from `os.homedir()`, so the probe child gets a private `USERPROFILE`/`HOME` instead |
| `$DSH_HOME` override | supported: `resolveDshHome` prefers an explicit path, then a **non-empty** `DSH_HOME`, then `~/.dsh`; blank is treated as unset |
| Probe-private DSH home | `dsh --profile acp --dump-config` with `DSH_HOME` pointed at `.probe/home/...` created `profiles/acp` and composed `@deepseek-ai/dsh-base` + the ACP app bundle; exit 0 |
| Model catalog exposed by the live server | `deepseek-official` group, current `["deepseek-official","deepseek-v4-flash"]`; also `deepseek-flash`, `deepseek-v4-pro`, `deepseek-v4-flash-vision-exp`; `reasoning_effort` options off/low/high/max |

### Windows-specific contract facts (these change Driver code, not just docs)

1. **Raw agent command strings are unsupported on win32.** `acpx --agent "<command>"`
   fails with an explicit error telling the caller to supply an argv array; the config form
   is `agents.<name>.argv`. The probe therefore uses the structured argv boundary only.
2. **A Windows batch shim cannot be launched by `CreateProcess` directly.** `dsh` resolves
   to `dsh.CMD`, and Python raises `FileNotFoundError` for that path; the launch must go
   through `cmd.exe /c`. This looked exactly like a DSH startup failure and is not one.
3. **The ACP server needs a live stdin pipe.** With stdin at EOF the server answers the
   pending request and exits 0, which reads as "the profile does nothing".

## What the probe actually proved

Layered, and the layers do not borrow each other's credibility (details in
`docs/m0-results.md`):

**`acpx -> mock ACP`** (a mock replaces DSH, so this is client-behaviour evidence only):
handshake, session create, one prompt turn, streamed updates, normal termination, explicit
JSON-RPC error, server death mid-turn, `--timeout` producing a distinct timeout error.

**`acpx -> real DSH ACP`** (bounded to **2 top-level submissions**):

- Non-prompt check: server started, `initialize` returned
  `agentInfo={"name":"deepseek-harness-acp","version":"0.0.1"}` with
  `sessionCapabilities={close,list,resume}` and `authMethods=[]`; `session/new` returned a
  real session id plus the live model catalog; `session/close` returned success. **No
  prompt was sent, so this cost zero inference.**
- Submission 1, *without* credentials: reached the real server, then the turn failed with
  `llm-deepseek: no API key for provider route "deepseek-official"`. The credential boundary
  is explicit and fail-closed.
- Submission 2, *with* controlled credential injection: `initialize`, `session/new`,
  `session/prompt`, streamed `agent_message_chunk` / `agent_thought_chunk` / `tool_call` /
  `tool_call_update` / `usage_update` (used 8476 -> 8695 of 1000000), the model really read
  `fixture.txt` via a tool call, echoed the nonce, and the turn settled on
  `stopReason: end_turn` with process exit 0.

**Credential handling used for submission 2**, recorded because it touches secrets: the
owner approved it explicitly. The probe reads **one** named reference
(`DEEPSEEK_API_KEY`) from the owner's own DSH managed store, holds the value in memory, and
passes it to the probe child through the environment - which is DSH's documented
highest-precedence source. The value was never printed, logged, or written to disk by the
probe, and the real `~/.dsh` was only read. The isolated probe home still has no
credentials of its own.

**Write scope, verified after the runs:** the acpx config, sessions and per-project state
all landed under `.probe/`; `~/.acpx` does not exist and `~/.dsh/sessions` was not touched
during the probe window. One orphaned `dsh-subprocess-local` node process from an early
crash-path run was found and killed; the later runs left no new processes behind.

**Budget accounting, stated precisely** (clarified from the recorded counter, no re-run):
three *attempted* top-level submissions, of which **two were admitted and sent** (one
without credentials, one with) and **one was blocked before dispatch** by the persisted
counter - it returned a `skipped` payload and left no workspace, no run directory and no
`session/prompt` anywhere in its record. So
`attempted=3, admitted=2, blocked_before_dispatch=1`. The M0 allowance is spent and is not
reset by creating new probe files or directories. That is a top-level task count, **not** an
API-request count and **not** a billing figure: internal retries and real billed usage are
not observable here, so cost stays `unknown`. No Codex, Reviewer, Planner or subagent calls
were made.

## Decision

1. **Select `acpx -> official DSH ACP` as the first production transport.** The necessary
   behaviours for a single-task, one-shot dispatch round trip were observed with real
   evidence on this machine and this DSH build.
2. **The headless fallback is not implemented.** It stays documented as the alternative
   should the selected path fail later; building both is still forbidden. M0 does not need
   it, because the selected path passed.
3. **One-shot semantics only for the first Driver.** Each task/review gets its own
   invocation and session; no transparent reconnect, no session replacement, no replay.
4. **The Driver is the next task, not this one.** `drivers/selected.py` keeps refusing with
   `NOT_IMPLEMENTED` until a thin Driver implements `probe/start/cancel/reconcile` against
   this transport and passes contract tests.
5. **Capabilities that were not proven stay unproven.** See the list below; the Driver must
   not advertise them.

## Capabilities: verified vs unverified

| Capability | State | Note |
|---|---|---|
| start server, initialize, session/new, session/close | `probed` (live) | observed with zero prompts |
| one prompt turn with tool use and streamed updates | `probed` (live) | nonce round trip, `end_turn`, exit 0 |
| explicit failure when credentials are missing | `probed` (live) | fail-closed, no silent fallback |
| launch, observe, collect through the thin Driver | `probed` (offline) | 23 contract tests against a stub process tree |
| forced stop of the managed process boundary | `probed` (offline) | Job Object teardown of a client + stubborn descendant; **not** exercised on DSH |
| model selection | `documented` + catalog observed | options came from the live server; never *set* one |
| reasoning-effort selection | `documented` + catalog observed | never set |
| `session/list`, `session/resume` | `documented` | advertised by the server; not exercised |
| cooperative cancellation (`session/cancel`) | **`unsupported` on this launch path** | the `exec` one-shot mode has no queue owner, and `acpx cancel` resolves through one; a CTRL_BREAK killed the client before any cancel reached the agent |
| orphan-free shutdown of the managed process tree | `probed` (weak) | no leftovers after successful runs and after forced stops; process-scope observation, not a sandbox proof |
| strong read-only enforcement | `unsupported` | nothing in this design confines writes |
| billed usage / quota observation | `unknown` | `usage_update` reports context usage, which is not a bill |
| long/interrupted turns, reconnect, resume-after-crash | `not_tested` | deliberately out of scope |

## Driver increment (offline)

`src/hflow/drivers/acpx_dsh.py` implements the one production transport behind the neutral
contract (`probe`, `start_handle`, `observe`, `collect`, `cancel_handle`,
`reconcile_handle`; plus the plain `start`/`cancel`/`reconcile` forms for
`HarnessDriver`-only callers). Facts it encodes rather than assumes:

* the agent is passed as structured **argv** in a per-invocation acpx config; a raw command
  string is rejected by acpx on win32;
* the Windows `.CMD` launcher goes through `cmd.exe /c`, and only the launcher path and the
  fixed profile flag appear there - task text, nonce and credentials never do;
* the task body travels as acpx's documented stdin input (``exec -f -``) and that pipe is
  closed once written; the ACP pipe between acpx and DSH belongs to acpx;
* the invocation runs inside a Windows **Job Object** boundary created before the process
  is resumed, so there is no "start it, then try to catch it" window;
* the dispatch marker is the observed `session/prompt` send, so "no model work happened" is
  a fact (``agent_turns=0``) when a stop lands before it;
* output is followed by explicit byte offset (a buffered reader that latches EOF silently
  drops everything after the first read), with a raw-log cap whose overflow marks the result
  untrustworthy rather than successful.

Stop semantics are split on purpose:

| Mechanism | Meaning | Receipt |
|---|---|---|
| cooperative | a protocol cancel finished the work | never reported here - unsupported on this path |
| forced | the managed process boundary was terminated | `confirmed_stopped`, `mechanism="forced"`, `local_process_stopped=true` |
| none | nothing was stopped (already exited, or unconfirmed) | `confirmed_stopped`/`still_running`/`unknown` with `mechanism="none"` |

`confirmed_stopped` with `mechanism="forced"` means **local execution stopped**. It does not
mean the protocol cancelled cleanly, does not mean the business result is known, and does
not mean remote billing stopped.

## Consequences

- The Driver must run the launched CLI through the structured **argv** boundary and must
  route the Windows `.cmd` shim through `cmd.exe`; both are now known-not-optional.
- The Driver must own the child's stdin pipe for the lifetime of a session, and must treat
  "process exited 0 with no result" as unknown rather than success.
- HFlow must pass a per-invocation environment that can carry a credential reference; the
  controller never stores or logs the value, and `probe` reports only whether a credential
  is resolvable.
- Unattended production execution is **not** approved yet: cooperative cancellation and
  post-cancel process termination are unverified, so a cancelled run may leave work
  running. Until that is measured, HFlow may at most claim basic round-trip success.
- `usage_update` is context usage (used/size), so the receipt keeps
  `provider_billed_tokens`, `provider_cost` and `subscription_quota_remaining` as `null`.

## Addendum 2026-10-02 — upstream survey and launch hardening

Appended, not a rewrite: the decision above stands (`acpx -> official DSH ACP`, one-shot
`exec`), and no capability state in the tables above is upgraded by anything here. This addendum
records an upstream survey made on 2026-10-02 by reading release metadata, source and
documentation, plus what the 2026-10-02 refinement now enforces and observes. **Nothing in the
survey was executed against real DSH or a model**; every upstream fact below is labelled
*documented* (read in upstream source, docs or release metadata), and the two local facts are
labelled for what they are.

### acpx (documented)

| Fact | Label |
|---|---|
| The pinned acpx 0.17.1 is tag `v0.17.1`, commit `50a47ad10a75431cbc276ec9b555d11fe1f69c84` (published 2026-09-20), the version installed at `.probe/acpx` | documented |
| The latest release is `v0.19.4`, commit `8e396609238086dee6a407fdb3b3ac46dbdedd70` (published 2026-10-01); `v0.17.1...v0.19.4` is 179 commits | documented |
| No acpx surface HFlow uses breaks in that range: the per-invocation config keys and `agents.<name>.argv`, `exec -f -` from stdin, the raw ACP JSON-RPC NDJSON stream under `--format json` (both directions), the exit codes, and the `ACPX_*` variables acpx reads. The changes are additive (a JSON-RPC error line for config start-up errors, a reported agent disconnect after partial output, a new `EXEC_DISABLED` code) | documented |
| 0.18.0 fixed a read-classification bug under `approve-reads` (a permission request with no kind whose title merely contained "read" or "cat" was auto-approved); 0.19.x adds Windows descendant snapshots through PowerShell, which lengthen start-up and exit; 0.19.3/0.19.4 tolerate `{}`/non-list `set_config_option` replies | documented |
| **The pin stays at 0.17.1** by the user's decision; the upgrade is a separate, later step that has to pass offline checks first | decision |
| acpx always loads `<--cwd>/.acpxrc.json`; scalar settings in it beat the global config, and a project `agents.<name>` entry replaces the global agent argv. There is no flag or variable to skip or pin it: upstream issue openclaw/acpx#835 (opened 2026-09-29) is open, and not fixed in v0.19.4 | documented |
| `exec` is wrapped in an interrupt handler: on SIGINT, SIGTERM or SIGHUP it sends `session/cancel` for the active prompt and waits up to 2.5 s before closing; SIGBREAK is not handled; `--timeout` expiry sends no `session/cancel` | documented |
| `--timeout` bounds each phase (start, session creation, model change, each config option, the prompt) separately, not the whole call | documented |
| The global `--model <id>` flag exists at 0.17.1: after `session/new` acpx picks the select option with category `model` (preferring id `model`; grouped options supported), sends `session/set_config_option` before `session/prompt` unless the value is already current, and refuses an unadvertised value (or an agent with no model option) with a `RUNTIME` error and no prompt sent. It also forwards the value in `session/new` `_meta.claudeCode.options.model`. The config file has no model key | documented; the behaviour against a mock agent is offline-tested |

The capability table's reason for `unsupported` cooperative cancellation ("the `exec` one-shot mode
has no queue owner") is imprecise in the light of the source: the `exec` path *does* have an
interrupt-driven `session/cancel`. The limit is delivery: HFlow starts the client with
`CREATE_NEW_PROCESS_GROUP`, which disables Ctrl+C for that group on Windows, Ctrl+Break reaches
Node as SIGBREAK (unhandled), and Windows offers no external SIGTERM/SIGHUP. The M0 CTRL_BREAK
observation (client killed, exit `0xC000013A`, no cancel reached the agent) fits that reading;
the rest is reasoned, not measured. The state stays `unsupported`.

### DSH (documented)

| Fact | Label |
|---|---|
| Surveyed range: `dsh-v0.1.7-rc.1` = `46a7f68b0922371ce7144b668b90e377d8e799f4` (2026-09-23), `dsh-v0.1.7-rc.2` = `477b4f420553e8a52c2fbccc464d7561b239c443`, `dsh-v0.2.0-rc.1` = `4878cdabd87d4041bdaff61d04c966883b9fd07a`, `dsh-v0.2.0-rc.2` = `639ed015397290b3745d163aafe02ffee4aa3f84` (2026-09-29, npm `latest`). All four are prereleases, and the 0.2.0-rc.2 README calls DSH a developer preview that will have compatibility-breaking changes | documented |
| Between `dsh-v0.1.7-rc.1` and `dsh-v0.2.0-rc.2` the ACP package and the `acp` app bundle change only translation files and version numbers: the ACP server source, the advertised methods (`initialize`, `authenticate`, `session/new`, `session/list`, `session/resume`, `session/close`, `session/set_config_option`, `session/prompt`, `session/cancel`) and the launcher flags (`--profile`, `--patch`, `--dump-config`, ...) are the same | documented |
| ACP `initialize` hard-codes `agentInfo {name: "deepseek-harness-acp", version: "0.0.1"}` at every surveyed version, so the DSH version cannot be read over ACP | documented |
| ACP removed the unstable `session/set_model` method (ACP 0.13.5); the stable model channel is a `configOptions` entry with category `model`, changed through `session/set_config_option`. DSH implements exactly that: option id `model`, value the opaque `JSON.stringify([provider, model])` (for example `["deepseek-official","deepseek-v4-pro"]`), plus a `reasoning_effort` option with category `thought_level`. DSH has no model CLI flag, environment variable or `_meta` field | documented |
| The live catalog in "Facts observed on this machine" was observed on DSH 0.1.5; at 0.2.0 the `model` select may carry a second provider group (`deepseek-account`), and the 0.2.0-rc.2 release notes say some older model ids were removed | documented |
| DSH maps `blocked` and `aborted` turns to `end_turn`, and can settle a prompt as `cancelled` without a client cancel (session disposal) | documented |
| DSH answers a failed prompt with a JSON-RPC error, not a stop reason: `RequestError.internalError` (-32603) gives `turn failed: ...` / `assistant output delivery failed: ...` after the turn and `prompt was not queued: ...` (plus invalid-params and content-admission errors) before any model work (`packages/acp/acp/src/session.ts` @639ed015). DSH uses `@agentclientprotocol/sdk` 1.4.0, which numbers each side's requests from 0, so DSH's permission request ids can equal acpx's prompt id. ACP 59172baf adds a v1 test commented "v1 reports prompt failures as JSON-RPC errors" | documented |
| DSH's permission requests carry only a tool-call id; DSH reads its sandbox mode from `DSH_PERMISSION_MODE` (default `workspace-write`, approval `ask`) and its tools mode from `DSH_TOOLS_MODE`; its session-log upload to DeepSeek is enabled by default | documented |
| This machine has only DeepSeek Harness Desktop 0.2.0.0 installed (bundled `dsh.cmd` shim, not on `PATH`); there is no npm-global `@deepseek-ai/dsh` and no `dsh` on `PATH`, so the npm `dsh.CMD` recorded at M0 is gone. At the time of the survey HFlow's launch resolution (`shutil.which("dsh")`) would then have fallen back to the bare name; it no longer does - it searches only absolute `PATH` entries and, with no `dsh` there, reports the launch not resolvable | observed read-only (a file-system listing; nothing was executed) |

### What the 2026-10-02 refinement enforces and observes

All of it is offline-tested (production driver over the Python stand-in client, and the pinned
acpx 0.17.1 against the project's mock agent); none of it is live evidence.

- **Enforced:** a workspace containing any `.acpxrc.json` entry is refused inside the spawn gate
  before any process exists (`workspace_client_config`); the DSH batch shim is wrapped in the
  absolute `%SystemRoot%\System32\cmd.exe` (no bare-name search of the worktree); ambient
  `DSH_PERMISSION_MODE` and `DSH_TOOLS_MODE` are removed from the child environment; a profile's
  validated `model_selection` other than `native_profile` is passed as the client's `--model`,
  bound in the launch digest (a user ruling, 2026-10-02, that such a value is a fixed flag under
  AGENTS.md rule 9); a turn's stop reason is taken only from the response to the observed
  `session/prompt` id (otherwise `OUTCOME_UNKNOWN` / `unbound_completion`); an error answering
  that id is recorded as `prompt_error_response` and a stop reason outside ACP v1's set as
  `unknown_stop_reason`, both still `OUTCOME_UNKNOWN` (the pinned acpx relaying the error is
  covered against the mock agent); an unrequested `cancelled` is `FAILED`; a stop asks the Job
  Object before confirming, and descendants left inside the Job after the client exits are
  terminated.
- **Observed per invocation:** the `session/new` `configOptions` model entry and the
  `thought_level` entry, outbound `session/set_config_option` and its response, and
  `config_option_update` notifications, recorded as `model_observation` / `model_applied`.
- **Unchanged capability states:** cooperative cancellation `unsupported` (reason corrected
  above), model selection `documented` (a live `set_config_option` round trip has not been
  observed, and concrete model ids for the current DSH build need an approved zero-prompt catalog
  check first), read-only enforcement `unsupported`, billed usage `unknown`.
- **Deferred:** binding the launch by program content rather than by path, and the acpx 0.19.x
  upgrade.

## Addendum 2026-10-03 — re-survey and batch F

Appended; the decision and every capability state above stand. A second read of the same upstreams
(release metadata, compare views and source, nothing executed) found nothing that changes HFlow's
transport:

| Fact | Label |
|---|---|
| acpx has no release after `v0.19.4`; `main` moved past it only by dependency bumps (a `pnpm-lock.yaml` change and `@types/ws`, head `27efb1b57b9de22105a91e9154b1d29b51ede8cb` on 2026-10-03). `src/async-control.ts` is byte-identical at `v0.17.1` and `v0.19.4`: `exec` still sends `session/cancel` only on SIGINT/SIGTERM/SIGHUP, `--timeout` still sends none, and `exec` offers a host no other cancel channel (no stdin control message, no IPC) | documented |
| openclaw/acpx#835 (pin or skip the project `.acpxrc.json`) is still open with no maintainer decision and no linked PR; none of the proposed flag names is settled, so HFlow keeps refusing such a workspace | documented |
| DSH has no release after `dsh-v0.2.0-rc.2`; its ACP package sources are blob-identical on `master`. DSH sends no `PromptResponse.usage` and no `usage_update.cost` (only `used`/`size`) | documented |
| ACP schema `v1.24.1` is unchanged: `PromptResponse` is still `{stopReason, _meta}`, the stop-reason set is still closed, and there is still no turn or prompt identifier on `session/update` | documented |

What batch F changed, all offline-tested (production driver over the Python stand-in client, the
pinned acpx 0.17.1 against the project's mock agent, and recorded-stream replays); none of it is
live evidence and no capability is upgraded: a JSON-RPC error answering the prompt is recorded
(`prompt_error_response`, still `OUTCOME_UNKNOWN`); a stop reason outside v1's set is
`OUTCOME_UNKNOWN`; the reviewer's answer is taken only from the session the first prompt named,
grouped by `messageId` so a same-id reasoning block neither splits nor joins it, and never from
a chunk sent after the prompt response or from a stream not read to its end; a stop is confirmed
only by an observed exit (`ERROR_INVALID_PARAMETER`, a signalled process, or the driver's own
`Popen` handle) together with an empty Job. See README "Non-negotiables" and "Not verified" for
the rest (shared Git metadata comparison, the launch-surface and DSH-context records).
