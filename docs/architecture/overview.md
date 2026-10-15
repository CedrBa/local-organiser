# Architecture

Local Organiser maintains persistent organisation state behind a FastAPI API.
Deterministic code controls access, selection and mutation. Models receive bounded
inputs and return outputs that must pass validation.

| Layer | Responsibility |
|---|---|
| `api` | HTTP contracts, dependencies, authentication and error translation |
| `domain` | Typed values, proposal contracts, limits and validation |
| `services` | Application workflows, state transitions and adapters |
| `db` | Persistence, SQLite configuration and migration setup |
| `migrations` | Historical schema transitions |

## State and knowledge

Projects/tasks represent actionable work. Contexts represent persistent activity
areas and may have parents and zero or more sources. Resources/snapshots describe
observed content. Topics connect subject matter, tasks and resources; events record provenance.
SQLite is canonical for structured state and relationships. Markdown is canonical
for qualitative knowledge. Original source files remain under the user's control.

## Intake and evidence

Registration is not permission. Scans recheck operator-allowed roots, source
identity and bounded descriptor access. Extracted text is a derived cache; PDF
parsing runs in an isolated bounded process.
The workspace selects data for exactly one context, redacts host paths, bounds
actual serialised output and reports omissions. Evidence carries provenance and
versions. The builder reads snapshots/knowledge, not original source trees, and writes nothing.

## Models and actions

Clients are replaceable. Context proposals are persisted before review and explicit
apply/reject. Apply checks relevant versions and claims the proposal before mutation.
This controls stale/repeated work; it cannot prove a model interpretation is correct.
Legacy inbox interpretation and task/project planning retain separate contracts.
Scheduling is deterministic; CalDAV writes are explicit Organiser-calendar operations.

## Consistency

Domain changes and events share database transactions. Knowledge operations span
two resources: record intent, verify the file result and record outcome. The shared
process-local lock is neither a distributed transaction nor a cross-process lock.
See [operations](../operations.md),
[limitations](../limitations.md) and [evaluation](../evaluation/benchmark-design.md).

## Service packages

| Package | Internal boundaries |
|---|---|
| `services/domain` | Projects/tasks/contexts/sources, binding rules, audit and commit policy |
| `services/source_scanner` | Operator policy, descriptors, bounded reading and enumeration |
| `services/resources` | Identity, observations, snapshots, scan/extraction orchestration and verification |
| `services/knowledge` | Markdown codec, shared filesystem, bounded reader and content updates |
| `services/context_knowledge` | Transaction ownership, immutable binding, audit and result verification |
| `services/topic_knowledge` | Topic contracts, entry guard, placeholder content and service coordination |
| `services/topics` | Lifecycle, context bindings, links, reads and integrity boundaries |
| `services/workspace_context` | Selection, redaction, budgets, projection, manifests and freshness checks |

Each package explicitly exports its supported entry points. Internal modules
import their direct dependencies rather than their own package facade. The source
layout is designed around application responsibilities rather than class count.

## Design decisions

### Structured state and readable knowledge

SQLite holds identities, relations and lifecycle state. Markdown holds qualitative
knowledge. Source ingestion never reorganises original files. This keeps knowledge
readable without treating free text as a relational database.

### Bounded evidence before model execution

Select deterministically and cap serialised packets. Include versions and omissions.
Documents remain untrusted evidence; text cannot redefine IDs or permissions.

### Reviewable actions

Persist context proposals, revalidate versions and claim them before applying.
Keep legacy immediate inbox interpretation explicit rather than silently changing it.

### Explicit consistency limits

A shared process-local writer lock coordinates files. Intent/outcome records expose
partial failures; no joint DB/filesystem commit is claimed. Uncertain outcomes need inspection.

### Rules before model routing

Use deterministic rules for constraints and scheduling. Keep clients replaceable.
Whether small routing models improve on rules is a research question to evaluate.
