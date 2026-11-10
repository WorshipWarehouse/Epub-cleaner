"""Transfer service: isolated storage, scanning, expiry, restart and deletion."""

from __future__ import annotations

import os

import pytest

from epub_purifier.sources.base import DownloadRefused, DownloadResult, SourceError
from epub_purifier.transfer import (
    BLOCKING_RULES,
    CLEANUP_INTERVAL_SECONDS,
    TRANSFER_TTL_SECONDS,
    CleanupWorker,
    TransferBusy,
    TransferRejected,
    TransferService,
    TransferUnavailable,
)


class FakeSource:
    name = "fake"
    label = "Fake Source"
    supports_download = True
    note = "test source"

    def __init__(self, data: bytes = b"", error: Exception | None = None) -> None:
        self.data = data
        self.error = error
        self.downloads = 0

    def search(self, query):
        return []

    def download(self, book_id):
        self.downloads += 1
        if self.error is not None:
            raise self.error
        return DownloadResult(
            data=self.data,
            file_name="fake-book.epub",
            origin_url="https://example.invalid/fake-book.epub",
        )


class LinkOnly(FakeSource):
    name = "linky"
    label = "Linky Source"
    supports_download = False


class Clock:
    def __init__(self, value: float = 1_000_000.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def make_service(tmp_path, data=b"", **kwargs):
    root = tmp_path / "transfers"
    source = FakeSource(data=data)
    service = TransferService(root=root, sources=[source], **kwargs)
    return service, source


def test_default_ttl_is_five_hours():
    assert TRANSFER_TTL_SECONDS == 5 * 60 * 60
    assert CLEANUP_INTERVAL_SECONDS == 10 * 60


def test_missing_epub_is_a_rejection_not_a_forbidden_source(tmp_path):
    source = FakeSource(
        error=DownloadRefused("Project Gutenberg does not list an EPUB edition for this book")
    )
    service = TransferService(root=tmp_path / "transfers", sources=[source])

    with pytest.raises(TransferRejected) as excinfo:
        service.start("fake", "28838")

    assert "EPUB edition" in str(excinfo.value)
    assert not list((tmp_path / "transfers").rglob("*.epub"))


def test_transfer_cleans_and_deletes_the_raw_download(tmp_path, sample_epub):
    service, source = make_service(tmp_path, data=sample_epub)

    job = service.start("fake", "42")

    assert source.downloads == 1
    assert job.output is not None and job.output.exists()
    assert job.output.name == "cleaned.epub"
    assert not (job.directory / "download.epub").exists()
    assert job.directory.parent == service.root
    assert job.status == "cleaned"

    payload = service.describe(job)
    assert payload["download_url"] == f"/api/transfer/{job.job_id}/download"
    assert payload["delete_url"] == f"/api/transfer/{job.job_id}"
    assert payload["file_name"] == "fake-book.cleaned.epub"
    assert payload["remaining_seconds"] == TRANSFER_TTL_SECONDS
    assert payload["expires_in"] == "5h 00m"
    assert payload["expires_at"].endswith("Z")
    security = payload["security"]
    assert security["status"] == "cleaned_and_verified"
    assert "not a guarantee" in security["disclaimer"].lower()
    assert "DRM" in security["safety_notice"]
    assert payload["report"]["summary"]["removed_count"] >= 0

    service.discard(job)
    assert not job.directory.exists()
    assert service.count() == 0


def test_remaining_time_counts_down(tmp_path, sample_epub):
    clock = Clock()
    service, _source = make_service(tmp_path, data=sample_epub, now=clock)
    job = service.start("fake", "42")

    clock.advance(3600)
    payload = service.describe(job)

    assert payload["remaining_seconds"] == TRANSFER_TTL_SECONDS - 3600
    assert payload["expires_in"] == "4h 00m"


def test_transfer_refuses_link_only_source(tmp_path):
    service = TransferService(root=tmp_path / "t", sources=[LinkOnly()])
    with pytest.raises(TransferUnavailable) as excinfo:
        service.start("linky", "1")
    assert "automated downloads" in str(excinfo.value)


def test_transfer_refuses_unknown_source(tmp_path):
    service = TransferService(root=tmp_path / "t", sources=[FakeSource()])
    with pytest.raises(TransferUnavailable):
        service.start("nope", "1")


def test_transfer_rejects_executable_payload(tmp_path, malicious_epub):
    service, _source = make_service(tmp_path, data=malicious_epub)

    with pytest.raises(TransferRejected) as excinfo:
        service.start("fake", "1")

    assert "active payloads" in str(excinfo.value)
    assert "archive.executable" in " ".join(BLOCKING_RULES)
    assert service.count() == 0
    assert list(service.root.iterdir()) == []


def test_transfer_rejects_zip_bomb(tmp_path, zip_bomb_epub):
    service, _source = make_service(tmp_path, data=zip_bomb_epub)
    with pytest.raises(TransferRejected):
        service.start("fake", "1")
    assert service.count() == 0


def test_transfer_rejects_drm(tmp_path, drm_epub):
    service, _source = make_service(tmp_path, data=drm_epub)
    with pytest.raises(TransferRejected) as excinfo:
        service.start("fake", "1")
    assert "DRM" in str(excinfo.value)


def test_transfer_rejects_oversized_download(tmp_path, sample_epub):
    service, _source = make_service(tmp_path, data=sample_epub, max_bytes=64)
    with pytest.raises(TransferRejected) as excinfo:
        service.start("fake", "1")
    assert "download limit" in str(excinfo.value)


def test_transfer_rejects_when_storage_limit_is_too_small(tmp_path, sample_epub):
    service, _source = make_service(tmp_path, data=sample_epub, max_storage_bytes=64)
    with pytest.raises(TransferRejected) as excinfo:
        service.start("fake", "1")
    assert "storage" in str(excinfo.value).lower()
    assert list(service.root.iterdir()) == []


def test_transfer_maps_source_errors(tmp_path):
    service = TransferService(
        root=tmp_path / "t", sources=[FakeSource(error=SourceError("upstream down"))]
    )
    with pytest.raises(TransferUnavailable) as excinfo:
        service.start("fake", "1")
    assert "upstream down" in str(excinfo.value)
    assert list(service.root.iterdir()) == []


def test_duplicate_request_reuses_the_cleaned_copy(tmp_path, sample_epub):
    service, source = make_service(tmp_path, data=sample_epub)

    first = service.start("fake", "7")
    second = service.start("fake", "7")

    assert second.job_id == first.job_id
    assert source.downloads == 1
    assert service.count() == 1


def test_expired_job_is_deleted_on_access(tmp_path, sample_epub):
    clock = Clock()
    service, _source = make_service(tmp_path, data=sample_epub, ttl=60, now=clock)
    job = service.start("fake", "1")

    clock.advance(61)
    assert service.get(job.job_id) is None
    assert not job.directory.exists()


def test_sweep_deletes_expired_files(tmp_path, sample_epub):
    clock = Clock()
    service, _source = make_service(tmp_path, data=sample_epub, ttl=60, now=clock)
    job = service.start("fake", "1")

    clock.advance(61)
    outcome = service.sweep()

    assert outcome["expired"] == 1
    assert outcome["remaining"] == 0
    assert not job.directory.exists()


def test_cleanup_worker_runs_the_sweep(tmp_path, sample_epub):
    clock = Clock()
    service, _source = make_service(tmp_path, data=sample_epub, ttl=60, now=clock)
    job = service.start("fake", "1")
    worker = CleanupWorker(service.sweep, interval=600)

    clock.advance(61)
    worker.run_once()

    assert worker.runs == 1
    assert worker.last_error is None
    assert not job.directory.exists()
    assert worker.interval == 600


def test_storage_usage_tracks_files_and_manual_delete_frees_it(tmp_path, sample_epub):
    service, _source = make_service(tmp_path, data=sample_epub)
    job = service.start("fake", "1")

    info = service.storage_info()
    assert info["used_bytes"] > 0
    assert info["jobs"] == 1
    assert info["ttl_seconds"] == TRANSFER_TTL_SECONDS
    assert info["cleanup_interval_seconds"] == CLEANUP_INTERVAL_SECONDS
    assert job.output is not None
    assert info["used_bytes"] >= job.output.stat().st_size

    assert service.delete(job.job_id) is True
    assert service.storage_info()["used_bytes"] == 0
    assert service.delete(job.job_id) is False


def test_jobs_survive_a_restart_and_stay_addressable(tmp_path, sample_epub):
    clock = Clock()
    root = tmp_path / "transfers"
    first = TransferService(root=root, sources=[FakeSource(sample_epub)], now=clock)
    job = first.start("fake", "9")
    first.close()

    assert job.directory.exists()
    assert (job.directory / "job.json").exists()
    assert (job.directory / "report.json").exists()

    clock.advance(60)
    second = TransferService(root=root, sources=[FakeSource(sample_epub)], now=clock)
    restored = second.get(job.job_id)

    assert restored is not None
    assert restored.output is not None and restored.output.exists()
    assert restored.report is not None
    assert restored.status == "cleaned"
    assert second.describe(restored)["remaining_seconds"] == TRANSFER_TTL_SECONDS - 60
    second.delete(job.job_id)


def test_expired_jobs_are_removed_during_restart(tmp_path, sample_epub):
    clock = Clock()
    root = tmp_path / "transfers"
    first = TransferService(root=root, sources=[FakeSource(sample_epub)], ttl=60, now=clock)
    job = first.start("fake", "9")
    first.close()

    clock.advance(61)
    second = TransferService(root=root, sources=[FakeSource(sample_epub)], ttl=60, now=clock)

    assert second.get(job.job_id) is None
    assert not job.directory.exists()
    assert second.count() == 0


def test_interrupted_download_is_discarded_on_restart(tmp_path, sample_epub):
    root = tmp_path / "transfers"
    first = TransferService(root=root, sources=[FakeSource(sample_epub)])
    job = first._create("fake", "1")  # simulate a crash mid-download
    assert (job.directory / "job.json").exists()
    first.close()

    second = TransferService(root=root, sources=[FakeSource(sample_epub)])

    assert second.get(job.job_id) is None
    assert not job.directory.exists()


def test_corrupt_metadata_is_removed_not_trusted(tmp_path):
    root = tmp_path / "transfers"
    root.mkdir(mode=0o700)
    broken = root / ("a" * 32)
    broken.mkdir(mode=0o700)
    (broken / "job.json").write_text("{not json", encoding="utf-8")
    (broken / "cleaned.epub").write_bytes(b"PK\x03\x04")

    service = TransferService(root=root, sources=[FakeSource()])

    assert service.count() == 0
    assert not broken.exists()


def test_orphaned_job_directory_is_swept(tmp_path):
    clock = Clock()
    service = TransferService(root=tmp_path / "t", sources=[FakeSource()], ttl=60, now=clock)
    orphan = service.root / ("b" * 32)
    orphan.mkdir(mode=0o700)
    old = clock.value - 3600
    os.utime(orphan, (old, old))

    clock.advance(7200)
    outcome = service.sweep()

    assert outcome["orphans"] == 1
    assert not orphan.exists()


def test_close_keeps_files_but_stops_the_worker(tmp_path, sample_epub):
    service, _source = make_service(tmp_path, data=sample_epub)
    job = service.start("fake", "1")
    service.start_cleanup()
    assert service._worker.running

    service.close()

    assert not service._worker.running
    assert job.output is not None and job.output.exists()
    service.clear()
    assert not service.root.exists()


def test_transfer_never_writes_outside_its_root(tmp_path, sample_epub):
    service, _source = make_service(tmp_path, data=sample_epub)
    job = service.start("fake", "1")
    assert job.output is not None
    assert service.root in job.output.parents
    assert job.output.stat().st_mode & 0o077 == 0


def test_busy_when_too_many_transfers(tmp_path, sample_epub):
    service, _source = make_service(tmp_path, data=sample_epub, max_transfers=1)
    service.start("fake", "1")
    with pytest.raises(TransferBusy):
        service.start("fake", "2")
