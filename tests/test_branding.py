"""Ocean-of-PDF style branding detection: removal, confidence and restraint."""

from __future__ import annotations

import io
import zipfile

from epub_purifier import clean_epub, inspect_epub
from epub_purifier.models import Category, Remediation


def _entries(data: bytes) -> dict[str, bytes]:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        return {name: archive.read(name) for name in archive.namelist()}


def _text_of(entries: dict[str, bytes]) -> str:
    return "\n".join(
        data.decode("utf-8", "replace")
        for name, data in entries.items()
        if name.endswith((".xhtml", ".opf", ".css", ".ncx"))
    )


PLAIN_FOOTER_CHAPTER = """<?xml version="1.0" encoding="utf-8"?>
<html xmlns="http://www.w3.org/1999/xhtml">
<head><title>Chapter One</title></head>
<body>
  <h1>Chapter One</h1>
  <p>The church has always been analog, and the room shapes what happens in it.</p>
  <p>Downloaded from OceanofPDF.com - free ebooks every day.</p>
</body>
</html>"""

INLINE_STYLE_CHAPTER = """<?xml version="1.0" encoding="utf-8"?>
<html xmlns="http://www.w3.org/1999/xhtml">
<head><title>Chapter One</title>
  <style>.promo::after { content: "oceanofpdf.com"; }</style>
</head>
<body>
  <h1>Chapter One</h1>
  <p>Ordinary prose that must survive untouched.</p>
</body>
</html>"""


def test_plain_text_promo_footer_is_removed_without_markup_hints():
    """A bare "Downloaded from …" paragraph needs no promo class to be removed."""

    from .conftest import simple_book

    data = simple_book(PLAIN_FOOTER_CHAPTER)
    report = inspect_epub(data)
    footer = [
        finding
        for finding in report.findings
        if finding.evidence and "Downloaded from OceanofPDF.com" in finding.evidence
    ]
    assert footer, "the footer should be reported"
    assert footer[0].remediation is Remediation.AUTO
    assert footer[0].confidence >= 0.85

    chapter = _entries(clean_epub(data).data)["OEBPS/chapter1.xhtml"].decode()
    assert "Downloaded from" not in chapter
    assert "always been analog" in chapter
    assert "Chapter One" in chapter


def test_inline_css_is_not_also_scored_as_a_text_block():
    """CSS bodies are reported once, by the style rule, not as promotional prose."""

    from .conftest import simple_book

    report = inspect_epub(simple_book(INLINE_STYLE_CHAPTER))
    text_blocks = [
        finding
        for finding in report.findings
        if finding.rule == "branding.text_block" and finding.target.endswith("chapter1.xhtml")
    ]
    assert not any("style" in (finding.location or "") for finding in text_blocks)
    assert any(finding.rule == "branding.inline_style" for finding in report.findings)


def test_promo_paragraph_removed_chapter_kept(sample_epub):
    result = clean_epub(sample_epub)
    entries = _entries(result.data)
    chapter = entries["OEBPS/chapter1.xhtml"].decode()

    assert "OceanofPDF" not in chapter
    assert "footer-note" not in chapter
    # The chapter itself, including its prose and heading, is untouched.
    assert "Chapter One" in chapter
    assert "The church has always been analog" in chapter
    assert "Screens are not evil" in chapter


def test_branding_is_high_confidence(sample_epub):
    report = inspect_epub(sample_epub)
    branding = [f for f in report.findings if f.category is Category.BRANDING]
    assert branding
    assert any(f.remediation is Remediation.AUTO and f.confidence >= 0.85 for f in branding)
    assert all(f.evidence for f in branding)


def test_metadata_branding_removed(sample_epub):
    entries = _entries(clean_epub(sample_epub).data)
    opf = entries["OEBPS/content.opf"].decode()
    assert "OceanofPDF" not in opf
    assert "Downloaded from" not in opf
    # Legitimate metadata survives.
    assert "Analog Church" in opf
    assert "Jay Y. Kim" in opf
    assert "InterVarsity Press" in opf


def test_promo_page_dropped(sample_epub):
    entries = _entries(clean_epub(sample_epub).data)
    assert "OEBPS/promo.xhtml" not in entries
    opf = entries["OEBPS/content.opf"].decode()
    assert 'id="promo"' not in opf
    assert 'idref="promo"' not in opf


def test_css_branding_and_expression_removed(sample_epub):
    css = _entries(clean_epub(sample_epub).data)["OEBPS/styles.css"].decode()
    assert "OceanofPDF" not in css
    assert "expression" not in css
    assert "font-family: Georgia, serif" in css


FOOTNOTE_CHAPTER = """<?xml version="1.0" encoding="utf-8"?>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">
<head><title>Notes</title></head>
<body>
  <h1>Notes</h1>
  <p>Ordinary prose about attention and typography.</p>
  <aside epub:type="footnote" class="footnote"><p>Source: oceanofpdf.com, accessed 12 March 2020.</p></aside>
</body>
</html>"""


def test_footnote_citation_is_informational_not_reviewable():
    """A footnote citation is reported, but never offered for removal."""

    from .conftest import simple_book

    data = simple_book(FOOTNOTE_CHAPTER)
    report = inspect_epub(data)
    footnote = [
        finding
        for finding in report.findings
        if finding.evidence and "Source: oceanofpdf.com" in finding.evidence
    ]
    assert footnote, "the citation should still be reported"
    assert footnote[0].remediation is Remediation.REPORT
    assert footnote[0].confidence < 0.5
    assert report.reviewable() == []

    chapter = _entries(clean_epub(data).data)["OEBPS/chapter1.xhtml"].decode()
    assert "Source: oceanofpdf.com" in chapter
    assert "Ordinary prose about attention" in chapter


def test_citation_is_never_removed(citation_epub):
    report = inspect_epub(citation_epub)
    mentions = [f for f in report.findings if f.category is Category.BRANDING]
    assert mentions, "the mention should still be reported"
    assert all(f.remediation is not Remediation.AUTO for f in mentions)

    entries = _entries(clean_epub(citation_epub).data)
    chapter = entries["OEBPS/chapter1.xhtml"].decode()
    assert "As the editors at OceanofPDF observe" in chapter


def test_ambiguous_mention_requires_approval(review_epub):
    report = inspect_epub(review_epub)
    reviewable = report.reviewable()
    assert len(reviewable) == 1
    assert reviewable[0].confidence < 0.85

    kept = _entries(clean_epub(review_epub).data)["OEBPS/chapter1.xhtml"].decode()
    assert "downloaded from OceanofPDF" in kept

    approved = clean_epub(review_epub, approved={reviewable[0].finding_id})
    removed = _entries(approved.data)["OEBPS/chapter1.xhtml"].decode()
    assert "OceanofPDF" not in removed
    assert "liturgy, community" in removed


def test_approve_all_removes_ambiguous(review_epub):
    result = clean_epub(review_epub, approve_all=True)
    chapter = _entries(result.data)["OEBPS/chapter1.xhtml"].decode()
    assert "OceanofPDF" not in chapter


def test_brand_spellings_are_matched():
    from epub_purifier.config import PurifierConfig

    pattern = PurifierConfig().brand_regex
    for spelling in (
        "Ocean of PDF",
        "OceanofPDF",
        "OCEANOFPDF.COM",
        "ocean-of-pdf",
        "O C E A N of PDF".replace(" ", ""),
        "www.oceanofpdf.com",
    ):
        assert pattern.search(spelling), spelling


def test_navigation_entry_removed():
    from .conftest import CHAPTER_TWO, CONTAINER, build_epub, opf_package

    nav = """<?xml version="1.0" encoding="utf-8"?>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">
<head><title>Contents</title></head>
<body><nav epub:type="toc"><ol>
  <li><a href="chapter1.xhtml">Chapter One</a></li>
  <li><a href="https://www.oceanofpdf.com">OceanofPDF free library</a></li>
</ol></nav></body></html>"""
    data = build_epub(
        [
            ("META-INF/container.xml", CONTAINER),
            (
                "OEBPS/content.opf",
                opf_package(
                    manifest='<item id="ch1" href="chapter1.xhtml" media-type="application/xhtml+xml"/>',
                    spine='<itemref idref="ch1"/>',
                ),
            ),
            ("OEBPS/nav.xhtml", nav),
            ("OEBPS/chapter1.xhtml", CHAPTER_TWO),
        ]
    )
    report = inspect_epub(data)
    entries = _entries(clean_epub(data).data)
    assert any(f.rule == "branding.navigation_entry" for f in report.findings)
    assert "OceanofPDF" not in entries["OEBPS/nav.xhtml"].decode()
    assert "Chapter One" in entries["OEBPS/nav.xhtml"].decode()
