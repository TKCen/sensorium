"""Endpoint URL contract tests for dynamic urllib request paths."""

from __future__ import annotations

import json
import urllib.request

import pytest

from agent_sensorium import memory_reflection, sensors, subconscious, talking_head
from agent_sensorium.http_urls import validate_http_endpoint_url
from agent_sensorium.talking_head import TalkingHeadRequest


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:8888/health",
        "https://provider.example/v1",
        "http://127.0.0.1:8892/health",
        "http://[::1]:8188/queue",
    ],
)
def test_http_endpoint_validator_allows_loopback_and_public_urls(url):
    assert validate_http_endpoint_url(url) == url


@pytest.mark.parametrize(
    "url",
    [
        "file:///tmp/endpoint",
        "ftp://example.test/resource",
        "javascript:alert(1)",
        "//example.test/endpoint",
        "http:///missing-host",
        "https:",
        "http://[::1",
        "http://example.test:not-a-port",
        " http://example.test/health",
        "http://example.test/health\n",
        "http://example.test/health\x00",
    ],
)
def test_http_endpoint_validator_rejects_non_http_and_hostless_urls(url):
    with pytest.raises(ValueError):
        validate_http_endpoint_url(url)


def test_invalid_urls_do_not_reach_urlopen(monkeypatch, tmp_path):
    def fail_urlopen(*_args, **_kwargs):
        raise AssertionError("urlopen must not be called for an invalid endpoint URL")

    monkeypatch.setattr(urllib.request, "urlopen", fail_urlopen)

    with pytest.raises(ValueError):
        memory_reflection.HttpHindsightMemoryClient(base_url="file:///tmp/hindsight").reflect(
            query="test", timeout_s=1
        )
    with pytest.raises(ValueError):
        subconscious._post_openai_chat_completion("http:///missing-host", {}, {}, 1)
    with pytest.raises(ValueError):
        sensors._http_json("ftp://example.test/data", timeout_seconds=1)

    health = sensors._local_health_sample(url="//example.test/health", timeout_seconds=1)
    assert health == {"available": False, "error": "ValueError"}

    request = TalkingHeadRequest(
        script_file=tmp_path / "script.txt",
        source_still=tmp_path / "source.png",
        slug="test",
        tts_base_url="javascript:alert(1)",
    )
    with pytest.raises(ValueError):
        talking_head._generate_chatterbox_audio(request, "hello", tmp_path / "audio.wav")


@pytest.mark.parametrize("url", ["http://127.0.0.1:8188/queue", "https://provider.example/v1/queue"])
def test_valid_loopback_and_public_urls_still_use_http_fetch(monkeypatch, url):
    seen: list[str] = []

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, *_args):
            return json.dumps({"ok": True}).encode()

    def fake_urlopen(request, **_kwargs):
        seen.append(request.full_url)
        return Response()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    assert sensors._http_json(url, timeout_seconds=1) == {"ok": True}
    assert seen == [url]
