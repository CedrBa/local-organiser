"""Shared, dependency-free fixtures for the workspace ingestion tests.

Everything here builds *temporary* artefacts. No test in this workstream reads a
real source file, a real knowledge file or the production database.

The PDF builders emit minimal but structurally valid PDF 1.4 documents with a
correct cross reference table, because a fixture that only "looks like a PDF"
would test pypdf's error recovery instead of the extraction path.
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from organiser.core.config import Settings
from organiser.db.models import Context, Resource, ResourceSnapshot
from organiser.db.session import create_session_factory
from organiser.services.domain import add_context_source, create_context


def make_pdf_bytes(pages: list[str]) -> bytes:
    """Build a text based PDF whose pages contain exactly the given strings."""
    objects: list[bytes] = []
    page_object_numbers = [3 + 2 * index for index in range(len(pages))]
    kids = " ".join(f"{number} 0 R" for number in page_object_numbers)
    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objects.append(f"<< /Type /Pages /Kids [{kids}] /Count {len(pages)} >>".encode("ascii"))
    font_number = 3 + 2 * len(pages)
    for index, text in enumerate(pages):
        page_number = page_object_numbers[index]
        content_number = page_number + 1
        objects.append(
            (
                f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 200] "
                f"/Contents {content_number} 0 R /Resources << /Font << /F1 {font_number} 0 R >> >> >>"
            ).encode("ascii")
        )
        escaped = text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")
        stream = f"BT /F1 12 Tf 20 100 Td ({escaped}) Tj ET".encode("latin-1", errors="replace")
        objects.append(
            b"<< /Length "
            + str(len(stream)).encode("ascii")
            + b" >>\nstream\n"
            + stream
            + b"\nendstream"
        )
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")

    out = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode("ascii") + body + b"\nendobj\n"
    xref_offset = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode("ascii")
    out += b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode("ascii")
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref_offset}\n%%EOF\n".encode(
        "ascii"
    )
    return bytes(out)


def make_image_only_pdf_bytes() -> bytes:
    """A structurally valid PDF with a page that carries no text layer."""
    content = b"q 1 0 0 RG 10 10 100 100 re f Q"
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 200] /Contents 4 0 R >>",
        b"<< /Length "
        + str(len(content)).encode("ascii")
        + b" >>\nstream\n"
        + content
        + b"\nendstream",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode("ascii") + body + b"\nendobj\n"
    xref_offset = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode("ascii") + b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode("ascii")
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref_offset}\n%%EOF\n".encode(
        "ascii"
    )
    return bytes(out)


def make_malformed_pdf_bytes() -> bytes:
    """A file that announces itself as a PDF and is not one."""
    return b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog >>\nendobj\nbroken without xref\n%%EOF\n"


def make_settings(tmp_path: Path, allowed_roots: tuple[Path, ...] = (), **overrides) -> Settings:
    """Settings with a temporary database and an explicit source allowlist."""
    return Settings(
        database_path=tmp_path / "ingestion.sqlite3",
        source_allowed_roots=tuple(Path(root).resolve() for root in allowed_roots),
        **overrides,
    )


def session_for(settings: Settings) -> Session:
    """Open a session against a freshly migrated temporary database."""
    from organiser.db.migrations import upgrade_database

    upgrade_database(settings)
    factory = create_session_factory(settings)
    return factory()


def close_boundary(session: Session) -> Session:
    """End a read transaction so the next service call gets a clean boundary.

    The resource services own their transactions and refuse to run inside a
    caller's. Asserting on ORM attributes opens a read transaction, so a test that
    inspects state and then calls a service again closes the boundary in between --
    the same thing a real caller has to do.
    """
    session.rollback()
    return session


def seed_context(
    session: Session, *, title: str = "Finanzierung", context_id: str | None = None
) -> Context:
    context = create_context(session, title=title, type="course")
    if context_id is not None:
        context.id = context_id
        session.flush()
    return context


def seed_source_id(
    session: Session,
    context: Context | str,
    *,
    path: str,
    source_type: str = "folder",
    status: str = "active",
) -> str:
    """Register a source and return its id. ``context`` may be a model or an id.

    Ids, not ORM objects: the resource services own their transactions, so a test
    that keeps a live model around would keep an open read transaction with it.
    """
    context_id = context if isinstance(context, str) else context.id
    source = add_context_source(
        session, context_id=context_id, source_type=source_type, path=path, status=status
    )
    return source.id


def resource_by_locator(
    session: Session, *, context_id: str, relative_path: str
) -> Resource | None:
    return session.scalar(
        select(Resource).where(
            Resource.context_id == context_id, Resource.relative_path == relative_path
        )
    )


def snapshots_of(session: Session, resource_id: str) -> list[ResourceSnapshot]:
    return list(
        session.scalars(
            select(ResourceSnapshot)
            .where(ResourceSnapshot.resource_id == resource_id)
            .order_by(ResourceSnapshot.created_at, ResourceSnapshot.id)
        )
    )
