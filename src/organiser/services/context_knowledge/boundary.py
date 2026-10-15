"""Own application transactions without committing or rolling back caller work."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator
from sqlalchemy import select
from sqlalchemy.orm import Session
from organiser.db.models import Context
from .contracts import ContextKnowledgeError, ContextKnowledgeNotInitializedError, _ContextBinding


class KnowledgeBoundary:
    """Own the service transaction; never commit or roll back caller work."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def _require_application_boundary(self) -> None:
        """Sicherstellen, dass hier eine eigene Application-Transaktion beginnt.

        Drei Faelle werden abgelehnt:

        1. ``defer_domain_commit`` -- der Aufrufer haelt die Transaktion offen.
           Dateisystemarbeit darin wuerde fuer andere Teilnehmer sichtbar werden,
           bevor die Datenbank zustimmt.
        2. Eine laufende Transaktion. Sie umfasst auch alles, was schon geflusht
           ist, und damit genau die Arbeit, die ein ``commit()`` dieses Service
           sonst mitnehmen wuerde: geflushte Inserts, Updates und Deletes sowie
           Schreibvorgaenge ueber ``session.execute``. ``new``/``dirty``/``deleted``
           erkennen eine Flushe nicht.
        3. Ausstehende, noch nicht geflushte ORM-Aenderungen -- ein ``add()`` ohne
           Flush beginnt keine Transaktion, wuerde aber beim naechsten Commit
           mitgeschrieben.

        Der Aufrufer muss seine Transaktion vorher beenden. Bewusst wird hier
        weder committet noch zurueckgerollt: fremde Arbeit gehoert dem Aufrufer.
        """
        if self._session.info.get("defer_domain_commit"):
            raise ContextKnowledgeError(
                "Dateisystemarbeit ist in einer defer_domain_commit-Transaktion nicht erlaubt"
            )
        if self._session.in_transaction():
            raise ContextKnowledgeError(
                "Der Context-Knowledge-Service ist eine eigene Application-Transaktion und "
                "darf nicht in eine laufende Transaktion des Aufrufers hineinlaufen; "
                "bestehende oder bereits geflushte Aenderungen wuerden sonst mitcommittet. "
                "Bitte die eigene Transaktion vorher committen oder zurueckrollen."
            )
        pending = [*self._session.new, *self._session.dirty, *self._session.deleted]
        if pending:
            names = sorted({type(item).__name__ for item in pending})
            raise ContextKnowledgeError(
                "Der Context-Knowledge-Service ist eine eigene Application-Transaktion und darf keine "
                f"fremden ausstehenden ORM-Aenderungen mitcommitten: {', '.join(names)}"
            )

    @contextmanager
    def _own_transaction(self) -> Iterator[None]:
        """Eigene Transaktion: Eintritt pruefen, eigenen Fehler sauber abschliessen.

        Nach jedem Fehler -- auch nach einem mehrdeutigen -- ist die Session
        wieder in einem Zustand, in dem der Aufrufer seine eigene Transaktion
        beenden und den Vorgang bewusst wiederholen kann. Es wird ausschliesslich
        die Transaktion dieses Service zurueckgerollt; eine fremde kann es unter
        dem Eintrittsschutz nicht geben.
        """
        self._require_application_boundary()
        try:
            yield
        except BaseException:
            try:
                self._session.rollback()
            except Exception:  # pragma: no cover - nur bei unbrauchbarer Verbindung
                pass
            raise

    def _assert_committed(self, record: _ContextBinding) -> None:
        """Sicherstellen, dass der Context weiterhin existiert und committet ist.

        Der Eintrittsschutz macht einen nicht committeten Context bereits
        unmoeglich: ohne Transaktion und ohne Pending-State kann die Session nur
        committete Zeilen sehen. Die Gegenprobe ueber eine eigene Verbindung
        bleibt als zweite, unabhaengige Zusage bestehen -- sie schliesst zusaetzlich
        den Fall, dass die Zeile zwischen Laden und Schreiben verschwindet, und
        haelt die Regel fuer den naechsten Leser sichtbar.
        """
        engine = self._session.get_bind()
        with engine.connect() as connection:
            committed = connection.execute(
                select(Context.id).where(Context.id == record.context_id)
            ).scalar()
        if committed is None:
            raise ContextKnowledgeNotInitializedError(
                f"Context {record.context_id} ist nicht (mehr) committet; es werden keine Dateien angelegt"
            )

    def _commit(self) -> None:
        try:
            self._session.commit()
        except Exception:
            self._session.rollback()
            raise
