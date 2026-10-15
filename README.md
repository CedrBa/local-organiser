# Local Organiser

[![Tests](https://github.com/CedrBa/local-organiser/actions/workflows/tests.yml/badge.svg)](https://github.com/CedrBa/local-organiser/actions/workflows/tests.yml)

A personal organisation assistant connecting documents, knowledge, tasks and planning.

Local Organiser turns unstructured inputs into persistent context and reviewable
changes. Local processing, traceability and explicit control over model-driven
updates guide its design. It is a single-user backend under active development.

The project also investigates this question:

> How much cloud usage can bounded context, local models and selective routing
> avoid while maintaining comparable task quality?

That evaluation is **planned**. No model rankings or savings claims are available yet.

## Current capabilities

- Idempotent inbox intake, including device timestamp normalisation.
- Projects, tasks, hierarchical contexts, sources, topics and resource links.
- Explicit, allowlisted scans with bounded text and isolated PDF extraction.
- Human-readable Markdown knowledge alongside structured SQLite state.
- Deterministic workspace packets with evidence, omissions and freshness manifests.
- Persisted context-action proposals with explicit apply/reject and audit history.
- Planning/scheduling proposals and optional CalDAV integration, including a
  separate explicit write path to the Organiser calendar.

The context-action workflow requires confirmation. The older inbox interpretation
workflow can apply validated changes immediately; these are distinct API paths.
There is currently no dashboard, general chat interface or automatic model router.

## Quickstart

Use a Linux checkout and Python 3.11 or newer. CI targets 3.11 and 3.14.
The descriptor-based source reader is designed for Linux.

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
export ORGANISER_API_TOKEN="$(openssl rand -hex 32)"
export ORGANISER_DATABASE_PATH="$(pwd)/data/demo.sqlite3"
export ORGANISER_KNOWLEDGE_ROOT="$(pwd)/data/knowledge"
.venv/bin/python -m uvicorn organiser_api:app --host 127.0.0.1 --port 8765
```

Startup applies pending migrations to the selected database. Start with a fresh
demo database. `.env.example` is a reference, **not** an automatically loaded file.
Health is available at `/health`; business endpoints require the Bearer token.
Interactive API documentation is served at `/docs`.

The [synthetic demo](examples/demo/README.md) demonstrates intake, workspace
and confirmed application without a model server or calendar.

## Design

| Concern | Approach |
|---|---|
| Structured state | SQLAlchemy, SQLite constraints and Alembic migrations |
| Qualitative knowledge | Controlled Markdown updates preserving user content |
| Model boundary | Typed validation, scoped IDs and evidence |
| Freshness | Version revalidation before context-action application |
| Traceability | Events and persisted processing/proposal state |
| Testing | Temporary data, fake adapters and failure/concurrency regressions |

See the [architecture](docs/architecture/overview.md),
[development guide](docs/development.md) and [documentation index](docs/index.md).
Source checkout installation is supported; standalone wheel deployment is not.

## Direction

Future work combines the backend with a task-specific benchmark, small-model
routing, context queries and better planning. See the [roadmap](docs/roadmap.md)
and [benchmark design](docs/evaluation/benchmark-design.md).

Public versions are intended as curated snapshots of completed, tested work.
Experiments and internal planning are maintained separately.

## Rights

Source visibility grants no additional licence to reuse, modify or redistribute
this project's original material. See [RIGHTS.md](RIGHTS.md).
Third-party components retain their respective licences.
