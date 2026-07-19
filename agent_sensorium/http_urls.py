"""Validation for configured HTTP endpoint URLs.

Configured endpoints are a trusted configuration and egress-policy boundary.
This module deliberately does not attempt generic SSRF prevention: local
sidecars and private-network providers are supported deployment patterns.
"""

from __future__ import annotations

from urllib.parse import urlsplit


def validate_http_endpoint_url(url: str) -> str:
    """Return *url* when it is an absolute HTTP(S) URL with a hostname.

    Accessing ``hostname`` and ``port`` also applies the standard library's
    validation for malformed bracketed hosts and invalid port values. Userinfo
    and valid ports remain supported intentionally.
    """
    if not isinstance(url, str):
        raise ValueError("HTTP endpoint URL must be a string")
    if not url or url != url.strip() or any(ord(char) < 32 or ord(char) == 127 for char in url):
        raise ValueError("HTTP endpoint URL must not contain surrounding whitespace or control characters")
    try:
        parsed = urlsplit(url)
        hostname = parsed.hostname
        parsed.port
    except ValueError as exc:
        raise ValueError(f"invalid HTTP endpoint URL: {url!r}") from exc
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("HTTP endpoint URL must use http or https")
    if not hostname:
        raise ValueError("HTTP endpoint URL must include a hostname")
    return url
