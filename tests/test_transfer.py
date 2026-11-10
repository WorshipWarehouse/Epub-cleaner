"""Transfer service: isolated storage, blocking rules, cleaning and deletion."""

from __future__ import annotations

import pytest

from epub_purifier.sources.base import DownloadResult, SourceError
from epub_purifier.transfer import (
    BLOCKING_RULES,
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


def make_service(tmp_path, data=b"", **kwargs):
    root = tmp_path / "transfers"
    source = FakeSource(data=data)
    service = TransferService(root=root, sources=[source], **kwargs)
    return service, source


def test_transfer_cleans_and_deletes_the_raw_download(tmp_path, sample_epub):
    service, source = make_service(tmp_path, data=sample_epub)

    job = service.start("fake", "42")

    assert source.downloads == 1
    assert job.output is not None and job.output.exists()
    assert job.output.name == "cleaned.epub"
    assert not (job.directory / "download.epub").exists()
    assert job.directory.parent == service.root

    payload = service.describe(job)
    assert payload["download_url"] == f"/api/transfer/{job.job_id}/download"
    assert payload["file_name"] == "fake-book.cleaned.epub"
    security = payload["security"]
    assert security["status"] == "cleaned_and_verified"
    assert "not a guarantee" in security["disclaimer"].lower()
    assert "DRM" in security["safety_notice"]
    assert payload["report"]["summary"]["removed_count"] >= 0

    service.discard(job)
    assert not job.directory.exists()
    assert service.count() == 0


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
    leftovers = list(service.root.iterdir())
    assert leftovers == []


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


def test_transfer_maps_source_errors(tmp_path):
    service = TransferService(
        root=tmp_path / "t", sources=[FakeSource(error=SourceError("upstream down"))]
    )
    with pytest.raises(TransferUnavailable) as excinfo:
        service.start("fake", "1")
    assert "upstream down" in str(excinfo.value)
    assert list(service.root.iterdir()) == []


def test_transfer_expiry_discards_files(tmp_path, sample_epub):
    service, _source = make_service(tmp_path, data=sample_epub, ttl=0)
    job = service.start("fake", "1")
    job.created_at -= 5
    assert service.get(job.job_id) is None
    assert not job.directory.exists()


def test_transfer_close_removes_root(tmp_path, sample_epub):
    service, _source = make_service(tmp_path, data=sample_epub)
    service.start("fake", "1")
    root = service.root
    service.close()
    assert not root.exists()
    assert service.count() == 0


def test_transfer_never_writes_outside_its_root(tmp_path, sample_epub):
    service, _source = make_service(tmp_path, data=sample_epub)
    job = service.start("fake", "1")
    assert job.output is not None
    assert service.root in job.output.parents
    assert job.output.stat().st_mode & 0o077 == 0
