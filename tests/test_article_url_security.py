"""Network-isolated regression tests for article-fetch SSRF defenses."""

from __future__ import annotations

from contextlib import contextmanager
from email.message import Message
import os
import socket
import unittest
from unittest.mock import Mock, patch

from daily_report.src.stock_daily_agent import article_fetcher


@contextmanager
def env_override(**overrides):
    """Set environment variables for the duration of the block, restoring only those keys.

    Unlike ``unittest.mock.patch.dict("os.environ", ...)`` this does NOT copy the
    entire process environment. That copy/restore trips the Windows 32767-character
    limit on a single variable when the sandbox carries a very large variable
    (e.g. ``ACC_PRODUCT_CONFIG_V3``), raising
    ``ValueError: the environment variable is longer than 32767 characters`` on exit.
    Touching only the specific keys keeps the test robust on Windows.
    """
    saved = {}
    for key, value in overrides.items():
        saved[key] = os.environ.get(key)
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    try:
        yield
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _addr(address: str, port: int = 80):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port))]


class _Response:
    def __init__(self, status: int = 200, body: bytes = b"", headers: dict[str, str] | None = None) -> None:
        self.status = status
        self.headers = Message()
        for key, value in (headers or {"Content-Type": "text/html; charset=utf-8"}).items():
            self.headers[key] = value
        self._body = body
        self.read_calls: list[int] = []
        self.closed = False

    def read(self, amount: int) -> bytes:
        self.read_calls.append(amount)
        return self._body[:amount]

    def close(self) -> None:
        self.closed = True


class ArticleUrlSecurityTests(unittest.TestCase):
    def test_classifies_non_public_address_ranges_as_unsafe(self) -> None:
        for address in [
            "127.0.0.1", "10.0.0.1", "172.16.0.1", "192.168.0.1",
            "169.254.169.254", "224.0.0.1", "0.0.0.0", "100.64.0.1",
            "::1", "fc00::1", "fe80::1", "ff00::1", "::ffff:127.0.0.1",
        ]:
            with self.subTest(address=address):
                self.assertFalse(article_fetcher._is_public_ip(address))
        self.assertTrue(article_fetcher._is_public_ip("93.184.216.34"))

    def test_rejects_private_dns_and_disallowed_url_forms_before_connection(self) -> None:
        blocked_urls = [
            "file:///etc/passwd",
            "gopher://example.com/",
            "http://user:password@example.com/",
            "http://safe.example:8080/",
        ]
        with env_override(ARTICLE_FETCH_ALLOWED_PORTS=""), patch.object(article_fetcher, "_open_pinned_article_request") as open_request:
            for url in blocked_urls:
                with self.subTest(url=url), self.assertRaises(article_fetcher.ArticleFetchSecurityError):
                    article_fetcher._fetch_article_text(url)
            with patch.object(article_fetcher.socket, "getaddrinfo", return_value=_addr("169.254.169.254")):
                with self.assertRaises(article_fetcher.ArticleFetchSecurityError):
                    article_fetcher._fetch_article_text("http://metadata.example/")
            with patch.object(article_fetcher.socket, "getaddrinfo", return_value=_addr("10.0.0.8")):
                with self.assertRaises(article_fetcher.ArticleFetchSecurityError):
                    article_fetcher._fetch_article_text("http://internal.example/")
        open_request.assert_not_called()

    def test_uses_validated_ip_instead_of_a_second_dns_lookup(self) -> None:
        response = _Response(body=b"<html><title>Acme</title><p>Revenue was 123 and market conditions remain stable.</p></html>")
        connection = Mock()
        with (
            patch.object(article_fetcher.socket, "getaddrinfo", return_value=_addr("93.184.216.34")) as resolve,
            patch.object(article_fetcher, "_open_pinned_article_request", return_value=(response, connection)) as open_request,
        ):
            record = article_fetcher._fetch_article_text("https://public.example/article")

        self.assertTrue(record["ok"])
        self.assertEqual(record["final_url"], "https://public.example/article")
        self.assertEqual(resolve.call_count, 1)
        self.assertEqual(open_request.call_args.args[1], ["93.184.216.34"])
        connection.close.assert_called_once()

    def test_validates_every_redirect_destination(self) -> None:
        redirect = _Response(status=302, headers={"Location": "http://127.0.0.1:80/admin"})
        connection = Mock()

        def resolve(host, port, **_kwargs):
            return _addr("93.184.216.34" if host == "public.example" else "127.0.0.1", port)

        with (
            patch.object(article_fetcher.socket, "getaddrinfo", side_effect=resolve),
            patch.object(article_fetcher, "_open_pinned_article_request", return_value=(redirect, connection)) as open_request,
            self.assertRaises(article_fetcher.ArticleFetchSecurityError),
        ):
            article_fetcher._fetch_article_text("https://public.example/redirect")

        self.assertEqual(open_request.call_count, 1)
        connection.close.assert_called_once()

    def test_allows_public_redirect_and_enforces_response_limit(self) -> None:
        redirect = _Response(status=302, headers={"Location": "https://second.example/article"})
        success = _Response(body=b"<html><p>Revenue was 123 and market conditions remain stable.</p></html>")
        first_connection = Mock()
        second_connection = Mock()

        def resolve(host, port, **_kwargs):
            return _addr("93.184.216.34" if host == "public.example" else "8.8.8.8", port)

        with (
            patch.object(article_fetcher.socket, "getaddrinfo", side_effect=resolve),
            patch.object(
                article_fetcher,
                "_open_pinned_article_request",
                side_effect=[(redirect, first_connection), (success, second_connection)],
            ) as open_request,
        ):
            record = article_fetcher._fetch_article_text("https://public.example/redirect")

        self.assertEqual(record["final_url"], "https://second.example/article")
        self.assertEqual(open_request.call_count, 2)

        too_large = _Response(body=b"x" * 1025)
        with (
            patch.object(article_fetcher.socket, "getaddrinfo", return_value=_addr("93.184.216.34")),
            patch.object(article_fetcher, "_open_pinned_article_request", return_value=(too_large, Mock())),
            env_override(ARTICLE_FETCH_MAX_RESPONSE_BYTES="1024"),
            self.assertRaises(article_fetcher.ToolError),
        ):
            article_fetcher._fetch_article_text("https://public.example/large")

    def test_enforces_redirect_limit(self) -> None:
        redirect = _Response(status=302, headers={"Location": "https://second.example/article"})
        with (
            patch.object(article_fetcher.socket, "getaddrinfo", return_value=_addr("93.184.216.34")),
            patch.object(article_fetcher, "_open_pinned_article_request", return_value=(redirect, Mock())) as open_request,
            env_override(ARTICLE_FETCH_MAX_REDIRECTS="0"),
            self.assertRaises(article_fetcher.ToolError),
        ):
            article_fetcher._fetch_article_text("https://public.example/redirect")
        self.assertEqual(open_request.call_count, 1)


if __name__ == "__main__":
    unittest.main()
