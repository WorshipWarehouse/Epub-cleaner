"""Isolated download → scan → clean → one-click download.

Nothing here keeps a book: every transfer gets its own temporary directory, the
raw download is deleted as soon as the cleaned copy exists, the cleaned copy is
deleted when it is served (or when the job expires), and the whole root is
removed on shutdown. Only a cleaned, rebuilt archive is ever offered for
download; a file that fails validation or our blocking rules never produces one.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from threading import Lock

from .config import PurifierConfig
from .errors import DrmProtectedError, PurifierError, ValidationError
from .pipeline import clean_epub, inspect_epub
from .report import DISCLAIMER, SAFETY_NOTICE
from .sources.base import BookSource, DownloadRefused, DownloadResult, SourceError
from .sources.registry import default_sources

TRANSFER_TTL_SECONDS = int(os.environ.get("EPUB_PURIFIER_TRANSFER_TTL", "900"))
MAX_TRANSFERS = int(os.environ.get("EPUB_PURIFIER_MAX_TRANSFERS", "16"))
MAX_DOWNLOAD_BYTES = int(os.environ.get("EPUB_PURIFIER_MAX_DOWNLOAD_MB", "64")) * 1024 * 1024

#: Archive-level findings that mean "do not hand this file out at all".
BLOCKING_RULES = frozenset({"archive.executable", "archive.unsafe_member", "archive.encrypted"})


class TransferError(PurifierError):
    """Base class for transfer failures, with an HTTP status for the web layer."""

    status_code = 502


class TransferUnavailable(TransferError):
    """This source cannot be downloaded from, or the server is full."""

    status_code = 403


class TransferBusy(TransferUnavailable):
    status_code = 503


class TransferRejected(TransferError):
    """The fetched file is unsafe or unusable, so no cleaned copy is offered."""

    status_code = 422


@dataclass
class TransferJob:
    job_id: str
    directory: Path
    source: str
    book_id: str
    display_name: str
    created_at: float
    output: Path | None = None
    report: dict | None = None
    origin_url: str | None = None


class TransferService:
    """Owns temporary storage for fetched-and-cleaned books."""

    def __init__(
        self,
        *,
        root: Path | None = None,
        ttl: int = TRANSFER_TTL_SECONDS,
        max_transfers: int = MAX_TRANSFERS,
        max_bytes: int = MAX_DOWNLOAD_BYTES,
        sources: Sequence[BookSource] | None = None,
    ) -> None:
        self.root = Path(root or tempfile.mkdtemp(prefix="epub-purifier-transfers-"))
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.ttl = ttl
        self.max_transfers = max_transfers
        self.max_bytes = max_bytes
        self._sources = {source.name: source for source in (sources or default_sources())}
        self._jobs: dict[str, TransferJob] = {}
        self._lock = Lock()

    # -- introspection ---------------------------------------------------
    def source_statuses(self) -> list[dict[str, object]]:
        return [
            {
                "name": source.name,
                "label": source.label,
                "supports_download": source.supports_download,
                "note": source.note,
            }
            for source in self._sources.values()
        ]

    def source(self, name: str) -> BookSource | None:
        return self._sources.get(name)

    def count(self) -> int:
        with self._lock:
            return len(self._jobs)

    # -- lifecycle -------------------------------------------------------
    def start(self, source_name: str, book_id: str) -> TransferJob:
        source = self._sources.get(source_name)
        if source is None:
            raise TransferUnavailable("Unknown source", detail=source_name)
        if not source.supports_download:
            raise TransferUnavailable(
                f"{source.label} does not allow automated downloads",
                detail="Open the source's search page instead.",
            )
        self._prune()
        if self.count() >= self.max_transfers:
            raise TransferBusy("Too many transfers in progress; try again shortly.")

        job = self._create(source_name, book_id)
        try:
            downloaded = source.download(book_id)
            self._ingest(job, downloaded)
        except DownloadRefused as exc:
            # The source answered but will not provide this book (no EPUB
            # edition, or no authorisation): a validation refusal, not a
            # forbidden request.
            self.discard(job)
            raise TransferRejected(str(exc)) from exc
        except SourceError as exc:
            self.discard(job)
            raise TransferUnavailable(str(exc)) from exc
        except Exception:
            self.discard(job)
            raise
        return job

    def _ingest(self, job: TransferJob, downloaded: DownloadResult) -> None:
        if len(downloaded.data) > self.max_bytes:
            raise TransferRejected(
                "The file is larger than the download limit",
                detail=f"{len(downloaded.data)} bytes > {self.max_bytes}",
            )
        if not downloaded.data:
            raise TransferRejected("The source returned an empty file")

        raw = job.directory / "download.epub"
        _write_private(raw, downloaded.data)
        job.display_name = downloaded.file_name
        job.origin_url = downloaded.origin_url

        try:
            report = inspect_epub(raw, None, file_name=downloaded.file_name)
        except DrmProtectedError as exc:
            raise TransferRejected(str(exc)) from exc
        except ValidationError as exc:
            raise TransferRejected(str(exc)) from exc

        blocked = sorted(
            {finding.rule for finding in report.findings if finding.rule in BLOCKING_RULES}
        )
        if blocked:
            raise TransferRejected(
                "Refusing to hand over a book that contains active payloads",
                detail=", ".join(blocked),
            )

        result = clean_epub(raw, PurifierConfig(), file_name=downloaded.file_name)
        if result.data is None:  # pragma: no cover - defensive
            raise TransferRejected("Cleaning produced no output")

        output = job.directory / "cleaned.epub"
        _write_private(output, result.data)
        raw.unlink(missing_ok=True)

        job.output = output
        job.report = result.report.to_dict()
        job.display_name = f"{Path(downloaded.file_name).stem}.cleaned.epub"

    def get(self, job_id: str) -> TransferJob | None:
        self._prune()
        if not job_id or not job_id.isalnum():
            return None
        with self._lock:
            job = self._jobs.get(job_id)
        if job is not None and time.time() - job.created_at > self.ttl:
            self.discard(job)
            return None
        return job

    def describe(self, job: TransferJob) -> dict[str, object]:
        report = job.report or {}
        summary = report.get("summary", {}) if isinstance(report, dict) else {}
        counts = summary.get("counts", {}) if isinstance(summary, dict) else {}
        return {
            "transfer_id": job.job_id,
            "source": job.source,
            "book_id": job.book_id,
            "file_name": job.display_name,
            "report": report,
            "download_url": f"/api/transfer/{job.job_id}/download",
            "security": {
                "status": "cleaned_and_verified",
                "headline": (
                    "The archive was scanned, cleaned and structurally verified "
                    "before this download was offered."
                ),
                "counts": counts,
                "needs_review": int((report.get("stats") or {}).get("needs_review", 0)),
                "disclaimer": DISCLAIMER,
                "safety_notice": SAFETY_NOTICE,
            },
        }

    def discard(self, job: TransferJob) -> None:
        with self._lock:
            self._jobs.pop(job.job_id, None)
        shutil.rmtree(job.directory, ignore_errors=True)

    def close(self) -> None:
        with self._lock:
            jobs = list(self._jobs.values())
            self._jobs.clear()
        for job in jobs:
            shutil.rmtree(job.directory, ignore_errors=True)
        shutil.rmtree(self.root, ignore_errors=True)

    # -- internals -------------------------------------------------------
    def _create(self, source_name: str, book_id: str) -> TransferJob:
        job_id = uuid.uuid4().hex
        directory = self.root / job_id
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        job = TransferJob(
            job_id=job_id,
            directory=directory,
            source=source_name,
            book_id=str(book_id)[:64],
            display_name="download.epub",
            created_at=time.time(),
        )
        with self._lock:
            self._jobs[job_id] = job
        return job

    def _prune(self) -> None:
        now = time.time()
        with self._lock:
            expired = [job for job in self._jobs.values() if now - job.created_at > self.ttl]
        for job in expired:
            self.discard(job)


def _write_private(path: Path, data: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(data)


__all__ = [
    "BLOCKING_RULES",
    "MAX_DOWNLOAD_BYTES",
    "TransferBusy",
    "TransferError",
    "TransferJob",
    "TransferRejected",
    "TransferService",
    "TransferUnavailable",
]
