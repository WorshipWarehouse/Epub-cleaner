"""Relevance ranking, strict filters and cross-source book grouping."""

from __future__ import annotations

from epub_purifier.sources.base import BookListing
from epub_purifier.sources.matching import SearchCriteria, group_listings, rank_listings
from epub_purifier.sources.registry import search_books

ISBN_10 = "0306406152"
ISBN_13 = "9780306406157"
OTHER_ISBN_13 = "9780141439518"


def listing(
    title: str,
    authors: tuple[str, ...] = ("Frank Herbert",),
    *,
    source: str = "gutenberg",
    book_id: str | None = None,
    isbn: tuple[str, ...] = (),
    languages: tuple[str, ...] = ("en",),
    formats: tuple[str, ...] = ("EPUB",),
) -> BookListing:
    return BookListing(
        source=source,
        source_label=source.title(),
        book_id=book_id or title.lower().replace(" ", "-"),
        title=title,
        authors=authors,
        isbn=isbn,
        languages=languages,
        formats=formats,
        downloadable=True,
    )


def ranked_titles(listings: list[BookListing], query: str = "dune") -> list[str]:
    return [item.listing.title for item in rank_listings(listings, SearchCriteria(query))]


def groups(listings: list[BookListing], query: str = "dune"):
    return group_listings(rank_listings(listings, SearchCriteria(query)))


def test_exact_title_ranks_before_close_variants():
    titles = ranked_titles(
        [
            listing("Dune Messiah", book_id="messiah"),
            listing("Children of Dune", book_id="children"),
            listing("Dune", book_id="dune"),
            listing("Dune: Deluxe Edition", book_id="deluxe"),
        ]
    )

    assert titles[0] == "Dune"
    assert set(titles[1:]) == {"Dune Messiah", "Children of Dune", "Dune: Deluxe Edition"}


def test_title_and_author_must_both_match():
    listings = [
        listing(
            "Pride and Prejudice",
            ("Jane Austen",),
            source="gutenberg",
            book_id="pp",
        ),
        listing(
            "Pride and Prejudice and Zombies",
            ("Seth Grahame-Smith",),
            source="internetarchive",
            book_id="zombies",
        ),
    ]

    results = groups(listings, "pride and prejudice jane austen")
    assert len(results) == 1
    assert results[0].title == "Pride and Prejudice"
    assert [item.book_id for item in results[0].listings] == ["pp"]


def test_incorrect_explicit_author_excludes_a_title_match():
    results = groups(
        [listing("Pride and Prejudice", ("Jane Austen",), book_id="pp")],
        "pride and prejudice",
    )
    assert results

    ranked = rank_listings(
        [listing("Pride and Prejudice", ("Jane Austen",), book_id="pp")],
        SearchCriteria("pride and prejudice", author="Stephen King"),
    )
    assert ranked == []


def test_unrelated_results_are_dropped():
    titles = ranked_titles(
        [
            listing("Dune", book_id="dune"),
            listing("The Girl on the Train", ("Paula Hawkins",), book_id="girl"),
            listing("Dune Messiah", book_id="messiah"),
        ]
    )

    assert "The Girl on the Train" not in titles
    assert titles == ["Dune", "Dune Messiah"]


def test_misspelled_title_is_a_fuzzy_match():
    titles = ranked_titles(
        [
            listing("Pride and Prejudice", ("Jane Austen",), book_id="pp"),
            listing("Persuasion", ("Jane Austen",), book_id="persuasion"),
        ],
        "pride and prejudce",
    )
    assert titles == ["Pride and Prejudice"]


def test_similar_titles_remain_distinct_books():
    results = groups(
        [
            listing("Dune", book_id="dune"),
            listing("Dune Messiah", book_id="messiah"),
        ]
    )
    assert [result.title for result in results] == ["Dune", "Dune Messiah"]
    assert all(len(result.listings) == 1 for result in results)


def test_same_book_from_different_sources_is_grouped():
    results = groups(
        [
            listing("Pride and Prejudice", ("Jane Austen",), source="gutenberg", book_id="g1"),
            listing(
                "Pride and Prejudice",
                ("Austen, Jane",),
                source="internetarchive",
                book_id="ia1",
            ),
            listing(
                "Pride and Prejudice (1817)",
                ("Jane Austen",),
                source="standardebooks",
                book_id="se1",
            ),
        ],
        "pride and prejudice",
    )

    assert len(results) == 1
    assert {item.source for item in results[0].listings} == {
        "gutenberg",
        "internetarchive",
        "standardebooks",
    }


def test_different_isbns_keep_editions_separate():
    results = groups(
        [
            listing("Pride and Prejudice", ("Jane Austen",), book_id="a", isbn=(ISBN_13,)),
            listing(
                "Pride and Prejudice",
                ("Jane Austen",),
                book_id="b",
                isbn=(OTHER_ISBN_13,),
            ),
        ],
        "pride and prejudice",
    )

    assert len(results) == 2


def test_isbn_search_only_returns_the_matching_isbn():
    results = groups(
        [
            listing("Dune", book_id="dune"),
            listing("Pride and Prejudice", ("Jane Austen",), book_id="pp", isbn=(ISBN_10,)),
        ],
        ISBN_13,
    )

    assert len(results) == 1
    assert results[0].title == "Pride and Prejudice"
    assert results[0].isbn == (ISBN_10,)


def test_isbn_10_and_13_are_equivalent():
    results = groups(
        [listing("Book", book_id="book", isbn=(ISBN_13,))],
        ISBN_10,
    )
    assert len(results) == 1


def test_invalid_isbn_returns_no_results_instead_of_title_matches():
    results = groups([listing("Dune", book_id="dune")], "9780306406158")
    assert results == ()


def test_explicit_isbn_filter_restricts_results():
    item = listing("Dune", book_id="dune", isbn=(ISBN_13,))
    other = listing("Dune", book_id="other", isbn=(OTHER_ISBN_13,))
    ranked = rank_listings([item, other], SearchCriteria("dune", isbn=ISBN_10))
    assert [match.listing.book_id for match in ranked] == ["dune"]


def test_language_and_format_filters_are_strict():
    item = listing("Dune", book_id="dune", languages=("en",), formats=("EPUB",))
    assert rank_listings([item], SearchCriteria("dune", language="English", format="epub"))
    assert rank_listings([item], SearchCriteria("dune", language="fr")) == []
    assert rank_listings([item], SearchCriteria("dune", format="PDF")) == []


class StaticSource:
    name = "gutenberg"
    label = "Project Gutenberg"
    supports_download = True
    note = "test"

    def __init__(self, listings: list[BookListing]) -> None:
        self.listings = listings

    def search(self, query):
        return list(self.listings)

    def download(self, book_id):
        raise AssertionError("not used")


def test_registry_deduplicates_the_same_source_listing():
    response = search_books(
        "dune",
        [StaticSource([listing("Dune", book_id="42"), listing("Dune", book_id="42")])],
    )
    assert len(response.results) == 1
    assert response.results[0].source_count == 1
    assert response.sources[0].result_count == 1
