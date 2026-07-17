# Workspace safety, conflicts, and recovery

Agentic OS lets tasks from different goals run at the same time when their
declared resources do not overlap. It protects shared project state with
project-scoped resource revisions, short-lived leases, and fencing tokens.
This document explains those guarantees, the cases that are serialized
automatically, and the cases that require an operator decision.

For the automated and manual evidence behind these guarantees, see
[Concurrent goal execution and workspace safety verification](concurrent-execution-verification.md).
For the surrounding deployment and process-recovery model, see
[Team VM deployment topology](team-vm-deployment.md) and
[Restart recovery verification](restart-recovery-verification.md).

## Three different kinds of state

The workspace protocol is deliberately separate from two related mechanisms:

| Mechanism | Responsibility | What it does not guarantee |
| --- | --- | --- |
| Product workspace | Coordinates project resources across tasks and workers; records leases, revisions, promotions, and conflicts in PostgreSQL | It does not capture an agent's internal execution position |
| Sandbox filesystem view | Gives one task run an isolated writable execution view | Isolation alone does not authorize publishing changes into shared project state |
| LangGraph checkpoint | Resumes model/agent execution from a durable safe boundary within one run | A checkpoint does not own a project resource, validate a fencing token, or promote workspace changes |

A run may resume from a valid checkpoint and still be refused permission to
publish if its workspace lease expired or was superseded. Conversely, a valid
workspace lease does not replace the need to checkpoint agent execution.

## Resource namespace and revisions

Every task declares `resource_intent` entries. A resource key identifies a
logical project resource, such as `docs/report.md`, and is scoped to one
project. Two projects may use the same key without sharing a lock or revision.

Keys must already be canonical POSIX project-relative paths:

- accepted: `docs/report.md`, `artifacts/research.json`;
- rejected: absolute paths, backslashes, empty segments, `.` segments, and
  `..` traversal.

Each written key has an independent non-negative revision in PostgreSQL. A
lease records the revision the worker observed when it acquired the key. A
successful promotion verifies that expected revision and then increments the
revision by one. This per-resource comparison is what lets disjoint writes
promote independently even when other project resources changed.

The current implementation does not expose a separate aggregate project
workspace revision. `WorkspaceResource.revision` rows and the corresponding
promotion record are the authoritative publication boundary.

## Claiming work and automatic serialization

When a worker claims a ready task, it considers tasks in creation order and
uses PostgreSQL transaction-scoped advisory locks to close races between
concurrent claimers. Every declared resource key participates in this
short-lived claim mutex, regardless of read/write intent. This is intentionally
conservative.

For each `write` intent, the winning worker also acquires a durable resource
lease. Acquisition:

1. locks or creates the project/resource row;
2. refuses the claim if another task holds an unexpired lease;
3. increments the resource's fencing token;
4. snapshots the current resource revision as `expected_revision`; and
5. records the task lease token, worker ID, expiry, and a
   `workspace.lease_acquired` audit event.

If one candidate cannot obtain all its resource locks, the scheduler skips it
for that claim attempt and may run a different non-conflicting task. It does
not partially assign the blocked candidate. Tasks that write the same declared
key therefore wait and execute serially; the user does not need to resolve
ordinary lock contention.

Disjoint keys can be leased and promoted concurrently. A task with no write
intent does not create a durable workspace resource lease or promotion.

## Lease renewal, expiry, and fencing

The task lease and every durable resource lease must continue to agree on the
worker, task, task lease token, and expiry. Workers renew these leases at safe
execution boundaries.

Every acquisition or reassignment advances the resource's monotonically
increasing fencing token. The promotion path accepts only the token that still
matches the resource's latest token. This prevents a worker that wakes up
after its lease expired from publishing after a replacement worker has taken
ownership.

An expired task remains eligible for recovery. A new worker can reclaim it,
advance the task and resource fencing state, mark the interrupted run failed,
and start a new attempt. The old worker's later promotion is denied even if it
still has an isolated filesystem or LangGraph state in memory. See
[Restart recovery verification](restart-recovery-verification.md) for the
process-kill recovery path and
[Team VM deployment topology](team-vm-deployment.md#independently-scaled-workers)
for multi-worker deployment guidance.

## Atomic promotion

Before publishing a run's writes, the worker renews its task/resource leases
and validates all written keys in one database transaction. Promotion succeeds
only when every key passes both checks:

- **ownership check:** the task lease and resource lease still belong to this
  worker and task attempt, are unexpired, and carry the current fencing token;
- **revision check:** each resource's current revision equals the revision
  captured at acquisition.

If all keys pass, the transaction increments every resource revision, releases
the resource leases, writes one `WorkspacePromotion` with status `promoted`,
and emits `workspace.promoted`. A transaction rollback discards the entire
promotion; it cannot leave only some keys advanced.

If any ownership check fails, publication is denied with promotion status
`denied` and audit event `workspace.promotion_denied`. If ownership is valid
but a resource revision changed, publication records status `conflict` and
`workspace.promotion_conflict`, including the expected and actual revision for
each affected key. Neither outcome advances a resource revision.

## Serialization versus user-facing conflict resolution

The distinction is operationally important:

| Situation | System behavior | Operator action |
| --- | --- | --- |
| Two tasks declare the same resource key before either runs | Scheduler/lease acquisition serializes them | None; monitor only if the wait is unexpectedly long |
| Tasks declare disjoint resource keys | They may execute and promote concurrently | None |
| A lease expires and a replacement worker reclaims it | Fencing advances; the interrupted attempt is reconciled and retried | Usually none; investigate repeated expiry |
| A stale or superseded worker tries to publish | Promotion is denied | Confirm a current worker owns/recovered the task; do not force the stale output into the workspace |
| A valid owner finds `actual_revision != expected_revision` | Promotion records a conflict and the task attempt fails without publishing | Choose discard or a controlled retry after reviewing the competing revision |

A promotion conflict is not ordinary resource contention. It means the
resource changed after the attempt's safe expected revision was established.
Agentic OS does not automatically merge content and does not treat a
model-generated merge as safe.

The concurrency panel exposes the recorded resource key, expected revision,
actual revision, task, run, and occurrence time. Its current resolution
controls operate at goal level:

- **Discard conflicting run** requests cancellation of the losing goal.
  Active work stops cooperatively and queued work is cancelled.
- **Retry from safe revision** requests goal resume. Resume is valid only for
  a paused goal. It does not by itself reset a failed task to a claimable
  status, so the control can be rejected or leave the conflicted task failed.
  Treat this control as a retry request, not proof that a new attempt started.
  Confirm the task becomes claimable and a new run appears before considering
  the conflict recovered.

The conflict record remains durable evidence after either choice. There is no
automatic semantic merge and no API that accepts a hand-edited merge as a
promotion resolution. The current backend also has no dedicated endpoint that
atomically rebases and requeues a failed conflict attempt. If resume cannot
produce a new run, keep the goal paused and use the normal governed steering
workflow to replace/revise the failed task against the current resource, or
cancel the goal. Cancelling is the safer choice when the failed attempt's
intended write is obsolete.

## Inspecting workspace evidence

Project members can inspect their project:

```text
GET /api/v1/projects/{project_id}/workspace/leases
GET /api/v1/projects/{project_id}/workspace/conflicts
GET /api/v1/projects/{project_id}/workspace/promotions
```

Installation admins can inspect all projects:

```text
GET /api/v1/admin/workspace/leases
GET /api/v1/admin/workspace/conflicts
GET /api/v1/admin/workspace/promotions
```

Lease endpoints accept `state=active`, `state=stale`, or `state=fenced`.
Interpret the evidence as follows:

- `active`: the resource and task lease still agree and have not expired;
- `stale`: the lease expired or no longer agrees with the task owner/token;
- `fenced`: its token was superseded by a later acquisition;
- promotion `promoted`: all listed revisions advanced atomically;
- promotion `conflict`: ownership was valid, but at least one revision
  differed;
- promotion `denied`: lease ownership, expiry, or fencing validation failed.

Conflict endpoints return only revision conflicts. Use the promotion endpoint
and audit events to investigate denied stale/fenced attempts.

## Operator troubleshooting

### A task waits while other tasks continue

1. Compare its `resource_intent` keys with active tasks.
2. Inspect project leases for the overlapping key.
3. If the lease is active, allow the owning task to finish; serialization is
   working as designed.
4. If it is stale, confirm a worker is alive and polling. A replacement worker
   should reclaim the task after expiry.

Do not delete lease rows or lower fencing tokens manually. That removes the
evidence the next promoter needs to reject stale work.

### Conflicts repeat after retry

1. Read the conflict's expected/actual revisions and task/run IDs.
2. Inspect promotion history for the same key to identify the successful
   competing write.
3. Confirm the task declares the correct, sufficiently specific resource key.
4. Pause the goal before retrying. Decide whether the current resource should
   win, whether the failed intent is still required, or whether to cancel.
5. Resume only after the expected input/state is stable.

Repeated conflicts commonly indicate incorrect or overly broad resource
intent, or an out-of-band writer changing project state without participating
in the lease protocol.

### A stale or fenced worker is visible

1. Check worker heartbeat/health and the lease expiry.
2. Confirm a live worker has capacity to reclaim the task.
3. Inspect `workspace.promotion_denied` and `run.interrupted` events.
4. If no worker is live, restore worker service using the restart order in
   [Team VM deployment topology](team-vm-deployment.md#restart-ordering-and-startup-dependencies).

A fenced record after recovery is useful historical evidence, not itself a
reason to edit the database.

### A worker dies during promotion

Promotion and its revision/lease updates share the worker transaction. If the
process dies before commit, PostgreSQL rolls the entire promotion back. After
the lease expires, a new worker reconciles the interrupted run and retries
from the last committed boundary. Validate this behavior with the procedures
in [Concurrent goal execution and workspace safety verification](concurrent-execution-verification.md)
and [Restart recovery verification](restart-recovery-verification.md).

## Safety boundaries

- Safety depends on accurate task `resource_intent`. Undeclared/out-of-band
  writes cannot be serialized by the workspace protocol.
- Resource locks are project-local, not global and not cross-project.
- PostgreSQL is the authority for revisions, leases, fencing, promotions, and
  audit evidence.
- Sandbox isolation and LangGraph checkpoints are necessary execution
  mechanisms, but neither can bypass workspace promotion validation.
- Automatic semantic merge and distributed cross-VM filesystem
  synchronization are outside the current model.
