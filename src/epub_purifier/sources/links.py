"""The generic external-listing adapter.

A :class:`LinkOnlySource` takes part in search and opens the site in the user's
browser, but it never hands this server a file: ``download()`` refuses outright
and ``supports_download`` is false, so the transfer endpoint rejects it before
any network call.

This is the shape used for sources an operator adds in the extra-sources file
(see :mod:`epub_purifier.sources.custom`). It is deliberately the *only* adapter
here: which catalogues exist is a server's decision, not a constant in this
package, and a source that cannot show it is authorised to distribute a file is
represented by a link rather than a download.
"""

from __future__ import annotations

import urllib.parse

from .base import BookListing, DownloadRefused, DownloadResult, normalize_query


class LinkOnlySource:
    """A source that is represented by a search link only."""

    supports_download = False

    def __init__(self, *, name: str, label: str, search_template: str, note: str, limitation: str):
        self.name = name
        self.label = label
        self.search_template = search_template
        self.note = note
        self.limitation = limitation

    def search_url(self, query: str) -> str:
        return self.search_template.format(query=urllib.parse.quote_plus(query))

    def search(self, query: str) -> list[BookListing]:
        query = normalize_query(query)
        return [
            BookListing(
                source=self.name,
                source_label=self.label,
                book_id="",
                title=query,
                details_url=self.search_url(query),
                downloadable=False,
                download_note=self.limitation,
                is_search_link=True,
            )
        ]

    def download(self, book_id: str) -> DownloadResult:
        raise DownloadRefused(
            f"{self.label} does not authorise automated downloads",
            detail="Open the source's search page instead.",
        )


__all__ = ["LinkOnlySource"]
