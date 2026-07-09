# Sentinel Security Journal

## 2025-05-15 - Protocol-based SSRF/LFI in urllib.request
**Vulnerability:** Python's `urllib.request.urlopen` supports multiple protocols by default, including `file://`, `gopher://`, and `ftp://`. If the URL is even partially user-controlled or source from configuration that isn't strictly validated, an attacker could read local system files (LFI) or probe internal services (SSRF).
**Learning:** This repo used `urlopen` for local health checks, TTS endpoints, and LLM providers. While intended for local loopback or specific APIs, the lack of scheme validation made it vulnerable to `file:///etc/passwd` style attacks.
**Prevention:** Always use a helper like `validate_http_url` to enforce `http` or `https` schemes before passing a URL to `urllib.request`. This acts as a critical fail-fast layer for network-bound requests.
