"""Security scanner behaviour and threat classification."""

from __future__ import annotations

import io
import zipfile

from epub_purifier import clean_epub, inspect_epub
from epub_purifier.models import Severity

from .conftest import CHAPTER_TWO, CONTAINER, build_epub, opf_package, simple_book


def _entries(data: bytes) -> dict[str, bytes]:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        return {name: archive.read(name) for name in archive.namelist()}


def _rules(report) -> dict[str, Severity]:
    return {finding.rule: finding.severity for finding in report.findings}


def test_script_is_critical(malicious_epub):
    rules = _rules(inspect_epub(malicious_epub))
    assert rules["security.script"] is Severity.CRITICAL
    assert rules["security.event_handler"] is Severity.HIGH
    assert rules["security.iframe"] is Severity.HIGH
    assert rules["security.meta_refresh"] is Severity.HIGH
    assert rules["security.script_url"] is Severity.HIGH
    assert rules["security.form"] is Severity.MEDIUM
    assert rules["archive.executable"] is Severity.CRITICAL


def test_active_content_removed(malicious_epub):
    entries = _entries(clean_epub(malicious_epub).data)
    chapter = entries["OEBPS/chapter1.xhtml"].decode().lower()
    assert "<script" not in chapter
    assert "javascript:" not in chapter
    assert "<iframe" not in chapter
    assert "<object" not in chapter
    assert "onload" not in chapter
    assert "http-equiv" not in chapter
    assert "<form" not in chapter
    # Legitimate prose survives.
    assert "ordinary paragraph that should survive" in chapter


def test_tracking_resources_removed(malicious_epub):
    entries = _entries(clean_epub(malicious_epub).data)
    chapter = entries["OEBPS/chapter1.xhtml"].decode().lower()
    assert "google-analytics" not in chapter
    assert "utm_source" not in chapter


def test_executable_dropped(malicious_epub):
    entries = _entries(clean_epub(malicious_epub).data)
    assert "OEBPS/tool.exe" not in entries


def test_xxe_declaration_stripped(xxe_epub):
    report = inspect_epub(xxe_epub)
    assert any(f.rule == "security.xml_entities" for f in report.findings)

    entries = _entries(clean_epub(xxe_epub).data)
    chapter = entries["OEBPS/chapter1.xhtml"].decode()
    assert "<!ENTITY" not in chapter
    assert "SYSTEM" not in chapter
    # Nothing from the referenced file can have been expanded into the document.
    assert "root:" not in chapter


def test_traversal_members_never_written(traversal_epub):
    entries = _entries(clean_epub(traversal_epub).data)
    assert not any(".." in name for name in entries)
    assert "evil.xhtml" not in entries


def test_severity_classification_covers_all_levels():
    for severity in Severity:
        assert severity.label
        assert isinstance(severity.weight, int)


def test_clean_report_does_not_promise_safety(sample_epub):
    report = inspect_epub(sample_epub)
    payload = report.to_dict()
    assert "not a guarantee" in payload["disclaimer"].lower()
    assert "not a guarantee" in payload["summary"]["verdict"].lower() or report.findings

    # Even a book with zero findings must not claim to be safe.
    clean = inspect_epub(simple_book(CHAPTER_TWO))
    assert "not a safety guarantee" in clean.verdict().lower()
    assert payload["safety_notice"].startswith("Only clean files")


def test_no_active_content_executed(xxe_epub, tmp_path, monkeypatch):
    """Parsing must never resolve entities, so no file content can leak."""

    secret = tmp_path / "secret.txt"
    secret.write_text("TOP-SECRET-MARKER")

    chapter = (
        '<?xml version="1.0"?>\n'
        '<!DOCTYPE html [ <!ENTITY leak SYSTEM "file://' + str(secret) + '"> ]>\n'
        '<html xmlns="http://www.w3.org/1999/xhtml"><head><title>t</title></head>'
        "<body><p>&leak;</p></body></html>"
    )
    from .conftest import simple_book

    report = inspect_epub(simple_book(chapter))
    assert "TOP-SECRET-MARKER" not in report.to_json()

    cleaned = clean_epub(simple_book(chapter))
    assert b"TOP-SECRET-MARKER" not in cleaned.data


def test_data_uri_active_content_removed():
    chapter = """<?xml version="1.0" encoding="utf-8"?>
<html xmlns="http://www.w3.org/1999/xhtml"><head><title>t</title></head>
<body><p>Text</p><img src="data:text/html;base64,PHNjcmlwdD5hbGVydCgxKTwvc2NyaXB0Pg=="/></body></html>"""
    report = inspect_epub(simple_book(chapter))
    assert any(f.rule == "security.data_uri_active" for f in report.findings)
    entries = _entries(clean_epub(simple_book(chapter)).data)
    assert "data:text/html" not in entries["OEBPS/chapter1.xhtml"].decode()


def test_svg_script_detected():
    svg = """<?xml version="1.0" encoding="utf-8"?>
<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10">
  <script>alert(1)</script>
  <rect width="10" height="10"/>
</svg>"""
    data = build_epub(
        [
            ("META-INF/container.xml", CONTAINER),
            (
                "OEBPS/content.opf",
                opf_package(
                    manifest=(
                        '<item id="ch1" href="chapter1.xhtml" media-type="application/xhtml+xml"/>'
                        '<item id="svg" href="art.svg" media-type="image/svg+xml"/>'
                    ),
                    spine='<itemref idref="ch1"/>',
                ),
            ),
            ("OEBPS/nav.xhtml", "<html><body><nav><ol><li><a href=\"chapter1.xhtml\">One</a></li></ol></nav></body></html>"),
            ("OEBPS/chapter1.xhtml", CHAPTER_TWO),
            ("OEBPS/art.svg", svg),
        ]
    )
    report = inspect_epub(data)
    assert any(f.rule == "security.script" and f.target == "OEBPS/art.svg" for f in report.findings)
    entries = _entries(clean_epub(data).data)
    assert b"<script" not in entries["OEBPS/art.svg"]
