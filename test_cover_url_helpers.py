#!/usr/bin/env python3
"""Cover-URL helpers used by POST /api/set-meta.

The book page placeholder accepts an Open Library link or a Google Books
link. Those used to live as dead code after return None inside fetch_from_isbn,
so saving such a URL raised NameError. These tests pin the extracted functions.
"""
import json
import unittest
from unittest.mock import patch

import server


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload if isinstance(payload, bytes) else payload.encode()

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def _json_response(data):
    return FakeResponse(json.dumps(data))


class CoverUrlHelperTests(unittest.TestCase):
    def test_openlibrary_ignores_non_ol_urls(self):
        self.assertIsNone(server.fetch_from_openlibrary_url("https://example.com/book"))
        self.assertIsNone(server.fetch_from_openlibrary_url("https://books.google.com/books?id=abc"))

    def test_googlebooks_ignores_non_gb_urls(self):
        self.assertIsNone(server.fetch_from_googlebooks_url("https://example.com/book"))
        self.assertIsNone(server.fetch_from_googlebooks_url("https://openlibrary.org/works/OL1W"))

    def test_openlibrary_work_url_fetches_title_author_cover(self):
        calls = []

        def fake_urlopen(url, timeout=8):
            calls.append(url)
            if url.endswith("/works/OL45804W.json"):
                return _json_response({
                    "title": "The Hobbit",
                    "authors": [{"author": {"key": "/authors/OL26320A"}}],
                    "covers": [14625765],
                })
            if url.endswith("/authors/OL26320A.json"):
                return _json_response({"name": "J. R. R. Tolkien"})
            raise AssertionError(f"unexpected url {url}")

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            data = server.fetch_from_openlibrary_url(
                "https://openlibrary.org/works/OL45804W/The_Hobbit"
            )

        self.assertEqual(data["title"], "The Hobbit")
        self.assertEqual(data["author"], "J. R. R. Tolkien")
        self.assertEqual(
            data["cover_url"],
            "https://covers.openlibrary.org/b/id/14625765-L.jpg",
        )
        self.assertEqual(calls, [
            "https://openlibrary.org/works/OL45804W.json",
            "https://openlibrary.org/authors/OL26320A.json",
        ])

    def test_openlibrary_book_url_uses_inline_author_name(self):
        def fake_urlopen(url, timeout=8):
            self.assertEqual(url, "https://openlibrary.org/books/OL262758W.json")
            return _json_response({
                "title": "Dune",
                "authors": [{"name": "Frank Herbert"}],
                "covers": [99],
            })

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            data = server.fetch_from_openlibrary_url(
                "https://openlibrary.org/books/OL262758W?foo=1"
            )

        self.assertEqual(data, {
            "title": "Dune",
            "author": "Frank Herbert",
            "cover_url": "https://covers.openlibrary.org/b/id/99-L.jpg",
        })

    def test_googlebooks_url_fetches_volume_and_rewrites_cover(self):
        def fake_urlopen(url, timeout=8):
            self.assertEqual(url, "https://www.googleapis.com/books/v1/volumes/zyTCAlFPjgYC")
            return _json_response({
                "volumeInfo": {
                    "title": "The Google Story",
                    "authors": ["David A. Vise"],
                    "imageLinks": {
                        "thumbnail": "http://books.google.com/cover?zoom=1&edge=curl",
                    },
                }
            })

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            data = server.fetch_from_googlebooks_url(
                "https://books.google.com/books?id=zyTCAlFPjgYC&printsec=frontcover"
            )

        self.assertEqual(data["title"], "The Google Story")
        self.assertEqual(data["author"], "David A. Vise")
        self.assertEqual(data["cover_url"], "http://books.google.com/cover?zoom=0")

    def test_helpers_are_callable_from_set_meta_names(self):
        # The NameError was that /api/set-meta called these names with no def.
        self.assertTrue(callable(server.fetch_from_openlibrary_url))
        self.assertTrue(callable(server.fetch_from_googlebooks_url))


if __name__ == "__main__":
    unittest.main()
