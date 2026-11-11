"""Output checks: XHTML serialization and EPUB 3 package requirements."""

from __future__ import annotations

import io
import zipfile

from lxml import etree

from epub_purifier import clean_epub
from epub_purifier.repackage import verify_epub

from .conftest import CHAPTER_TWO, CONTAINER, NAV, build_epub, simple_book

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
    assert f'xmlns="{XHTML}"'.encode() in output
    assert b"<br/>" in output
    assert b"</br>" not in output
    assert b"\xc2\xa0" in output

    root = etree.fromstring(output)  # noqa: S320 - output produced by this test
    assert root.tag == f"{{{XHTML}}}html"
    assert verify_epub(result.data) == []


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
