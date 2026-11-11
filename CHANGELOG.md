# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and this project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Removed

- Anna's Archive and Ocean of PDF are no longer search sources. Neither can show
  that a file is distributed with the publisher's authorisation, so they were link
  only and contributed nothing to a card beyond a search link. `LinkOnlySource`
  remains as the adapter for operator-added external sources, and the OceanofPDF
  branding patterns stay in the cleaner, where they belong.

### Fixed

- Cleaned XHTML is serialized as XML with self-closing void elements and the
  XHTML namespace, so malformed HTML chapters no longer turn `<br>` and `<img>`
  into containers with text children. Rebuilt EPUB 2 content documents now have
  the XHTML 1.1 doctype and EPUB 3 documents the HTML5 doctype, and output
  verification rejects malformed XHTML, non-empty void elements, missing EPUB 2
  doctypes and missing EPUB 3 nav or metadata before a cleaned file is offered.

### Added

- Multi-file upload queue: the web UI accepts up to ten EPUBs at once,
  inspects them sequentially through the existing single-file endpoint, and
  keeps each report, review selection and cleaned download separate.
- Server-defined extra sources: a JSON (or TOML) file named by
  `EPUB_PURIFIER_EXTRA_SOURCES`, default `sources.json`, adds catalogues to search
  without patching the package. `kind: "link"` entries are external listings that
  are never fetched; `kind: "module"` entries load an adapter you supply and may
  use the normal download, scanning and cleaning pipeline. Entries are validated
  when the source list is built, and a bad entry names itself instead of silently
  vanishing from search.
- Legal-availability resolution: a matched book with no downloadable copy of its
  own (a record-only listing, a lending scan, an HTML-only edition) is looked up
  by exact title and author in the openly licensed sources, and a copy that is
  found becomes the card's download and flows through the existing transfer,
  scanning and five-hour-expiry pipeline. Anna's Archive and Ocean of PDF are
  never fetched; the lookup is capped per search, skips sources already on the
  card, accepts only strong title-and-author verdicts, and degrades a card to
  "available elsewhere" on errors instead of failing the search.
- Five-hour temporary storage for fetched books: a cleaned copy and its metadata
  are deleted five hours after the server starts downloading the book, whether
  or not it was downloaded. A background worker sweeps every ten minutes, and
  expiry is enforced on every access too.
- Persistent transfer metadata (`job.json` and `report.json` next to each
  cleaned EPUB) so a restarted server rehydrates surviving jobs, deletes expired
  ones, and discards interrupted downloads. `EPUB_PURIFIER_TRANSFER_DIR` selects
  the storage root; the Docker image mounts it as a volume.
- Temporary-storage management: a storage cap, duplicate-request reuse, orphan
  directory sweeping, and immediate cleanup after failed or oversized downloads.
- `GET /api/transfers` (remaining time and storage use) and
  `DELETE /api/transfer/{id}` (manual **Delete now**), with a UI panel that
  counts down to automatic deletion.
- `scanning.py`, a dedicated scan gate between downloaded bytes and the download
  endpoint, with tests for executables, traversal members, DRM, ZIP bombs and
  non-EPUB bodies.
- Unified book search across Project Gutenberg (its official OPDS catalogue),
  Anna's Archive and Ocean of PDF, with per-source availability, formats and
  cover images.
- Relevance-ranked search with strict matching: exact titles first, then
  title/author combinations, ISBN matches, close title variations and bounded
  fuzzy matches. ISBN metadata is read from Gutenberg, Open Library, Internet
  Archive and Standard Ebooks; unsupported or invalid ISBNs return no books.
- Search filters for title, author, ISBN, language and file format, plus
  cross-source grouping that shows one book with its matching source listings.
  Link-only sites are attached as unverified search links, never as confirmed
  holdings.
- Wikisource and Internet Archive adapters with real downloads: Wikisource pages
  are exported to EPUB by Wikimedia's WS Export, and Internet Archive results are
  limited to openly downloadable, non-lending items whose metadata is re-checked
  before any file is requested.
- Open Library adapter: its official search API is filtered to works marked
  `ebook_access=public`, and the EPUB is downloaded from Internet Archive only
  after the item metadata is re-checked, so lending copies are refused.
- OAPEN Library and Directory of Open Access Books adapters built on their
  documented DSpace REST APIs: records are searched with metadata and bitstreams
  expanded, and an EPUB is downloaded only when the record attaches one. Every
  request stays on the repository host, calls are limited to one per second, and
  a temporary anti-scraping `403` becomes a per-source status.
- Standard Ebooks adapter built on its official OPDS search feed: entries are
  parsed with defusedxml into listings, and downloads use the entry's
  open-access EPUB link under a standardebooks.org-only allowlist. The
  membership-gated browse feeds and the site's HTML are never used.
- Modular source adapters: an authorised EPUB download path for Project
  Gutenberg, and link-only adapters that refuse downloads for the two sites
  without distribution rights to prove.
- Project Gutenberg search runs against its official OPDS catalogue; the EPUB
  edition is confirmed while fetching rather than assumed from the search feed.
- Isolated transfer pipeline that fetches into a private temporary directory,
  rejects archives that fail validation or contain executable payloads, and
  reuses the existing cleaning engine before serving the rebuilt EPUB.
- Cover images proxied through the server with an allowlist, size caps and an
  in-memory cache, so the browser never contacts a source directly.
- Automated tests for search parsing, rate limiting, URL/SSRF guards, download
  selection, transfer rejection paths, temporary-file deletion and the new HTTP
  endpoints, including the Wikisource export and Internet Archive refusal paths.

### Fixed

- A plain-text "Downloaded from OceanofPDF.com - free ebooks every day." footer
  now reaches auto-removal confidence without needing a promotional class: a
  brand's domain suffix counts toward how much of the block the brand occupies.
- Inline `<style>` bodies are no longer also scored as promotional text blocks,
  so one CSS declaration produces one finding instead of two.
- Quotations, citations and footnotes are capped below the review threshold, so
  they are reported but never offered for removal however strong the other
  signals are.
- A promotional page linked from the table of contents only through a branded
  entry is now dropped with its manifest and spine references, instead of
  surviving as an empty document at the end of the book.
- Search results now report a per-listing security status (scan-on-fetch versus
  link-only) instead of omitting the field.
- Search no longer fills the result list with unrelated keyword matches. Exact
  titles outrank sequels and similar titles, title/author searches require both
  fields to match, duplicate listings are removed, and different ISBNs remain
  separate editions rather than being merged.
- Short accidental token overlaps (for example `dune` against `jane`) no longer
  qualify as author/title matches, so a search cannot pull in a book merely
  because two short words share a few characters.
- A book without an EPUB edition is refused as a validation error (422) rather
  than reported as a forbidden request (403).

### Security

- Outbound requests are https-only, host-allowlisted, DNS-checked for public
  addresses, redirect-checked per hop and size-capped during the read.
- No endpoint serves an unscanned or raw downloaded file; nested archives,
  symlinks, traversal members and executables reject the whole download rather
  than being cleaned around.
- Temporary files are `0600` inside a `0700` root, written atomically, and never
  expose the source's original bytes.

## [0.1.0] — 2026-11-10

### Added

- Three-stage pipeline: validation, inspection, cleaning, with a shared
  inspection/cleaning mutation model.
- ZIP hardening: member-name normalisation (traversal, absolute paths, drive
  letters, control characters, backslashes), symlink and encrypted-member
  refusal, entry/size/compression-ratio budgets, duplicate-member handling.
- DRM refusal for Adobe rights files and non-font encryption.
- EPUB 2 (NCX) and EPUB 3 (nav) support, including manifest, spine, guide and
  navigation pruning when documents are removed.
- Branding detection with a documented confidence model covering text blocks,
  footers, links, image sources and alt text, CSS generated content, package
  metadata, guide references and navigation entries.
- Dedicated promotional-page removal guarded by TOC membership, front-matter
  protection and residual-content thresholds.
- Security scanning for scripts, event handlers, script URLs, active data URIs,
  iframes, objects, forms, meta refresh, SVG abuse, XML entity declarations,
  tracker hosts, tracking pixels, remote assets, executables and nested archives.
- Severity classification (critical/high/medium/low/informational) and explicit
  review-required findings.
- Spec-compliant repackaging (stored `mimetype` first) with structural
  verification of the output.
- CLI (`inspect`, `clean`, approvals, dry run, JSON reports, severity-based exit
  codes) and a local FastAPI web UI with TTL job storage, rate limiting and a
  strict CSP.
- TOML/JSON configuration with validated keys, and a test suite covering hostile
  archives, citation safety and idempotence.

### Notes

- Image-based branding is detected through file names, alt text and surrounding
  markup; no OCR is performed.
- EPUB Purifier is not an anti-malware engine and a clean report is not proof
  that a file is safe.
