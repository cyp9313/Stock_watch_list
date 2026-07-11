"""Article fetching subsystem with SSRF defenses.

Extracted from ``tools.py`` (P2-10B) to isolate the security-critical
network-fetching code into a single auditable module.

All public helpers remain importable from ``tools`` for backward
compatibility — ``tools.py`` re-exports them via ``from .article_fetcher
import ...`` at module load time.
"""

from __future__ import annotations

import http.client
import ipaddress
import os
import re
import socket
import ssl
from html import unescape
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urljoin, urlparse

from .utils import ToolError


# ---------------------------------------------------------------------------
# HTML text extraction
# ---------------------------------------------------------------------------

class _ArticleTextParser(HTMLParser):
    """Small stdlib HTML text extractor; intentionally dependency-light."""

    def __init__(self) -> None:
        super().__init__()
        self.skip_depth = 0
        self.parts: list[str] = []
        self.title_parts: list[str] = []
        self.meta_description = ""
        self.meta_date = ""
        self.in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag_l = tag.lower()
        attrs_d = {k.lower(): (v or "") for k, v in attrs}
        if tag_l in {"script", "style", "noscript", "svg", "nav", "footer", "form"}:
            self.skip_depth += 1
        if tag_l == "title":
            self.in_title = True
        if tag_l == "meta":
            name = (attrs_d.get("name") or attrs_d.get("property") or "").lower()
            content = attrs_d.get("content") or ""
            if name in {"description", "og:description", "twitter:description"} and not self.meta_description:
                self.meta_description = content.strip()
            if name in {"article:published_time", "published_time", "date", "dc.date", "dc.date.issued", "pubdate"} and not self.meta_date:
                self.meta_date = content.strip()[:10]

    def handle_endtag(self, tag: str) -> None:
        tag_l = tag.lower()
        if tag_l in {"script", "style", "noscript", "svg", "nav", "footer", "form"} and self.skip_depth > 0:
            self.skip_depth -= 1
        if tag_l == "title":
            self.in_title = False
        if tag_l in {"p", "br", "li", "h1", "h2", "h3", "div"} and self.skip_depth == 0:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not data or self.skip_depth > 0:
            return
        txt = unescape(data).strip()
        if not txt:
            return
        if self.in_title:
            self.title_parts.append(txt)
        # Keep paragraph-like text and meaningful short metadata text.
        if len(txt) >= 30 or any(ch.isdigit() for ch in txt):
            self.parts.append(txt)

    @property
    def title(self) -> str:
        return re.sub(r"\s+", " ", " ".join(self.title_parts)).strip()

    @property
    def text(self) -> str:
        raw = " ".join(self.parts)
        raw = re.sub(r"\s+", " ", raw)
        return raw.strip()


# ---------------------------------------------------------------------------
# Security exception
# ---------------------------------------------------------------------------

class ArticleFetchSecurityError(ToolError):
    """Raised when a URL is outside the article fetcher's network boundary."""


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_ARTICLE_REDIRECT_STATUSES = {301, 302, 303, 307, 308}
_MAX_ARTICLE_RESPONSE_BYTES = 2_000_000


# ---------------------------------------------------------------------------
# Configuration helpers
# ---------------------------------------------------------------------------

def _article_fetch_int_env(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.environ.get(name, default))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(value, maximum))


def _article_fetch_allowed_ports(scheme: str) -> set[int]:
    ports: set[int] = {443 if scheme == "https" else 80}
    raw = os.environ.get("ARTICLE_FETCH_ALLOWED_PORTS", "")
    for token in raw.split(","):
        try:
            port = int(token.strip())
        except (TypeError, ValueError):
            continue
        if 1 <= port <= 65535:
            ports.add(port)
    return ports


# ---------------------------------------------------------------------------
# IP / URL validation (SSRF defense layer)
# ---------------------------------------------------------------------------

def _is_public_ip(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    # Explicit checks are required because some Python versions report
    # multicast ranges as ``is_global``.
    return bool(
        ip.is_global
        and not ip.is_loopback
        and not ip.is_private
        and not ip.is_link_local
        and not ip.is_multicast
        and not ip.is_reserved
        and not ip.is_unspecified
        and not getattr(ip, "is_site_local", False)
    )


def _validate_article_url(url: str) -> tuple[Any, list[str]]:
    """Validate a URL and resolve it to globally routable IP addresses only."""
    value = str(url or "").strip()
    try:
        parsed = urlparse(value)
    except ValueError as exc:
        raise ArticleFetchSecurityError("Article URL is malformed.") from exc
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"}:
        raise ArticleFetchSecurityError("Article URL must use http or https.")
    if not parsed.netloc or parsed.username is not None or parsed.password is not None:
        raise ArticleFetchSecurityError("Article URL must not contain credentials and must include a host.")
    host = parsed.hostname
    if not host:
        raise ArticleFetchSecurityError("Article URL host is missing.")
    try:
        port = parsed.port or (443 if scheme == "https" else 80)
    except ValueError as exc:
        raise ArticleFetchSecurityError("Article URL has an invalid port.") from exc
    if port not in _article_fetch_allowed_ports(scheme):
        raise ArticleFetchSecurityError(f"Article URL port {port} is not allowed.")

    try:
        results = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise ArticleFetchSecurityError("Article URL host could not be resolved.") from exc
    addresses: list[str] = []
    for _, _, _, _, sockaddr in results:
        address = str(sockaddr[0])
        if not _is_public_ip(address):
            raise ArticleFetchSecurityError("Article URL resolves to a non-public address.")
        if address not in addresses:
            addresses.append(address)
    if not addresses:
        raise ArticleFetchSecurityError("Article URL host did not resolve to an address.")
    return parsed, addresses


def _article_request_target(parsed: Any) -> str:
    target = parsed.path or "/"
    if parsed.params:
        target += ";" + parsed.params
    if parsed.query:
        target += "?" + parsed.query
    return target


def _article_host_header(parsed: Any) -> str:
    host = parsed.hostname or ""
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
    if (parsed.scheme.lower(), port) not in {("http", 80), ("https", 443)}:
        host += f":{port}"
    return host


# ---------------------------------------------------------------------------
# SSL-pinned connection
# ---------------------------------------------------------------------------

class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """Connect to a validated IP while validating TLS for the original hostname."""

    def __init__(self, host: str, port: int, *, server_hostname: str, timeout: float) -> None:
        self._server_hostname = server_hostname
        super().__init__(host, port, timeout=timeout, context=ssl.create_default_context())

    def connect(self) -> None:
        self.sock = socket.create_connection((self.host, self.port), self.timeout, self.source_address)
        self.sock = self._context.wrap_socket(self.sock, server_hostname=self._server_hostname)


def _open_pinned_article_request(parsed: Any, addresses: list[str], timeout: float, headers: dict[str, str]) -> tuple[Any, Any]:
    """Open one request using a validated DNS result, preventing DNS rebinding."""
    errors: list[str] = []
    port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
    request_headers = {"Host": _article_host_header(parsed), "Connection": "close", **headers}
    for address in addresses:
        connection: Any = None
        try:
            if parsed.scheme.lower() == "https":
                connection = _PinnedHTTPSConnection(address, port, server_hostname=parsed.hostname, timeout=timeout)
            else:
                connection = http.client.HTTPConnection(address, port, timeout=timeout)
            connection.request("GET", _article_request_target(parsed), headers=request_headers)
            return connection.getresponse(), connection
        except (OSError, http.client.HTTPException, ssl.SSLError) as exc:
            errors.append(str(exc))
            if connection is not None:
                connection.close()
    detail = errors[-1] if errors else "no usable address"
    raise ToolError(f"Article request failed: {detail}")


# ---------------------------------------------------------------------------
# Response reading
# ---------------------------------------------------------------------------

def _read_article_response(response: Any, max_bytes: int) -> bytes:
    header = response.headers.get("Content-Length")
    if header:
        try:
            if int(header) > max_bytes:
                raise ToolError(f"Article response exceeds {max_bytes} byte limit.")
        except ValueError:
            pass
    body = response.read(max_bytes + 1)
    if len(body) > max_bytes:
        raise ToolError(f"Article response exceeds {max_bytes} byte limit.")
    return body


def _decode_article_response(body: bytes, response: Any) -> str:
    charset = response.headers.get_content_charset() or "utf-8"
    try:
        return body.decode(charset, errors="replace")
    except LookupError:
        return body.decode("utf-8", errors="replace")


# ---------------------------------------------------------------------------
# Main fetch entry point
# ---------------------------------------------------------------------------

def _fetch_article_text(url: str, timeout: float = 12, max_chars: int = 5000) -> dict[str, Any]:
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; qwen-stock-skill-agent/0.5; +https://example.local)",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    current_url = str(url or "").strip()
    max_redirects = _article_fetch_int_env("ARTICLE_FETCH_MAX_REDIRECTS", 4, 0, 10)
    max_bytes = _article_fetch_int_env(
        "ARTICLE_FETCH_MAX_RESPONSE_BYTES",
        _MAX_ARTICLE_RESPONSE_BYTES,
        1024,
        _MAX_ARTICLE_RESPONSE_BYTES,
    )
    for redirect_count in range(max_redirects + 1):
        parsed, addresses = _validate_article_url(current_url)
        response, connection = _open_pinned_article_request(parsed, addresses, timeout, headers)
        try:
            if response.status in _ARTICLE_REDIRECT_STATUSES:
                location = response.headers.get("Location")
                if not location:
                    raise ToolError("Article redirect did not include a Location header.")
                if redirect_count >= max_redirects:
                    raise ToolError(f"Article redirect limit exceeded ({max_redirects}).")
                current_url = urljoin(current_url, location)
                continue
            if response.status >= 400:
                raise ToolError(f"Article request returned HTTP {response.status}.")
            body = _read_article_response(response, max_bytes)
            content_type = response.headers.get("Content-Type", "")
            response_text = _decode_article_response(body, response)
        finally:
            response.close()
            connection.close()
        break
    else:  # pragma: no cover - loop exits through redirect limit above.
        raise ToolError("Article redirect handling failed.")

    if "html" not in content_type.lower() and not response_text.lstrip().startswith("<"):
        return {
            "url": url,
            "ok": False,
            "error": f"Unsupported content type: {content_type}",
            "status_code": response.status,
        }
    parser = _ArticleTextParser()
    parser.feed(response_text[: max(max_chars * 20, 120000)])
    text = parser.text
    if len(text) > max_chars:
        text = text[:max_chars].rsplit(" ", 1)[0] + " ...[TRUNCATED]"

    # Lazy imports to avoid circular dependency: tools.py imports this module
    # at load time, so we cannot import from tools at module scope.
    from .tools import _source_domain, _article_text_quality_ok, _is_blocked_or_consent_text

    record = {
        "url": url,
        "final_url": current_url,
        "ok": bool(text or parser.meta_description),
        "status_code": response.status,
        "title": parser.title,
        "meta_description": parser.meta_description,
        "published_date": parser.meta_date,
        "text": text,
        "text_chars": len(text),
        "source_domain": _source_domain(current_url),
    }
    record["article_text_quality_ok"] = _article_text_quality_ok(record)
    if not record["article_text_quality_ok"]:
        if _is_blocked_or_consent_text(" ".join([parser.title, parser.meta_description, text]), current_url):
            record["quality_reason"] = "blocked_or_consent_or_login_page"
        elif len(text) < int(os.environ.get("ARTICLE_MIN_TEXT_CHARS", "800")):
            record["quality_reason"] = f"text_too_short:{len(text)}"
        else:
            record["quality_reason"] = "missing_finance_context_or_numbers"
    return record


# ---------------------------------------------------------------------------
# Batch enrichment
# ---------------------------------------------------------------------------

def _enrich_evidence_with_articles(items: list[dict[str, Any]], max_urls: int, max_chars: int, timeout: float) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if max_urls <= 0:
        return items, []

    # Lazy imports to avoid circular dependency.
    from .tools import _source_domain, _source_quality_score

    enriched: list[dict[str, Any]] = []
    article_records: list[dict[str, Any]] = []
    candidates = sorted(items, key=lambda x: int(x.get("source_quality_score") or _source_quality_score(x)), reverse=True)
    urls_done = 0
    url_to_article: dict[str, dict[str, Any]] = {}
    for item in candidates:
        url = str(item.get("url") or "").strip()
        if not url or urls_done >= max_urls:
            break
        domain = _source_domain(url)
        # Skip sources that are commonly hostile to scraping or too low value unless they scored high.
        if int(item.get("source_quality_score") or _source_quality_score(item)) < 55 and not any(x in domain for x in ["oracle.com", "sec.gov"]):
            continue
        try:
            article = _fetch_article_text(url, timeout=timeout, max_chars=max_chars)
        except Exception as exc:
            article = {"url": url, "ok": False, "error": str(exc), "source_domain": domain}
        url_to_article[url] = article
        article_records.append(article)
        urls_done += 1

    for item in items:
        url = str(item.get("url") or "").strip()
        article = url_to_article.get(url)
        if article and article.get("ok"):
            item = dict(item)
            item["article_fetch_ok"] = True
            item["article_text_quality_ok"] = bool(article.get("article_text_quality_ok"))
            item["article_quality_reason"] = article.get("quality_reason", "")
            if article.get("published_date") and str(item.get("source_date") or "unknown").lower() == "unknown":
                item["source_date"] = article.get("published_date")
            title = article.get("title") or item.get("title")
            meta = article.get("meta_description") or ""
            body = article.get("text") or ""
            combined = " ".join(x for x in [meta, body] if x).strip()
            if combined and article.get("article_text_quality_ok"):
                item["article_text"] = combined[:max_chars]
                # facts remains compact but now grounded in fetched page text, not only SERP snippet.
                item["facts"] = (combined[:900].rsplit(" ", 1)[0] + " ...") if len(combined) > 900 else combined
            elif meta:
                item.setdefault("meta_description", meta)
            if title:
                item["title"] = title
        enriched.append(item)
    return enriched, article_records
