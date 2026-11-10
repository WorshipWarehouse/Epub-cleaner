"""Isolated download → scan → clean → five-hour temporary storage.

Every transfer gets its own private directory. The raw download is deleted as
soon as the cleaned copy exists, and the cleaned copy plus its metadata are
deleted five hours after the server *started* downloading the book — whether or
not the user ever fetched it. A background sweep runs every ten minutes, expired
jobs are also checked on every access, and metadata is written to disk so an
expired or surviving job is handled correctly after a restart.

The directory layout is deliberately boring:

    <root>/<32 hex>/job.json       private job metadata (no book content)
    <root>/<32 hex>/report.json    the cleaning report
    <root>/<32 hex>/download.epub  raw bytes, only until cleaning succeeds
    <root>/<32 hex>/cleaned.epub   the only file that is ever served

All of it lives under a 0700 root with 0600 files. Nothing is written outside
that root and only a cleaned, structurally verified archive is served.
"""

from __future__ import annotations

import contextlib
import json
import math
import os
import re
import shutil
import tempfile
import threading
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .config import PurifierConfig
from .errors import PurifierError
from .report import DISCLAIMER, SAFETY_NOTICE
from .scanning import BLOCKING_RULES, ScanRejected, scan_and_clean
from .sources.base import BookSource, DownloadRefused, DownloadResult, SourceError
from .sources.registry import default_sources

#: Five hours from the start of the server-side download, whatever happens next.
TRANSFER_TTL_SECONDS = int(
    os.environ.get("EPUB_PURIFIER_TRANSFER_TTL", str(5 * 60 * 60))
)
#: The background cleanup pass. Expiry is also enforced on every job access.
CLEANUP_INTERVAL_SECONDS = int(os.environ.get("EPUB_PURIFIER_CLEANUP_INTERVAL", "600"))
MAX_TRANSFERS = int(os.environ.get("EPUB_PURIFIER_MAX_TRANSFERS", "16"))
MAX_DOWNLOAD_BYTES = int(os.environ.get("EPUB_PURIFIER_MAX_DOWNLOAD_MB", "64")) * 1024 * 1024
MAX_STORAGE_BYTES = int(os.environ.get("EPUB_PURIFIER_MAX_STORAGE_MB", "1024")) * 1024 * 1024

META_NAME = "job.json"
REPORT_NAME = "report.json"
RAW_NAME = "download.epub"
OUTPUT_NAME = "cleaned.epub"
META_SCHEMA = 1

#: A uuid4 hex job id. Directory names are generated, never taken from input.
_JOB_ID = re.compile(r"^[0-9a-f]{32}$")
_MAX_REPORT_BYTES = 8 * 1024 * 1024
_MAX_META_BYTES = 64 * 1024


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
    expires_at: float
    output: Path | None = None
    report: dict | None = None
    origin_url: str | None = None
    status: str = "downloading"
    input_bytes: int = 0
    output_bytes: int = 0


class CleanupWorker:
    """A daemon thread that sweeps expired transfers on a fixed interval.

    It only calls :meth:`TransferService.sweep`, which is idempotent and takes
    the service lock, so a sweep running at the same moment as a request is
    harmless. Tests drive :meth:`run_once` directly instead of waiting on wall
    clock time.
    """

    def __init__(
        self,
        sweep: Callable[[], object],
        interval: int = CLEANUP_INTERVAL_SECONDS,
    ) -> None:
        self._sweep = sweep
        self.interval = max(1, int(interval))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self.runs = 0
        self.last_error: str | None = None

    @property
    def running(self) -> bool:
        thread = self._thread
        return bool(thread and thread.is_alive())

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._loop,
                name="epub-purifier-cleanup",
                daemon=True,
            )
            self._thread.start()

    def _loop(self) -> None:
        while not self._stop.wait(self.interval):
            self.run_once()

    def run_once(self) -> None:
        try:
            self._sweep()
            self.runs += 1
            self.last_error = None
        except Exception as exc:  # pragma: no cover - defensive background guard
            self.last_error = str(exc)[:200]

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            thread = self._thread
            self._thread = None
        if thread is not None:
            thread.join(timeout=5)


class TransferService:
    """Owns temporary storage for fetched-and-cleaned books."""

    def __init__(
        self,
        *,
        root: Path | None = None,
        ttl: int = TRANSFER_TTL_SECONDS,
        max_transfers: int = MAX_TRANSFERS,
        max_bytes: int = MAX_DOWNLOAD_BYTES,
        max_storage_bytes: int = MAX_STORAGE_BYTES,
        sources: Sequence[BookSource] | None = None,
        cleanup_interval: int = CLEANUP_INTERVAL_SECONDS,
        now: Callable[[], float] = time.time,
    ) -> None:
        configured = os.environ.get("EPUB_PURIFIER_TRANSFER_DIR")
        if root is not None:
            self.root = Path(root)
        elif configured:
            self.root = Path(configured)
        else:
            # A stable path under the system temp directory, so a restarted
            # process finds and expires the same jobs instead of orphaning
            # them. Set EPUB_PURIFIER_TRANSFER_DIR (the Docker image does) for
            # a location that also survives a reboot or a replaced container.
            self.root = Path(tempfile.gettempdir()) / "epub-purifier-transfers"
        if self.root.is_symlink():
            raise TransferError("The temporary transfer directory must not be a symlink")
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        with contextlib.suppress(OSError):  # pragma: no cover - unusual filesystem
            os.chmod(self.root, 0o700)
        self.ttl = max(1, int(ttl))
        self.max_transfers = max(1, int(max_transfers))
        self.max_bytes = max(1, int(max_bytes))
        self.max_storage_bytes = max(1, int(max_storage_bytes))
        self.cleanup_interval = max(1, int(cleanup_interval))
        self._now = now
        self._sources = {source.name: source for source in (sources or default_sources())}
        self._jobs: dict[str, TransferJob] = {}
        self._lock = threading.RLock()
        self._worker = CleanupWorker(self.sweep, self.cleanup_interval)
        self._load_existing()

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

    def storage_usage(self) -> int:
        """Bytes used by this service's own job files, never following symlinks."""

        total = 0
        with self._lock:
            jobs = list(self._jobs.values())
        for job in jobs:
            for name in (META_NAME, REPORT_NAME, RAW_NAME, OUTPUT_NAME):
                path = job.directory / name
                try:
                    if path.is_symlink() or not path.is_file():
                        continue
                    total += path.stat().st_size
                except OSError:  # pragma: no cover - raced with a sweep
                    continue
        return total

    def storage_info(self) -> dict[str, int]:
        used = self.storage_usage()
        return {
            "used_bytes": used,
            "max_bytes": self.max_storage_bytes,
            "remaining_bytes": max(0, self.max_storage_bytes - used),
            "jobs": self.count(),
            "ttl_seconds": self.ttl,
            "cleanup_interval_seconds": self.cleanup_interval,
        }

    def describe_all(self) -> list[dict[str, object]]:
        self.sweep()
        with self._lock:
            jobs = sorted(self._jobs.values(), key=lambda job: job.created_at)
        return [self.describe(job) for job in jobs]

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
        book_id = _clean_book_id(book_id)

        self.sweep()
        existing = self._find(source_name, book_id)
        if existing is not None:
            if existing.status == "cleaned" and existing.output is not None:
                return existing
            raise TransferBusy("That book is already being prepared; try again shortly.")

        if self.count() >= self.max_transfers:
            raise TransferBusy("Too many transfers in progress; try again shortly.")
        if self.storage_usage() >= self.max_storage_bytes:
            raise TransferBusy("Temporary storage is full; try again after a cleanup pass.")

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
        except ScanRejected as exc:
            self.discard(job)
            raise TransferRejected(exc.message, detail=exc.detail) from exc
        except SourceError as exc:
            self.discard(job)
            raise TransferUnavailable(str(exc)) from exc
        except BaseException:
            # Oversized downloads, storage limits, rebuild failures and
            # interrupts all leave nothing behind.
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
        if self.storage_usage() + len(downloaded.data) > self.max_storage_bytes:
            raise TransferRejected(
                "Temporary storage is full",
                detail=f"limit {self.max_storage_bytes} bytes",
            )

        raw = job.directory / RAW_NAME
        _write_private(raw, downloaded.data)
        job.input_bytes = len(downloaded.data)
        job.display_name = downloaded.file_name
        job.origin_url = downloaded.origin_url
        self._write_meta(job)

        outcome = scan_and_clean(
            downloaded.data,
            config=PurifierConfig(),
            file_name=downloaded.file_name,
        )
        if self.storage_usage() + len(outcome.data) > self.max_storage_bytes:
            raise TransferRejected(
                "Temporary storage is full",
                detail=f"limit {self.max_storage_bytes} bytes",
            )

        output = job.directory / OUTPUT_NAME
        _write_private(output, outcome.data)
        raw.unlink(missing_ok=True)

        job.output = output
        job.report = outcome.report.to_dict()
        job.output_bytes = len(outcome.data)
        job.status = "cleaned"
        job.display_name = f"{Path(downloaded.file_name).stem}.cleaned.epub"
        self._write_report(job)
        self._write_meta(job)

    def get(self, job_id: str) -> TransferJob | None:
        self.sweep()
        if not _JOB_ID.match(str(job_id or "")):
            return None
        with self._lock:
            job = self._jobs.get(job_id)
        if job is None:
            return None
        if self._now() >= job.expires_at:
            self.discard(job)
            return None
        return job

    def describe(self, job: TransferJob) -> dict[str, object]:
        report = job.report or {}
        summary = report.get("summary", {}) if isinstance(report, dict) else {}
        counts = summary.get("counts", {}) if isinstance(summary, dict) else {}
        now = self._now()
        # Round up so a job created this instant reports its full window rather
        # than 17999 seconds because a fraction of a second already elapsed.
        remaining = max(0, int(math.ceil(job.expires_at - now)))
        return {
            "transfer_id": job.job_id,
            "source": job.source,
            "book_id": job.book_id,
            "file_name": job.display_name,
            "status": job.status,
            "size_bytes": int(job.output_bytes or job.input_bytes),
            "origin_url": job.origin_url,
            "report": report,
            "download_url": f"/api/transfer/{job.job_id}/download",
            "delete_url": f"/api/transfer/{job.job_id}",
            "created_at": _iso(job.created_at),
            "expires_at": _iso(job.expires_at),
            "expires_at_epoch": job.expires_at,
            "remaining_seconds": remaining,
            "expires_in": _human_seconds(remaining),
            "ttl_seconds": self.ttl,
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

    def delete(self, job_id: str) -> bool:
        """Delete one temporary transfer now. Returns False if it is already gone."""

        if not _JOB_ID.match(str(job_id or "")):
            return False
        self.sweep()
        with self._lock:
            job = self._jobs.get(job_id)
        if job is None:
            return False
        self.discard(job)
        return True

    def discard(self, job: TransferJob) -> None:
        with self._lock:
            self._jobs.pop(job.job_id, None)
        shutil.rmtree(job.directory, ignore_errors=True)

    def sweep(self) -> dict[str, int]:
        """Delete expired jobs and orphaned directories left by a crash."""

        now = self._now()
        with self._lock:
            expired = [job for job in self._jobs.values() if now >= job.expires_at]
        for job in expired:
            self.discard(job)
        orphans = self._sweep_orphans(now)
        return {
            "expired": len(expired),
            "orphans": orphans,
            "remaining": self.count(),
        }

    def start_cleanup(self) -> None:
        self._worker.start()

    def stop_cleanup(self) -> None:
        self._worker.stop()

    def close(self) -> None:
        """Stop the background worker and keep files for a possible restart.

        Files and metadata are not deleted here: they are already bounded by
        ``expires_at``, and keeping them is what lets a restarted server clean
        up the same jobs it was holding before. ``EPUB_PURIFIER_TRANSFER_DIR``
        must point at persistent storage for that to span a container restart.
        """

        self.stop_cleanup()

    def clear(self) -> None:
        """Remove every job and the root directory (tests and explicit teardown)."""

        self.stop_cleanup()
        with self._lock:
            jobs = list(self._jobs.values())
            self._jobs.clear()
        for job in jobs:
            shutil.rmtree(job.directory, ignore_errors=True)
        shutil.rmtree(self.root, ignore_errors=True)

    # -- internals -------------------------------------------------------
    def _find(self, source_name: str, book_id: str) -> TransferJob | None:
        with self._lock:
            for job in self._jobs.values():
                if job.source == source_name and job.book_id == book_id:
                    return job
        return None

    def _create(self, source_name: str, book_id: str) -> TransferJob:
        job_id = uuid.uuid4().hex
        directory = self.root / job_id
        directory.mkdir(parents=True, exist_ok=False, mode=0o700)
        with contextlib.suppress(OSError):  # pragma: no cover - unusual filesystem
            os.chmod(directory, 0o700)
        created = self._now()
        job = TransferJob(
            job_id=job_id,
            directory=directory,
            source=source_name,
            book_id=book_id,
            display_name="download.epub",
            created_at=created,
            expires_at=created + self.ttl,
        )
        with self._lock:
            self._jobs[job_id] = job
        self._write_meta(job)
        return job

    def _sweep_orphans(self, now: float) -> int:
        removed = 0
        try:
            entries = list(self.root.iterdir())
        except OSError:  # pragma: no cover - root disappeared
            return 0
        with self._lock:
            known = set(self._jobs)
        for entry in entries:
            if entry.name in known:
                continue
            try:
                if entry.is_symlink() or not entry.is_dir():
                    continue
                if not _JOB_ID.match(entry.name):
                    continue
                aged = now - entry.stat().st_mtime > self.ttl
                if aged:
                    shutil.rmtree(entry, ignore_errors=True)
                    removed += 1
            except OSError:  # pragma: no cover - raced with another sweep
                continue
        return removed

    def _write_meta(self, job: TransferJob) -> None:
        payload = {
            "schema": META_SCHEMA,
            "job_id": job.job_id,
            "source": job.source,
            "book_id": job.book_id,
            "display_name": job.display_name,
            "created_at": job.created_at,
            "expires_at": job.expires_at,
            "origin_url": job.origin_url,
            "status": job.status,
            "input_bytes": job.input_bytes,
            "output_bytes": job.output_bytes,
        }
        _write_private(
            job.directory / META_NAME,
            json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        )

    def _write_report(self, job: TransferJob) -> None:
        if job.report is None:
            return
        try:
            encoded = json.dumps(job.report, ensure_ascii=False).encode("utf-8")
        except (TypeError, ValueError):  # pragma: no cover - report is JSON-ready
            return
        if len(encoded) > _MAX_REPORT_BYTES:  # pragma: no cover - defensive cap
            return
        _write_private(job.directory / REPORT_NAME, encoded)

    def _load_existing(self) -> None:
        """Rehydrate jobs written by a previous process, deleting what expired.

        Only directories named exactly like one of our job ids are considered,
        symlinks are never followed, and a job without a valid metadata file, a
        completed status and its cleaned output is removed rather than guessed
        at.
        """

        now = self._now()
        try:
            entries = list(self.root.iterdir())
        except OSError:  # pragma: no cover - root was removed underneath us
            return
        for entry in entries:
            try:
                if entry.is_symlink() or not entry.is_dir():
                    continue
                if not _JOB_ID.match(entry.name):
                    continue
                job = _load_job(entry)
                if job is None or now >= job.expires_at or job.output is None:
                    shutil.rmtree(entry, ignore_errors=True)
                    continue
                with self._lock:
                    self._jobs[job.job_id] = job
            except OSError:  # pragma: no cover - defensive
                shutil.rmtree(entry, ignore_errors=True)


# -- helpers -------------------------------------------------------------
def _clean_book_id(book_id: str) -> str:
    candidate = str(book_id or "").strip()
    if not candidate or len(candidate) > 128:
        raise TransferUnavailable("That book identifier is not usable")
    return candidate[:64]


def _load_job(directory: Path) -> TransferJob | None:
    meta = _read_json(directory / META_NAME, limit=_MAX_META_BYTES)
    if not isinstance(meta, dict) or meta.get("schema") != META_SCHEMA:
        return None
    if meta.get("job_id") != directory.name:
        return None
    if meta.get("status") != "cleaned":
        # An interrupted download is abandoned, not resumed: its bytes were
        # never scanned and must not be served.
        return None

    source = meta.get("source")
    book_id = meta.get("book_id")
    display_name = meta.get("display_name")
    created_at = meta.get("created_at")
    expires_at = meta.get("expires_at")
    if not isinstance(source, str) or not source or len(source) > 40:
        return None
    if not isinstance(book_id, str) or len(book_id) > 64:
        return None
    if not isinstance(display_name, str) or not display_name or len(display_name) > 255:
        return None
    if not isinstance(created_at, (int, float)) or not isinstance(expires_at, (int, float)):
        return None
    if not math.isfinite(created_at) or not math.isfinite(expires_at):
        return None
    if expires_at <= created_at or expires_at - created_at > 30 * 24 * 3600:
        return None

    output = directory / OUTPUT_NAME
    if output.is_symlink() or not output.is_file():
        return None
    try:
        output_size = output.stat().st_size
    except OSError:
        return None

    report = _read_json(directory / REPORT_NAME, limit=_MAX_REPORT_BYTES)
    return TransferJob(
        job_id=directory.name,
        directory=directory,
        source=source,
        book_id=book_id,
        display_name=display_name,
        created_at=float(created_at),
        expires_at=float(expires_at),
        output=output,
        report=report if isinstance(report, dict) else None,
        origin_url=meta.get("origin_url") if isinstance(meta.get("origin_url"), str) else None,
        status="cleaned",
        input_bytes=_non_negative(meta.get("input_bytes")),
        output_bytes=output_size,
    )


def _non_negative(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return max(0, int(value))


def _read_json(path: Path, *, limit: int) -> object | None:
    try:
        if path.is_symlink() or not path.is_file():
            return None
        if path.stat().st_size > limit:
            return None
        with path.open("rb") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


def _write_private(path: Path, data: bytes) -> None:
    """Write 0600 bytes atomically, so a crash never leaves a partial file."""

    descriptor, temporary = tempfile.mkstemp(prefix=".tmp-", dir=str(path.parent))
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        with contextlib.suppress(OSError):  # pragma: no cover - nothing left to clean
            os.unlink(temporary)
        raise


def _iso(timestamp: float) -> str:
    return (
        datetime.fromtimestamp(timestamp, tz=timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def _human_seconds(seconds: int) -> str:
    seconds = max(0, int(seconds))
    hours, rest = divmod(seconds, 3600)
    minutes, remainder = divmod(rest, 60)
    if hours:
        return f"{hours}h {minutes:02d}m"
    if minutes:
        return f"{minutes}m {remainder:02d}s"
    return f"{remainder}s"


__all__ = [
    "BLOCKING_RULES",
    "CLEANUP_INTERVAL_SECONDS",
    "CleanupWorker",
    "MAX_DOWNLOAD_BYTES",
    "MAX_STORAGE_BYTES",
    "TRANSFER_TTL_SECONDS",
    "TransferBusy",
    "TransferError",
    "TransferJob",
    "TransferRejected",
    "TransferService",
    "TransferUnavailable",
]
