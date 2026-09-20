# ADR 0001 — First production transport (M0)

- **Status:** decided — `acpx -> official DSH ACP` is the first production transport
- **Date:** 2026-09-19 (M0 probe executed 2026-09-20 local time)
- **Decision owner:** project owner (credentials policy approved explicitly for this probe)
- **Related plan sections:** 7 (DSH-first adaptation), 18 (M0), 3 (reuse decisions)
- **Evidence:** `docs/m0-results.md`, `tools/m0_probe/` (runner, mock, client), probe JSON reports

## Decision fields

```text
preferred_transport   = acpx-dsh-acp
production_transport  = acpx-dsh-acp      # selected; Driver implementation is the NEXT task
live_interop          = executed (bounded, 2 top-level submissions)
codex_invocations     = 0
```

Nothing below claims that HFlow's production Driver exists. The probe proved the
transport works; `drivers/selected.py` still refuses to run and stays that way until the
thin Driver is written and tested.

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

**Budget accounting, stated precisely:** the experiment allowed 2 top-level submissions.
Probing the failure mode and the success mode as separate submissions, plus one attempt the
counter correctly refused, means **3 actual submissions** were consumed (1 refused by the
cap). That is a top-level task count, **not** an API-request count and **not** a billing
figure: internal retries and real billed usage are not observable here, so cost stays
`unknown`. no Codex, Reviewer, Planner or subagent calls were made.

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
| model selection | `documented` + catalog observed | options came from the live server; never *set* one |
| reasoning-effort selection | `documented` + catalog observed | never set |
| `session/list`, `session/resume` | `documented` | advertised by the server; not exercised |
| cancellation of an in-flight turn | **`not_tested`** | a hard CTRL_BREAK killed the client before any `session/cancel` reached the agent; cooperative cancel is unverified |
| orphan-free shutdown of the managed process tree | `probed` (weak) | no leftovers seen after successful runs; this is process-scope observation, not a sandbox proof |
| strong read-only enforcement | `unsupported` | nothing in this probe confines writes |
| billed usage / quota observation | `unknown` | `usage_update` reports context usage, which is not a bill |
| long/interrupted turns, reconnect, resume-after-crash | `not_tested` | deliberately out of scope |

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
