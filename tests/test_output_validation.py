"""Output checks: XHTML serialization and EPUB 3 package requirements."""

from __future__ import annotations

import io
import zipfile

from lxml import etree

from epub_purifier import clean_epub
from epub_purifier.repackage import verify_epub

from .conftest import CHAPTER_TWO, CONTAINER, NAV, build_epub, opf_package, simple_book

XHTML = "http://www.w3.org/1999/xhtml"


def _content(data: bytes, name: str = "OEBPS/chapter1.xhtml") -> bytes:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        return archive.read(name)


def test_malformed_html_is_rewritten_as_valid_xhtml():
    chapter = (
        "<html><head><title>One</title></head><body>"
        "<p>Ordinary<br>prose&nbsp;continues.</p>"
        "<p>Downloaded from OceanofPDF.com</p>"
        "</body></html>"
    )
    result = clean_epub(simple_book(chapter))
    assert result.data is not None

    output = _content(result.data)
    assert output.startswith(b'<?xml version="1.0" encoding="utf-8"?>')
    assert b"<!DOCTYPE html>" in output
    assert f'xmlns="{XHTML}"'.encode() in output
    assert b"<br/>" in output
    assert b"</br>" not in output
    assert b"\xc2\xa0" in output

    root = etree.fromstring(output)  # noqa: S320 - output produced by this test
    assert root.tag == f"{{{XHTML}}}html"
    assert verify_epub(result.data) == []


def test_epub2_content_gets_an_xhtml_11_doctype():
    chapter = (
        "<html><head><title>One</title></head><body>"
        "<p>Ordinary prose.</p>"
        "<p>Downloaded from OceanofPDF.com - free ebooks.</p>"
        "</body></html>"
    )
    opf = opf_package(
        manifest='<item id="ch1" href="chapter1.xhtml" media-type="application/xhtml+xml"/>',
        spine='<itemref idref="ch1"/>',
        version="2.0",
    )
    data = build_epub(
        [
            ("META-INF/container.xml", CONTAINER),
            ("OEBPS/content.opf", opf),
            ("OEBPS/nav.xhtml", NAV),
            ("OEBPS/chapter1.xhtml", chapter),
        ]
    )

    result = clean_epub(data)
    assert result.data is not None
    output = _content(result.data)
    assert b'<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.1//EN"' in output
    assert b'"http://www.w3.org/TR/xhtml11/DTD/xhtml11.dtd"' in output
    assert verify_epub(result.data) == []


def test_verifier_rejects_epub2_content_without_a_doctype():
    opf = opf_package(
        manifest='<item id="ch1" href="chapter1.xhtml" media-type="application/xhtml+xml"/>',
        spine='<itemref idref="ch1"/>',
        version="2.0",
    )
    data = build_epub(
        [
            ("META-INF/container.xml", CONTAINER),
            ("OEBPS/content.opf", opf),
            ("OEBPS/nav.xhtml", NAV),
            ("OEBPS/chapter1.xhtml", CHAPTER_TWO),
        ]
    )

    problems = verify_epub(data)
    assert any("no XHTML doctype" in problem for problem in problems)


def test_verifier_rejects_non_empty_xhtml_void_elements():
    chapter = (
        f'<html xmlns="{XHTML}"><head><title>One</title></head>'
        "<body><p>Text<br>more text</br></p></body></html>"
    )
    problems = verify_epub(simple_book(chapter))
    assert any("non-empty <br>" in problem for problem in problems)


def test_verifier_requires_an_epub3_navigation_document():
    opf = """<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="bookid">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:identifier id="bookid">urn:uuid:12345678-1234-1234-1234-123456789012</dc:identifier>
    <dc:title>No Nav</dc:title>
    <dc:language>en</dc:language>
    <meta property="dcterms:modified">2020-01-01T00:00:00Z</meta>
  </metadata>
  <manifest>
    <item id="ch1" href="chapter1.xhtml" media-type="application/xhtml+xml"/>
  </manifest>
  <spine><itemref idref="ch1"/></spine>
</package>"""
    data = build_epub(
        [
            ("META-INF/container.xml", CONTAINER),
            ("OEBPS/content.opf", opf),
            ("OEBPS/chapter1.xhtml", CHAPTER_TWO),
        ]
    )
    problems = verify_epub(data)
    assert any("navigation document" in problem for problem in problems)


def test_verifier_requires_a_valid_modified_timestamp():
    opf = """<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="bookid">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:identifier id="bookid">urn:uuid:12345678-1234-1234-1234-123456789012</dc:identifier>
    <dc:title>No Modified</dc:title>
    <dc:language>en</dc:language>
  </metadata>
  <manifest>
    <item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>
    <item id="ch1" href="chapter1.xhtml" media-type="application/xhtml+xml"/>
  </manifest>
  <spine><itemref idref="ch1"/></spine>
</package>"""
    data = build_epub(
        [
            ("META-INF/container.xml", CONTAINER),
            ("OEBPS/content.opf", opf),
            ("OEBPS/nav.xhtml", NAV),
            ("OEBPS/chapter1.xhtml", CHAPTER_TWO),
        ]
    )
    problems = verify_epub(data)
    assert any("dcterms:modified" in problem for problem in problems)


def _rewrite(data, name, transform):
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        return build_epub([
            (entry, transform(archive.read(entry)) if entry == name else archive.read(entry))
            for entry in archive.namelist() if entry != "mimetype"
        ])


def test_html_style_text_survives_xml_serialization():
    from epub_purifier.markup import parse_markup, serialize_markup

    css = 'p::after { content: "A & B < C"; }'
    soup, parser = parse_markup(f"<html><head><style>{css}</style></head><body><br></body></html>")
    root = etree.fromstring(serialize_markup(soup, parser, xml_declaration=True))
    assert root.find(f"{{{XHTML}}}head/{{{XHTML}}}style").text == css


def test_refined_modified_timestamp_is_preserved():
    from bs4 import BeautifulSoup

    from epub_purifier.opf import (
        PackageDocument,
        has_valid_modified_timestamp,
        modified_timestamps,
        set_modified_timestamp,
    )

    data = _rewrite(simple_book(CHAPTER_TWO), "OEBPS/content.opf", lambda value: value.replace(
        b'<meta property="dcterms:modified">',
        b'<meta property="dcterms:modified" refines="#bookid">1999-01-01T00:00:00Z</meta>'
        b'<meta property="dcterms:modified">',
    ))
    assert verify_epub(data) == []
    pkg = PackageDocument("OEBPS/content.opf", BeautifulSoup(_content(data, "OEBPS/content.opf"), "xml"), "3.0")
    assert has_valid_modified_timestamp(pkg)
    set_modified_timestamp(pkg, "2024-02-29T12:30:59Z")
    assert modified_timestamps(pkg) == ["2024-02-29T12:30:59Z"]
    assert pkg.metadata.find("meta", refines="#bookid").text == "1999-01-01T00:00:00Z"


def test_timestamp_calendar_validation_is_shared():
    from bs4 import BeautifulSoup

    from epub_purifier.opf import PackageDocument, has_valid_modified_timestamp

    for value in ["2023-02-29T00:00:00Z", "2024-13-01T00:00:00Z", "2024-01-01T24:00:00Z", "2024-01-01T00:60:00Z", "2024-01-01T00:00:60Z", "0000-01-01T00:00:00Z", "2024-1-01T00:00:00Z", ""]:
        data = _rewrite(simple_book(CHAPTER_TWO), "OEBPS/content.opf", lambda content: content.replace(b"2020-01-01T00:00:00Z", value.encode()))
        pkg = PackageDocument("OEBPS/content.opf", BeautifulSoup(_content(data, "OEBPS/content.opf"), "xml"), "3.0")
        assert not has_valid_modified_timestamp(pkg), value
        assert any("dcterms:modified" in problem for problem in verify_epub(data)), value


def test_missing_ordinary_content_respects_reference_flag():
    data = _rewrite(simple_book(CHAPTER_TWO), "OEBPS/content.opf", lambda value: value.replace(b'href="chapter1.xhtml"', b'href="absent.xhtml"'))
    assert verify_epub(data) == []
    assert any("absent.xhtml" in problem for problem in verify_epub(data, check_references=True))
    data = _rewrite(data, "OEBPS/content.opf", lambda value: value.replace(b'href="nav.xhtml"', b'href="absent-nav.xhtml"'))
    assert any("navigation document missing" in problem for problem in verify_epub(data))


def test_navigation_requires_namespaced_toc_token():
    for attribute, valid in [
        ('epub:type="landmarks toc"', True),
        ('epub:type="landmarks"', False),
        ('type="toc"', False),
        ('epub:type="toc-other"', False),
    ]:
        data = _rewrite(simple_book(CHAPTER_TWO), "OEBPS/nav.xhtml", lambda value: value.replace(b'epub:type="toc"', attribute.encode()))
        problems = verify_epub(data)
        assert bool(any("no table of contents" in problem for problem in problems)) != valid
