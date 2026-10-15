"""Make a bound context knowledge path unique across contexts.

Revision ID: 0009_context_knowledge_path_unique
Revises: 0008_context_foundation
Create Date: 2026-09-29

Additive migration. It adds a partial unique index on ``contexts.knowledge_path``
covering only non-NULL values, so "not yet bound" contexts keep coexisting while a
bound knowledge storage can never be shared by two contexts.

The migration refuses to guess. Existing rows are preflighted first:

* duplicate non-NULL values are rejected with the offending context IDs, because
  picking a winner would silently move one context's knowledge;
* values that do not satisfy the shared path contract (absolute paths, traversal,
  control characters, over-long paths, reserved store segments) are rejected with
  the same level of detail.

The migration never renames, normalises or deletes a value. Manual cleanup is a
documented operator step; see ``docs/architecture/0009-context-knowledge-integration.md``.

``0008`` is intentionally left untouched: rewriting an already released revision
would make the recorded history disagree with databases that already ran it.

Scope of the preflight correction
---------------------------------
This revision is not released yet, so the drive-prefix rule was corrected in place
instead of adding a new revision. Previously ``C:relative`` passed the preflight
even though the shared runtime validator correctly rejects it as a Windows drive
path: such a value would have been indexed and then been unusable for every later
write. Only the frozen preflight logic changed; the schema operation is untouched
and no existing database is touched or downgraded.

Databases that already stand on ``0009`` do **not** re-run the preflight, because
Alembic executes a revision exactly once. They keep the index and the values they
have. Re-validating them would be a separate, explicit operator step outside this
migration; this file neither performs nor requires it.
"""

from alembic import op
import sqlalchemy as sa


revision = "0009_context_knowledge_path_unique"
down_revision = "0008_context_foundation"
branch_labels = None
depends_on = None


INDEX_NAME = "uq_contexts_knowledge_path"

# Reservierte Segmente des Knowledge Stores. Identisch zu
# ``organiser.domain.knowledge_path.RESERVED_CONTEXT_SEGMENTS``; die Liste wird hier
# bewusst festgeschrieben, damit eine Migration unabhaengig vom Anwendungscode
# laeuft und nicht von einer spaeteren Aenderung der Domain mitgezogen wird.
RESERVED_SEGMENTS = ("notes.md", "overview.md", "progress.md", "topics")
MAX_PATH_LENGTH = 512

_MIGRATION_HINT = (
    "Bereinigung manuell und ausserhalb von Alembic, nach Backup und mit einer "
    "dokumentierten Einzelentscheidung je Context. knowledge_path auf NULL zu setzen "
    "oder einen bestehenden Wert umzubenennen ist KEINE Standardreparatur: NULL bedeutet "
    "'noch nicht gebunden', ein spaeteres ensure_context_knowledge wuerde auf "
    "contexts/<id> binden und dort geschriebenes Wissen verwaisen lassen. Vorher "
    "vorhandene Inhalte, Eigentuemerklaerung und Backup klaeren; Reihenfolge und "
    "Grenzen siehe docs/architecture/CONTEXT_KNOWLEDGE_RECOVERY.md Abschnitt 7."
)


def _preflight(connection) -> None:
    """Reject existing values that would make the new index wrong or ambiguous."""
    invalid: list[str] = []
    duplicates: dict[str, list[str]] = {}
    rows = connection.execute(
        sa.text("SELECT id, knowledge_path FROM contexts WHERE knowledge_path IS NOT NULL ORDER BY id")
    ).fetchall()

    for context_id, value in rows:
        problem = _problem(value)
        if problem is not None:
            invalid.append(f"  context {context_id}: {problem} (knowledge_path={value!r})")
            continue
        duplicates.setdefault(value, []).append(context_id)

    conflicting = [f"  knowledge_path={value!r} vergeben an: {', '.join(ids)}" for value, ids in sorted(duplicates.items()) if len(ids) > 1]

    if invalid or conflicting:
        details = []
        if invalid:
            details.append("Ungueltige bestehende knowledge_path-Werte:\n" + "\n".join(invalid))
        if conflicting:
            details.append("Mehrfach vergebene bestehende knowledge_path-Werte:\n" + "\n".join(conflicting))
        raise RuntimeError(
            "Migration 0009 wird abgebrochen; die Datenbank wurde nicht veraendert.\n"
            + "\n".join(details)
            + f"\n{_MIGRATION_HINT}"
        )


def _problem(value: object) -> str | None:
    """Return a human readable reason why ``value`` is not a canonical context path."""
    if not isinstance(value, str) or not value:
        return "Wert ist kein nicht-leerer String"
    if len(value) > MAX_PATH_LENGTH:
        return f"laenger als {MAX_PATH_LENGTH} Zeichen"
    if value != value.strip():
        return "fuehrender oder abschliessender Whitespace"
    if "\\" in value:
        return "Backslash im Pfad"
    if any(character == "\x00" or ord(character) < 0x20 or ord(character) == 0x7F for character in value):
        return "Steuerzeichen im Pfad"
    if value.startswith("/") or value[1:3] == ":\\" or value[1:3] == ":/":
        return "absoluter Pfad"
    # Windows-Laufwerkspraefix: ein einzelnes Zeichen an Position 0 mit Doppelpunkt
    # an Position 1. Auch die laufwerksrelative Schreibweise ``C:relative`` fuehrt
    # unter Windows in das Arbeitsverzeichnis des Laufwerks statt unter die
    # Wissenswurzel; der Preflight hat diese Werte vorher durchgewunken lassen.
    #
    # Die Regel ist bewusst festgeschrieben statt aus
    # ``organiser.domain.knowledge_path`` importiert: eine historische Migration
    # darf nicht vom spaeter veraenderlichen Domaincode abhaengen, sonst wuerde ein
    # Release den Index gegen eine Regel pruefen, die es zur Laufzeit gar nicht gibt.
    # ``tests/test_context_knowledge_migration_gate.py`` haelt beide
    # Implementierungen ueber eine gemeinsame parametrische Matrix zusammen.
    if value[1:2] == ":":
        return "Windows-Laufwerkspfad"
    if value.endswith("/"):
        return "abschliessender Slash"
    for segment in value.split("/"):
        if not segment:
            return "leeres Pfadsegment"
        if segment != segment.strip():
            return "Segment mit Randleerzeichen"
        if segment in {".", ".."}:
            return "'.'- oder '..'-Segment"
        if segment.startswith("-"):
            return "Segment beginnt mit '-'"
        if segment.casefold() in RESERVED_SEGMENTS:
            return f"reserviertes Store-Segment {segment!r}"
    return None


def upgrade() -> None:
    connection = op.get_bind()
    _preflight(connection)
    op.create_index(
        INDEX_NAME,
        "contexts",
        ["knowledge_path"],
        unique=True,
        sqlite_where=sa.text("knowledge_path IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index(INDEX_NAME, table_name="contexts")
