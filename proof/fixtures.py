"""Fresh EPUB fixtures for the independent proof.

These builders are deliberately standalone: they do not import anything from
``tests/`` and they assemble every byte themselves, so the proof does not inherit
any assumption from the unit-test suite.

Run ``python proof/prove.py`` to build them and execute the scenarios.
"""

from __future__ import annotations

import io
import os
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "proof" / "fixtures"
WORK = ROOT / "proof" / "work"

MIMETYPE = b"application/epub+zip"

CONTAINER = """<?xml version="1.0" encoding="utf-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles>
    <rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>"""


def zip_bytes(entries: list[tuple[str, bytes | str]]) -> bytes:
    """Write a ZIP with a correct leading mimetype member."""

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        info = zipfile.ZipInfo("mimetype")
        info.compress_type = zipfile.ZIP_STORED
        archive.writestr(info, MIMETYPE)
        for name, data in entries:
            if isinstance(data, str):
                data = data.encode("utf-8")
            archive.writestr(name, data)
    return buffer.getvalue()


# ---------------------------------------------------------------------------
# Scenario 1 — EPUB 2 with Ocean-of-PDF insertions
# ---------------------------------------------------------------------------
S1_OPF = """<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="2.0" unique-identifier="BookId">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:opf="http://www.idpf.org/2007/opf">
    <dc:title>The Analog Church</dc:title>
    <dc:creator opf:role="aut">Jay Y. Kim</dc:creator>
    <dc:language>en</dc:language>
    <dc:identifier id="BookId">urn:uuid:6f2a1f7e-1c2b-4a9d-8f3e-1b2c3d4e5f60</dc:identifier>
    <dc:publisher>OceanofPDF Free Library</dc:publisher>
    <dc:description>Downloaded from OceanofPDF.com - free ebooks every day.</dc:description>
  </metadata>
  <manifest>
    <item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>
    <item id="css" href="styles/book.css" media-type="text/css"/>
    <item id="cover" href="text/cover.xhtml" media-type="application/xhtml+xml"/>
    <item id="ch1" href="text/chapter1.xhtml" media-type="application/xhtml+xml"/>
    <item id="promo" href="text/promo.xhtml" media-type="application/xhtml+xml"/>
  </manifest>
  <spine toc="ncx">
    <itemref idref="cover"/>
    <itemref idref="ch1"/>
    <itemref idref="promo"/>
  </spine>
  <guide>
    <reference type="cover" title="Cover" href="text/cover.xhtml"/>
    <reference type="text" title="Chapter One" href="text/chapter1.xhtml"/>
    <reference type="other.promo" title="OceanofPDF Free Library" href="text/promo.xhtml"/>
  </guide>
</package>"""

S1_NCX = """<?xml version="1.0" encoding="utf-8"?>
<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">
  <head><meta name="dtb:uid" content="urn:uuid:6f2a1f7e-1c2b-4a9d-8f3e-1b2c3d4e5f60"/></head>
  <docTitle><text>The Analog Church</text></docTitle>
  <navMap>
    <navPoint id="np1" playOrder="1">
      <navLabel><text>Cover</text></navLabel>
      <content src="text/cover.xhtml"/>
    </navPoint>
    <navPoint id="np2" playOrder="2">
      <navLabel><text>Chapter One</text></navLabel>
      <content src="text/chapter1.xhtml"/>
    </navPoint>
    <navPoint id="np3" playOrder="3">
      <navLabel><text>OceanofPDF free library</text></navLabel>
      <content src="https://www.oceanofpdf.com"/>
    </navPoint>
  </navMap>
</ncx>"""

S1_CSS = """@charset "utf-8";

p.lead {
  font-style: italic;
}

p.promo-footer::after {
  content: "OceanofPDF";
}

body {
  font-family: Georgia, serif;
}
"""

S1_COVER = """<?xml version="1.0" encoding="utf-8"?>
<html xmlns="http://www.w3.org/1999/xhtml">
<head><title>Cover</title><link rel="stylesheet" type="text/css" href="../styles/book.css"/></head>
<body>
  <h1>The Analog Church</h1>
  <p>Jay Y. Kim</p>
</body>
</html>"""

S1_CHAPTER = """<?xml version="1.0" encoding="utf-8"?>
<html xmlns="http://www.w3.org/1999/xhtml">
<head><title>Chapter One</title><link rel="stylesheet" type="text/css" href="../styles/book.css"/></head>
<body>
  <h1>Chapter One</h1>
  <p class="lead">The church has always been analog: real people, real places, and real things.</p>
  <p>Screens are not evil, but they are not neutral either.</p>
  <p>Readers who want to compare translations will find the discussion in the appendix useful, and the notes below point to further reading, including a catalogue maintained at <a href="https://www.oceanofpdf.com">OceanofPDF</a>.</p>
  <div class="promo-footer">
    <p>Downloaded from OceanofPDF.com - support us by visiting the site.</p>
  </div>
</body>
</html>"""

S1_PROMO = """<?xml version="1.0" encoding="utf-8"?>
<html xmlns="http://www.w3.org/1999/xhtml">
<head><title>Free books</title></head>
<body>
  <h1>Free Library</h1>
  <p>Download more free books at OceanofPDF.com</p>
  <p><a href="https://www.oceanofpdf.com">OceanofPDF</a></p>
</body>
</html>"""


def epub2_branded() -> bytes:
    return zip_bytes(
        [
            ("META-INF/container.xml", CONTAINER),
            ("OEBPS/content.opf", S1_OPF),
            ("OEBPS/toc.ncx", S1_NCX),
            ("OEBPS/styles/book.css", S1_CSS),
            ("OEBPS/text/cover.xhtml", S1_COVER),
            ("OEBPS/text/chapter1.xhtml", S1_CHAPTER),
            ("OEBPS/text/promo.xhtml", S1_PROMO),
        ]
    )


# ---------------------------------------------------------------------------
# Scenario 2 — legitimate mentions that must survive
# ---------------------------------------------------------------------------
S2_OPF = """<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="uid">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:identifier id="uid">urn:uuid:11111111-2222-3333-4444-555555555555</dc:identifier>
    <dc:title>Notes on Sources</dc:title>
    <dc:creator>An Author</dc:creator>
    <dc:language>en</dc:language>
    <meta property="dcterms:modified">2024-02-02T00:00:00Z</meta>
  </metadata>
  <manifest>
    <item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>
    <item id="ch1" href="text/notes.xhtml" media-type="application/xhtml+xml"/>
  </manifest>
  <spine><itemref idref="ch1"/></spine>
</package>"""

S2_NAV = """<?xml version="1.0" encoding="utf-8"?>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">
<head><title>Contents</title></head>
<body><nav epub:type="toc"><ol>
  <li><a href="text/notes.xhtml">Notes on Sources</a></li>
</ol></nav></body>
</html>"""

S2_CHAPTER = """<?xml version="1.0" encoding="utf-8"?>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">
<head><title>Notes on Sources</title></head>
<body>
  <h1>Notes on Sources</h1>
  <p>The ocean stretched out before us, wide and indifferent. Later that evening I opened the PDF on my laptop and read until midnight.</p>
  <blockquote><p>As the editors at OceanofPDF put it, digitisation changes how a page is read.</p></blockquote>
  <aside epub:type="footnote" class="footnote"><p>Source: oceanofpdf.com, accessed 12 March 2020.</p></aside>
  <p>This copy was downloaded from OceanofPDF and is shared for review.</p>
  <p>The chapter closes with ordinary prose about liturgy and attention.</p>
</body>
</html>"""


def epub3_legit_mentions() -> bytes:
    return zip_bytes(
        [
            ("META-INF/container.xml", CONTAINER),
            ("OEBPS/content.opf", S2_OPF),
            ("OEBPS/nav.xhtml", S2_NAV),
            ("OEBPS/text/notes.xhtml", S2_CHAPTER),
        ]
    )


# ---------------------------------------------------------------------------
# Scenario 3 — hostile archive
# ---------------------------------------------------------------------------
S3_CHAPTER = """<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html [ <!ENTITY xxe SYSTEM "file:///etc/passwd"> ]>
<html xmlns="http://www.w3.org/1999/xhtml">
<head>
  <title>Chapter One</title>
  <link rel="stylesheet" type="text/css" href="https://cdn.example.org/imported.css"/>
</head>
<body onload="boot()">
  <h1>Chapter One</h1>
  <p>Ordinary paragraph that should survive.</p>
  <img src="https://tracker.example/pixel.gif" width="1" height="1" alt=""/>
  <a href="javascript:alert(1)">click</a>
  <img src="plate.png" onerror="alert(2)" alt="plate"/>
  <iframe src="https://evil.example/frame"></iframe>
  <script>document.write('owned')</script>
  <p>Entity probe: &xxe;</p>
</body>
</html>"""

S3_SVG = """<?xml version="1.0" encoding="utf-8"?>
<svg xmlns="http://www.w3.org/2000/svg" width="20" height="20">
  <script>alert(3)</script>
  <rect width="20" height="20" fill="#ccc"/>
</svg>"""

S3_CSS = """@import url("https://cdn.example.org/imported.css");

body {
  font-family: Georgia, serif;
  background: url(https://cdn.example.org/bg.png);
}
"""

S3_OPF = """<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="uid">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:identifier id="uid">urn:uuid:99999999-8888-7777-6666-555555555555</dc:identifier>
    <dc:title>Hostile Book</dc:title>
    <dc:language>en</dc:language>
    <meta property="dcterms:modified">2024-03-03T00:00:00Z</meta>
  </metadata>
  <manifest>
    <item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>
    <item id="ch1" href="text/chapter1.xhtml" media-type="application/xhtml+xml"/>
    <item id="art" href="art/logo.svg" media-type="image/svg+xml"/>
    <item id="plate" href="text/plate.png" media-type="image/png"/>
    <item id="css" href="styles/book.css" media-type="text/css"/>
  </manifest>
  <spine><itemref idref="ch1"/></spine>
</package>"""

S3_NAV = """<?xml version="1.0" encoding="utf-8"?>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">
<head><title>Contents</title></head>
<body><nav epub:type="toc"><ol>
  <li><a href="text/chapter1.xhtml">Chapter One</a></li>
</ol></nav></body>
</html>"""


def _plate_png() -> bytes:
    """A real illustration; the hostile part of the fixture is the event handler."""

    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (8, 8), (200, 200, 200)).save(buffer, format="PNG")
    return buffer.getvalue()


def _nested_zip() -> bytes:
    return zip_bytes([("README.txt", b"nested payload")])


def _oversized_pdf() -> bytes:
    # Incompressible so the archive looks like a normal large book, not a bomb.
    return b"%PDF-1.4\n" + os.urandom(2 * 1024 * 1024)


def _bomb_member() -> bytes:
    return b"\x00" * (30 * 1024 * 1024)


def hostile(include_bomb: bool) -> bytes:
    entries: list[tuple[str, bytes | str]] = [
        ("../escape.html", "<html><body>escaped</body></html>"),
        ("/absolute/evil.xhtml", "<html><body>absolute</body></html>"),
        ("OEBPS/../../deep.txt", "escaped"),
        ("META-INF/container.xml", CONTAINER),
        ("OEBPS/content.opf", S3_OPF),
        ("OEBPS/nav.xhtml", S3_NAV),
        ("OEBPS/text/chapter1.xhtml", S3_CHAPTER),
        ("OEBPS/art/logo.svg", S3_SVG),
        ("OEBPS/text/plate.png", _plate_png()),
        ("OEBPS/styles/book.css", S3_CSS),
        ("OEBPS/extras/bonus.zip", _nested_zip()),
        ("OEBPS/extras/companion.pdf", _oversized_pdf()),
    ]
    if include_bomb:
        entries.append(("OEBPS/extras/bomb.bin", _bomb_member()))
    return zip_bytes(entries)


# ---------------------------------------------------------------------------
# Scenario 4 — DRM and signature handling
# ---------------------------------------------------------------------------
DRM_ENCRYPTION = """<?xml version="1.0" encoding="utf-8"?>
<encryption xmlns="urn:oasis:names:tc:opendocument:xmlns:container"
            xmlns:enc="http://www.w3.org/2001/04/xmlenc#">
  <enc:EncryptedData>
    <enc:EncryptionMethod Algorithm="http://www.w3.org/2001/04/xmlenc#aes256-cbc"/>
    <enc:CipherData><enc:CipherReference URI="OEBPS/text/chapter1.xhtml"/></enc:CipherData>
  </enc:EncryptedData>
</encryption>"""

SIGNATURES = """<?xml version="1.0" encoding="utf-8"?>
<signatures xmlns="http://www.w3.org/2000/09/xmldsig#"/>
"""

S4_CHAPTER = """<?xml version="1.0" encoding="utf-8"?>
<html xmlns="http://www.w3.org/1999/xhtml">
<head><title>Chapter One</title></head>
<body>
  <h1>Chapter One</h1>
  <p>This paragraph must survive any cleaning.</p>
  <div class="chapter-footer"><p>Downloaded from OceanofPDF.com - free ebooks.</p></div>
</body>
</html>"""

S4_OPF = """<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="uid">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:identifier id="uid">urn:uuid:aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee</dc:identifier>
    <dc:title>Signed Book</dc:title>
    <dc:language>en</dc:language>
    <meta property="dcterms:modified">2024-04-04T00:00:00Z</meta>
  </metadata>
  <manifest>
    <item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>
    <item id="ch1" href="text/chapter1.xhtml" media-type="application/xhtml+xml"/>
  </manifest>
  <spine><itemref idref="ch1"/></spine>
</package>"""

S4_NAV = """<?xml version="1.0" encoding="utf-8"?>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">
<head><title>Contents</title></head>
<body><nav epub:type="toc"><ol><li><a href="text/chapter1.xhtml">Chapter One</a></li></ol></nav></body>
</html>"""


def drm_book() -> bytes:
    return zip_bytes(
        [
            ("META-INF/container.xml", CONTAINER),
            ("META-INF/encryption.xml", DRM_ENCRYPTION),
            ("OEBPS/content.opf", S4_OPF),
            ("OEBPS/nav.xhtml", S4_NAV),
            ("OEBPS/text/chapter1.xhtml", S4_CHAPTER),
        ]
    )


def signed_book() -> bytes:
    return zip_bytes(
        [
            ("META-INF/container.xml", CONTAINER),
            ("META-INF/signatures.xml", SIGNATURES),
            ("OEBPS/content.opf", S4_OPF),
            ("OEBPS/nav.xhtml", S4_NAV),
            ("OEBPS/text/chapter1.xhtml", S4_CHAPTER),
        ]
    )


S4_CLEAN_CHAPTER = """<?xml version="1.0" encoding="utf-8"?>
<html xmlns="http://www.w3.org/1999/xhtml">
<head><title>Chapter One</title></head>
<body>
  <h1>Chapter One</h1>
  <p>This book has no insertions at all, and should produce no findings.</p>
  <p>Ordinary prose continues for a second paragraph.</p>
</body>
</html>"""


def clean_book() -> bytes:
    """A book with nothing to report: used to test the wording of an empty report."""

    return zip_bytes(
        [
            ("META-INF/container.xml", CONTAINER),
            ("OEBPS/content.opf", S4_OPF),
            ("OEBPS/nav.xhtml", S4_NAV),
            ("OEBPS/text/chapter1.xhtml", S4_CLEAN_CHAPTER),
        ]
    )


def build_all() -> dict[str, Path]:
    """Write every fixture and return name -> path."""

    FIXTURES.mkdir(parents=True, exist_ok=True)
    WORK.mkdir(parents=True, exist_ok=True)
    files = {
        "s1-epub2-branded.epub": epub2_branded(),
        "s2-legit-mentions.epub": epub3_legit_mentions(),
        "s3-hostile.epub": hostile(include_bomb=True),
        "s3-hostile-no-bomb.epub": hostile(include_bomb=False),
        "s4-drm.epub": drm_book(),
        "s4-signed.epub": signed_book(),
        "s4-clean.epub": clean_book(),
    }
    paths: dict[str, Path] = {}
    for name, data in files.items():
        path = FIXTURES / name
        path.write_bytes(data)
        paths[name] = path
    return paths


if __name__ == "__main__":  # pragma: no cover
    for name, path in build_all().items():
        print(f"{name}: {path} ({path.stat().st_size} bytes)")
