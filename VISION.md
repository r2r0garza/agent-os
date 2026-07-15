# Agentic OS Vision

**Status:** Foundational product and architecture direction  
**Audience:** Project maintainers and the coding agents that plan and execute sprint work  
**Scope:** The north star and durable constraints for Agentic OS, not a substitute for sprint-level specifications or architecture decision records

## Main objective

Agentic OS is a local-first, self-hostable operating environment in which a user gives the system a goal and a governed team of agents works together to achieve it.

The system must turn a goal into observable, durable work: select suitable agents, decompose the goal into tasks, schedule sequential and parallel execution, provide each agent with the right tools and context, preserve progress across failures, manage collisions, enforce permissions and budgets, and return inspectable results. A server restart must interrupt work, not erase it.

The initial product serves one operator. Its architecture must grow cleanly into a cloud-hosted deployment for a trusted team without replacing the execution or persistence model.

## Product promise

A user should be able to:

1. Create a project with a persistent workspace.
2. Create or install agents with explicit capabilities, instructions, model configuration, skills, MCP servers, permissions, and budgets.
3. Submit one or more goals to the project.
4. See the system form an appropriate agent team and decompose each goal into an inspectable graph of tasks.
5. Watch work progress in real time, including model calls, tool activity, artifacts, costs, warnings, failures, and retries.
6. Stop the server at any point, start it again, and resume from the last safe execution boundary.
7. Review what happened, why it happened, what it cost, which resources changed, and what the agents produced.

“Operating system” describes the product’s role: it coordinates agents, resources, policies, and work. It does not mean recreating a kernel or granting models unrestricted access to the host.

## Core product model

The product should use a stable vocabulary:

- A **team** is an ownership and administrative boundary. An installation may contain multiple teams, even though the initial deployment may use only one.
- A **user** is a person with either the admin or regular-user role.
- A **project** contains goals, project configuration, knowledge, and one logical workspace.
- A **goal** is a user-owned desired outcome. A goal is the top-level unit users create, pause, resume, cancel, and evaluate.
- A **task** is a schedulable unit of work produced while decomposing a goal. Tasks form a dependency graph and may run sequentially or in parallel.
- A **run** is one durable attempt by an assigned agent to execute a task. It has a stable task parent, attempt number, LangGraph thread, and lifecycle. Goal status is an aggregate of its task graph rather than a second kind of run.
- An **agent** is a versioned, reusable worker definition with a capability manifest, instructions, model profile, tools, skills, policies, and default budget.
- A **skill** is a versioned package of instructions and supporting resources that an agent can load.
- An **MCP server** is a configured external tool provider with scoped credentials and policy controls.
- An **artifact** is an output of work, including uploaded source material, normalized documents, reports, patches, and logs intended for users. Artifact versions are immutable; an update creates a new version with lineage to its predecessor.
- A **workspace** is the project’s durable resource namespace. Concurrent runs operate through isolated execution views and promote changes back through controlled, auditable operations.

Agent, skill, and MCP configuration used by a run must be snapshotted or version-pinned. Editing a definition must not silently change the meaning of an already-running or resumed task.

## Guiding principles

### Goals, not chats

Conversation may help define or steer work, but the durable object is a goal with state, tasks, artifacts, policy, budget, and history. The system is successful when it completes accountable work, not when it merely produces plausible messages.

### Teams are composed for the work

No fixed group of agents is assumed. The orchestrator selects agents from their declared capabilities and the goal’s needs. One goal may use Agents A, D, and F while another concurrently uses B, C, and D. Selection and delegation must be visible and overridable rather than hidden inside an opaque prompt.

### Durable by construction

Every state transition that matters must be persisted. Execution is modeled as resumable steps with explicit inputs, outputs, dependencies, attempts, and side effects. In-memory state may accelerate execution but can never be the sole record of active work.

### Safe autonomy

The default “auto” mode allows agents to act without interrupting the user, but only inside an isolated workspace and within explicit policy, credential, network, resource, and budget boundaries. Autonomy means freedom inside a governed sandbox, not unrestricted host access.

### Extensible from the beginning

Skills and MCP servers are first-class product objects, not later integrations. Users can add capabilities without waiting for a hard-coded tool release. Built-in tools should remain a small, dependable foundation.

### Observable and explainable

Users should be able to reconstruct an execution from goal to result. Admins need deeper governance, budget, and system-wide operational views. Agent reasoning need not expose private chain-of-thought; useful explanations, decisions, inputs, outputs, tool calls, state transitions, and evidence must be preserved.

### Both applications are first-class

The backend is not complete without a usable frontend workflow, and the frontend is not complete when powered by mock behavior. Sprints deliver end-to-end slices across UI, API, execution, persistence, observability, and tests.

## Agent collaboration and scheduling

A goal is decomposed into a directed acyclic graph of tasks. Each task declares dependencies, required capabilities, expected outputs, relevant resources, policy context, and budget context. The scheduler assigns one eligible top-level agent to each task run and runs ready tasks concurrently when doing so is safe. A multi-agent goal is represented by tasks assigned to different agents; delegation to a subagent inside one run remains subordinate to, and checkpointed within, the top-level assignment.

The initial selection mechanism should favor explicit, inspectable capability metadata over semantic guesswork. Embedding-based matching may augment selection later, but must not become an unexplainable source of authority.

Concurrent goals are normal. The first implementation of collision handling must use a concrete workspace protocol:

- Each task run starts from an immutable project workspace revision and receives an isolated writable view.
- Tasks declare resource intent using canonical project-relative resource keys. Inferred intent may make locking more conservative but cannot weaken an existing lock.
- Mutations require exclusive, renewable leases with monotonically increasing fencing tokens. Every state change and workspace promotion verifies the active token so an expired worker cannot continue writing after recovery begins.
- Promotion records expected revisions for every mutated resource key and validates them atomically. It then advances those resource revisions and the aggregate project revision in one database transaction.
- Runs that mutate disjoint resource keys may promote independently even if the aggregate project revision advanced. A changed expected resource revision creates an explicit conflict state; no model-generated merge is silently accepted as safe.
- When safe merging cannot be established, the system serializes work or asks the user to resolve the conflict according to policy.
- Stale leases expire and can be recovered after worker failure.

Parallelism must never weaken durability. A task becomes complete only after its outputs and state transition are committed. Retries use stable idempotency keys. Operations with external side effects must be idempotent, compensatable, or explicitly recorded as requiring reconciliation; checkpointing alone cannot make an arbitrary external action exactly-once.

## Technical direction

### Backend and API

The backend will be Python 3.12 with FastAPI. It owns domain rules, orchestration, policy evaluation, persistence, model and tool access, sandbox lifecycle, and streaming execution events. The frontend communicates through versioned HTTP APIs and a resumable event stream; Server-Sent Events are the preferred initial transport unless a sprint demonstrates a need for bidirectional WebSockets.

API handlers should accept work and report state rather than host long-running execution in the request process. Durable workers claim queued work using database-backed leases. API and worker processes may run together for local development but remain separable deployment roles.

### Agent framework

Use **Deep Agents as the agent harness on top of LangGraph**.

Deep Agents provides a useful starting set of filesystem and execution tools, context management, subagents, skills, human-in-the-loop behavior, and pluggable backends. LangGraph provides the lower-level execution graph, checkpoints, interrupts, and resumption semantics required by this product.

This choice is an accelerator, not permission to couple the product domain to framework internals:

- Agentic OS owns goals, tasks, runs, agents, policies, budgets, artifacts, and audit events.
- LangGraph checkpoints execution state at safe boundaries.
- Deep Agents tools are exposed only through Agentic OS policy and workspace backends.
- Framework identifiers are mapped to stable Agentic OS identifiers.
- Framework state is never the only queryable record of product state.
- Custom middleware and tools are expected where governance, metering, workspace isolation, or MCP integration require finer control.

The product task DAG and the LangGraph execution graph have different scopes. Agentic OS schedules dependencies between tasks. Each task run owns one LangGraph thread and its checkpoints; LangGraph controls resumable steps within that run. A goal DAG is not compiled into one monolithic LangGraph thread.

If Deep Agents later blocks a core invariant, the affected layer can move closer to LangGraph without rewriting the product model.

### Model access

The first model contract is OpenAI-compatible BYOK with, at minimum:

- base URL;
- API key;
- model identifier;
- optional headers and organization/project metadata;
- timeout and retry policy;
- capability and pricing metadata.

Credentials are encrypted at rest, never returned to the browser after creation, redacted from logs and traces, and scoped to the narrowest practical team, project, or agent boundary.

“OpenAI-compatible” guarantees the standard interface Agentic OS explicitly tests; it does not imply that every compatible endpoint supports tool calling, structured output, streaming, token accounting, reasoning fields, embeddings, or identical error behavior. Model profiles must record or probe these capabilities. A task may only be assigned to a model that satisfies its requirements.

Provider-specific adapters can be added later without changing the agent or task model.

### Transactional persistence

Use **PostgreSQL as the primary database from the first vertical slice**.

PostgreSQL stores users, roles, teams, projects, goals, task graphs, runs, agent definitions and versions, skill and MCP metadata, policies, approvals, budgets, cost ledger entries, workspace metadata, artifacts, events, leases, and LangGraph checkpoints. It provides one transactional foundation for local Docker/Podman deployment and the projected team server.

SQLite may be used for isolated tests, but it is not a supported production persistence mode. Building production semantics around SQLite would defer concurrency and migration problems into later sprints.

Database migrations are mandatory. Relational current-state records are the operational source of truth. Important transitions update that state and append audit events in the same database transaction so the system can answer both “what is true now?” and “how did it get here?” Transactional outbox records carry committed changes to event streams and optional telemetry without making those systems part of the write transaction.

### Files, artifacts, and project knowledge

Binary and large artifacts belong in an object-storage abstraction: a local durable volume initially and an S3-compatible backend when deployed for a team. PostgreSQL stores their metadata, ownership, hashes, lineage, and access controls.

Cross-store writes use a staged protocol: content is written under an immutable content hash, then PostgreSQL atomically commits the referencing metadata, product state, and outbox event. Finalization is idempotent, and background reconciliation removes orphaned staged content. A task cannot report completion while referenced content is unavailable.

Uploaded documents are preserved unchanged as source artifacts. Ingestion produces normalized Markdown or text plus structural metadata and links back to the original. Agents normally consume bounded normalized representations, but users can always retrieve the source.

Embeddings are not required for the first slice. Begin with explicit file access, metadata filters, and PostgreSQL full-text search. Introduce `pgvector` behind a retrieval interface when document scale or observed retrieval quality justifies semantic search. Store chunks, citations, embedding model/version, and vectors alongside project metadata in PostgreSQL rather than operating a separate vector database prematurely.

### Sandboxed execution

Docker and Podman are supported sandbox runtimes behind one internal interface. A run declares its image, mounts, environment, network policy, CPU and memory limits, timeout, and allowed capabilities. The runtime adapter creates, monitors, stops, and cleans up the container without exposing provider-specific details to agents.

Default sandboxes must:

- run without privileged mode;
- use a non-root user where possible;
- mount only the assigned workspace view and necessary tool resources;
- receive secrets just in time and avoid writing them into artifacts;
- deny or restrict network access according to policy;
- enforce resource and wall-clock limits;
- persist intended workspace outputs outside the container lifecycle;
- emit auditable lifecycle and execution events.

The container boundary is one defense layer, not the whole security model. Host socket access, privileged containers, arbitrary mounts, and unfiltered credential injection are outside the safe default.

The sandbox controller is a trusted infrastructure component and is separate from agent containers. Only the controller may access the Docker or Podman API; that access is never mounted into an agent sandbox. The controller enforces allowlisted images and mounts, verifies fencing tokens, and exposes a narrow lifecycle API to workers. Compromise of this controller is treated as host compromise and must be visible in the threat model.

### Skills and MCP

Skills and MCP servers have versioned definitions, ownership, visibility, provenance, declared capabilities, and policy metadata. Installing or editing them is distinct from granting an agent permission to use them.

An MCP connection must support scoped credentials, tool discovery, health status, per-tool enablement, timeouts, output limits, audit events, and policy interception. Tool descriptions from an external server are untrusted input and cannot override system policy.

An agent can be private, team-visible, or public. Public agents are discoverable and reusable by other teams in the same Agentic OS installation, subject to installation and team policy. Installing a public agent pins a versioned definition and never copies credentials or grants. Definitions must also be exportable with secrets removed so sharing across installations can be added without redesigning the format.

## Governance and permissions

Agentic OS has two initial roles:

- **Regular users** can create and use projects, goals, agents, skills, and MCP configurations within their granted scope.
- **Admins** can do the same and additionally inspect installation-wide governance, observability, budgets, policy violations, overrides, and operational health.

Authorization must be enforced by the backend for every resource; hiding a frontend control is not security.

Ownership and access follow these initial rules:

| Resource | Owner and access rule |
| --- | --- |
| Installation | Admins govern installation settings, teams, global policy, and system health. |
| Team | Admins manage membership. Team membership is the default boundary for team resources. |
| Project | Owned by one team and attributed to a creating user. The creator or an admin grants project access to other members of that team. |
| Goal, task, run, workspace, artifact | Inherit project access and retain creator or actor attribution. They cannot be shared independently around project policy. |
| Agent and skill definition | Has a creating user, home team, immutable versions, and private, team-visible, or public visibility. Only the owner or an admin can publish or change visibility. Public grants read/install access, not edit rights. |
| MCP configuration | Owned by a team or project and never made public with credentials. Sharing an MCP server definition does not share credentials or grants. |
| Installed public definition | Becomes a version-pinned team resource governed by the installing team’s policies; its source owner cannot mutate that installed version. |

Execution supports three approval modes:

1. **Auto:** execute without user prompts inside effective policy and sandbox boundaries.
2. **Consequential actions:** pause for actions classified by policy as consequential, such as external publication, destructive mutation, sensitive data access, or elevated sandbox/network privileges.
3. **Every tool call:** pause before each tool execution.

The policy engine evaluates actions before dispatch. Policies can be layered at installation, team, project, agent, goal, MCP server, and tool scope. Actions include model calls, tool calls, sandbox lifecycle changes, credential access, network access, artifact or workspace promotion, and external side effects. The decision order is `deny`, then `approval required`, then `allow`; a more permissive lower scope cannot weaken a restrictive higher scope. An admin override is a separate, explicitly authorized decision rather than another ordinary policy.

Agent, skill, MCP, and model definitions are pinned for reproducibility, but safety policy is evaluated against the current effective policy before every action. Newly restrictive policy therefore applies immediately to running and resumed work. Every decision records the evaluated policy versions and previous decisions remain in the audit history.

Approval requests are durable interrupts with expiry and a clear action preview. An approval applies only to the described action or bounded action class, not to an open-ended future capability.

## Budgeting and cost control

Budgets are enforceable policy, not dashboard decoration. The first implementation supports an agent-scoped lifetime budget; later budgets may compose across goals, projects, users, periods, and teams. A budget has a currency, amount, enforcement mode, thresholds, and override policy. No configured budget means unlimited execution with full cost ledgering, unless a higher-level policy requires a budget.

- A **warning budget** allows work to continue and produces visible user and admin alerts.
- A **hard-stop budget** prevents new chargeable actions once the cap would be exceeded. Work remains blocked until the budget is disabled, raised, reset by its period, or explicitly overridden by an authorized admin.

The backend maintains an authoritative append-only cost ledger. Before a model call or other metered action, it reserves a pessimistic maximum cost in a serializable or row-locked database transaction shared by all concurrent runs under that budget. Configured output limits bound the reservation. Afterward it reconciles the reservation with actual usage. If a provider can exceed the reserved amount, the excess is recorded and all subsequent chargeable work remains blocked until policy permits it. Monetary values use fixed-precision decimal or integer minor units, never floating point.

OpenAI-compatible providers do not guarantee reliable cost data. Model profiles therefore require configurable pricing and accounting rules. Unknown pricing must be visible; hard budgets reject unpriced actions instead of pretending their cost is zero. Provider-reported token usage, locally estimated usage, pricing version, and final cost are recorded separately. A timed-out call with an uncertain provider result is recorded as an uncertain external side effect and possible duplicate cost before any retry.

Metered MCP tools use versioned per-tool pricing metadata, estimation rules, and currency. Unpriced metered tools are rejected under hard budgets. Non-chargeable tools still emit an explicit zero-cost ledger entry so the audit trail distinguishes “free” from “not measured.” The first-slice test MCP tool is non-chargeable.

An admin override is scoped, attributed, reasoned, time-bounded where appropriate, and part of the audit history.

## Observability and auditability

Use OpenTelemetry-compatible instrumentation and **Langfuse as the initial LLM observability backend**. Langfuse should be self-hostable and optional at deployment time: disabling it may reduce rich trace analysis but must not break execution, governance, or the core audit trail.

Every request, goal, task, run, model call, tool call, sandbox, checkpoint, approval, artifact, and ledger entry receives correlated identifiers. The UI should connect high-level progress to detailed traces without making users manually join systems.

Langfuse captures LLM-oriented traces, latency, token usage, cost observations, prompts and outputs when policy permits, and evaluation data. Agentic OS remains authoritative for task state, permissions, approvals, budgets, and audit events. A telemetry delivery failure cannot bypass a policy decision or lose the canonical cost ledger.

Observability must respect governance:

- secrets are always redacted;
- prompt, output, and artifact capture is configurable by scope;
- sensitive fields can be masked before export;
- retention and access follow team policy;
- regular users see their permitted work while admins can inspect installation-wide operational and governance views;
- dropped or delayed telemetry is itself measurable.

System health also requires conventional structured logs and metrics for queues, workers, database operations, sandbox lifecycle, event-stream delivery, retries, and failures. Langfuse complements rather than replaces operational monitoring.

## Durability contract

Durability is a product invariant with visible semantics:

- Acknowledged goals and user changes are committed before success is returned.
- Task decomposition and dependency changes are transactional and versioned.
- Each execution step has a stable identity and persisted status.
- Each safe boundary commits or durably references the run and step state, LangGraph checkpoint, ordered product events, artifact and workspace references, budget reservations, and external-side-effect status. No boundary is considered committed if those references cannot be reconciled.
- Checkpoints are associated with product runs and never advance the visible product state by themselves.
- Workers claim tasks through renewable leases; another worker may recover an expired claim.
- Heartbeats distinguish active, stalled, and abandoned work.
- Events are ordered per run and can be replayed after frontend reconnection.
- Retries and resume operations preserve attempt history rather than overwriting it.
- Cancellation is durable and cooperative, with forced sandbox termination available after a grace period.
- The system can classify uncertain external side effects and request reconciliation instead of silently repeating them.

The minimum failure test for every execution sprint is: terminate the worker or server during active work, restart it, and demonstrate either safe continuation from the last committed boundary or an explicit recoverable state. Acceptance suites distinguish process restart, host restart with durable volumes, and backup/restore; later infrastructure milestones may add these progressively, but must name which level they prove.

## Local-first deployment

The reference local deployment uses Compose-compatible configuration and supports both Docker and Podman. It should make the frontend, API, worker, PostgreSQL, artifact storage, sandbox runtime integration, and optional Langfuse services understandable and operable by one person.

The projected team deployment runs the same application roles on a cloud VM with durable volumes, TLS at the edge, backups, secret management, and independently scalable workers. Moving from local to team deployment must not require changing domain models or abandoning stored work.

Local-first does not mean single-process, host-bound, or disposable. Defaults should be convenient, while architecture and documentation make persistence, security boundaries, and service dependencies explicit.

Secret encryption uses a master-key provider abstraction. The local deployment must document key generation, file permissions, backup, recovery, and rotation; team deployments can add external secret managers without changing stored credential envelopes. Losing the master key must never silently downgrade encryption or leak stored credentials.

## Vertical sprint contract

GitHub milestones represent ordered sprints. Every sprint delivers one vertical, usable, testable slice of the operating system and leaves the main branch in a coherent state.

A sprint is not complete unless it includes, as applicable:

- a user-visible workflow in the Next.js/shadcn frontend;
- versioned API behavior and validation;
- domain and persistence changes with migrations;
- agent, worker, sandbox, or integration behavior needed by the slice;
- authorization, policy, budget, and audit treatment;
- loading, empty, error, retry, cancellation, and recovery states;
- structured telemetry with correlated identifiers;
- automated tests at the appropriate unit, integration, and end-to-end levels;
- a restart or failure-recovery test when durable execution is involved;
- updated operator and developer documentation.

Backend-only infrastructure and frontend-only mockups may be intermediate commits, but they are not sprint outcomes. New abstractions should be introduced when demanded by a vertical slice or a documented invariant, not to speculate about distant features.

## First integrated foundation slice

Because skills, MCP, budgeting, and both sandbox runtimes are required from the outset, the first milestone is an integrated foundation rather than a minimal durability prototype. It must still prove the hardest invariant through one coherent workflow:

1. An operator configures an OpenAI-compatible model profile.
2. The operator creates a project and submits a simple goal.
3. The operator can create or install a versioned skill and configure an MCP server, inspect its discovered tools, and grant selected capabilities to an agent.
4. A single agent receives one or more persisted tasks, uses the attached skill and a test MCP tool, and works in an isolated sandbox. The sandbox conformance suite runs against both Docker and Podman even if the operator selects only one runtime for the demonstration.
5. The operator assigns the agent a lifetime warning or hard-stop budget; all model and tool costs are ledgered.
6. The frontend streams durable progress, tool activity, cost, and artifacts.
7. The process is deliberately terminated mid-run.
8. After restart, the system recovers the task from PostgreSQL and resumes from the last safe boundary.
9. The operator can inspect the final result, task history, trace linkage, budget ledger, and audit events.

This slice uses stable project, goal, task, run, agent-version, skill-version, MCP-version, and policy identifiers with non-singleton database cardinalities. It does not implement unexercised multi-agent scheduling or concurrent-goal orchestration merely to anticipate them. Subsequent milestones add those capabilities as complete vertical workflows.

## Deliberate deferrals and non-goals

The following are not foundational requirements for the first slice:

- a public internet marketplace for agents or skills;
- cross-installation identity or federation;
- a standalone vector database;
- automatic semantic agent selection before explicit capability selection works;
- Kubernetes or distributed multi-region deployment;
- perfect exactly-once semantics for arbitrary external systems;
- unrestricted host execution;
- a large catalog of bespoke built-in integrations;
- pixel-complete administrative analytics before canonical events and ledgers exist.

These may become valid future work. They must not compromise the initial durability, governance, or end-to-end sprint discipline.

## Success criteria

Agentic OS is moving toward its vision when:

- users can express meaningful goals without manually orchestrating every agent;
- the selected agent team and task graph are understandable and steerable;
- concurrent goals make progress without corrupting shared project resources;
- active work survives routine process and host restarts;
- every consequential action and cost can be attributed to a user, goal, task, run, agent, model, and policy decision;
- hard budgets reliably prevent additional chargeable work;
- skills and MCP servers add capabilities without bypassing governance;
- admins can investigate failures and policy violations from correlated product and LLM telemetry;
- local operators can install, back up, restore, upgrade, and troubleshoot the system;
- each milestone produces a workflow a user can run and test, not only another architectural layer.

## Decision guardrails

Future coding agents and maintainers should preserve these decisions unless an explicit architecture decision record changes them with evidence:

1. Goals, tasks, runs, policies, budgets, and audit events belong to Agentic OS—not to an agent framework or observability vendor.
2. PostgreSQL is the production system of record and LangGraph uses durable PostgreSQL-backed checkpointing.
3. Deep Agents accelerates the harness while LangGraph supplies execution semantics; neither defines the product model.
4. Agent execution is isolated through Docker or Podman and mediated by policy.
5. Skills and MCP servers are first-class, versioned, governed resources.
6. Langfuse is the initial LLM observability backend; canonical audit and budget enforcement remain inside Agentic OS.
7. OpenAI-compatible BYOK is the initial model interface, with explicit capability and pricing metadata.
8. Original documents are preserved; normalized representations are derivatives with provenance.
9. Embeddings and `pgvector` are introduced behind a retrieval interface when evidence shows they are needed.
10. Every sprint is a frontend-to-backend vertical slice with failure, recovery, governance, and observability considered from the start.
