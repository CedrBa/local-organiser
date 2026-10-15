"""Guard caller ownership before locking and roll back only application-owned work."""

from __future__ import annotations

from functools import wraps


def _application_operation(method):
    """Guard caller ownership before acquiring the writer lock and entering cleanup."""

    @wraps(method)
    def owned(self, *args, **kwargs):
        # Entry refusal must NEVER roll back caller work. Only after this guard
        # succeeds can we clean up transactions created by this operation.
        self._require_application_boundary()
        with self._store._lock:
            try:
                return method(self, *args, **kwargs)
            except BaseException:
                try:
                    self._session.rollback()
                except Exception:
                    pass
                raise

    return owned
