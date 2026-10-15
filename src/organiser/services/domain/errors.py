"""Stable errors for deterministic domain operations."""

from __future__ import annotations


class DomainError(Exception):
    """Base class for expected domain failures translated at the API boundary."""

    pass


class NotFoundError(DomainError):
    """An explicitly addressed domain entity does not exist in the required scope."""

    pass


class ConflictError(DomainError):
    """The requested change conflicts with lifecycle, relationship or current state."""

    pass


class DomainValidationError(DomainError):
    """Input violates the deterministic domain contract."""

    pass
