"""A realistic book after an Ocean-of-PDF-style promotional injection.

Built in code, never downloaded: three chapters of ordinary prose, a figure
referenced from the manifest, working navigation and a stylesheet, plus the
promotional layer a re-packaging site bolts on — repeated footers, a header
banner, standalone blurbs, a dedicated promo page, links, a script, a tracking
pixel, and branding in the OPF metadata and CSS.

The prose deliberately includes a discussion of the site and a footnote citing
it: those are legitimate and must survive every clean.
"""

from __future__ import annotations

from .conftest import CONTAINER, build_epub, opf_package, png_bytes

CHAPTER_ONE = """<?xml version="1.0" encoding="utf-8"?>
<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="en">
<head><title>Chapter One</title><link rel="stylesheet" type="text/css" href="styles.css"/></head>
<body>
  <h1>Chapter One: The Room</h1>
  <p>The room shapes what happens in it: low light, hard chairs, a window that gives onto the street.</p>
  <p>Nobody designed the acoustics on purpose, and yet the building teaches everyone how to listen.</p>
  <figure>
    <img src="images/plate.png" alt="A quiet sanctuary with wooden pews"/>
    <figcaption>Plate 1. The sanctuary, before the renovation.</figcaption>
  </figure>
  <p>For a different edition of this argument, see the
    <a href="https://www.gutenberg.org/ebooks/1342">Project Gutenberg text</a>.</p>
  <p>Downloaded from OceanofPDF.com - free ebooks every day.</p>
  <script>alert(document.cookie)</script>
</body>
</html>"""

CHAPTER_TWO = """<?xml version="1.0" encoding="utf-8"?>
<html xmlns="http://www.w3.org/1999/xhtml">
<head><title>Chapter Two</title><link rel="stylesheet" type="text/css" href="styles.css"/></head>
<body>
  <header class="ocean-banner"><p>Ocean of PDF</p></header>
  <h1>Chapter Two: Attention</h1>
  <p>Attention is a discipline before it is a feeling, and every room asks for a different kind of it.</p>
  <p>Visit us at www.oceanofpdf.com for more books.</p>
  <img src="https://www.google-analytics.com/collect?utm_source=book" width="1" height="1" alt=""/>
</body>
</html>"""

CHAPTER_THREE = """<?xml version="1.0" encoding="utf-8"?>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">
<head><title>Chapter Three</title><link rel="stylesheet" type="text/css" href="styles.css"/></head>
<body>
  <h1>Chapter Three: Sources</h1>
  <p>As the editors at OceanofPDF observe, digitisation changes how a page is read: the scroll
     replaces the turn, the bookmark replaces the dog-ear, and the sense of progress becomes a
     percentage rather than a stack of paper. Their argument deserves more attention than it
     usually receives in discussions of reading.</p>
  <aside epub:type="footnote" class="footnote"><p>Source: oceanofpdf.com, accessed 12 March 2020.</p></aside>
  <p>Download more free books at OceanofPDF.com</p>
</body>
</html>"""

PROMO_PAGE = """<?xml version="1.0" encoding="utf-8"?>
<html xmlns="http://www.w3.org/1999/xhtml">
<head><title>Free Library</title><link rel="stylesheet" type="text/css" href="styles.css"/></head>
<body>
  <h1>Free Library</h1>
  <p>Download thousands of free ebooks at OceanofPDF.com.</p>
  <p><a href="https://www.oceanofpdf.com/library">OceanofPDF free library</a></p>
</body>
</html>"""

NAV = """<?xml version="1.0" encoding="utf-8"?>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">
<head><title>Contents</title></head>
<body>
  <nav epub:type="toc" id="toc">
    <h1>Contents</h1>
    <ol>
      <li><a href="chapter1.xhtml">Chapter One: The Room</a></li>
      <li><a href="chapter2.xhtml">Chapter Two: Attention</a></li>
      <li><a href="chapter3.xhtml">Chapter Three: Sources</a></li>
      <li><a href="free-library.xhtml">OceanofPDF free library</a></li>
    </ol>
  </nav>
</body>
</html>"""

STYLES = """@charset "utf-8";

body {
  font-family: Georgia, serif;
  margin: 1em;
}

figure img {
  max-width: 100%;
}

p.ocean-note::after {
  content: "OceanofPDF";
  color: #777;
}
"""

MANIFEST = "\n    ".join(
    [
        '<item id="ch1" href="chapter1.xhtml" media-type="application/xhtml+xml"/>',
        '<item id="ch2" href="chapter2.xhtml" media-type="application/xhtml+xml"/>',
        '<item id="ch3" href="chapter3.xhtml" media-type="application/xhtml+xml"/>',
        '<item id="free" href="free-library.xhtml" media-type="application/xhtml+xml"/>',
        '<item id="css" href="styles.css" media-type="text/css"/>',
        '<item id="plate" href="images/plate.png" media-type="image/png"/>',
    ]
)

SPINE = "\n    ".join(
    [
        '<itemref idref="ch1"/>',
        '<itemref idref="ch2"/>',
        '<itemref idref="ch3"/>',
        '<itemref idref="free"/>',
    ]
)

#: opf_package() already emits title/creator/language; this adds the rest.
METADATA = """<dc:publisher>InterVarsity Press</dc:publisher>
    <dc:description>Downloaded from OceanofPDF.com - free ebooks every day.</dc:description>"""


def converted_book() -> bytes:
    """Return the fixture as EPUB bytes."""

    return build_epub(
        [
            ("META-INF/container.xml", CONTAINER),
            (
                "OEBPS/content.opf",
                opf_package(
                    manifest=MANIFEST,
                    spine=SPINE,
                    metadata=METADATA,
                    version="3.0",
                ),
            ),
            ("OEBPS/nav.xhtml", NAV),
            ("OEBPS/chapter1.xhtml", CHAPTER_ONE),
            ("OEBPS/chapter2.xhtml", CHAPTER_TWO),
            ("OEBPS/chapter3.xhtml", CHAPTER_THREE),
            ("OEBPS/free-library.xhtml", PROMO_PAGE),
            ("OEBPS/styles.css", STYLES),
            ("OEBPS/images/plate.png", png_bytes((40, 40))),
        ]
    )


#: Strings the promotional layer injected; none may survive a clean.
PROMO_MARKERS = (
    "Downloaded from OceanofPDF.com",
    "Visit us at www.oceanofpdf.com",
    "Download more free books",
    "OceanofPDF free library",
    "Download thousands of free ebooks",
    "ocean-banner",
    "google-analytics.com",
    "<script",
    'content: "OceanofPDF"',
)

#: Strings that belong to the book and must survive a clean.
LEGIT_MARKERS = (
    "The room shapes what happens in it",
    "Attention is a discipline before it is a feeling",
    "digitisation changes how a page is read",
    "Source: oceanofpdf.com, accessed 12 March 2020.",
    "Project Gutenberg text",
)
