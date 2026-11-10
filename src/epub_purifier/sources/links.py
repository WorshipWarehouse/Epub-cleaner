"""Sources we link to but never fetch from.

Neither Anna's Archive nor Ocean of PDF offers an authorised public interface for
automated downloads, and Ocean of PDF rehosts books without any way for us to
verify distribution rights. For both we return a search link and refuse
``download()`` outright, so a user can inspect availability in their own browser.
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


ANNAS_ARCHIVE = LinkOnlySource(
    name="annas",
    label="Anna's Archive",
    search_template="https://annas-archive.org/search?q={query}",
    note=(
        "Metadata search across several shadow libraries. No authorised public API "
        "for automated downloads, so this is a link only."
    ),
    limitation="Opens Anna's Archive in a new tab; EPUB Purifier does not download from it.",
)

OCEAN_OF_PDF = LinkOnlySource(
    name="oceanofpdf",
    label="Ocean of PDF",
    search_template="https://oceanofpdf.com/?s={query}",
    note=(
        "Third-party rehosting site. EPUB Purifier cannot verify distribution rights "
        "for its files, so it only shows a search link and never downloads from it."
    ),
    limitation=(
        "Search link only: this site redistributes books without publisher "
        "authorisation, so EPUB Purifier will not download from it."
    ),
)


__all__ = ["ANNAS_ARCHIVE", "OCEAN_OF_PDF", "LinkOnlySource"]
