"""Stage 1 behaviour: what we accept, what we refuse, and why."""

from __future__ import annotations

import pytest

from epub_purifier.archive import SafeArchive
from epub_purifier.config import Limits, PurifierConfig
from epub_purifier.errors import ArchiveError, DrmProtectedError, ValidationError
from epub_purifier.models import Severity
from epub_purifier.validation import validate

from .conftest import CHAPTER_TWO, CONTAINER, MIMETYPE, build_epub


def test_accepts_epub3(sample_epub):
    result = validate(sample_epub)
    assert result.version.startswith("3.")
    assert result.package.entry == "OEBPS/content.opf"
    assert len(result.package.spine_items()) == 3


def test_accepts_epub2():
    opf = """<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="2.0" unique-identifier="bookid">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:title>Old Book</dc:title>
    <dc:identifier id="bookid">old-book</dc:identifier>
  </metadata>
  <manifest>
    <item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>
    <item id="ch1" href="chapter1.xhtml" media-type="application/xhtml+xml"/>
  </manifest>
  <spine toc="ncx"><itemref idref="ch1"/></spine>
</package>"""
    ncx = """<?xml version="1.0" encoding="utf-8"?>
<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">
  <head><meta name="dtb:uid" content="old-book"/></head>
  <docTitle><text>Old Book</text></docTitle>
  <navMap><navPoint id="n1" playOrder="1"><navLabel><text>One</text></navLabel>
  <content src="chapter1.xhtml"/></navPoint></navMap>
</ncx>"""
    data = build_epub(
        [
            ("META-INF/container.xml", CONTAINER),
            ("OEBPS/content.opf", opf),
            ("OEBPS/toc.ncx", ncx),
            ("OEBPS/chapter1.xhtml", CHAPTER_TWO),
        ]
    )
    result = validate(data)
    assert result.version == "2.0"
    assert result.package.ncx_entry() == "OEBPS/toc.ncx"


def test_rejects_non_zip():
    with pytest.raises(ArchiveError):
        validate(b"this is definitely not a zip file")


def test_rejects_empty_input():
    with pytest.raises(ArchiveError):
        validate(b"")


def test_rejects_missing_container():
    data = build_epub([("OEBPS/chapter1.xhtml", CHAPTER_TWO)])
    with pytest.raises(ValidationError):
        validate(data)


def test_rejects_empty_spine():
    opf = """<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>x</dc:title></metadata>
  <manifest><item id="ch1" href="chapter1.xhtml" media-type="application/xhtml+xml"/></manifest>
  <spine></spine>
</package>"""
    data = build_epub(
        [
            ("META-INF/container.xml", CONTAINER),
            ("OEBPS/content.opf", opf),
            ("OEBPS/chapter1.xhtml", CHAPTER_TWO),
        ]
    )
    with pytest.raises(ValidationError):
        validate(data)


def test_reports_missing_mimetype():
    data = build_epub(
        [
            ("META-INF/container.xml", CONTAINER),
            ("OEBPS/content.opf", _minimal_opf()),
            ("OEBPS/chapter1.xhtml", CHAPTER_TWO),
        ],
        mimetype=False,
    )
    result = validate(data)
    assert "epub.mimetype_missing" in {finding.rule for finding in result.findings}


def test_refuses_drm(drm_epub):
    with pytest.raises(DrmProtectedError):
        validate(drm_epub)


def test_detects_path_traversal(traversal_epub):
    result = validate(traversal_epub)
    unsafe = [f for f in result.findings if f.rule == "archive.unsafe_member"]
    assert len(unsafe) == 2
    assert all(f.severity is Severity.CRITICAL for f in unsafe)
    assert all(f.drops_entry for f in unsafe)


def test_zip_bomb_rejected(zip_bomb_epub):
    config = PurifierConfig(limits=Limits(max_total_uncompressed=1_000_000))
    with pytest.raises(ArchiveError) as excinfo:
        validate(zip_bomb_epub, config)
    assert "limit" in str(excinfo.value)


def test_entry_count_limit(sample_epub):
    config = PurifierConfig(limits=Limits(max_entries=3))
    with pytest.raises(ArchiveError):
        validate(sample_epub, config)


def test_executable_member_flagged(malicious_epub):
    result = validate(malicious_epub)
    exe = [f for f in result.findings if f.rule == "archive.executable"]
    assert exe and exe[0].severity is Severity.CRITICAL
    assert exe[0].drops_entry == "OEBPS/tool.exe"


def test_duplicate_members_deduplicated():
    data = build_epub(
        [
            ("META-INF/container.xml", CONTAINER),
            ("OEBPS/content.opf", _minimal_opf()),
            ("OEBPS/chapter1.xhtml", CHAPTER_TWO),
            ("OEBPS/chapter1.xhtml", "<html><body>second copy</body></html>"),
        ]
    )
    archive = SafeArchive(data)
    assert archive.duplicate_members == ["OEBPS/chapter1.xhtml"]
    assert archive.read_text("OEBPS/chapter1.xhtml").strip().startswith("<?xml")


def test_mimetype_bytes_are_correct(sample_epub):
    archive = SafeArchive(sample_epub)
    assert archive.read("mimetype") == MIMETYPE
    assert archive.names[0] == "mimetype"


def test_empty_archive_rejected():
    data = build_epub([])
    with pytest.raises(ValidationError):
        validate(data)


def _minimal_opf() -> str:
    return """<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="id">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:identifier id="id">min</dc:identifier><dc:title>Minimal</dc:title>
  </metadata>
  <manifest><item id="ch1" href="chapter1.xhtml" media-type="application/xhtml+xml"/></manifest>
  <spine><itemref idref="ch1"/></spine>
</package>"""
