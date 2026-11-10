# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and this project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Unified book search across Project Gutenberg (Gutendex), Anna's Archive and
  Ocean of PDF, with per-source availability, formats and cover images.
- Modular source adapters: an authorised EPUB download path for Project
  Gutenberg, and link-only adapters that refuse downloads for the other two.
- Isolated transfer pipeline that fetches into a private temporary directory,
  rejects archives that fail validation or contain executable payloads, reuses
  the existing cleaning engine, and serves the cleaned EPUB exactly once.
- Cover images proxied through the server with an allowlist, size caps and an
  in-memory cache, so the browser never contacts a source directly.
- Automated tests for search parsing, rate limiting, URL/SSRF guards, download
  selection, transfer rejection paths, temporary-file deletion and the new HTTP
  endpoints.

### Security

- Outbound requests are https-only, host-allowlisted, DNS-checked for public
  addresses, redirect-checked per hop and size-capped during the read.
- No endpoint serves an unscanned or raw downloaded file.

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
