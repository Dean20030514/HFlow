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

### Live forced-stop trial (2026-09-20): INCONCLUSIVE

One live top-level task was authorized by the user for baseline `8335cfc` and sent. It
**did not reach the harness**, so the trial is inconclusive and `forced_local_stop_live`
stays `not_tested`. The authorization is consumed; there was no retry, and the M0 ledger is
unchanged.

What the record shows:

| Fact | Evidence |
|---|---|
| the client never started | the invocation's stdout is 0 bytes; stderr is a Python `SyntaxError` on acpx's `dist/cli.js` |
| the harness never booted | the probe's isolated `DSH_HOME` has no `profiles/` directory at all |
| no model work happened | no `session/prompt` was ever observed (`dispatched=false`); no helper READY |
| the trial's own gates behaved | no `helper_ready.json`, no extra dispatch, no late `ACCEPTED`, no orphan processes |

Root cause: the driver launched the Node CLI with the **Python** interpreter
(``python -u .../dist/cli.js``). That is a defect in this repository's driver, not an
upstream or environment problem, and it is a class of bug the offline tests could not catch
because the stand-in client *is* a Python script. The fix selects the interpreter from the
entry point (Node for `.js`/`.mjs`/`.cjs`, Python for `.py`, direct execution otherwise) and
is covered by a regression test that asserts the selected interpreter per entry-point kind.

The mechanism itself is still only offline-proven: an earlier self-check run of the same
probe (stand-in client, no model) reached the full stop sequence - helper started by the
agent, `IsProcessInJob` true for *this* invocation's job, 3 processes in the boundary,
forced stop confirmed in 2.02s, heartbeat stopped, no late acceptance. A stand-in client
replaces DSH, so that run is INCONCLUSIVE by construction rather than a live pass.

A second live task would need a new explicit authorization. Per the trial specification, a
concluded inconclusive result does not return budget.

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
