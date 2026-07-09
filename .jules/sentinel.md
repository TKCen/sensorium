## 2026-07-08 - SSRF and LFI mitigation via URL scheme validation

**Vulnerability:** Several locations in the codebase used `urllib.request.urlopen` with configurable or external URLs (e.g., TTS base URL, health check URLs, internal admin API URLs) without validating the URL scheme. An attacker could provide a malicious URL using the `file://` scheme to read arbitrary system files (Local File Inclusion/LFI) or other protocols like `gopher://` to interact with internal services (Server-Side Request Forgery/SSRF).

**Learning:** `urllib.request.urlopen` is more permissive than other common HTTP libraries (like `requests` or `httpx`) and supports several non-HTTP schemes by default. Relying on documentation or "typical" usage is not sufficient for security; library-specific behavior must be accounted for.

**Prevention:** Always validate URL schemes before passing them to network libraries that support multiple protocols. Use a centralized validation helper (e.g., `validate_http_url`) to enforce `http` and `https` schemes consistently across the application.
