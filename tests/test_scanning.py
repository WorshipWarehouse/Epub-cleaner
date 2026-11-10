"""The scan gate between downloaded bytes and a servable cleaned EPUB."""

from __future__ import annotations

import pytest

from epub_purifier.scanning import BLOCKING_RULES, ScanRejected, scan_and_clean


def test_clean_book_is_scanned_and_rebuilt(sample_epub):
    outcome = scan_and_clean(sample_epub, file_name="book.epub")

    assert outcome.data[:2] == b"PK"
    assert outcome.data != sample_epub
    assert outcome.report.mode == "clean"
    assert outcome.report.to_dict()["summary"]["removed_count"] > 0
    assert outcome.blocked_rules == ()
    # Scripts and tracking pixels are removed by the cleaner, not a rejection.
    assert "security.script" in {finding.rule for finding in outcome.report.findings}


def test_executable_payload_rejects_the_download(malicious_epub):
    with pytest.raises(ScanRejected) as excinfo:
        scan_and_clean(malicious_epub, file_name="bad.epub")

    assert "active payloads" in str(excinfo.value)
    assert "archive.executable" in excinfo.value.rules
    assert set(excinfo.value.rules) <= BLOCKING_RULES


def test_traversal_archive_rejects_the_download(traversal_epub):
    with pytest.raises(ScanRejected) as excinfo:
        scan_and_clean(traversal_epub)

    assert "archive.unsafe_member" in excinfo.value.rules


def test_zip_bomb_is_rejected(zip_bomb_epub):
    with pytest.raises(ScanRejected):
        scan_and_clean(zip_bomb_epub)


def test_drm_is_rejected(drm_epub):
    with pytest.raises(ScanRejected) as excinfo:
        scan_and_clean(drm_epub)
    assert "DRM" in str(excinfo.value)


def test_empty_download_is_rejected():
    with pytest.raises(ScanRejected) as excinfo:
        scan_and_clean(b"")
    assert "empty" in str(excinfo.value)


def test_non_epub_bytes_are_rejected():
    with pytest.raises(ScanRejected):
        scan_and_clean(b"this is not a zip archive")
