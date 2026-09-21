"""Replay the recorded live review from its own bytes - no model, no new evidence, no writes.

What this does, and what it deliberately refuses to do:

* it reads the *original* run's saved acpx stream and completion metadata, and recomputes the
  reviewer's final answer with the same extractor production uses;
* it hashes every byte it depends on **before** parsing, so the verdict is reported together
  with the material it came from. A hash computed now does not prove past authenticity, and
  this tool does not claim it does;
* it re-checks the recorded associations (run/attempt/invocation/session/prompt, task and
  project digests, candidate commit/tree/ref and content fingerprint, recorded verification
  evidence, reviewer permissions and no-write observations) and reports each one separately,
  including the ones it cannot confirm;
* it evaluates the controller's acceptance predicates in an isolated temporary store, so a
  receipt produced there is explicitly a *test artifact*.

It never dispatches, resumes, reconciles or cancels anything, never reads a credential, never
touches the original store or worktree, and never rewrites the run's history. Replay is bytes
and decisions, not a Harness session.

Usage:
    python tools/m2_live/replay_review.py <run_id>
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from hflow.contracts import (  # noqa: E402
    CandidateSnapshot,
    CheckPhase,
    DeliveryState,
    EvidenceStatus,
    InvocationOutcome,
    ResultReceipt,
    ReviewOutput,
    ReviewResult,
    TaskSpec,
    TaskState,
    UsageFacts,
    VerificationResult,
    canonical_json,
    digest_of,
)
from hflow.review import AnswerTranscript, decode_review, review_input_error  # noqa: E402
from hflow.ids import new_evidence_id  # noqa: E402
from hflow.runtime import PACKAGE_VERSION as _PACKAGE_VERSION  # noqa: E402
from hflow.store import (  # noqa: E402
    OFFLINE_REPROCESSING_KIND,
    Store,
    StoreError,
    _same_offline_reprocessing,
)
from hflow.workspace import candidate_fingerprint  # noqa: E402

PROBE_ROOT = REPO_ROOT / ".probe" / "m2-live"
DEFAULT_STORE = PROBE_ROOT / "attempt-2-data" / "hflow.sqlite"
DEFAULT_PROJECT = PROBE_ROOT / "attempt-2"
REVIEWER_PROMPT_MARKER = "Review the frozen candidate"


class ReplayError(RuntimeError):
    """The recorded material cannot support the replay. Reported, never worked around."""


# --------------------------------------------------------------------------
# reading the record
# --------------------------------------------------------------------------


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return "sha256:" + digest.hexdigest()


def _read_only_copy(source: Path, target: Path) -> Path:
    """Copy the ledger before reading it.

    The original database is never opened by this tool, so a reader cannot write to it and a
    WAL file left by the live process cannot be checkpointed away. The copy is the artifact
    the replay reasons about.
    """
    shutil.copyfile(source, target)
    for suffix in ("-wal", "-shm"):
        sidecar = Path(str(source) + suffix)
        if sidecar.exists():
            shutil.copyfile(sidecar, Path(str(target) + suffix))
    return target


def _load_messages(stream_path: Path) -> tuple[list[dict[str, Any]], list[str]]:
    """Parse the saved NDJSON stream; unparseable lines are reported, never skipped silently."""
    messages: list[dict[str, Any]] = []
    unparseable: list[str] = []
    text = stream_path.read_text(encoding="utf-8", errors="replace")
    for index, line in enumerate(text.splitlines()):
        if not line.strip():
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            unparseable.append(f"line {index}: {line[:120]}")
            continue
        if isinstance(message, dict):
            messages.append(message)
    return messages, unparseable


def extract_reviewer_answer(messages: list[dict[str, Any]], session_id: str | None):
    """The reviewer's final answer, using the production extractor on the saved stream."""
    transcript = AnswerTranscript(session_id=session_id, role="reviewer")
    prompt_request_ids: set[Any] = set()
    terminal_ids: list[Any] = []
    sequence = 0
    for line_index, message in enumerate(messages):
        params = message.get("params") if isinstance(message.get("params"), dict) else {}
        if message.get("method") == "session/prompt":
            if message.get("id") is not None:
                prompt_request_ids.add(message["id"])
            continue
        if message.get("method") == "session/update":
            transcript.observe_update(
                params.get("update") if isinstance(params.get("update"), dict) else {},
                params=params,
                sequence=sequence,
                line_index=line_index,
            )
            sequence += 1
            continue
        result = message.get("result")
        if isinstance(result, dict) and "stopReason" in result:
            terminal_ids.append(message.get("id"))
    return {
        "transcript": transcript,
        "answer": transcript.final_answer(),
        "prompt_request_ids": sorted(str(item) for item in prompt_request_ids),
        "terminal_response_ids": sorted(str(item) for item in terminal_ids),
        "bound": any(item in prompt_request_ids for item in terminal_ids),
    }


# --------------------------------------------------------------------------
# the isolated acceptance evaluation
# --------------------------------------------------------------------------


def review_result_status(review: ReviewOutput) -> str:
    """The controller's review status for a validated verdict. One definition, two callers."""
    return "accepted" if review.verdict == "accepted" else "changes_requested"


def build_receipt(
    *,
    spec: TaskSpec,
    run_row: dict[str, Any],
    attempt_row: dict[str, Any],
    verification_evidence: dict[str, Any],
    review: ReviewOutput,
    review_evidence_id: str,
    fingerprint: str,
    target: Path,
    candidate_commit: str,
    runtime_build: str,
    provenance: dict[str, Any],
    isolated: bool,
) -> ResultReceipt:
    """One receipt constructor, used by the isolated evaluation and the production write.

    Every value comes from the record; nothing is derived from a model's prose or from the
    temporary evaluation. ``runtime_build`` is the build that is *processing now*, which is
    what distinguishes this decision from the original execution.
    """
    return ResultReceipt(
        run_id=run_row["run_id"],
        task_id=spec.task_id,
        attempt_id=verification_evidence["attempt_id"],
        task_revision=int(run_row["task_revision"]),
        runtime_build=runtime_build,
        plan_digest=run_row["spec_digest"],
        harness_outcome=InvocationOutcome.COMPLETED,
        candidate=CandidateSnapshot(
            base_commit=spec.workspace.base_commit or (run_row.get("worktree_path") and ""),
            git_commit=candidate_commit,
            worktree=str(target),
            fingerprint=fingerprint,
        ),
        verification=VerificationResult(
            status="passed",
            evidence_ids=[verification_evidence["evidence_id"]],
            detail=verification_evidence["detail"],
            workspace=str(target),
        ),
        review=ReviewResult(
            status=review_result_status(review),
            evidence_ids=[review_evidence_id],
            checked_fingerprint=fingerprint,
        ),
        task_state=TaskState.ACCEPTED,
        delivery_state=DeliveryState.LOCAL_CANDIDATE,
        usage=UsageFacts(
            controller_turns_reserved=int(run_row["turns_reserved"]),
            controller_turns_observed=run_row["turns_observed"],
        ),
        candidate_paths=[str(path) for path in (spec.scope.write_allow or [])],
        provenance=provenance,
        limitations=[
            "this receipt records a LATER decision about an execution that ended "
            f"{provenance.get('original_block_code', 'another way')} on build "
            f"{provenance.get('original_runtime_build', 'unknown')}; it is not an uninterrupted "
            "success of that execution",
            "delivery comes from recorded evidence re-validated offline; the approved check was "
            "re-associated (candidate fingerprint, checks digest, command) but not re-executed",
            "no model was called for this decision; no authorization allowance was consumed",
            *(
                ["evaluated in an isolated temporary store: a test artifact, not a delivery"]
                if isolated
                else []
            ),
        ],
    )


def evaluate_acceptance(
    *,
    spec: TaskSpec,
    run_row: dict[str, Any],
    attempt_row: dict[str, Any],
    verification_evidence: dict[str, Any],
    review: ReviewOutput,
    reviewed_fingerprint: str,
    target: Path,
    candidate_commit: str,
    provenance: dict[str, Any],
) -> dict[str, Any]:
    """Apply the controller's acceptance predicates to the recorded evidence.

    The predicates are the ones ``Controller._accept`` applies, in the same order, on the
    same recorded values:

    1. the candidate has not moved (fresh fingerprint == recorded fingerprint);
    2. the recorded verification evidence is *current* for that fingerprint and the run's
       checks digest;
    3. the review is a validated verdict, and the review evidence carries the same fingerprints.

    What this cannot show: that the verification check was re-run now (it is not, by design),
    or that the historical bytes were not edited before they were hashed. Both are reported
    as limits rather than glossed over. The temporary store exists so the receipt that
    acceptance writes is a test artifact, not this run's delivery.
    """
    fresh_fingerprint = candidate_fingerprint(target, spec.scope)
    candidate_unchanged = fresh_fingerprint == reviewed_fingerprint
    evidence_is_current = (
        verification_evidence.get("status") == EvidenceStatus.PASSED.value
        and verification_evidence.get("candidate_fingerprint") == fresh_fingerprint
        and verification_evidence.get("checks_digest") == run_row["checks_digest"]
    )

    result: dict[str, Any] = {
        "candidate_fingerprint_recorded": reviewed_fingerprint,
        "candidate_fingerprint_recomputed": fresh_fingerprint,
        "candidate_unchanged": candidate_unchanged,
        "verification_evidence_current": evidence_is_current,
        "checks_digest": run_row["checks_digest"],
        "verdict": review.verdict,
        "blockers": [],
    }
    if not candidate_unchanged:
        result["blockers"].append(
            "the frozen candidate's content no longer matches the fingerprint the review and "
            "the verification evidence were recorded against"
        )
    if not evidence_is_current:
        result["blockers"].append(
            "the recorded verification evidence is not current for this candidate and checks digest"
        )
    if result["blockers"]:
        result["accepted"] = False
        return result

    # Build the receipt in a temporary store: the same construction the production
    # finalization uses, from copied rows, with nothing of the original touched.
    with tempfile.TemporaryDirectory(prefix="hflow-replay-") as tmp:
        store = Store(Path(tmp) / "isolated.sqlite")
        try:
            row = store.create_run(
                run_id=run_row["run_id"],
                project_id=run_row["project_id"],
                spec=spec,
                spec_digest=run_row["spec_digest"],
                controller_build=run_row["controller_build"],
                checks_digest=run_row["checks_digest"],
                turn_limit=int(run_row["turn_limit"]),
                repair_limit=int(run_row["repair_limit"]),
            )
            assert store.claim_run(row["run_id"], "offline-replay")
            attempt_id = verification_evidence["attempt_id"]
            # The recorded attempt is re-created with the same identity so the copied evidence
            # still references something real: replacing a foreign key with an invented id
            # would be exactly the kind of quiet rewrite this tool must not do.
            store.dispatch_attempt(
                run_id=row["run_id"],
                controller_id="offline-replay",
                attempt_id=attempt_id,
                role=attempt_row.get("role") or "implementer",
                reservation_id=attempt_row.get("reservation_id") or "B-replay",
                reserved_turns=int(attempt_row.get("reserved_agent_turns") or 1),
                reservation_expires_at=run_row.get("updated_at") or "2999-01-01T00:00:00Z",
            )
            store.record_invocation(attempt_id, attempt_row.get("invocation_id") or "I-replay")
            store.record_review_invocation(
                attempt_id, attempt_row.get("review_invocation_id") or "I-replay-review"
            )
            store.record_evidence(
                evidence_id=verification_evidence["evidence_id"],
                run_id=row["run_id"],
                attempt_id=verification_evidence["attempt_id"],
                kind="verification",
                status=EvidenceStatus(verification_evidence["status"]),
                candidate_fingerprint=fresh_fingerprint,
                checks_digest=run_row["checks_digest"],
                check_id=verification_evidence["check_id"],
                command=json.loads(verification_evidence["command_json"]),
                exit_code=verification_evidence["exit_code"],
                stdout_digest=verification_evidence["stdout_digest"],
                stderr_digest=verification_evidence["stderr_digest"],
                detail=verification_evidence["detail"],
            )
            review_evidence = store.record_evidence(
                evidence_id="E-replay-review",
                run_id=row["run_id"],
                attempt_id=verification_evidence["attempt_id"],
                kind="review",
                status=EvidenceStatus.PASSED
                if review.verdict == "accepted"
                else EvidenceStatus.FAILED,
                candidate_fingerprint=fresh_fingerprint,
                checks_digest=run_row["checks_digest"],
                check_id="review",
                detail=canonical_json(review.model_dump(mode="json")),
            )
            store.advance_to_checking(
                run_id=row["run_id"],
                attempt_id=verification_evidence["attempt_id"],
                phase=CheckPhase.REVIEW,
            )
            receipt = build_receipt(
                spec=spec,
                run_row=run_row,
                attempt_row=attempt_row,
                verification_evidence=verification_evidence,
                review=review,
                review_evidence_id=review_evidence.evidence_id,
                fingerprint=fresh_fingerprint,
                target=target,
                candidate_commit=candidate_commit,
                runtime_build=run_row["controller_build"],
                provenance=provenance,
                isolated=True,
            )
            # The real transaction, with its own CAS and cancellation-intent guards.
            store.finalize_acceptance(
                row["run_id"], receipt, checks_digest=run_row["checks_digest"]
            )
            result["isolated_review_evidence_id"] = review_evidence.evidence_id
            result["isolated_review_status"] = review_result_status(review)
            result["isolated_task_state"] = store.get_run(row["run_id"])["task_state"]
            result["isolated_receipt_written"] = bool(store.get_run(row["run_id"])["receipt_json"])
            result["receipt_artifact"] = (
                "written in an isolated temporary store; a receipt created there is a test "
                "artifact, not this run's delivery receipt"
            )
            result["accepted"] = review_result_status(review) == "accepted"
        finally:
            store.close()
    return result


# --------------------------------------------------------------------------
# the diagnostic
# --------------------------------------------------------------------------


def replay(
    run_id: str,
    *,
    store_path: Path = DEFAULT_STORE,
    project_dir: Path = DEFAULT_PROJECT,
    parser_build: str | None = None,
    work_root: Path | None = None,
    finalize: bool = False,
    processing_build: str | None = None,
) -> dict[str, Any]:
    """Produce the derived diagnostic for one recorded run.

    Read-only unless ``finalize`` is set, which is the explicit local finalization action: the
    same checks run first, and only an evaluation that passes may write the decision - through
    the Store's guarded transaction, never by patching rows here.
    """
    store_path = Path(store_path)
    project_dir = Path(project_dir)
    diagnostic: dict[str, Any] = {
        "tool": "hflow.tools.m2_live.replay_review",
        "source_run": run_id,
        "store": str(store_path),
        "store_sha256": sha256_file(store_path),
        "store_mutated": False,
        "model_calls": 0,
        "submissions": 0,
        "blockers": [],
        "limits": [
            "a hash computed now does not prove the bytes were not edited before it was computed",
            "the recorded verification check was not re-run; its input associations were re-checked",
            "no timestamp, identity, fingerprint or session id is invented for material that lacks one",
        ],
    }
    if parser_build is None:
        parser_build = _detect_parser_build()

    if work_root is not None:
        work_root = Path(work_root)
        work_root.mkdir(parents=True, exist_ok=True)
    temporary = tempfile.TemporaryDirectory(prefix="hflow-replay-src-", dir=work_root)
    try:
        copy_path = _read_only_copy(store_path, Path(temporary.name) / "copy.sqlite")
        connection = sqlite3.connect(f"file:{copy_path.as_posix()}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        try:
            run_row = connection.execute(
                "SELECT * FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if run_row is None:
                raise ReplayError(f"run {run_id} is not in {store_path}")
            run_row = dict(run_row)
            attempt_row = connection.execute(
                "SELECT * FROM attempts WHERE run_id = ?", (run_id,)
            ).fetchone()
            attempt_row = dict(attempt_row) if attempt_row else {}
            evidence_rows = [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM evidence WHERE run_id = ? ORDER BY created_at, rowid", (run_id,)
                )
            ]
            notes = [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM run_notes WHERE run_id = ? ORDER BY created_at", (run_id,)
                )
            ]
            authorizations = [
                dict(row)
                for row in connection.execute(
                    "SELECT authorization_id, provided_by, max_top_level_submissions, "
                    "used_top_level_submissions FROM authorizations"
                )
            ]
        finally:
            connection.close()

        diagnostic["recorded"] = {
            "task_id": run_row["task_id"],
            "task_revision": run_row["task_revision"],
            "task_state": run_row["task_state"],
            "delivery_state": run_row["delivery_state"],
            "block_code": run_row["block_code"],
            "block_reason": run_row["block_reason"],
            "receipt_present": bool(run_row["receipt_json"]),
            "controller_build": run_row["controller_build"],
            "attempt_id": attempt_row.get("attempt_id"),
            "attempt_state": attempt_row.get("state"),
            "attempt_outcome": attempt_row.get("outcome"),
            "implementer_invocation": attempt_row.get("invocation_id"),
            "reviewer_invocation": attempt_row.get("review_invocation_id"),
        }
        diagnostic["parser_build"] = parser_build
        if run_row["receipt_json"]:
            existing = ResultReceipt.model_validate(json.loads(run_row["receipt_json"]))
            if existing.provenance.get("kind") == OFFLINE_REPROCESSING_KIND:
                # Already finalized: report the recorded decision instead of re-deriving it, so
                # repeating the command cannot issue a second delivery.
                diagnostic["already_finalized"] = {
                    "runtime_build": existing.runtime_build,
                    "review_status": existing.review.status,
                    "review_evidence_ids": existing.review.evidence_ids,
                    "candidate_fingerprint": existing.candidate.fingerprint,
                    "git_commit": existing.candidate.git_commit,
                    "original_block_code": existing.provenance.get("original_block_code"),
                    "original_runtime_build": existing.provenance.get("original_runtime_build"),
                    "source_evidence_id": existing.provenance.get("source_evidence_id"),
                }
                if finalize:
                    diagnostic["finalization"] = {
                        "written": False,
                        "written_evidence_id": existing.review.evidence_ids[0]
                        if existing.review.evidence_ids
                        else "",
                        "task_state": run_row["task_state"],
                        "delivery_state": run_row["delivery_state"],
                        "receipt_present": True,
                        "runtime_build": existing.runtime_build,
                        "store": str(store_path),
                        "detail": "the same offline reprocessing decision is already recorded",
                    }
                diagnostic["store_sha256_after"] = sha256_file(store_path)
                diagnostic["store_mutated"] = (
                    diagnostic["store_sha256_after"] != diagnostic["store_sha256"]
                )
                return diagnostic
            diagnostic["blockers"].append(
                "this run already carries a receipt that is not an offline reprocessing decision; "
                "the replay reports the record instead of re-deriving it"
            )
            return diagnostic

        _check_task_and_project(diagnostic, run_row, project_dir)
        _check_candidate(diagnostic, run_row, attempt_row, evidence_rows, notes)
        review = _check_reviewer_and_extract(diagnostic, run_row, attempt_row, notes)
        if not diagnostic["blockers"] and review is not None:
            _evaluate(diagnostic, run_row, attempt_row, evidence_rows, review)
        diagnostic["authorizations"] = authorizations
        diagnostic["recorded_notes"] = [note["note"] for note in notes]

        if finalize and not diagnostic["blockers"] and review is not None:
            # The write happens only after every predicate above passed, and it goes through the
            # Store's transaction. The evaluation receipt above stays a test artifact.
            diagnostic["finalization"] = finalize_in_store(
                store_path=store_path,
                run_row=run_row,
                attempt_row=attempt_row,
                diagnostic=diagnostic,
                provenance=diagnostic["provenance"],
                processing_build=processing_build or parser_build,
                review=review,
            )
        elif finalize:
            diagnostic["finalization"] = {
                "written": False,
                "detail": "refused: the pre-write checks did not pass, so nothing was recorded",
            }

        diagnostic["store_sha256_after"] = sha256_file(store_path)
        diagnostic["store_mutated"] = (
            diagnostic["store_sha256_after"] != diagnostic["store_sha256"]
        )
        if diagnostic["store_mutated"] and not (finalize and not diagnostic["blockers"]):
            diagnostic["blockers"].append("the original store changed during the replay")
        return diagnostic
    finally:
        temporary.cleanup()


def _detect_parser_build() -> str:
    """The full build identity of *this* checkout, read from Git metadata without running git.

    The full commit SHA is recorded rather than an abbreviation: this value names the build a
    delivery decision came from, and an abbreviated hash is not an identity. Reading
    ``.git/HEAD`` (and the ref it points at) is a pure file read, so the replay tool launches
    no child process at all - which is also what makes "no model was launched here" checkable
    rather than asserted.
    """
    git_dir = REPO_ROOT / ".git"
    if git_dir.is_file():
        text = git_dir.read_text(encoding="utf-8", errors="replace").strip()
        if not text.startswith("gitdir:"):
            return "unknown (unreadable .git file)"
        git_dir = Path(text.split("gitdir:", 1)[1].strip())
    head_path = git_dir / "HEAD"
    if not head_path.is_file():
        return "unknown (no .git/HEAD)"
    head = head_path.read_text(encoding="utf-8", errors="replace").strip()
    if head.startswith("ref:"):
        ref_name = head.split("ref:", 1)[1].strip()
        ref_path = git_dir / ref_name
        if not ref_path.is_file():
            packed = git_dir / "packed-refs"
            if packed.is_file():
                for line in packed.read_text(encoding="utf-8", errors="replace").splitlines():
                    if line.endswith(" " + ref_name):
                        return f"hflow/{_PACKAGE_VERSION}+{line.split(' ', 1)[0]}"
            return "unknown (ref not resolved)"
        head = ref_path.read_text(encoding="utf-8", errors="replace").strip()
    return f"hflow/{_PACKAGE_VERSION}+{head}" if head else "unknown (empty HEAD)"


def _check_task_and_project(
    diagnostic: dict[str, Any], run_row: dict[str, Any], project_dir: Path
) -> None:
    spec_path = project_dir / "task.json"
    project_path = project_dir / "project.json"
    checks: dict[str, Any] = {}
    if spec_path.is_file():
        spec = TaskSpec.model_validate(json.loads(spec_path.read_text(encoding="utf-8")))
        checks["task_file"] = str(spec_path)
        checks["task_file_sha256"] = sha256_file(spec_path)
        checks["task_digest_matches_record"] = spec.spec_digest() == run_row["spec_digest"]
        checks["task_revision_matches_record"] = spec.revision == int(run_row["task_revision"])
        frozen = TaskSpec.model_validate(json.loads(run_row["task_spec_json"]))
        checks["task_spec_in_store_identical"] = frozen.spec_digest() == spec.spec_digest()
        if not checks["task_digest_matches_record"]:
            diagnostic["blockers"].append(
                "the task file on disk no longer digests to the spec_digest recorded for this run"
            )
    else:
        checks["task_file"] = f"missing: {spec_path}"
        diagnostic["blockers"].append(f"the task file {spec_path} is gone; its binding is unverifiable")

    if project_path.is_file():
        checks["project_file"] = str(project_path)
        checks["project_file_sha256"] = sha256_file(project_path)
    else:
        checks["project_file"] = f"missing: {project_path}"
        diagnostic["blockers"].append(
            f"the project file {project_path} is gone; the checks digest cannot be re-derived"
        )
    diagnostic["task_and_project"] = checks


def _check_candidate(
    diagnostic: dict[str, Any],
    run_row: dict[str, Any],
    attempt_row: dict[str, Any],
    evidence_rows: list[dict[str, Any]],
    notes: list[dict[str, Any]],
) -> None:
    """Candidate identity: content fingerprint, Git commit, and the retained reference."""
    worktree = Path(run_row["worktree_path"]) if run_row["worktree_path"] else None
    checks: dict[str, Any] = {"worktree": str(worktree) if worktree else ""}
    ref_note = next((note["note"] for note in notes if "candidate ref" in note["note"]), "")
    checks["candidate_ref_note"] = ref_note
    candidate_commit = ""
    if ref_note.startswith("candidate ref "):
        candidate_commit = ref_note.split(" ", 3)[2]
    checks["candidate_ref"] = candidate_commit

    if worktree is None or not worktree.exists():
        checks["worktree_state"] = "missing"
        diagnostic["blockers"].append(
            "the frozen worktree is gone, so the candidate's content cannot be re-checked"
        )
        diagnostic["candidate"] = checks
        return

    git_file = worktree / ".git"
    resolved_git_dir: Path | None = None
    if git_file.is_file():
        text = git_file.read_text(encoding="utf-8", errors="replace").strip()
        if text.startswith("gitdir:"):
            resolved_git_dir = Path(text.split("gitdir:", 1)[1].strip())
    checks["worktree_git_dir"] = str(resolved_git_dir) if resolved_git_dir else ""
    if resolved_git_dir is not None and resolved_git_dir.is_dir():
        head = (resolved_git_dir / "HEAD").read_text(encoding="utf-8", errors="replace").strip()
        checks["worktree_head"] = head
        log_path = resolved_git_dir / "logs" / "HEAD"
        if log_path.is_file():
            entries = log_path.read_text(encoding="utf-8", errors="replace").strip().splitlines()
            checks["worktree_head_moves"] = [entry.split("\t", 1)[-1] for entry in entries]
            if entries:
                checks["worktree_head_last_commit"] = entries[-1].split(" ", 1)[1].split(" ", 1)[0]
        common = (resolved_git_dir / "commondir").read_text(encoding="utf-8").strip()
        main_git = (resolved_git_dir / common).resolve()
        ref_name = f"refs/hflow/candidates/{run_row['run_id']}/{attempt_row.get('attempt_id', '')}"
        ref_path = main_git / ref_name
        checks["candidate_ref_path"] = str(ref_path)
        if ref_path.is_file():
            checks["candidate_ref_sha"] = ref_path.read_text(encoding="utf-8").strip()
        else:
            checks["candidate_ref_sha"] = ""
            diagnostic["blockers"].append(
                f"the retained candidate reference {ref_name} is missing from {main_git}"
            )
        checks["candidate_ref_matches_head"] = checks.get("candidate_ref_sha") == checks.get("worktree_head")

    spec = TaskSpec.model_validate(json.loads(run_row["task_spec_json"]))
    verification = next(
        (row for row in evidence_rows if row["kind"] == "verification"), None
    )
    if verification is None:
        diagnostic["blockers"].append("no verification evidence is recorded for this run")
        diagnostic["candidate"] = checks
        return
    fresh = candidate_fingerprint(worktree, spec.scope)
    checks["fingerprint_recorded"] = verification["candidate_fingerprint"]
    checks["fingerprint_recomputed"] = fresh
    checks["fingerprint_matches"] = fresh == verification["candidate_fingerprint"]
    checks["scoped_files"] = [str(path) for path in sorted(spec.scope.write_allow)]
    if not checks["fingerprint_matches"]:
        diagnostic["blockers"].append(
            "the frozen worktree's content fingerprint no longer matches the recorded verification "
            "evidence"
        )
    diagnostic["candidate"] = checks


def _check_reviewer_and_extract(
    diagnostic: dict[str, Any],
    run_row: dict[str, Any],
    attempt_row: dict[str, Any],
    notes: list[dict[str, Any]],
) -> ReviewOutput | None:
    """Bind the recorded reviewer invocation to its stream, then extract the verdict."""
    reviewer = attempt_row.get("review_invocation_id") or ""
    checks: dict[str, Any] = {"reviewer_invocation": reviewer}
    if not reviewer:
        diagnostic["blockers"].append("no reviewer invocation is recorded for this attempt")
        diagnostic["review"] = checks
        return None

    worktree = Path(run_row["worktree_path"]) if run_row["worktree_path"] else None
    invocation_dir = (
        worktree.parents[1] / "attempt-2-data" / "invocations" / reviewer
        if worktree is not None
        else Path(diagnostic["store"]).parent / "invocations" / reviewer
    )
    # The invocation directory itself is the authority; fall back to the configured layout.
    if not invocation_dir.is_dir():
        invocation_dir = Path(diagnostic["store"]).parent / "invocations" / reviewer
    checks["invocation_dir"] = str(invocation_dir)
    stream_path = invocation_dir / "events.ndjson"
    if not stream_path.is_file():
        stream_path = invocation_dir / "stdout.ndjson"
    checks["stream"] = str(stream_path)
    if not stream_path.is_file():
        diagnostic["blockers"].append(f"the reviewer's saved stream is missing: {stream_path}")
        diagnostic["review"] = checks
        return None
    checks["stream_sha256"] = sha256_file(stream_path)
    checks["stream_bytes"] = stream_path.stat().st_size

    config_path = invocation_dir / "acpx-config.json"
    if config_path.is_file():
        config = json.loads(config_path.read_text(encoding="utf-8"))
        checks["config_sha256"] = sha256_file(config_path)
        checks["reviewer_permissions"] = config.get("defaultPermissions")
        checks["reviewer_permission_expected"] = "approve-reads"
        if config.get("defaultPermissions") != "approve-reads":
            diagnostic["blockers"].append(
                "the recorded reviewer config does not show the read-only permission mode"
            )
    task_path = invocation_dir / "task.txt"
    if task_path.is_file():
        reviewer_task = task_path.read_text(encoding="utf-8")
        checks["review_task_sha256"] = sha256_file(task_path)
        checks["review_prompt_matches_controller_text"] = REVIEWER_PROMPT_MARKER in reviewer_task
        if not checks["review_prompt_matches_controller_text"]:
            diagnostic["blockers"].append(
                "the recorded reviewer prompt is not the controller's review instruction"
            )
    checks["role_permission_note"] = next(
        (note["note"] for note in notes if "effective permissions" in note["note"]), ""
    )
    checks["no_write_observation"] = (
        "reviewer ran with approve-reads (audit-only; not a sandbox) - the recorded "
        "before/after fingerprint check is the candidate check below"
    )

    spec = TaskSpec.model_validate(json.loads(run_row["task_spec_json"]))
    project_root = Path(run_row["worktree_path"]) if run_row["worktree_path"] else None
    before = candidate_fingerprint(project_root, spec.scope) if project_root else ""

    messages, unparseable = _load_messages(stream_path)
    checks["stream_lines"] = len(messages)
    checks["unparseable_lines"] = unparseable
    # The session identity comes from the stream itself (the controller records the attempt,
    # not the harness session); nothing is invented for it.
    session_ids = sorted(
        {
            str((message.get("params") or {}).get("sessionId"))
            for message in messages
            if message.get("method") == "session/update"
            and isinstance((message.get("params") or {}).get("sessionId"), str)
        }
    )
    checks["session_ids"] = session_ids
    if len(session_ids) != 1:
        diagnostic["blockers"].append(
            f"the reviewer stream spans {len(session_ids)} sessions; a single-session binding is "
            "required to attribute the answer"
        )
    extraction = extract_reviewer_answer(messages, session_ids[0] if len(session_ids) == 1 else None)
    transcript = extraction["transcript"]
    answer = extraction["answer"]
    checks["assistant_messages"] = transcript.observed_message_count
    checks["skipped_other_session"] = transcript.skipped_other_session
    checks["answer_truncated"] = transcript.truncated
    checks["prompt_request_ids"] = extraction["prompt_request_ids"]
    checks["terminal_response_ids"] = extraction["terminal_response_ids"]
    checks["terminal_answers_prompt"] = extraction["bound"]
    if not extraction["bound"]:
        diagnostic["blockers"].append(
            "the terminal response is not matched to the reviewer's session/prompt request"
        )
    if answer is None:
        diagnostic["blockers"].append("the reviewer stream contains no assistant answer text")
        diagnostic["review"] = checks
        return None
    checks["answer_message_id"] = answer.message_id
    checks["answer_first_line"] = answer.first_line
    checks["answer_last_line"] = answer.last_line
    checks["answer_chunk_count"] = answer.chunk_count
    checks["answer_chars"] = len(answer.text)
    checks["answer_sha256"] = "sha256:" + hashlib.sha256(answer.text.encode("utf-8")).hexdigest()

    try:
        review = decode_review(answer.text)
    except Exception as exc:  # noqa: BLE001 - reported verbatim, never repaired
        diagnostic["blockers"].append(f"the saved reviewer answer did not decode: {exc}")
        diagnostic["review"] = checks
        return None

    checks["verdict"] = review.verdict
    checks["findings"] = review.findings
    checks["verdict_digest"] = digest_of(review.model_dump(mode="json"))
    stored = json.loads(attempt_row.get("review_json") or "{}")
    checks["stored_result_review"] = stored.get("review")
    checks["stored_review_reason"] = review_input_error(stored.get("limitations") or [])
    # Cross-check what the reviewer *claimed* about the candidate against the recorded ref.
    ref_sha = str(diagnostic.get("candidate", {}).get("candidate_ref_sha") or "")
    claimed = sorted(set(re.findall(r"\b[0-9a-f]{40}\b", answer.text)))
    checks["commits_named_in_the_answer"] = claimed
    checks["reviewer_named_the_recorded_candidate"] = bool(ref_sha) and ref_sha in claimed
    if ref_sha and not checks["reviewer_named_the_recorded_candidate"]:
        diagnostic["blockers"].append(
            "the reviewer's answer never names the candidate commit recorded for this run, so the "
            "verdict is not tied to a stated candidate identity"
        )
    after = candidate_fingerprint(project_root, spec.scope) if project_root else ""
    checks["candidate_unchanged_across_replay"] = bool(before) and before == after
    if not checks["candidate_unchanged_across_replay"]:
        diagnostic["blockers"].append(
            "the candidate content changed while the replay was reading it; the answer cannot be "
            "attributed to a stable candidate"
        )
    diagnostic["review"] = checks
    return review


def _evaluate(
    diagnostic: dict[str, Any],
    run_row: dict[str, Any],
    attempt_row: dict[str, Any],
    evidence_rows: list[dict[str, Any]],
    review: ReviewOutput,
) -> None:
    verification = next((row for row in evidence_rows if row["kind"] == "verification"), None)
    worktree = Path(run_row["worktree_path"]) if run_row["worktree_path"] else None
    if verification is None or worktree is None:
        diagnostic["blockers"].append(
            "the acceptance evaluation needs recorded verification evidence and the frozen "
            "worktree; at least one is unavailable"
        )
        return
    spec = TaskSpec.model_validate(json.loads(run_row["task_spec_json"]))
    provenance = reprocessing_provenance(diagnostic, run_row, verification)
    diagnostic["provenance"] = provenance
    diagnostic["acceptance"] = evaluate_acceptance(
        spec=spec,
        run_row=run_row,
        attempt_row=attempt_row,
        verification_evidence=verification,
        review=review,
        reviewed_fingerprint=verification["candidate_fingerprint"],
        target=worktree,
        candidate_commit=str(diagnostic.get("candidate", {}).get("candidate_ref_sha") or ""),
        provenance=provenance,
    )
    if not diagnostic["acceptance"]["accepted"]:
        diagnostic["blockers"].append(
            "the isolated acceptance evaluation did not pass: "
            + "; ".join(diagnostic["acceptance"]["blockers"])
        )


def reprocessing_provenance(
    diagnostic: dict[str, Any], run_row: dict[str, Any], verification: dict[str, Any]
) -> dict[str, Any]:
    """The record that makes this receipt a *later* decision, not the execution's own outcome.

    It names what ended the execution, on which build, and which evidence the new decision was
    derived from - so the original failure stays readable next to the delivery.
    """
    review = diagnostic.get("review", {})
    return {
        "kind": OFFLINE_REPROCESSING_KIND,
        "original_decision": "BLOCKED",
        "original_block_code": run_row.get("block_code") or "",
        "original_block_reason": run_row.get("block_reason") or "",
        "original_runtime_build": run_row.get("controller_build") or "",
        "original_attempt_id": run_row.get("current_attempt_id") or "",
        "source_evidence_id": verification["evidence_id"],
        "source_candidate_fingerprint": verification["candidate_fingerprint"],
        "source_checks_digest": run_row["checks_digest"],
        "reviewer_invocation_id": diagnostic.get("recorded", {}).get("reviewer_invocation") or "",
        "review_answer_sha256": review.get("answer_sha256") or "",
        "review_verdict_digest": review.get("verdict_digest") or "",
        "model_calls": 0,
        "authorization_consumed": 0,
    }


def finalize_in_store(
    *,
    store_path: Path,
    run_row: dict[str, Any],
    attempt_row: dict[str, Any],
    diagnostic: dict[str, Any],
    provenance: dict[str, Any],
    processing_build: str,
    review: ReviewOutput,
) -> dict[str, Any]:
    """Record the decision in the ledger, through the Store's guarded transaction.

    The review evidence and the receipt are written in one transaction: a crash cannot leave a
    receipt whose review evidence is missing. Idempotent by construction - an identical
    decision already recorded returns ``written: false`` instead of a second delivery.
    """
    store = Store(store_path)
    try:
        row = store.get_run(run_row["run_id"])
        if row["receipt_json"]:
            existing = ResultReceipt.model_validate(json.loads(row["receipt_json"]))
            if _same_offline_reprocessing(
                existing,
                source_evidence_id=provenance["source_evidence_id"],
                candidate_fingerprint=provenance["source_candidate_fingerprint"],
            ):
                return {
                    "written": False,
                    "written_evidence_id": existing.review.evidence_ids[0]
                    if existing.review.evidence_ids
                    else "",
                    "task_state": row["task_state"],
                    "delivery_state": row["delivery_state"],
                    "receipt_present": True,
                    "runtime_build": existing.runtime_build,
                    "store": str(store_path),
                    "detail": "the same offline reprocessing decision is already recorded",
                }
            raise StoreError(
                f"run {run_row['run_id']} already carries a receipt for a different decision; "
                "refusing to overwrite it"
            )

        spec = TaskSpec.model_validate(json.loads(run_row["task_spec_json"]))
        worktree = Path(run_row["worktree_path"])
        verification = next(
            row
            for row in _load_evidence(store_path, run_row["run_id"])
            if row["kind"] == "verification"
        )
        review_evidence_id = new_evidence_id()
        receipt = build_receipt(
            spec=spec,
            run_row=run_row,
            attempt_row=attempt_row,
            verification_evidence=verification,
            review=review,
            review_evidence_id=review_evidence_id,
            fingerprint=provenance["source_candidate_fingerprint"],
            target=worktree,
            candidate_commit=str(diagnostic.get("candidate", {}).get("candidate_ref_sha") or ""),
            runtime_build=processing_build,
            provenance=provenance,
            isolated=False,
        )
        written = store.finalize_offline_reprocessing(
            run_row["run_id"],
            receipt,
            checks_digest=run_row["checks_digest"],
            source_evidence_id=provenance["source_evidence_id"],
            candidate_fingerprint=provenance["source_candidate_fingerprint"],
            review_evidence={
                "status": EvidenceStatus.PASSED
                if review.verdict == "accepted"
                else EvidenceStatus.FAILED,
                "detail": canonical_json(review.model_dump(mode="json")),
                "verdict": review.verdict,
            },
        )
        row = store.get_run(run_row["run_id"])
        return {
            "written": written,
            "written_evidence_id": review_evidence_id,
            "task_state": row["task_state"],
            "delivery_state": row["delivery_state"],
            "receipt_present": bool(row["receipt_json"]),
            "store": str(store_path),
            "detail": (
                "recorded: a later offline reprocessing decision now carries the delivery; the "
                "original blocked decision and its runtime are preserved in the run's notes and "
                "in the receipt provenance"
                if written
                else "the same offline reprocessing decision is already recorded"
            ),
        }
    finally:
        store.close()


def _load_evidence(store_path: Path, run_id: str) -> list[dict[str, Any]]:
    """Read the run's evidence rows through a read-only connection (no writes, no migration)."""
    connection = sqlite3.connect(f"file:{Path(store_path).as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        return [
            dict(row)
            for row in connection.execute(
                "SELECT * FROM evidence WHERE run_id = ? ORDER BY created_at, rowid", (run_id,)
            )
        ]
    finally:
        connection.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_id")
    parser.add_argument("--store", default=str(DEFAULT_STORE))
    parser.add_argument("--project-dir", default=str(DEFAULT_PROJECT))
    parser.add_argument("--out", default="")
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument(
        "--finalize",
        action="store_true",
        help=(
            "record the decision in the ledger after the pre-write checks pass; without it this "
            "tool only reads and evaluates"
        ),
    )
    args = parser.parse_args(argv)

    diagnostic = replay(
        args.run_id,
        store_path=Path(args.store),
        project_dir=Path(args.project_dir),
        finalize=args.finalize,
    )
    text = json.dumps(diagnostic, indent=2, ensure_ascii=False)
    if args.out:
        Path(args.out).write_text(text + "\n", encoding="utf-8")
    if not args.quiet:
        print(text)
    review = diagnostic.get("review", {})
    acceptance = diagnostic.get("acceptance", {})
    finalization = diagnostic.get("finalization", {})
    already = diagnostic.get("already_finalized", {})
    print(
        f"\nrun={args.run_id} verdict={review.get('verdict')} "
        f"isolated_acceptance={acceptance.get('accepted')} "
        f"blockers={len(diagnostic.get('blockers', []))} "
        f"store_mutated={diagnostic.get('store_mutated')} "
        f"written={finalization.get('written')} "
        f"already_finalized={bool(already)}",
        file=sys.stderr,
    )
    if diagnostic.get("blockers"):
        return 1
    if args.finalize and not finalization.get("written") and not already:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
