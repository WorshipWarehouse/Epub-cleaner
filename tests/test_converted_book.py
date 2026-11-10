"""End-to-end behaviour on a realistic third-party converted book.

These tests pin the promises the README makes: the injected promotional layer
goes away, the reading experience survives, citations are never proposed for
removal, and the documented confidence examples score what the docs say.
"""

from __future__ import annotations

import io
import json
import zipfile

import pytest

from epub_purifier import PurifierConfig, clean_epub, inspect_epub
from epub_purifier.models import Remediation, Severity
from epub_purifier.repackage import verify_epub

from .conftest import simple_book
from .converted_book import LEGIT_MARKERS, PROMO_MARKERS, converted_book


def _entries(data: bytes) -> dict[str, bytes]:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        return {name: archive.read(name) for name in archive.namelist()}


def _markup(entries: dict[str, bytes]) -> str:
    return "\n".join(
        data.decode("utf-8", "replace")
        for name, data in entries.items()
        if name.endswith((".xhtml", ".opf", ".ncx", ".css", ".html"))
    )


def _finding(report, needle: str):
    matches = [f for f in report.findings if f.evidence and needle in f.evidence]
    assert matches, f"no finding mentions {needle!r}"
    return matches[0]


def test_fixture_opf_declares_exactly_one_nav_and_one_title():
    """The fixture must be a valid EPUB before cleaning can prove anything."""

    data = converted_book()
    assert verify_epub(data) == []
    opf = _entries(data)["OEBPS/content.opf"].decode()
    assert opf.count('properties="nav"') == 1
    assert opf.count("<dc:title>") == 1
    assert opf.count("<dc:creator>") == 1
    assert opf.count("<dc:language>") == 1


def test_cleaned_converted_book_is_a_valid_epub():
    result = clean_epub(converted_book())

    assert result.data is not None
    assert verify_epub(result.data) == []
    with zipfile.ZipFile(io.BytesIO(result.data)) as archive:
        first = archive.infolist()[0]
    assert first.filename == "mimetype" and first.compress_type == zipfile.ZIP_STORED


def test_every_injected_promo_marker_is_gone():
    result = clean_epub(converted_book())
    entries = _entries(result.data)
    text = _markup(entries).lower()

    for marker in PROMO_MARKERS:
        assert marker.lower() not in text, marker

    opf = entries["OEBPS/content.opf"].decode()
    assert "Downloaded from OceanofPDF" not in opf
    assert "free-library" not in opf


def test_legitimate_content_images_and_styles_survive():
    entries = _entries(clean_epub(converted_book()).data)
    text = _markup(entries)

    for marker in LEGIT_MARKERS:
        assert marker in text, marker

    chapter = entries["OEBPS/chapter1.xhtml"].decode()
    assert "Chapter One: The Room" in chapter
    assert 'src="images/plate.png"' in chapter
    assert 'href="https://www.gutenberg.org/ebooks/1342"' in chapter
    assert "OEBPS/images/plate.png" in entries
    assert entries["OEBPS/images/plate.png"].startswith(b"\x89PNG")

    styles = entries["OEBPS/styles.css"].decode()
    assert "font-family: Georgia, serif" in styles
    assert "max-width: 100%" in styles


def test_navigation_still_works_after_the_promo_page_is_dropped():
    entries = _entries(clean_epub(converted_book()).data)
    nav = entries["OEBPS/nav.xhtml"].decode()

    for chapter in ("chapter1.xhtml", "chapter2.xhtml", "chapter3.xhtml"):
        assert chapter in nav
        assert f"OEBPS/{chapter}" in entries
    assert "free-library.xhtml" not in nav
    assert "OEBPS/free-library.xhtml" not in entries
    assert nav.count("<li>") == 3

    opf = entries["OEBPS/content.opf"].decode()
    assert 'idref="free"' not in opf and 'id="free"' not in opf
    assert "itemref" in opf


def test_promo_page_is_reported_and_dropped():
    report = inspect_epub(converted_book())
    pages = [f for f in report.findings if f.rule == "branding.promo_page"]
    assert pages and pages[0].remediation is Remediation.AUTO

    cleaned = clean_epub(converted_book())
    assert "OEBPS/free-library.xhtml" in cleaned.report.stats["dropped_entries"]


def test_citation_and_footnote_survive_even_approving_everything():
    result = clean_epub(converted_book(), approve_all=True)
    entries = _entries(result.data)
    text = _markup(entries)

    assert "digitisation changes how a page is read" in text
    assert "Source: oceanofpdf.com, accessed 12 March 2020." in text
    # The blurb next to them is still promotional, not protected prose.
    assert "Download more free books" not in text


def test_readme_plain_footer_example_scores_099_as_the_trailing_block():
    report = inspect_epub(converted_book())
    footer = _finding(report, "Downloaded from OceanofPDF.com")
    assert footer.remediation is Remediation.AUTO
    assert footer.confidence == 0.99

    entries = _entries(clean_epub(converted_book()).data)
    assert "Downloaded from OceanofPDF.com" not in _markup(entries)


def test_readme_footer_example_scores_090_when_content_follows_it():
    chapter = """<?xml version="1.0" encoding="utf-8"?>
<html xmlns="http://www.w3.org/1999/xhtml">
<head><title>One</title></head>
<body>
  <h1>One</h1>
  <p>Ordinary prose before the insertion.</p>
  <p>Downloaded from OceanofPDF.com - free ebooks every day.</p>
  <p>Ordinary prose after the insertion.</p>
</body>
</html>"""
    report = inspect_epub(simple_book(chapter))
    footer = _finding(report, "Downloaded from OceanofPDF.com")
    assert footer.remediation is Remediation.AUTO
    assert footer.confidence == 0.9


def test_readme_blockquote_example_scores_010_and_is_kept():
    chapter = """<?xml version="1.0" encoding="utf-8"?>
<html xmlns="http://www.w3.org/1999/xhtml">
<head><title>Notes</title></head>
<body>
  <blockquote><p>As the editors at OceanofPDF observe, digitisation changes how a page is read.</p></blockquote>
  <p>Ordinary prose follows the quotation.</p>
</body>
</html>"""
    data = simple_book(chapter)
    report = inspect_epub(data)
    quote = _finding(report, "As the editors at OceanofPDF observe")
    assert quote.remediation is Remediation.REPORT
    assert quote.confidence == 0.1

    kept = _entries(clean_epub(data, approve_all=True).data)["OEBPS/chapter1.xhtml"].decode()
    assert "As the editors at OceanofPDF observe" in kept


def test_readme_review_example_scores_075_and_waits_for_approval(review_epub):
    report = inspect_epub(review_epub)
    mention = _finding(report, "This copy was downloaded from OceanofPDF")
    assert mention.remediation is Remediation.REVIEW
    assert mention.confidence == 0.75
    assert [f.finding_id for f in report.reviewable()] == [mention.finding_id]

    kept = _entries(clean_epub(review_epub).data)["OEBPS/chapter1.xhtml"].decode()
    assert "downloaded from OceanofPDF" in kept

    approved = _entries(clean_epub(review_epub, approved={mention.finding_id}).data)[
        "OEBPS/chapter1.xhtml"
    ].decode()
    assert "OceanofPDF" not in approved


def test_css_inside_style_is_not_reported_as_a_text_block():
    chapter = """<?xml version="1.0" encoding="utf-8"?>
<html xmlns="http://www.w3.org/1999/xhtml">
<head><title>One</title><style>.note::after { content: "oceanofpdf.com"; }</style></head>
<body><h1>One</h1><p>Ordinary prose.</p></body>
</html>"""
    report = inspect_epub(simple_book(chapter))
    text_blocks = [f for f in report.findings if f.rule == "branding.text_block"]
    assert not any("style" in (f.location or "") for f in text_blocks)
    assert any(f.rule == "branding.inline_style" for f in report.findings)


def test_report_never_claims_the_file_is_malware_free():
    report = clean_epub(converted_book()).report
    payload = json.loads(report.to_json())
    serialised = json.dumps(payload).lower()

    assert "not a guarantee" in payload["disclaimer"].lower()
    assert "malware-free" not in serialised
    assert "virus-free" not in serialised
    assert payload["summary"]["counts"][Severity.CRITICAL.value] >= 1
    assert report.stats["needs_review"] >= 0


def test_cleaning_the_converted_book_twice_changes_nothing():
    once = clean_epub(converted_book())
    twice = clean_epub(once.data)

    assert twice.report.removed == []
    assert sorted(_entries(once.data)) == sorted(_entries(twice.data))


def test_converted_book_with_page_dropping_disabled_keeps_the_page():
    config = PurifierConfig(drop_promo_pages=False)
    entries = _entries(clean_epub(converted_book(), config).data)
    assert "OEBPS/free-library.xhtml" in entries
    assert "OceanofPDF" not in entries["OEBPS/free-library.xhtml"].decode()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__]))
