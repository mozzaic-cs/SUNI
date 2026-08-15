"""
Web tools — search and fetch for SUNI.
Uses DuckDuckGo (no API key) and requests for URL fetching.
"""
from __future__ import annotations
import ipaddress
import re
import socket
import textwrap
from html.parser import HTMLParser
from urllib.parse import urlparse, urljoin

import requests

_SESSION = requests.Session()
_SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (compatible; SUNI/1.0)"
})
_TIMEOUT = 12
_MAX_REDIRECTS = 5


def _is_safe_url(url: str) -> tuple[bool, str]:
    """
    SSRF guard: allow only http(s) URLs whose host resolves entirely to public
    addresses. Blocks loopback, private (RFC-1918), link-local (incl. the
    169.254.169.254 cloud-metadata IP), reserved, multicast and unspecified
    ranges. Residual risk: DNS rebinding between this check and the actual
    connect — acceptable for a localhost dev tool.
    """
    try:
        parsed = urlparse(url)
    except Exception:
        return False, "unparseable URL"
    if parsed.scheme not in ("http", "https"):
        return False, f"scheme '{parsed.scheme}' not allowed"
    host = parsed.hostname
    if not host:
        return False, "missing host"
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except Exception:
        return False, "DNS resolution failed"
    for info in infos:
        ip_str = info[4][0]
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError:
            return False, "invalid resolved address"
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified):
            return False, f"host resolves to non-public address {ip_str}"
    return True, ""

# ---------------------------------------------------------------------------
# HTML stripper
# ---------------------------------------------------------------------------

class _Stripper(HTMLParser):
    SKIP = {"script", "style", "noscript", "nav", "footer", "header", "aside"}

    def __init__(self):
        super().__init__()
        self._parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self._skip += 1

    def handle_endtag(self, tag):
        if tag in self.SKIP and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip:
            self._parts.append(data)

    def get_text(self) -> str:
        raw = " ".join(self._parts)
        raw = re.sub(r"\s{2,}", " ", raw)
        return raw.strip()


def _strip_html(html: str) -> str:
    p = _Stripper()
    try:
        p.feed(html)
    except Exception:
        pass
    return p.get_text()


# ---------------------------------------------------------------------------
# Schemas + handlers
# ---------------------------------------------------------------------------

FETCH_SCHEMA = {
    "name": "web_fetch",
    "description": (
        "Fetch the text content of any public URL. "
        "Use this to read articles, documentation, or any web page. "
        "Returns up to 4000 characters of clean text."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "The full URL to fetch (must include https://)"},
            "max_chars": {"type": "integer", "description": "Max characters to return (default 4000)", "default": 4000},
        },
        "required": ["url"],
    },
}


async def fetch_url(url: str, max_chars: int = 4000) -> str:
    try:
        # Follow redirects manually so every hop is SSRF-checked — a public URL
        # can 30x-redirect into an internal one, which allow_redirects would follow.
        current = url
        for _ in range(_MAX_REDIRECTS + 1):
            ok, reason = _is_safe_url(current)
            if not ok:
                return f"Fetch error: blocked target ({reason})"
            r = _SESSION.get(current, timeout=_TIMEOUT, allow_redirects=False)
            if r.is_redirect or r.is_permanent_redirect:
                loc = r.headers.get("location")
                if not loc:
                    break
                current = urljoin(current, loc)
                continue
            r.raise_for_status()
            ct = r.headers.get("content-type", "")
            text = _strip_html(r.text) if "html" in ct else r.text
            text = text[:max_chars]
            return text or "(no readable content)"
        return "Fetch error: too many redirects"
    except requests.RequestException as e:
        return f"Fetch error: {e}"


# ---------------------------------------------------------------------------

SEARCH_SCHEMA = {
    "name": "web_search",
    "description": (
        "Search the web using DuckDuckGo. Returns titles, URLs, and snippets. "
        "Use this whenever you need current information, news, or facts not in your training data."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query":       {"type": "string", "description": "The search query"},
            "max_results": {"type": "integer", "description": "Number of results to return (default 6)", "default": 6},
        },
        "required": ["query"],
    },
}


def _ddg_search(query: str, max_results: int) -> list[dict]:
    """DuckDuckGo HTML search — no API key required."""
    try:
        r = _SESSION.post(
            "https://html.duckduckgo.com/html/",
            data={"q": query, "b": "", "kl": "wt-wt"},
            timeout=_TIMEOUT,
        )
        r.raise_for_status()
    except requests.RequestException as e:
        return [{"error": str(e)}]

    results = []
    # Parse result blocks: each has class="result"
    blocks = re.findall(
        r'class="result__title".*?href="([^"]+)"[^>]*>(.*?)</a>.*?class="result__snippet"[^>]*>(.*?)</div>',
        r.text, re.DOTALL
    )
    for url, title, snippet in blocks[:max_results]:
        # DDG wraps URLs in a redirect — extract real URL
        real_url = re.search(r"uddg=([^&]+)", url)
        real_url = requests.utils.unquote(real_url.group(1)) if real_url else url
        title   = _strip_html(title).strip()
        snippet = _strip_html(snippet).strip()
        if title and real_url:
            results.append({"title": title, "url": real_url, "snippet": snippet})

    return results


async def search(query: str, max_results: int = 6) -> str:
    results = _ddg_search(query, max_results)
    if not results:
        return "No results found."
    if "error" in results[0]:
        return f"Search error: {results[0]['error']}"
    lines = []
    for i, r in enumerate(results, 1):
        lines.append(f"{i}. {r['title']}\n   {r['url']}\n   {r.get('snippet','')}")
    return "\n\n".join(lines)
