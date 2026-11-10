# EPUB Purifier

[![CI](https://github.com/epub-purifier/epub-purifier/actions/workflows/ci.yml/badge.svg)](https://github.com/epub-purifier/epub-purifier/actions/workflows/ci.yml)

**Clean EPUB files. Keep the book.**

EPUB Purifier removes third-party promotional insertions from EPUB files — the
cover banners, "downloaded from …" footers, injected promo pages, tracking
pixels and scripts that get bolted onto books by scraping and re-packaging
sites — while leaving the actual text, chapter structure, styles, illustrations
and navigation untouched.

It is also a scanner: every uploaded book is treated as hostile input and
checked for active content, tracking infrastructure and archive-level attacks,
with findings ranked **critical → high → medium → low → informational**.

> **Scope and legality.** Only process files you own or are authorised to
> modify. EPUB Purifier does not remove DRM, does not bypass access controls,
> and refuses encrypted books instead of guessing.

---

## Why this exists

Books re-packaged by third-party sites routinely arrive with:

- a branded footer paragraph appended to every chapter;
- an extra "download more free books" page added to the spine;
- the publisher's description replaced by a promotional blurb;
- CSS `::after { content: "…" }` watermarks;
- 1×1 tracking images, analytics beacons and `onerror` payloads;
- scripts, iframes and `javascript:` links that some readers will execute.

Deleting those by hand means unzipping, editing XML and re-zipping without
breaking the container. Doing it automatically usually means over-eager regex
that also deletes a legitimate citation. This project is the careful version:
DOM-aware removal, explicit confidence scores, and a review step for anything
ambiguous.

---

## What it does

| Capability | Detail |
| --- | --- |
| EPUB 2 + EPUB 3 | OPF 2.0 and 3.0, NCX and EPUB 3 nav documents |
| Validation | ZIP integrity, `mimetype` placement, container/OPF structure, DRM refusal |
| Branding detection | Text blocks, footers, headings, links, images, metadata, CSS content, nav entries, dedicated promo pages |
| Promo removal | Element-level removal with empty-wrapper pruning, so the chapter survives |
| Security scanning | Scripts, event handlers, `javascript:`/data URIs, iframes, objects, forms, meta refresh, tracking pixels, tracker hosts, SVG abuse, XXE, executables, traversal, ZIP bombs |
| Confidence model | Every branding finding carries a score; only high-confidence items are removed automatically |
| Repackaging | Spec-compliant ZIP (stored `mimetype` first), reference pruning, `dcterms:modified` refresh |
| Reporting | Human-readable text and machine-readable JSON, with explicit "this is not a safety guarantee" language |

## What it will not do

- **No DRM removal.** Books with `META-INF/rights.xml` or non-font encryption are rejected.
- **No OCR.** Branding burned into a JPEG is only detectable through file name, `alt` text and surrounding markup.
- **No anti-malware engine.** Findings are structural and heuristic; nothing is compared against a virus database.
- **No network access.** The scanner never fetches a remote resource, follows a link, or expands an XML entity.
- **No silent edits to ambiguous content.** Citations, quotations and prose that merely mention a site are reported, not removed.

---

## Install

Python 3.10+.

```bash
git clone https://github.com/epub-purifier/epub-purifier.git
cd epub-purifier
python -m venv .venv && source .venv/bin/activate
pip install -e ".[web]"      # drop [web] for the CLI only
```

## Quick start

```bash
# 1. Look before you touch anything
epub-purifier inspect "Analog Church.epub"

# 2. Machine-readable report
epub-purifier inspect "Analog Church.epub" --format json > report.json

# 3. Clean it
epub-purifier clean "Analog Church.epub" -o "Analog Church.cleaned.epub" \
    --report clean-report.json

# 4. Review the ambiguous items the first pass left alone
epub-purifier clean "Analog Church.epub" -o out.epub --approve f3a91c02b7d4e6a1
```

Interactive runs list ambiguous findings and ask once whether to remove them.
Non-interactive runs (CI, pipes) keep them and tell you which ids to approve.

### CLI reference

```
epub-purifier inspect FILE [--format text|json] [--json PATH] [--fail-on LEVEL]
                          [--min-confidence 0..1] [--review-confidence 0..1]
epub-purifier clean   FILE [-o OUTPUT] [--report PATH] [--json] [--dry-run]
                          [--approve ID]... [--approve-all]
                          [--remove-remote] [--keep-promo-pages]
                          [--min-confidence 0..1] [--review-confidence 0..1]
```

Exit codes: `0` success, `1` findings above `--fail-on`, `2` usage/config error,
`3` invalid or unreadable EPUB, `4` DRM-protected input, `5` rebuild failure.

### Web UI

```bash
epub-purifier-web          # http://127.0.0.1:8000
```

Upload → review the report → tick any ambiguous items → clean → download.
The server binds to loopback by default (`EPUB_PURIFIER_HOST`,
`EPUB_PURIFIER_PORT`), keeps uploads in a private temp directory, deletes each
job after 30 minutes (`EPUB_PURIFIER_JOB_TTL`), and serves a strict
`default-src 'self'` CSP with no external assets.

---

## Search and safe downloads

The web UI can search seven sources by title, author or ISBN and compare what
each one offers:

| Source | Search | Downloads |
|---|---|---|
| Project Gutenberg | official OPDS catalogue | EPUB (confirmed on fetch), public domain — fetched, scanned and cleaned here |
| Wikisource | MediaWiki search API | EPUB built on demand by Wikimedia's WS Export; public domain and freely licensed — fetched, scanned and cleaned here |
| Internet Archive | advanced-search API | EPUB for openly downloadable items, including Gutenberg mirrors; lending copies are refused before any file is requested |
| Open Library | official search API, filtered to public scans | EPUB downloaded from Internet Archive for works marked `public`; the item metadata is re-checked and lending copies are refused |
| Standard Ebooks | official OPDS search feed | EPUB from the entry's own open-access acquisition link; standardebooks.org-only allowlist, size cap and EPUB checks before cleaning |
| Anna's Archive | link to its search page | none: no authorised public API |
| Ocean of PDF | link to its search page | none: distribution rights cannot be verified |

Standard Ebooks is searched through its official OPDS feed. Its unfiltered
catalogue feeds (`/feeds/opds`, `/feeds/opds/all`) still answer `401` with a
Patrons Circle Basic realm, and those feeds are never used. The same feed serves
public search results at
`https://standardebooks.org/feeds/opds/all?query=…`, advertises an OpenSearch
description and links an equivalent public HTML search, so EPUB Purifier uses
only that documented query endpoint — no membership credentials, no HTML
scraping. Downloads come from the entry's own `application/epub+zip`
open-access acquisition link.

Anna's Archive and Ocean of PDF stay link-only on purpose. Neither can show that
a given file is distributed with the publisher's authorisation, so fetching their
catalogues automatically would turn EPUB Purifier into a downloader for
unauthorised copies, which this project's scope rules out. Cleaning is still
available for those books: download the file yourself in the browser, then use
the upload path — the same validation, scanning and cleaning pipeline runs on any
EPUB you supply, whatever its origin.

Results show the source, available formats, language and a cover image (proxied
through this server, so your browser never contacts the source). Only listings
with an authorised EPUB get a **Fetch, scan and clean** button.

For those, the server downloads the file into its own temporary directory, runs
the same validation and cleaning pipeline as `epub-purifier clean`, and only then
offers a download:

```bash
curl -s http://127.0.0.1:8000/api/search -H 'content-type: application/json' \
  -d '{"query": "pride and prejudice"}'
curl -s http://127.0.0.1:8000/api/transfer -H 'content-type: application/json' \
  -d '{"source": "gutenberg", "book_id": "1342"}'
```

- Files that fail archive validation (ZIP bombs, traversal members, DRM) or that
  contain executable payloads are rejected, and no cleaned copy is produced.
- Only the rebuilt, structurally verified EPUB is served, once. The raw download
  is deleted as soon as cleaning succeeds, the cleaned copy is deleted when it is
  downloaded (or after `EPUB_PURIFIER_TRANSFER_TTL` seconds), and the temporary
  root is removed on shutdown.
- Ambiguous removals are **not** auto-approved on this path: they remain `review`
  items in the report, so an automated transfer never deletes a possible
  citation.
- A scan pass is not a safety guarantee; the transfer response repeats the same
  disclaimer as the CLI report.

Outbound fetching is restricted to https, a per-source host allowlist and
globally routable addresses; redirects are re-checked hop by hop, and bodies are
size-capped while they are read. A source that does not authorise automated
downloads is never fetched from, even if asked.

Environment knobs: `EPUB_PURIFIER_TRANSFER_TTL` (seconds, default 900),
`EPUB_PURIFIER_MAX_TRANSFERS` (default 16), `EPUB_PURIFIER_MAX_DOWNLOAD_MB`
(default 64), `EPUB_PURIFIER_SEARCH_RATE_LIMIT` and
`EPUB_PURIFIER_TRANSFER_RATE_LIMIT` (requests per minute per client).

## How it works

```
                 ┌───────────────┐
   EPUB bytes ──►│ 1. Validation │  structure, budget, DRM, member hygiene
                 └──────┬────────┘
                        │  SafeArchive + PackageDocument
                 ┌──────▼────────┐
                 │ 2. Inspection │  every resource, every detector
                 └──────┬────────┘
                        │  findings + typed mutations (with confidence)
                 ┌──────▼────────┐
                 │ 3. Cleaning   │  approved mutations only, DOM-aware
                 └──────┬────────┘
                        │  pruned manifest/spine/guide/nav, rebuilt ZIP
                        ▼
                  cleaned EPUB + report
```

Each stage is a module you can call on its own:
`epub_purifier.validation.validate`, `epub_purifier.analysis.analyze`,
`epub_purifier.cleaning.clean_book`, or the convenience facade
`inspect_epub` / `clean_epub`.

### Inspection and cleaning never disagree

Detectors return **mutations** — a finding plus the concrete edit that would fix
it (remove this element, strip this attribute, drop this file). The report is
rendered from the findings; the cleaner applies the approved mutations. One pass,
one source of truth.

Finding ids are content-derived hashes, not random values, so an id shown by
`inspect` still identifies the same element during a later `clean` of the same
file. That is what makes the web review step and `--approve ID` work.

---

## The confidence model

A brand name by itself is never enough. Confidence is assembled from context:

| Signal | Weight |
| --- | --- |
| Brand pattern matched | base 0.45 |
| Short, standalone block (≤ 25 words) | +0.15 |
| Brand occupies ≥ 25 % of the block's text | +0.15 |
| Promotional wording ("download", "free ebooks", "visit us", …) | +0.15 |
| Promotional markup (`class="footer-note"`, `rel="sponsor"`, …) | +0.20 |
| In a footer/aside, or the last block on the page | +0.10 – 0.15 |
| Contains a link to the promotional domain | +0.15 |
| **Inside a quotation, citation, footnote or reference** | **−0.50** |
| Long prose (> 60 words) | −0.30 |
| Brand is incidental (< 8 % of the text) | −0.15 |

- **≥ 0.85** — removed automatically and listed in the report.
- **0.50 – 0.85** — kept until you confirm (`--approve`, `--approve-all`, or a tick in the UI).
- **< 0.50** — informational only; nothing is touched.

So `Downloaded from OceanofPDF.com - free ebooks every day.` scores 0.99 as the
last block on a page (0.90 when other content follows it) and is removed, while
`As the editors at OceanofPDF observe, digitisation changes how a page is read.`
inside a `<blockquote>` scores 0.10 and stays.

Quotations, citations and footnotes are additionally capped below
`review_confidence`: they are always reported with their evidence, but they can
never be offered for removal, however many other signals stack up around them.

Whole *pages* are only dropped when the document is almost entirely branding:
the page must not be reachable from the table of contents except through an
entry that is itself branding, must not look like
front matter (cover/title/copyright), must have no substantive heading, and
must be left with ≤ 200 characters and ≤ 2 content blocks once the branded
elements are removed. A short chapter that happens to end with a promo line
therefore survives.

---

## Detection rules

### Branding and promotion

| Rule | Severity | Action |
| --- | --- | --- |
| `branding.text_block` | high / medium / info | remove element, or report only |
| `branding.promo_link` | high | remove the link (and its `li`/`p` wrapper) |
| `branding.promo_link_target` | medium | strip `href`, keep the words |
| `branding.promo_image` | high | remove the image element |
| `branding.promo_image_alt` | low | clear the `alt` text |
| `branding.promo_image_file` | medium | drop an unreferenced branded image |
| `branding.inline_style` / `_attribute` | medium | rewrite or strip the declaration |
| `branding.css_generated_content` | high | remove the CSS declaration |
| `branding.css_url` | high | remove the declaration |
| `branding.metadata` | high | remove the metadata element |
| `branding.metadata_title` | medium | strip branding from `dc:title` |
| `branding.guide_reference` | high | remove the guide entry |
| `branding.navigation_entry` | high | remove the TOC/NCX entry |
| `branding.promo_page` | high | drop the whole document |

### Security

| Rule | Severity | Action |
| --- | --- | --- |
| `security.script` | critical | remove `<script>` |
| `security.event_handler` | high | strip `on*` attributes |
| `security.script_url` | high | strip `javascript:` / `vbscript:` URLs |
| `security.data_uri_active` | high | remove the element |
| `security.iframe`, `.object`, `.embed`, `.applet` | high | remove the element |
| `security.meta_refresh` | high | remove the redirect |
| `security.html_import` | high | remove the import |
| `security.css_expression` | high | strip the declaration/attribute |
| `security.xml_entities` | high | discard the doctype, never expand entities |
| `security.svg_external_use` | high | remove the referencing node |
| `security.form` / `.input` / `.base` | medium | remove the element |
| `security.svg_foreign_object` | low | report only |
| `tracking.pixel` | high | remove the image |
| `tracking.tracker_resource` | high | remove the element |
| `tracking.link` | medium | strip the link target |
| `tracking.css_url` / `.style_import` | high | remove the declaration |
| `external.remote_resource` | low | review, or auto-remove with `--remove-remote` |
| `external.manifest_item` | medium/high | review, then drop the manifest item |

### Archive

| Rule | Severity | Action |
| --- | --- | --- |
| `archive.unsafe_member` (traversal, absolute path, control chars) | critical | never written |
| `archive.symlink_member` | critical | never written |
| `archive.executable` | critical | dropped |
| `archive.structure_anomaly` | high | report only |
| `archive.duplicate_member` | medium | keep first |
| `archive.nested_archive` | medium | review |
| `archive.junk_file` (`__MACOSX`, `.DS_Store`, …) | low | dropped |
| `epub.mimetype_*` | medium/info | rewritten correctly |
| `epub.digital_signature` | info | reported (cleaning invalidates it) |
| `epub.font_obfuscation` | info | untouched |

---

## Configuration

`--config config.toml` (or `.json`) extends or replaces the defaults. See
[`config.example.toml`](config.example.toml) for every key.

```toml
brand_patterns = ["ocean\\s*of\\s*pdf", "oceanofpdf", "examplebooks"]
promo_domains = ["oceanofpdf.com", "examplebooks.com"]

auto_remove_confidence = 0.85   # remove at or above this score
review_confidence = 0.5         # report at or above this score
remove_remote_resources = false
neutralize_promo_links = true
clean_metadata = true
drop_promo_pages = true

[limits]
max_entries = 5000
max_total_uncompressed = 1073741824
max_compression_ratio = 120.0
```

Unknown keys are rejected rather than silently ignored.

## Report format

```json
{
  "tool": {"name": "epub-purifier", "version": "0.1.0"},
  "mode": "clean",
  "file": {"name": "book.epub", "size": 3998765, "epub_version": "3.0"},
  "output": {"name": "book.cleaned.epub", "size": 3991020, "sha256": "…"},
  "summary": {
    "counts": {"critical": 2, "high": 9, "medium": 4, "low": 3, "informational": 1},
    "highest_severity": "critical",
    "verdict": "Dangerous or unwanted content was detected and the flagged items were removed or neutralised.",
    "removed_count": 19
  },
  "findings": [{"id": "…", "rule": "branding.text_block", "severity": "high", "confidence": 0.99, "…": "…"}],
  "removed": [{"id": "…", "action": "remove_element", "target": "OEBPS/chapter1.xhtml"}],
  "disclaimer": "…absence of findings is NOT a guarantee that a file is safe…"
}
```

---

## Security model

| Threat | Mitigation |
| --- | --- |
| ZIP bomb / decompression amplification | total and per-entry uncompressed caps, compression-ratio limits checked before extraction |
| Path traversal / zip-slip | member names normalized, `..`/absolute/drive-letter/backslash/control characters rejected, unwritten members cannot escape |
| Symlinks and encrypted members | refused (encryption is treated as DRM) |
| Malicious XML (XXE, billion laughs) | `defusedxml` for container/encryption parsing; entity declarations are detected and the doctype is discarded **before** any parse; external DTDs are never loaded |
| Script execution | markup is parsed as data only; no `eval`, no subprocess, no browser, no network |
| Unexpected file types | executables, DLLs, scripts, nested archives flagged; executables dropped |
| Resource exhaustion | upload cap, entry cap, depth cap, per-read cap, bounded report sizes |
| Web exposure | loopback bind, upload size cap, rate limit, TTL job store, strict CSP, `nosniff`, no external assets, no filename-controlled paths |

Encrypted/DRM-protected inputs raise `DrmProtectedError` and exit `4` without
writing any output.

## Privacy

- The CLI makes no network calls.
- The web server stores uploads in a per-job temp directory and deletes them on expiry or shutdown.
- File contents are never logged; reports contain only short evidence snippets.
- No analytics, fonts, or scripts are loaded from third parties by the UI.

## Limitations

- Image-based branding is detected from file names, `alt`/`title` text, markup
  position and surrounding promotional content — there is no OCR, so text burned
  into artwork is not found.
- Heuristics can be wrong in both directions. Everything removed is listed with
  its evidence; review the report before relying on a cleaned file.
- A clean report is **not** a safety certificate. EPUB Purifier is not an
  anti-malware engine and cannot prove the absence of a threat.
- Signature-preserving cleaning is impossible by definition; signed books are
  flagged.
- Output is structurally verified, but running
  [EPUBCheck](https://github.com/w3c/epubcheck) on the result is still the best
  final check for your reader.

---

## Development

```bash
pip install -e ".[dev,web]"
pytest -q          # full suite, including hostile archives
ruff check src tests proof
python proof/prove.py  # end-to-end proof against the real CLI (see below)
```

Tests build their fixtures programmatically: a normal book, a citation-only
book, an ambiguous-removal book, an XXE document, a DRM book, a traversal
archive and a ZIP bomb. No third-party EPUBs are redistributed.

`proof/prove.py` is a second, independent check: it builds its fixtures from
scratch (without importing `tests/`), drives the installed CLI as a subprocess,
and asserts the four behaviours the project promises — EPUB 2 branding removal,
false-positive protection with the `--approve` flow, hostile-archive refusal and
neutralisation, and honest reporting for clean, DRM and signed books. It writes
only to `proof/fixtures/` and `proof/work/`, both git-ignored.

Pass `--epubcheck /path/to/epubcheck.jar` (the official W3C release, downloaded
separately; it is not a dependency) to have EPUBCheck judge every cleaned output
and fail the proof on any FATAL or ERROR message.

### Continuous integration

[`.github/workflows/ci.yml`](.github/workflows/ci.yml) runs on every push and
pull request and enforces what this README claims:

- **Lint** — `ruff check src tests proof` on Python 3.12.
- **Tests** — the full suite on Python 3.10–3.13 with the `dev` and `web` extras
  installed. The job fails if *any* test is skipped, so a missing optional
  dependency cannot hide behind `importorskip`; setting
  `EPUB_PURIFIER_REQUIRE_WEB=1` makes the conftest fail collection instead.
- **Web** — imports FastAPI, uvicorn, httpx and python-multipart, runs the web
  test modules, then starts `epub-purifier-web` and requests `/api/health` and
  `/` from it.
- **Proof** — the independent harness driving the installed CLI on its own
  fixtures.

CI installs from PyPI only and uses no secrets. EPUBCheck is deliberately not
downloaded there: the proof harness accepts `--epubcheck` but never requires it,
so spec conformance stays a local, opt-in check.

### Layout

```
src/epub_purifier/
├── archive.py        # safe ZIP reader (names, budgets, ratios)
├── validation.py     # stage 1: structure, DRM, member hygiene
├── analysis.py       # stage 2: resource graph, detectors, promo pages
├── cleaning.py       # stage 3: mutation application, reference pruning
├── repackage.py      # EPUB writer + output verification
├── opf.py            # package document access and structural edits
├── markup.py         # parsing/serialisation, DOM helpers
├── detectors/        # branding, security, resources, styles, metadata
├── report.py         # report model, text + JSON rendering
├── pipeline.py       # inspect_epub / clean_epub facade
├── cli.py            # argparse CLI
└── web/              # FastAPI app + static UI
```

### Adding a rule

1. Pick the detector module (branding → `detectors/content.py`, active content →
   `detectors/security.py`, remote/tracking → `detectors/resources.py`).
2. Emit a `Mutation(finding=Finding(...), kind="remove_element", element=…)`.
3. Set `remediation` to `AUTO` only when the edit cannot damage legitimate
   content; otherwise `REVIEW`.
4. Add a fixture and an assertion to `tests/`.

## Contributing

Bug reports with a minimal reproduction EPUB (or a description of the markup),
new promotional-insertion patterns, and detector improvements are welcome. See
[CONTRIBUTING.md](CONTRIBUTING.md) and [SECURITY.md](SECURITY.md).

## License

MIT — see [LICENSE](LICENSE).

EPUB Purifier is not affiliated with Ocean of PDF or any other site whose
branding it recognises. Brand names appear only as detection patterns.
