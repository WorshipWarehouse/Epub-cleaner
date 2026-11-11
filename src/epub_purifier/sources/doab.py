"""Directory of Open Access Books: open-access discovery with attached files.

DOAB indexes peer-reviewed open-access books. Its documented DSpace 6 REST API
expands each record with the bitstreams DOAB actually holds, so this adapter
offers a download only when an EPUB is attached to the record and otherwise
links to the record page for the publisher's own copy.
"""

from __future__ import annotations

from .dspace import DSpaceBookSource


class DoabSource(DSpaceBookSource):
    name = "doab"
    label = "Directory of Open Access Books"
    base = "https://directory.doabooks.org"
    allowed_hosts = ("doabooks.org",)
    supports_download = True
    note = (
        "Open-access books indexed by DOAB through its documented DSpace REST "
        "API. Records with an attached EPUB can be fetched, scanned and cleaned "
        "here; others link to the record page."
    )
    download_note = (
        "Open-access EPUB attached to this DOAB record; doabooks.org-only "
        "allowlist, size cap and ZIP check before cleaning."
    )


__all__ = ["DoabSource"]
