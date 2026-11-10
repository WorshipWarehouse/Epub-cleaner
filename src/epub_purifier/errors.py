"""Exception types used across the pipeline.

Each exception carries a stable ``exit_code`` so the CLI and the web layer can
map failures onto predictable results without string matching.
"""

from __future__ import annotations


class PurifierError(Exception):
    """Base class for every error raised by EPUB Purifier."""

    exit_code = 1

    def __init__(self, message: str, *, detail: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.message if not self.detail else f"{self.message} ({self.detail})"


class UsageError(PurifierError):
    """Bad arguments or configuration supplied by the caller."""

    exit_code = 2


class ValidationError(PurifierError):
    """The input is not a usable EPUB file."""

    exit_code = 3


class DrmProtectedError(ValidationError):
    """The input is encrypted or access-controlled; we refuse to touch it."""

    exit_code = 4


class CleaningError(PurifierError):
    """The file was readable but could not be rebuilt safely."""

    exit_code = 5


class ArchiveError(ValidationError):
    """The ZIP container is unreadable or unsafe."""


class UnsupportedContentError(PurifierError):
    """A resource type that cannot be processed safely."""
