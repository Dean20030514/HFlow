"""Read the authorized B attempt's evidence straight from the store (no model, no re-run)."""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from hflow.store import Store  # noqa: E402

DB = REPO_ROOT / ".probe" / "m2-live" / "attempt-2-data" / "hflow.sqlite"


def main() -> int:
    store = Store(DB)
    try:
        runs = store.list_runs(limit=5)
        for row in runs:
            run_id = row["run_id"]
            print(f"run {run_id}  task_state={row['task_state']} phase={row['phase']} "
                  f"delivery={row['delivery_state']} block={row['block_code']}")
            print(f"  spec_digest={row['spec_digest']}")
            for note in store.notes_for(run_id):
                print(f"    note: {note}")
            for attempt in store.attempts_for(run_id):
                print(f"    attempt {attempt['attempt_id']} rev={attempt['task_revision']} "
                      f"role={attempt['role']} state={attempt['state']} outcome={attempt['outcome']}")
                print(f"      invocation={attempt['invocation_id']} "
                      f"review_invocation={attempt['review_invocation_id']}")
            for evidence in store.evidence_for(run_id):
                print(f"    evidence {evidence['evidence_id']} kind={evidence['kind']} "
                      f"check={evidence['check_id']} status={evidence['status']} "
                      f"exit={evidence['exit_code']}")
                if evidence["kind"] == "review":
                    print(f"      review detail: {evidence['detail'][:900]}")
                if evidence["command_json"] not in ("[]", ""):
                    print(f"      command: {evidence['command_json']}")
            print(f"  block_reason: {row['block_reason']}")
        print("\nauthorization rows:")
        for row in store._fetchall(
            "SELECT authorization_id, mode, provided_by, max_top_level_submissions, "
            "used_top_level_submissions FROM authorizations"
        ):
            print(" ", dict(row))
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
