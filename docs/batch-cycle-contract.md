# Contract — `weaver batch`: the fill cycle (2+ applications, one window)

## Problem
A `--hold --tab` run used to block in `wait_for_human` until a human closed the
tab, so its ledger row was written only after human intervention — and the next
queued run starved. That checkout bug is fixed in `local_driver.launch`
(tab mode: mark complete on fill, leave the held tab open, disconnect).
What is still missing is the ORCHESTRATOR: the loop that runs a queue of
fills back-to-back and relays skips at the end.

## The flow (Leo's spec, 2026-08-18)
collect job sources → review the required forms → skip unknown forms →
fill application A → mark complete ON FILL → open application B → repeat for
1..X jobs → one cycle report: (A) fields a human must finish, (B) tabs ready
to send. All as tabs of ONE shared window; the cycle never stops on one job.

## Command
`weaver batch <resume_id> [<resume_id>...] [--force] [--max-actions N]
[--notify/--no-notify]`

Per resume, in order, never aborting the cycle on one bad entry:
1. **No such resume / no linked job with a URL** → record a skip, continue.
2. **Already terminal** (an application row with status `held`, `submitted`,
   or `applied` exists) → skip: re-running a cycle must never refill a form a
   human is reviewing or has sent. This holds at the RESUME level and at the
   JOB level: a re-adapted resume (new id, same posting — e.g. pdf→docx) must
   never open a second application on a job with a terminal row.
3. **Preflight** (unless `--force`): required questions vs the applicant's
   facts, no browser. Verdict `fail` → record the skip WITH the uncovered
   questions, continue. This is "review the required forms → skip unknown
   forms".
4. **Fill**: `cmd_apply` with `--visible --hold --tab` semantics and
   `force=True` (the gate already ran in step 3). Per-run notifications are
   OFF — the cycle pings once at the end. The run marks complete on fill
   (ledger `held`, exit 0) and its tab stays open in the shared window.
5. Record the outcome from the run's exit code + its newest ledger row:
   `held`/`submitted` → filled; `audit_pending` → filled, a human finishes one
   field; anything else → failed (reason from the ledger row's response).

## The cycle report (the "relay" step)
One block, human + JSON, after the last fill:
- `held`   — ready to send: review the tab, press send (per job: role @ org, ledger #)
- `pending` — a human finishes the named field first, then sends
- `skipped` — WHY (missing facts verbatim from preflight / no job / already done)
- `failed` — reason
Exit 0 when nothing failed (skips are expected, not failures); exit 2 when
any run failed. One macOS ping summarises the cycle (`--no-notify` silences).

## Invariants
- Batch NEVER submits: every fill is a `--hold` park; the human sends.
- "FILLED + HELD" means something was FILLED: a hold-stop with zero landed
  values and no named required gap is `stopped` (failed), never `held`
  (2026-08-19 live test: two runs stopped on a listing page and claimed held).
- Piped/redirected batch stdout is ONE JSON document: inner runs are forced
  human and printed to stderr.
- A cycle entry can never block the entries after it (no waits on humans).
- No edits to `local_driver.py` / `local_agent.py` for this command — it is
  pure orchestration in `cli.py` over the already-fixed checkout.
