# Operations

## Service setup

Use one writer process. Adapt `service/organiser-inbox.service` to your Linux user,
checkout, virtual environment and external settings file. It is a template, not an
installer. Default examples bind to loopback. Protect any broader deployment separately.
Use private permissions for credentials; do not put real values into `.env.example`.

## Migrations and backup

Startup applies pending migrations. Before upgrading personal data, stop writers
and back up both SQLite and the complete knowledge root as one operational snapshot.
For SQLite, use its backup facility or copy the database and any WAL state while
stopped; do not assume an arbitrary copy of an active database is consistent.
Test the application migration helper on an isolated copy with an explicitly
configured `ORGANISER_DATABASE_PATH` first: Keep the original backup and record the revision.

## Knowledge write outcomes

Database commits and file replacement are distinct. Intent, completion and failure
records carry operation provenance. If an outcome is ambiguous, stop automated
retries, retain the original files/events and compare expected destination and
persisted content. Inspect the database binding and actual file before deciding
whether work completed or needs manual reconciliation. Appending again can duplicate content.
Restoring only the database or only Markdown can break correspondence; restore a
matched backup into an isolated location and inspect before resuming writers.

## Calendar and sources

Normal calendar queries read selected calendars. Setup and sync endpoints can create
and write the Organiser calendar. Trial deployments should use a dedicated test account.
Source registration is separate from operator permission. A missing/failed/partial
scan is not proof that every absent file was deleted. Inspect warnings and budgets.

```bash
.venv/bin/python -c "from organiser.core.config import get_settings; from organiser.db.migrations import upgrade_database; upgrade_database(get_settings())"
```

The plain Alembic CLI uses its INI URL; it does not automatically read the
application database environment variable.
