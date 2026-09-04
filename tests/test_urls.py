from __future__ import annotations

import pytest

from ainews.sources.urls import canonical_url


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # Tracking parameters are decoration and go.
        (
            "https://example.com/post?utm_source=twitter&utm_medium=social",
            "https://example.com/post",
        ),
        ("https://example.com/post?fbclid=abc123", "https://example.com/post"),
        ("https://example.com/post?ref=hn&utm_campaign=x", "https://example.com/post"),
        # Mirrors and schemes collapse onto one host.
        ("http://www.example.com/post", "https://example.com/post"),
        ("https://m.example.com/post", "https://example.com/post"),
        ("https://amp.example.com/post", "https://example.com/post"),
        # Trailing slash and fragment are presentational.
        ("https://example.com/post/", "https://example.com/post"),
        ("https://example.com/post#section-2", "https://example.com/post"),
        # A load-bearing query survives.
        (
            "https://news.ycombinator.com/item?id=44001&utm_source=rss",
            "https://news.ycombinator.com/item?id=44001",
        ),
        # The root path stays a root path rather than becoming empty.
        ("https://example.com/", "https://example.com/"),
    ],
)
def test_canonicalisation(raw: str, expected: str) -> None:
    assert canonical_url(raw) == expected


def test_variants_of_one_article_share_a_key() -> None:
    """The point of the whole module: one story, one database row."""
    variants = [
        "https://example.com/a/story",
        "http://www.example.com/a/story/",
        "https://m.example.com/a/story?utm_source=newsletter",
        "https://example.com/a/story#top",
    ]
    assert len({canonical_url(v) for v in variants}) == 1


def test_malformed_input_does_not_raise() -> None:
    """A collection run must survive one broken link in one feed."""
    assert canonical_url("") == ""
    for junk in ("not a url", "http://", "://///", "javascript:void(0)"):
        assert isinstance(canonical_url(junk), str)
