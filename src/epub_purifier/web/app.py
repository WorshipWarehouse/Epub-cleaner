"""FastAPI application serving the EPub Purifier UI and JSON API.

Endpoints
---------
GET  /                     single-page UI
GET  /api/health           liveness probe
GET  /api/sources          source capabilities (download vs link-only)
POST /api/search           search the public-domain sources and link-only sources
POST /api/transfer         fetch, scan, clean and hold one cleaned copy
GET  /api/transfer/{id}/download   download the cleaned copy until it expires
GET  /api/transfers        temporary store: remaining time and storage use
DELETE /api/transfer/{id}  delete one temporary copy now
GET  /api/cover/{src}/{id} proxied cover image
POST /api/inspect          upload an EPUB, get a report
POST /api/clean            clean an inspected job (approved ids optional)
GET  /api/download/batch   download several cleaned EPUBs as one ZIP
GET  /api/download/{job}   download the cleaned EPUB
"""

from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
import time
import uuid
import zipfile
from collections import OrderedDict
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from threading import Lock
from typing import BinaryIO

from fastapi import FastAPI, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask

from ..config import PurifierConfig
from ..errors import DrmProtectedError, PurifierError, ValidationError
from ..pipeline import clean_epub, inspect_epub
from ..sources.base import BookListing, BookResult, BookSource, SearchQueryError, SourceError
from ..sources.registry import default_sources, search_books
from ..transfer import (
    TransferBusy,
    TransferError,
    TransferRejected,
    TransferService,
    TransferUnavailable,
)

JOB_TTL_SECONDS = int(os.environ.get("EPUB_PURIFIER_JOB_TTL", "1800"))
MAX_JOBS = int(os.environ.get("EPUB_PURIFIER_MAX_JOBS", "32"))
MAX_BATCH_DOWNLOADS = 10
MAX_CONCURRENT_BATCH_DOWNLOADS = 2
RATE_LIMIT = int(os.environ.get("EPUB_PURIFIER_RATE_LIMIT", "20"))
SEARCH_RATE_LIMIT = int(os.environ.get("EPUB_PURIFIER_SEARCH_RATE_LIMIT", "30"))
TRANSFER_RATE_LIMIT = int(os.environ.get("EPUB_PURIFIER_TRANSFER_RATE_LIMIT", "10"))

SEARCH_NOTICE = (
    "The built-in sources return public-domain or openly licensed editions: the EPUB "
    "is fetched to this server, scanned and cleaned before it is offered. When a "
    "matched book has no downloadable copy, the openly licensed sources are asked for "
    "the same title and author and that copy is offered instead. Sources this server "
    "added in its extra-sources file are listed with the capabilities they declared."
)

TRANSFER_NOTICE = (
    "Fetched books are held in private temporary storage only until they finish "
    "scanning and cleaning. Every cleaned copy and its metadata are deleted five "
    "hours after the server began downloading it, whether or not it was downloaded, "
    "and a cleanup pass runs every ten minutes. Delete now removes a copy immediately."
)

STATIC_DIR = Path(__file__).parent / "static"
TEMPLATE_DIR = Path(__file__).parent / "templates"
INDEX = TEMPLATE_DIR / "index.html"

SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
        "connect-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Opener-Policy": "same-origin",
    "X-Frame-Options": "DENY",
    "Permissions-Policy": "geolocation=(), microphone=(), camera=()",
}


@dataclass
class Job:
    job_id: str
    directory: Path
    source: Path
    display_name: str
    created_at: float
    report: dict | None = None
    output: Path | None = None
    approved: list[str] = field(default_factory=list)
    pinned: int = 0


class CleanRequest(BaseModel):
    job_id: str = Field(..., min_length=8, max_length=64)
    approved: list[str] = Field(default_factory=list, max_length=500)
    approve_all: bool = False
    remove_remote_resources: bool = False
    drop_promo_pages: bool = True


class SearchRequest(BaseModel):
    query: str = Field(..., min_length=2, max_length=200)
    sources: list[str] | None = Field(default=None, max_length=8)
    title: str | None = Field(default=None, max_length=200)
    author: str | None = Field(default=None, max_length=200)
    isbn: str | None = Field(default=None, max_length=32)
    language: str | None = Field(default=None, max_length=32)
    format: str | None = Field(default=None, max_length=32)


class TransferRequest(BaseModel):
    source: str = Field(..., min_length=2, max_length=40, pattern=r"^[a-z0-9_-]+$")
    book_id: str = Field(..., min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._-]+$")


class CoverCache:
    """Small in-memory LRU for proxied cover images (never written to disk)."""

    def __init__(self, size: int = 64, ttl: float = 3600.0) -> None:
        self.size = size
        self.ttl = ttl
        self._items: OrderedDict[str, tuple[float, tuple[bytes, str]]] = OrderedDict()
        self._lock = Lock()

    def get(self, key: str) -> tuple[bytes, str] | None:
        now = time.time()
        with self._lock:
            item = self._items.get(key)
            if item is None:
                return None
            stamp, value = item
            if now - stamp > self.ttl:
                self._items.pop(key, None)
                return None
            self._items.move_to_end(key)
            return value

    def put(self, key: str, value: tuple[bytes, str]) -> None:
        with self._lock:
            self._items[key] = (time.time(), value)
            self._items.move_to_end(key)
            while len(self._items) > self.size:
                self._items.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._items.clear()


def create_app(
    *,
    sources: Sequence[BookSource] | None = None,
    transfer: TransferService | None = None,
) -> FastAPI:
    app = FastAPI(
        title="EPUB Purifier",
        version="0.1.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    store = JobStore()
    source_list = list(sources) if sources is not None else default_sources()
    transfers = transfer or TransferService(sources=source_list)
    app.state.jobs = store
    app.state.batch_slots = asyncio.Semaphore(MAX_CONCURRENT_BATCH_DOWNLOADS)
    app.state.rate_limiter = RateLimiter()
    app.state.search_limiter = RateLimiter(limit=SEARCH_RATE_LIMIT)
    app.state.transfer_limiter = RateLimiter(limit=TRANSFER_RATE_LIMIT)
    app.state.sources = source_list
    app.state.transfer = transfers
    app.state.covers = CoverCache()

    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.middleware("http")
    async def harden(request: Request, call_next):
        response = await call_next(request)
        for header, value in SECURITY_HEADERS.items():
            response.headers.setdefault(header, value)
        if request.url.path.startswith("/api/"):
            response.headers.setdefault("Cache-Control", "no-store")
        return response

    @app.get("/", response_class=HTMLResponse)
    async def index() -> HTMLResponse:
        if not INDEX.exists():  # pragma: no cover - packaging failure
            return HTMLResponse("<h1>EPUB Purifier</h1><p>UI assets are missing.</p>", status_code=500)
        return HTMLResponse(INDEX.read_text(encoding="utf-8"))

    @app.get("/api/health")
    async def health() -> dict[str, object]:
        return {
            "status": "ok",
            "version": "0.1.0",
            "jobs": store.count(),
            "transfers": transfers.count(),
            "storage": transfers.storage_info(),
        }

    @app.get("/api/sources")
    async def sources_endpoint() -> JSONResponse:
        return JSONResponse({"sources": transfers.source_statuses(), "notice": SEARCH_NOTICE})

    @app.post("/api/search")
    async def search_endpoint(request: Request, payload: SearchRequest) -> JSONResponse:
        if not request.app.state.search_limiter.allow(_client_id(request)):
            raise HTTPException(status_code=429, detail="Too many searches; wait a moment.")
        selected = source_list
        if payload.sources:
            wanted = set(payload.sources)
            unknown = sorted(wanted - {source.name for source in source_list})
            if unknown:
                raise HTTPException(status_code=400, detail=f"Unknown source: {', '.join(unknown)}")
            selected = [source for source in source_list if source.name in wanted]
        try:
            response = await asyncio.to_thread(
                search_books,
                payload.query,
                selected,
                title=payload.title,
                author=payload.author,
                isbn=payload.isbn,
                language=payload.language,
                format=payload.format,
            )
        except SearchQueryError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except SourceError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        body = response.to_dict()
        body["results"] = [_public_result(result) for result in response.results]
        body["source_links"] = [_public_listing(listing) for listing in response.source_links]
        body["notice"] = SEARCH_NOTICE
        return JSONResponse(body)

    @app.post("/api/transfer")
    async def transfer_endpoint(request: Request, payload: TransferRequest) -> JSONResponse:
        if not request.app.state.transfer_limiter.allow(_client_id(request)):
            raise HTTPException(status_code=429, detail="Too many downloads; wait a moment.")
        try:
            job = await asyncio.to_thread(transfers.start, payload.source, payload.book_id)
        except TransferBusy as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except TransferUnavailable as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except TransferRejected as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except TransferError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        except PurifierError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        return JSONResponse(transfers.describe(job))

    @app.get("/api/transfer/{job_id}/download")
    async def transfer_download(job_id: str) -> FileResponse:
        job = transfers.get(job_id)
        if job is None or job.output is None or not job.output.exists():
            raise HTTPException(
                status_code=404, detail="No cleaned file is available for that transfer."
            )
        # The file is not consumed here: it stays available until the five-hour
        # expiry, so a failed download or a second device does not lose it. The
        # cleanup worker, the expiry check on access, and Delete now are what
        # remove it.
        return FileResponse(
            job.output,
            media_type="application/epub+zip",
            filename=job.display_name,
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/api/transfers")
    async def transfers_endpoint() -> JSONResponse:
        return JSONResponse(
            {
                "transfers": transfers.describe_all(),
                "storage": transfers.storage_info(),
                "notice": TRANSFER_NOTICE,
            }
        )

    @app.delete("/api/transfer/{job_id}")
    async def transfer_delete(job_id: str) -> JSONResponse:
        if not transfers.delete(job_id):
            raise HTTPException(
                status_code=404,
                detail="No temporary copy is stored for that transfer.",
            )
        return JSONResponse(
            {
                "deleted": True,
                "transfer_id": job_id,
                "storage": transfers.storage_info(),
            }
        )

    @app.get("/api/cover/{source_name}/{book_id}")
    async def cover_endpoint(source_name: str, book_id: str) -> Response:
        source = next((item for item in source_list if item.name == source_name), None)
        getter = getattr(source, "cover", None) if source is not None else None
        if getter is None:
            raise HTTPException(status_code=404, detail="No cover is available for that source.")
        key = f"{source_name}:{book_id}"
        cached = app.state.covers.get(key)
        if cached is None:
            try:
                cached = await asyncio.to_thread(getter, book_id)
            except SourceError as exc:
                raise HTTPException(status_code=404, detail="That cover could not be fetched.") from exc
            if cached is None:
                raise HTTPException(status_code=404, detail="That book has no cover image.")
            app.state.covers.put(key, cached)
        data, media_type = cached
        return Response(
            content=data,
            media_type=media_type,
            headers={"Cache-Control": "private, max-age=600"},
        )

    @app.post("/api/inspect")
    async def inspect_endpoint(request: Request, file: UploadFile) -> JSONResponse:
        client = _client_id(request)
        if not request.app.state.rate_limiter.allow(client):
            raise HTTPException(status_code=429, detail="Too many requests; wait a moment.")
        if store.count() >= MAX_JOBS:
            raise HTTPException(status_code=503, detail="Server is busy; try again shortly.")

        display_name = _safe_display_name(file.filename)
        job = store.create(display_name)
        data = await _read_upload(file, job.source, PurifierConfig().limits.max_upload_bytes)
        if not data:
            store.discard(job)
            raise HTTPException(status_code=400, detail="The uploaded file was empty.")

        try:
            report = await asyncio.to_thread(inspect_epub, job.source.read_bytes(), None, file_name=display_name)
        except DrmProtectedError as exc:
            store.discard(job)
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except ValidationError as exc:
            store.discard(job)
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except PurifierError as exc:
            store.discard(job)
            raise HTTPException(status_code=500, detail=str(exc)) from exc

        payload = report.to_dict()
        job.report = payload
        return JSONResponse(
            {
                "job_id": job.job_id,
                "file_name": display_name,
                "report": payload,
                "reviewable": [
                    finding for finding in payload["findings"] if finding["remediation"] == "review"
                ],
            }
        )

    @app.post("/api/clean")
    async def clean_endpoint(payload: CleanRequest) -> JSONResponse:
        """Clean an inspected upload and persist its downloadable EPUB."""

        job = store.get(payload.job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="That inspection expired; upload the file again.")
        if job.report is None:
            raise HTTPException(status_code=409, detail="Inspect the file before cleaning it.")

        config = PurifierConfig().with_overrides(
            remove_remote_resources=payload.remove_remote_resources,
            drop_promo_pages=payload.drop_promo_pages,
        )
        try:
            result = await asyncio.to_thread(
                clean_epub,
                job.source.read_bytes(),
                config,
                approved=set(payload.approved),
                approve_all=payload.approve_all,
                file_name=job.display_name,
            )
        except PurifierError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        if result.data is None:  # pragma: no cover - defensive
            raise HTTPException(status_code=500, detail="No output was produced.")
        store.save_output(job, result.data, payload.approved)

        report = result.report.to_dict()
        report["output"]["download_url"] = f"/api/download/{job.job_id}"
        return JSONResponse({"job_id": job.job_id, "report": report})

    @app.get("/api/download/batch")
    async def batch_download(job_ids: str) -> FileResponse:
        """Return every requested cleaned EPUB as one ZIP archive."""

        ids = list(dict.fromkeys(part.strip() for part in job_ids.split(",") if part.strip()))
        if not ids or len(ids) > MAX_BATCH_DOWNLOADS:
            raise HTTPException(
                status_code=400,
                detail=f"Choose between 1 and {MAX_BATCH_DOWNLOADS} cleaned files.",
            )
        await app.state.batch_slots.acquire()
        task = asyncio.create_task(asyncio.to_thread(store.batch_zip, ids))
        task.add_done_callback(lambda _: app.state.batch_slots.release())
        try:
            archive_path = await asyncio.shield(task)
        except asyncio.CancelledError:
            task.add_done_callback(_discard_batch_archive)
            raise
        if archive_path is None:
            raise HTTPException(
                status_code=404,
                detail="No cleaned file is available for every job in that batch.",
            )
        return FileResponse(
            archive_path,
            media_type="application/zip",
            filename="epub-purifier-cleaned.zip",
            headers={"Cache-Control": "no-store"},
            background=BackgroundTask(archive_path.unlink, missing_ok=True),
        )

    @app.get("/api/download/{job_id}")
    async def download(job_id: str) -> FileResponse:
        job = store.get(job_id)
        if job is None or job.output is None or not job.output.exists():
            raise HTTPException(status_code=404, detail="No cleaned file available for that job.")
        name = f"{Path(job.display_name).stem}.cleaned.epub"
        return FileResponse(
            job.output,
            media_type="application/epub+zip",
            filename=name,
            headers={"Cache-Control": "no-store"},
        )

    @app.on_event("startup")
    def _startup() -> None:
        # Rehydrated jobs have already been checked; sweep once so a restart
        # that happened after an expiry boundary removes those files at once,
        # then let the worker continue every ten minutes.
        transfers.sweep()
        transfers.start_cleanup()

    @app.on_event("shutdown")
    def _cleanup() -> None:
        store.close()
        # Temporary downloads are kept within their five-hour window so a
        # restart can still expire and delete them; nothing survives it longer.
        transfers.close()

    return app


# -- job storage ---------------------------------------------------------
class JobStore:
    """Bounded, TTL-based store of uploaded files on local disk."""

    def __init__(self, root: Path | None = None, ttl: int = JOB_TTL_SECONDS) -> None:
        self.root = Path(root or tempfile.mkdtemp(prefix="epub-purifier-"))
        self.ttl = ttl
        self._jobs: dict[str, Job] = {}
        self._lock = Lock()

    def create(self, display_name: str) -> Job:
        self._prune()
        job_id = uuid.uuid4().hex
        directory = self.root / job_id
        directory.mkdir(parents=True, exist_ok=True)
        job = Job(
            job_id=job_id,
            directory=directory,
            source=directory / "input.epub",
            display_name=display_name,
            created_at=time.time(),
        )
        with self._lock:
            self._jobs[job_id] = job
        return job

    def get(self, job_id: str) -> Job | None:
        """Return a live job after pruning expired temporary jobs."""

        self._prune()
        if not job_id.isalnum():
            return None
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return None
            if not job.pinned and time.time() - job.created_at > self.ttl:
                self._jobs.pop(job_id, None)
            else:
                return job
        shutil.rmtree(job.directory, ignore_errors=True)
        return None

    def save_output(self, job: Job, data: bytes, approved: list[str]) -> Path:
        """Write a cleaned EPUB atomically and publish it for download."""

        output = job.directory / "cleaned.epub"
        temporary = job.directory / f"cleaned-{uuid.uuid4().hex}.epub"
        try:
            temporary.write_bytes(data)
            with self._lock:
                os.replace(temporary, output)
                job.output = output
                job.approved = list(approved)
        finally:
            temporary.unlink(missing_ok=True)
        return output

    def batch_zip(self, job_ids: list[str]) -> Path | None:
        """Open cleaned outputs under the lock, then ZIP them outside it."""

        handles: list[tuple[Job, BinaryIO]] = []
        try:
            with self._lock:
                now = time.time()
                for job_id in job_ids:
                    job = self._jobs.get(job_id)
                    if job is None or now - job.created_at > self.ttl:
                        return None
                    if job.output is None or not job.output.exists():
                        return None
                    handle = job.output.open("rb")
                    job.pinned += 1
                    handles.append((job, handle))
            return _zip_cleaned_jobs(handles, self.root)
        finally:
            for _, handle in handles:
                handle.close()
            with self._lock:
                for job, _ in handles:
                    job.pinned -= 1

    def count(self) -> int:
        with self._lock:
            return len(self._jobs)

    def discard(self, job: Job) -> None:
        with self._lock:
            self._jobs.pop(job.job_id, None)
        shutil.rmtree(job.directory, ignore_errors=True)

    def _prune(self) -> None:
        """Remove expired, unpinned jobs from the temporary store."""

        now = time.time()
        with self._lock:
            expired = [
                job
                for job in self._jobs.values()
                if not job.pinned and now - job.created_at > self.ttl
            ]
            for job in expired:
                self._jobs.pop(job.job_id, None)
        for job in expired:
            shutil.rmtree(job.directory, ignore_errors=True)

    def close(self) -> None:
        with self._lock:
            jobs = list(self._jobs.values())
            self._jobs.clear()
        for job in jobs:
            shutil.rmtree(job.directory, ignore_errors=True)
        shutil.rmtree(self.root, ignore_errors=True)


class RateLimiter:
    """Tiny in-memory sliding window; enough for a single-user local server."""

    def __init__(self, limit: int = RATE_LIMIT, window: float = 60.0) -> None:
        self.limit = limit
        self.window = window
        self._hits: dict[str, list[float]] = {}
        self._lock = Lock()

    def allow(self, key: str) -> bool:
        now = time.time()
        with self._lock:
            hits = [hit for hit in self._hits.get(key, []) if now - hit < self.window]
            if len(hits) >= self.limit:
                self._hits[key] = hits
                return False
            hits.append(now)
            self._hits[key] = hits
            if len(self._hits) > 1000:  # bound memory on a shared host
                self._hits = {key: hits}
        return True


# -- helpers -------------------------------------------------------------
def _client_id(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()[:64]
    return (request.client.host if request.client else "unknown")[:64]


def _safe_display_name(name: str | None) -> str:
    candidate = Path(name or "upload.epub").name
    candidate = "".join(ch for ch in candidate if ch.isprintable() and ch not in {"/", "\\"})
    candidate = candidate.strip() or "upload.epub"
    return candidate[:120]


def _discard_batch_archive(task: asyncio.Task[Path | None]) -> None:
    """Delete a finished batch archive after its request was cancelled."""

    if task.cancelled():
        return
    try:
        archive_path = task.result()
    except Exception:
        return
    if archive_path is not None:
        archive_path.unlink(missing_ok=True)


def _zip_cleaned_jobs(entries: list[tuple[Job, BinaryIO]], directory: Path) -> Path:
    """Create one temporary ZIP from open cleaned-output streams."""

    with tempfile.NamedTemporaryFile(
        prefix="epub-purifier-", suffix=".zip", dir=directory, delete=False
    ) as handle:
        path = Path(handle.name)
    try:
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            used: set[str] = set()
            for job, source in entries:
                stem = Path(job.display_name).stem or "cleaned"
                name = f"{stem}.cleaned.epub"
                suffix = 2
                while name.casefold() in used:
                    name = f"{stem} ({suffix}).cleaned.epub"
                    suffix += 1
                used.add(name.casefold())
                with archive.open(name, "w") as target:
                    shutil.copyfileobj(source, target)
    except Exception:
        path.unlink(missing_ok=True)
        raise
    return path


def _public_result(result: BookResult) -> dict[str, object]:
    """Serialise a grouped book, preserving per-source actions and metadata."""

    payload = result.to_dict()
    primary = result.primary
    if primary.cover_url and primary.book_id and not primary.is_search_link:
        payload["cover_url"] = f"/api/cover/{primary.source}/{primary.book_id}"
    payload["listings"] = [_public_listing(listing) for listing in result.listings]
    payload["security"] = _listing_security(primary)
    return payload


def _public_listing(listing: BookListing) -> dict[str, object]:
    """Serialise a listing, routing remote covers through our own proxy."""

    payload = listing.to_dict()
    if listing.cover_url and listing.book_id and not listing.is_search_link:
        payload["cover_url"] = f"/api/cover/{listing.source}/{listing.book_id}"
    payload["security"] = _listing_security(listing)
    return payload


def _listing_security(listing: BookListing) -> dict[str, object]:
    """State up front what a listing's security status actually is."""

    if listing.downloadable:
        return {
            "status": "scan_on_fetch",
            "headline": (
                "Fetched to this server, scanned and cleaned before the download "
                "is offered."
            ),
            "disclaimer": (
                "A clean scan is not a guarantee that a file is malware-free."
            ),
        }
    return {
        "status": "link_only",
        "headline": "Search link only: no file is fetched or scanned here.",
        "disclaimer": "",
    }


async def _read_upload(upload: UploadFile, destination: Path, limit: int) -> int:
    total = 0
    with destination.open("wb") as handle:
        while True:
            chunk = await upload.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > limit:
                handle.close()
                destination.unlink(missing_ok=True)
                raise HTTPException(status_code=413, detail="That file is larger than the upload limit.")
            handle.write(chunk)
    await upload.close()
    return total


def main() -> None:  # pragma: no cover - server bootstrap
    import uvicorn

    host = os.environ.get("EPUB_PURIFIER_HOST", "127.0.0.1")
    port = int(os.environ.get("EPUB_PURIFIER_PORT", "8000"))
    uvicorn.run(create_app(), host=host, port=port, log_level="info")


if __name__ == "__main__":  # pragma: no cover
    main()
