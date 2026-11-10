# Security policy

EPUB Purifier parses untrusted files. Its security posture is part of the
product, not an afterthought.

## Supported versions

| Version | Supported |
| --- | --- |
| 0.1.x | yes |

## Reporting a vulnerability

Please do **not** open a public issue for a vulnerability that lets a crafted
EPUB escape the intended constraints (file write outside the job directory,
code execution, entity expansion, denial of service that defeats the limits,
path traversal in the web layer).

Report it privately to the maintainers via GitHub's "Report a vulnerability"
advisory flow, including:

- the affected version and platform;
- a minimal reproducing EPUB (a ZIP with a handful of members is usually enough);
- the observed versus expected behaviour;
- whether the web server or only the CLI is affected.

You can expect an acknowledgement within a few days. Please give us a reasonable
window to ship a fix before publishing details.

## Threat model

The adversary controls the entire EPUB: member names, compression ratios,
markup, CSS, metadata, SVG, image bytes and archive metadata. The defender is a
short-lived process that must never execute, resolve, fetch or trust any of it.

| Threat | Mitigation | Where |
| --- | --- | --- |
| ZIP bomb / decompression amplification | total and per-entry uncompressed caps, compression-ratio limits, checks before extraction | `archive.py`, `validation._check_budget` |
| Zip-slip / path traversal | NFC normalisation, rejection of `..`, absolute paths, drive letters, backslashes and control characters; only normalised names are ever written | `archive.normalize_member_name` |
| Symlink or device members | refused and excluded | `archive._is_symlink`, `validation._check_members` |
| Encrypted content / DRM circumvention | encrypted members and non-font encryption are rejected with `DrmProtectedError`; no attempt is made to decrypt | `validation._check_drm` |
| XXE / external entity expansion | `defusedxml` for container and encryption documents; entity declarations are detected and the doctype is removed before any parse; no network access, no external DTD loading | `markup.parse_markup`, `validation` |
| Script execution | no `eval`/`exec`, no subprocesses, no browser engine; markup is parsed as data | all parsers |
| Embedded executables | extension-based detection, dropped from the output | `validation._check_junk_files` |
| Resource exhaustion | upload bytes, member count, path depth, per-read cap, bounded report evidence | `config.Limits` |
| Output corruption / archive smuggling | rebuilt ZIP only from normalised, verified members; structural verification of the result | `repackage.py` |
| Web abuse | loopback bind by default, upload cap, per-client rate limit, bounded job table, TTL deletion, strict CSP and `nosniff`, no user-controlled paths | `web/app.py` |
| SSRF via a book source | outbound requests are https-only, restricted to a per-source host allowlist, resolved and rejected unless every address is globally routable, redirects are re-checked hop by hop, and the body is capped during the read | `sources/net.py` |
| Unscanned download exposure | every fetched file is written to a private temp directory, validated and cleaned before a download exists; only the rebuilt EPUB is served, once, and the raw file is deleted immediately | `transfer.py` |

## Non-goals

- Detecting malware by signature or reputation. The scanner reports structural
  indicators; it cannot certify a file as clean.
- Preserving digital signatures. Any modification invalidates them; signed books
  are flagged in the report.
- Rendering fidelity. A cleaned EPUB is structurally verified, but EPUBCheck and
  a real reading system remain the final authority for a particular device.

## Operational guidance

- Run the web UI bound to `127.0.0.1` unless you put it behind an authenticated
  reverse proxy. It has no user accounts by design.
- Run the CLI as an unprivileged user.
- Treat cleaned output as untrusted until it has been opened in your reader.

## Dependencies

`defusedxml`, `lxml`, `beautifulsoup4`, `Pillow` and `ebooklib` are the parsing
surface; `fastapi`/`uvicorn` are optional and only used by the web UI. Keep them
patched — `pip install -e ".[dev]"` plus `pip list --outdated` is enough for a
local check.
