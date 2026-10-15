# Limitations

- Single-user, single-writer-process application. A Bearer token is not per-context
  authorisation or a multi-user access-control system.
- Knowledge locks are process-local. Multiple writers sharing a root are unsupported.
  Database and filesystem writes do not share an atomic transaction.
- Source access uses Linux descriptor primitives. Other platforms are not validated.
  Bounded PDF extraction is not OCR.
- Scans are explicit. No permanent watcher, general chat or dashboard is included.
- Context actions need confirmation; legacy inbox interpretation can apply validated
  changes immediately. These remain distinct workflows.
- CalDAV can write through explicit Organiser-calendar operations. Read-only queries
  do not make the whole integration read-only.
- An uncertain write outcome is not an ordinary refusal. Inspect before retrying,
  especially for append operations.
- Checkout installation is supported. Alembic assets and the compatibility entry
  point currently depend on that layout; wheel deployment is not validated.
- Planned model benchmarks and savings have not been measured.

See [operations](operations.md) for backup and reconciliation guidance.
