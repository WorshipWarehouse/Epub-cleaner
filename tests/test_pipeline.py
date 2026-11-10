"""Pipeline behaviour: valid output, idempotence, ids and reporting."""

from __future__ import annotations

import io
import zipfile

from epub_purifier import PurifierConfig, clean_epub, inspect_epub
from epub_purifier.models import Category, Remediation, Severity
from epub_purifier.repackage import verify_epub


def _zip(data: bytes) -> zipfile.ZipFile:
    return zipfile.ZipFile(io.BytesIO(data))


def test_cleaned_output_is_valid(sample_epub):
    result = clean_epub(sample_epub)
    assert result.data is not None
    assert verify_epub(result.data) == []

    with _zip(result.data) as archive:
        infos = archive.infolist()
        assert infos[0].filename == "mimetype"
        assert infos[0].compress_type == zipfile.ZIP_STORED
        assert archive.read("mimetype") == b"application/epub+zip"
        assert "META-INF/container.xml" in archive.namelist()


def test_opf_remains_well_formed(sample_epub):
    from bs4 import BeautifulSoup

    with _zip(clean_epub(sample_epub).data) as archive:
        opf = archive.read("OEBPS/content.opf").decode()
    soup = BeautifulSoup(opf, "lxml-xml")
    assert soup.find("package") is not None
    assert soup.find("spine") is not None
    assert len(soup.find_all("itemref")) == 2
    assert "dcterms:modified" in opf


def test_cleaning_is_idempotent(sample_epub):
    once = clean_epub(sample_epub)
    twice = clean_epub(once.data)
    assert twice.report.removed == []
    with _zip(once.data) as first, _zip(twice.data) as second:
        assert sorted(first.namelist()) == sorted(second.namelist())


def test_inspect_does_not_modify_input(sample_epub):
    before = sample_epub
    inspect_epub(sample_epub)
    assert sample_epub == before


def test_finding_ids_are_stable(sample_epub):
    first = inspect_epub(sample_epub)
    second = inspect_epub(sample_epub)
    assert [f.finding_id for f in first.findings] == [f.finding_id for f in second.findings]

    cleaned = clean_epub(sample_epub)
    inspect_ids = {f.finding_id for f in first.findings}
    clean_ids = {f.finding_id for f in cleaned.report.findings}
    assert clean_ids == inspect_ids


def test_report_is_json_serialisable(sample_epub):
    import json

    payload = json.loads(clean_epub(sample_epub).report.to_json())
    assert payload["tool"]["name"] == "epub-purifier"
    assert payload["summary"]["removed_count"] > 0
    assert payload["output"]["sha256"]
    assert payload["file"]["epub_version"].startswith("3.")
    assert "findings" in payload and "removed" in payload


def test_dry_run_writes_nothing(sample_epub, tmp_path):
    target = tmp_path / "out.epub"
    result = clean_epub(sample_epub, output=target, dry_run=True)
    assert result.data is None
    assert not target.exists()
    assert result.report.removed


def test_output_file_and_paths(sample_epub, tmp_path):
    target = tmp_path / "nested" / "book.cleaned.epub"
    result = clean_epub(sample_epub, output=target)
    assert target.exists()
    assert result.output_path == str(target)
    assert verify_epub(target.read_bytes()) == []


def test_clean_report_lists_actions(sample_epub):
    report = clean_epub(sample_epub).report
    actions = {item.action for item in report.removed}
    assert "drop_entry" in actions
    assert "remove_element" in actions
    assert any(item.target == "OEBPS/promo.xhtml" for item in report.removed)


def test_stats_are_populated(sample_epub):
    report = clean_epub(sample_epub).report
    assert report.stats["entries_before"] > report.stats["entries_after"]
    assert "OEBPS/promo.xhtml" in report.stats["dropped_entries"]
    assert report.stats["removed"] == len(report.removed)


def test_report_orders_by_severity(sample_epub):
    findings = inspect_epub(sample_epub).findings_by_severity()
    weights = [finding.severity.weight for finding in findings]
    assert weights == sorted(weights, reverse=True)


def test_thresholds_are_configurable(review_epub):
    strict = PurifierConfig(auto_remove_confidence=0.6)
    report = inspect_epub(review_epub, strict)
    assert any(f.remediation is Remediation.AUTO for f in report.findings)

    lenient = PurifierConfig(auto_remove_confidence=0.99, review_confidence=0.9)
    lenient_report = inspect_epub(review_epub, lenient)
    assert all(f.remediation is not Remediation.AUTO for f in lenient_report.findings)


def test_remove_remote_resources_option():
    chapter = """<?xml version="1.0" encoding="utf-8"?>
<html xmlns="http://www.w3.org/1999/xhtml"><head><title>t</title></head>
<body><p>Prose</p><img src="https://images.example.org/plate.png" alt="plate"/></body></html>"""
    from .conftest import simple_book

    default_report = inspect_epub(simple_book(chapter))
    assert any(f.rule == "external.remote_resource" for f in default_report.findings)
    assert any(
        f.remediation is Remediation.REVIEW
        for f in default_report.findings
        if f.rule == "external.remote_resource"
    )

    config = PurifierConfig(remove_remote_resources=True)
    entries = _zip(clean_epub(simple_book(chapter), config).data).read("OEBPS/chapter1.xhtml").decode()
    assert "images.example.org" not in entries
    assert "Prose" in entries


def test_keeps_promo_pages_when_disabled(sample_epub):
    config = PurifierConfig(drop_promo_pages=False)
    result = clean_epub(sample_epub, config)
    with _zip(result.data) as archive:
        assert "OEBPS/promo.xhtml" in archive.namelist()


def test_severity_counts_are_consistent(sample_epub):
    report = inspect_epub(sample_epub)
    counts = report.counts()
    assert sum(counts.values()) == len(report.findings)
    assert counts[Severity.CRITICAL.value] >= 1
    assert report.highest_severity() is Severity.CRITICAL
    assert report.stats["needs_review"] == len(report.reviewable())


def test_category_distribution(sample_epub):
    categories = {f.category for f in inspect_epub(sample_epub).findings}
    assert Category.SCRIPT in categories
    assert Category.BRANDING in categories
    assert Category.TRACKING in categories
