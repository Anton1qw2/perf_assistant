# Performance Assistant implementation plan

Status: proposed implementation roadmap; no implementation is claimed complete.
Date: 2026-10-23.

## 1. Goal and scope

Build a local performance investigation workbench that turns conversation into an
agreed task, investigates profiler and source evidence, and hands approved work to
external execution infrastructure. Web and TUI share history, task state, HTTP API
and durable execution. Include real connection/access diagnostics from chat and a
compact connection status bar.

This is a self-contained plan for building the system from scratch. It defines
requirements, architecture, delivery stages and acceptance criteria in full.
No accompanying project documents are required. Component and API names
below are proposed design choices.
The first delivery target is one local user without authentication. Multi-user
hosting, autonomous code changes and distributed scheduling are later decisions.

## 2. Product invariants

- Discussion, consultation, task preparation, diagnostics and execution are distinct.
- Task changes require task intent; investigation starts only after explicit Run.
- Launch needs a summary, no material open questions, and a repository or profile.
  Do not require both or a universal metrics questionnaire.
- A profile is an opaque result ID, never a local path.
- Runs retain immutable task snapshots; later settings changes do not mutate them.
- Questions and confirmations have correlated, retry-stable identifiers.
- Model output proposes actions; host code validates and authorizes them.
- Configuration, connection, authorization and selected-object access are separate
  facts. An unknown result remains unknown.
- Analysis completion, code correctness and measured improvement are separate facts.
- Demo fixtures remain explicitly synthetic, including diagnostic results.
- Tokens stay in encrypted backend storage and transport headers, outside prompts,
  tasks, evidence, chat, SSE, browser drafts and logs.

## 3. Architecture and ownership

Use a modular Python application with separate API and Worker processes, SQLite WAL
on local storage, and HTTP/SSE clients. Start without a separate message broker or
microservices. Keep LangGraph behind application-owned lifecycle contracts.

| Boundary | Responsibility |
| --- | --- |
| API | Validate commands, check versions, enqueue jobs, expose snapshots/events |
| Worker | Claim jobs, enforce leases/deadlines, invoke handlers, commit outcomes |
| Conversation service | Route intent, prepare task patches, discuss responses |
| Task service | Validate agreed state, apply partial updates, decide readiness |
| Analysis workflow | Advance bounded, checkpointed investigation steps |
| Evidence store | Preserve original responses, scope, hashes and provenance |
| Integration adapters | MCP transport, local Git, documentation and semantic probes |
| Diagnostics service | Perform bounded checks and produce host-owned status records |
| Credential service | Encrypt, bind, resolve and retire credential references |
| Presentation | Safe Markdown and consistent Web/TUI state |

API requests do not execute agents or external checks inline. Worker execution
continues after a browser closes. UI code never reads SQLite or invokes agents.

## 4. Core contracts

Define versioned Pydantic contracts before implementation and export JSON schemas
from those models. Establish explicit compatibility and migration rules before
changing persisted contracts after the first release.

| Entity | Essential information |
| --- | --- |
| Conversation / Turn | Messages, classification, history boundary, response links |
| TaskSpec | Summary, target, agreed options, open questions, version |
| TaskPatch | Explicit unchanged/set/clear semantics; expected task version |
| Run | Immutable task snapshot, status, budget, workflow/checkpoint version |
| RunContext | Accepted profile/repository/ref and ordered context revisions |
| Question | ID, kind, owner, expected answer and continuation point |
| Evidence | ID, kind, origin, scope, timestamp, hash and payload reference |
| Finding / Report | Observations, hypotheses, citations, limitations, validation |
| ConnectionCheck | Scope, per-stage outcomes, freshness, safe reason codes |

Use typed options for built-in analysis. Keep a bounded extension area for custom
executors rather than putting all application state in an unrestricted dictionary.
Keep consultation state and diagnostic state outside the performance task.

Keep large evidence separate from workflow checkpoints. A checkpoint contains
evidence IDs and control state. Version checkpoints and reports independently;
define whether an older active workflow can resume or requires a new run.

The executor boundary advances one durable step:
`step(run_id, task_snapshot, checkpoint, answer=None)` returns an event and a
JSON-serializable checkpoint; `cancel(run_id)` requests cooperative cancellation.
Events represent progress, a correlated question or a final result. Implementations
must tolerate repeated steps and bound their transports. The analysis boundary uses
the same principle while returning structured evidence-backed reports.

### Initial operational policies

- Use at-least-once job delivery. Commit state, events and job completion in one
  transaction only while the worker owns the lease.
- Track cumulative active execution time across steps; waiting for user input does
  not consume it. Every model/tool call is limited by the remaining active budget.
- Define explicit code-owned bounds for discovery pages, tool counts, payload size,
  planning rounds, tool calls, source reads, hotspots and report size. Select initial
  values during phase 0 and test exhaustion behavior before real execution.
- Treat schemas, documentation, source comments and tool responses as untrusted data.
  They cannot grant permissions or override host instructions.
- Validate schemas without remote reference retrieval. Pin integration policy and
  selected tool definitions per run; reject incompatible drift before execution.
- Bind personal credentials to their intended destination and repository where
  applicable. Use HTTPS for authenticated remote requests, reject authenticated
  redirects, and use an OS-backed secret store or encrypted backend records.
- Escape raw HTML, reject unsafe links and disable remote chat images. Render the
  same supported Markdown subset in both clients.
- Bind the initial unauthenticated deployment to loopback. Require an explicit
  authentication/access-control design before offering shared remote hosting.

## 5. Delivery phases

### Phase 0: requirements and design decisions

- Define user journeys, contracts, state transitions and extension boundaries.
- Record decisions for state ownership, evidence storage and version compatibility.
- Choose one actual profiler integration for the first validated vertical slice;
  discover its schemas and establish read-only permissions without inventing tools.
- Define a small evaluation corpus of English/Russian intents and ambiguous replies.

Exit: a prioritized backlog, initial contract definitions and explicit architecture
decisions are ready. Deferred features and deployment assumptions are documented.

### Phase 1: durable application foundation

- Implement conversation/task/run repositories, migrations and version checks.
- Persist jobs, state changes and client events atomically.
- Implement leases with stale-worker write rejection and request deduplication.
- Implement one-active-run enforcement, cancellation and restart recovery.
- Add snapshot endpoints and SSE replay with ordered event IDs.
- Provide backend-owned failure codes without exception text or provider payloads.

Exit: a deterministic executor survives restart, duplicate delivery, stale leases
and client disconnection without duplicate state commits or lost questions.

### Phase 2: conversation and task preparation

- Process pending execution answers and explicit commands before normal routing.
- Route task, mixed, consultation, capabilities, dialogue and diagnostics intents.
- Supply only whitelisted current selection/task metadata to routing.
- Use a structured extraction call for task patches; validate changes in host code.
- Preserve fields omitted by a partial update and reject stale task versions.
- Persist turn classifications and pending consultation questions atomically.
- Exclude non-task turns and expert guidance from later task extraction.
- Render capabilities deterministically from registered metadata/configuration.
- Keep explanation/reformatting separate from saved reports and investigations.
- Bound routing, extraction and response generation with one shared intake deadline
  and serialized model-input budget. Start with a 120-second turn deadline; reserve
  output capacity within the selected model's context limit.

Exit: consultations, capability answers, discussion and diagnostics leave the full
task and spec_version unchanged; task routing never automatically executes analysis.

### Phase 3: integrations and connection diagnostics

Implement this phase before the first complete real-analysis delivery, so users can
verify dependencies without starting an investigation.

- Implement bounded MCP discovery/calls, schema validation and credential transports.
- Implement the diagnostic model, adapter probes and job handler in section 6.
- Add chat routing, deterministic results and read/recheck API commands.
- Add the shared Web/TUI status presentation in section 7.
- Validate against controlled fixtures and then the chosen actual deployment.

Exit: chat and UI report the same persisted check, distinguish connectivity from
object access, and never use synthetic checks to claim a real connection.

### Phase 4: first real analysis vertical slice

- Freeze run task/context, integration policy and local source revision if used.
- Execute discover -> plan -> call/source -> plan -> report -> format_report.
- Persist each planned call before execution and its response before further planning.
- Enforce read-only allowlists, schema checks, catalog drift checks and deadlines.
- Save original evidence and deliver bounded, explicitly selected context to models.
- Validate structured interpretations and delivered citation IDs.
- Save invalid raw output for audit without promoting it to accepted observations.
- Format chat reports deterministically after report persistence.

Exit: one real profiler task produces a traceable report; restart at formatting does
not repeat profiler/model calls. Deployment validation is recorded explicitly.

### Phase 5: clarification, source and context lifecycle

- Persist ordered question/answer journals and retry-stable clarification IDs.
- Accept optional metric preselection, plain names and supported no-selection replies.
- Implement bounded local Git reads against immutable regular UTF-8 blobs.
- Preserve required source ranges independently of planner-selected excerpts.
- Add typed context proposals, correlated confirmation and a host-owned ledger.
- Preserve the original task; archive old scopes and refresh only after acceptance.
- Keep cumulative budgets and credential destination bindings across context changes.

Exit: a run resumes across questions/restarts; replacing a profile cannot mix old
evidence into active findings, and repository changes cannot forward old personal tokens.

### Phase 6: hotspot investigations and reference experts

- Select exact records from existing evidence into a bounded sequential hotspot queue.
- Give each investigation only its selected records and local follow-up evidence.
- Preserve partial findings and budget limitations; synthesize structured findings.
- Register trusted experts through factories with explicit context policies.
- Implement full-document documentation lookup and validated source registries.
- Snapshot optional local knowledge once per run; treat it as untrusted guidance.

Exit: findings remain attributable to their scopes; references never become measured
evidence or authorize tools. Missing/oversized documentation is an explicit limitation.

### Phase 7: external execution boundary

- Integrate ExecutorGateway with stable run/operation identities and checkpoints.
- Translate external progress, questions, cancellation and results into host contracts.
- Require external deduplication for side effects; local leases alone are insufficient.
- Retain before/after evidence, correctness results and artifacts separately.
- Define a separate proposal/approval workflow before enabling mutating execution.

Exit: a retry cannot repeat an accepted external mutation; successful execution does
not claim improvement without suitable measurement evidence and correctness checks.

## 6. Diagnostics design

### Scope and result model

Represent each check as independent stages, with outcomes such as pass, fail, unknown,
not_applicable or skipped. Keep freshness separate from outcome.

| Stage | What it can establish |
| --- | --- |
| Configuration | Required settings and a permitted policy are present |
| Protocol connection | MCP initialization/discovery completed successfully |
| Authorization | The tested operation was accepted or explicitly denied |
| Permitted tools | Discovered tools overlap the configured read-only allowlist |
| Selected object | A supported probe read the selected profile/repository |
| Documentation | The configured document is readable, nonempty and within bounds |

A successful public discovery does not prove an authenticated read is authorized.
If a server conflates missing and forbidden resources, retain that ambiguity. Do not
derive a root cause from arbitrary error text.

ConnectionCheck should contain check ID, source kind, selection/settings revision,
stage outcomes, safe reason codes, checked time, expiry and synthetic classification.
Persist no tokens, raw endpoint query parameters or raw provider errors. Keep any
internal scope fingerprint opaque and do not hash secret values for display/storage.

### Probe adapters

- Generic MCP probe: initialize and discover with explicit transport bounds; report
  only protocol and permitted-catalog facts actually established.
- Semantic adapter: optionally perform a minimal permitted read to check a selected
  object. Validate returned identity only where the server contract supports it.
- Local Git probe: resolve the explicitly scoped selection without clone/fetch or
  changing the working tree. Source readability is distinct from build linkage.
- Documentation probe: use a shared document loader and source registry; report content hash and
  version only when supplied or verifiable. Reading a document does not prove that
  the LLM can answer every question or that every reference link is reachable.

For arbitrary MCP schemas without a semantic adapter, selected-object access stays
unknown. Do not ask an LLM to choose speculative tools solely to make a status green.
Discovery must not expand the allowlist. Do not execute code, collect new profiles,
load remote documentation links or perform mutations as part of diagnostics.

### Chat and job behavior

- Support requests such as "Check connections", "Can you access this repository?"
  and their Russian equivalents. Proposed explicit fallback: /check [all|profiler|git|docs].
- Define /ask as explicit consultation and /task as explicit task preparation; these
  commands override automatic routing. Pending execution clarification answers have
  priority over normal intake; a separate UI action can request diagnostics meanwhile.
- Keep capabilities deterministic; a capability question alone starts no probe.
- Enqueue a deduplicated diagnostics job and expose persisted progress/completion.
- Use the current saved conversation settings and credential references, not an
  active run's immutable settings. Label the checked selection accordingly.
- Deduplicate concurrent equivalent checks. A settings change makes old results stale;
  a late result cannot replace a status for a newer settings revision.
- Render answers from structured records without a second model call.
- Preserve task/version, exclude diagnostics from task-extraction history and define
  pending-expert switching consistently with other explicit route changes.
- Share the intake deadline for chat-triggered checks; give every transport
  a bounded timeout. Record independent source failures so partial checks remain useful.
- Expired/stale-worker results cannot commit successful completion or generated replies.

### Proposed API surface

Finalize naming during contract review. Define these endpoints as part of the new API.

- POST /api/conversations/{id}/connection-checks: enqueue an explicit check with
  request_id, requested sources and expected settings revision.
- GET /api/conversations/{id}/connection-checks: read latest scoped records; no probes.
- SSE connection-check events: carry safe summaries and invalidate/refetch client state.

No diagnostics credentials are accepted in chat or ad hoc endpoint arguments. Saving
credentials uses dedicated settings commands and destination-binding checks.

## 7. Status bar and client behavior

- Show compact Profiler, Git and Documentation items with text plus color/icon.
- Distinguish unchecked, checking, verified at a stated scope, partial, failed and stale.
- Never display generic "connected" when only configuration was detected.
- Expand an item to show stage outcomes, checked time, scope and a Check again action.
- Store results server-side so Web, TUI and chat agree after reload/reconnection.
- Initial rendering reads saved status without making external calls. MVP checks
  are explicit; continuous background polling is deferred.
- Mark affected results stale after changes to endpoint, credentials, policy or selection.
  Reconcile document changes when observable or on the next check/expiry.
- Use a code-owned initial freshness window of five minutes; expiry makes a check
  stale, not failed. Do not add a new environment variable without a concrete need.
- Use compact blue styling, a two-line scrollable execution status with Copy all,
  independently collapsible settings, narrow-screen overlays, accessible text labels
  and reduced-motion behavior. Use muted red for failures and green for completion.
- Animate only in-flight checks. Keep failures, stale states and waiting states static.
- Implement per-conversation drafts, request ownership, SSE ownership and snapshot retries.
  A late snapshot cannot erase a disconnect notice or newer connection status.

## 8. Validation strategy

| Area | Required scenarios |
| --- | --- |
| State/durability | Crash before/after commit, redelivery, stale lease, duplicate request |
| Intake | Russian/English paraphrases, commands, pending questions, unchanged task |
| MCP | Timeout, rejected auth, empty catalog, denied allowlist, drift, malformed output |
| Diagnostics | Server reachable/object denied, unsupported probe, independent failures |
| Scope/freshness | Switch conversation, change token/selection, late response, expiry |
| Secrets | No credential/provider payload leakage through errors, events or model input |
| Evidence | Empty data, zero/false, invalid citation, wrong scope, invalid interpretation |
| Sources | Pinned revision, missing required ranges, path bounds, no symlink traversal |
| UI | Reload/reconnect, stale snapshot, narrow screen, keyboard, reduced motion |
| External execution | Idempotency, clarification replay, cancellation, incomplete validation |

Use deterministic transport/model fixtures for regression tests and a separate
deployment smoke test for real MCP/model compatibility. Record model evaluation
results separately from schema/transport test results; passing schemas does not
establish accurate intent classification or factual interpretation.

Set up Python unit/integration tests, static checks and JavaScript client-state tests
as part of the first delivery. Suitable choices are pytest, Ruff and Node's test
runner. Add a schema export command and a drift check so generated schemas match
the versioned models. Add browser inspection for layout and accessibility. Run the
checks relevant to each change; documentation-only edits need consistency checks.

## 9. Milestones and ordering

| Milestone | Dependencies | Demonstrable result |
| --- | --- | --- |
| M1: durable shell | Phases 0-1 | Restart-safe task/run and event delivery |
| M2: chat preparation | M1, phase 2 | Agreed task with isolated non-task discussion |
| M3: access visibility | M2, phase 3 | Real scoped checks from chat and status bar |
| M4: useful investigation | M3, phase 4 | First real evidence-backed analysis report |
| M5: resumable investigation | M4, phase 5 | Clarifications, pinned source, context changes |
| M6: deeper analysis | M5, phase 6 | Hotspot findings and bounded reference experts |
| M7: execution handoff | M5, phase 7 | Retry-safe external execution with validation |

Validate one real server at M3/M4 before generalizing more adapters. Begin with
phase 0 and deliver milestones in dependency order. Estimate calendar work after
requirements, staffing and the first integration contract are understood.

## 10. Completion and documentation

Create documentation alongside implementation: installation/startup instructions,
API and executor contracts, integration setup, credential handling, diagnostic
semantics, recovery procedures and known limitations. Export schemas when contracts
change. Keep deployment configuration in one validated settings layer, and keep
algorithmic/security bounds explicit in the components that enforce them.

Release acceptance requires restart/retry safety, task isolation, scoped evidence,
credential isolation and accurate diagnostics for the supported deployment. Record
actual validation limitations. Keep a release validation record with tests performed,
supported deployments and unresolved issues. Review this roadmap after each milestone
so planned work remains clearly distinguishable from delivered product behavior.
