"""OAPEN Library: open-access scholarly books with EPUB bitstreams.

OAPEN publishes a documented DSpace 6 REST API for search and bitstreams, needs
no registration, and hosts the EPUB itself. This adapter uses only that API,
never HTML scraping, and restricts every request to oapen.org.
"""

from __future__ import annotations

from .dspace import DSpaceBookSource


class OapenSource(DSpaceBookSource):
    name = "oapen"
    label = "OAPEN Library"
    base = "https://library.oapen.org"
    allowed_hosts = ("oapen.org",)
    supports_download = True
    note = (
        "Open-access scholarly books from the OAPEN Library's documented DSpace "
        "REST API. Attached EPUBs are fetched from OAPEN bitstreams, scanned and "
        "cleaned here."
    )
    download_note = (
        "Open-access EPUB from the OAPEN Library; oapen.org-only allowlist, size "
        "cap and ZIP check before cleaning."
    )


__all__ = ["OapenSource"]
