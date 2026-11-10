"""Scan and clean a downloaded EPUB before any copy is offered to a user.

This is the gate between a source's raw bytes and the download endpoint. It
reuses the same validation, inspection and cleaning pipeline as the CLI and the
upload path, so a fetched book gets exactly the treatment an uploaded one does,
with one deliberate difference: findings that mean the *archive itself* is
unsafe are never cleaned around. They reject the download outright, because a
cleaned copy is not a reason to have accepted a payload in the first place.

Nothing here executes, renders or follows anything from the file.
"""

from __future__ import annotations

from dataclasses import dataclass

from .config import PurifierConfig
from .errors import DrmProtectedError, PurifierError, ValidationError
from .pipeline import clean_epub, inspect_epub
from .report import Report

#: Archive-level findings that abort a download instead of being cleaned away.
#: Scripts and tracking beacons are removable, so they are cleaned; executables,
#: traversal members, symlinks and nested archives are not.
BLOCKING_RULES = frozenset(
    {
        "archive.executable",
        "archive.unsafe_member",
        "archive.symlink_member",
        "archive.nested_archive",
        "archive.encrypted",
    }
)


class ScanRejected(PurifierError):
    """The downloaded bytes are unsafe or unusable, so no cleaned copy exists."""

    status_code = 422

    def __init__(
        self,
        message: str,
        *,
        detail: str | None = None,
        rules: tuple[str, ...] = (),
    ) -> None:
        super().__init__(message, detail=detail)
        self.rules = rules


@dataclass(frozen=True)
class ScanOutcome:
    """A cleaned EPUB plus the report that justified handing it over."""

    data: bytes
    report: Report
    blocked_rules: tuple[str, ...] = ()


def scan_and_clean(
    data: bytes,
    *,
    config: PurifierConfig | None = None,
    file_name: str = "download.epub",
) -> ScanOutcome:
    """Validate, scan and clean one downloaded book.

    Raises :class:`ScanRejected` for anything that must not reach a user: an
    empty or oversized body, a non-EPUB archive, DRM, an unsafe archive member,
    a nested archive, or a rebuild that cannot be verified.
    """

    config = config or PurifierConfig()
    if not data:
        raise ScanRejected("The source returned an empty file")
    if len(data) > config.limits.max_upload_bytes:
        raise ScanRejected(
            "The file is larger than the processing limit",
            detail=f"{len(data)} bytes > {config.limits.max_upload_bytes}",
        )

    try:
        inspection = inspect_epub(data, config, file_name=file_name)
    except DrmProtectedError as exc:
        raise ScanRejected(str(exc)) from exc
    except ValidationError as exc:
        raise ScanRejected(str(exc)) from exc

    blocked = tuple(
        sorted({finding.rule for finding in inspection.findings if finding.rule in BLOCKING_RULES})
    )
    if blocked:
        raise ScanRejected(
            "Refusing to hand over a book that contains active payloads",
            detail=", ".join(blocked),
            rules=blocked,
        )

    try:
        result = clean_epub(data, config, file_name=file_name)
    except PurifierError as exc:
        raise ScanRejected(str(exc)) from exc

    if result.data is None:  # pragma: no cover - clean_epub only returns None on dry runs
        raise ScanRejected("Cleaning produced no output")

    return ScanOutcome(data=result.data, report=result.report)


__all__ = ["BLOCKING_RULES", "ScanOutcome", "ScanRejected", "scan_and_clean"]
