from __future__ import annotations

import httpx

from drifthunter.config import EdgarConfig
from drifthunter.ingest.http import EdgarClient


def _build_client(tmp_path, mock_transport):
    cfg = EdgarConfig(user_agent="test agent", max_requests_per_sec=1000)
    client = EdgarClient(cfg, tmp_path)
    # White-box: swap in a mocked transport so get_bytes hits no real network.
    client._client = httpx.Client(
        transport=mock_transport, headers={"User-Agent": cfg.user_agent}
    )
    return client


def test_get_bytes_retries_transport_error_then_succeeds(tmp_path, monkeypatch):
    monkeypatch.setattr("drifthunter.ingest.http.time.sleep", lambda _: None)
    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        if calls["count"] == 1:
            raise httpx.ConnectError("boom", request=request)
        return httpx.Response(200, content=b"ok")

    client = _build_client(tmp_path, httpx.MockTransport(handler))

    result = client.get_bytes("https://example.com/doc", "doc.bin")

    assert result == b"ok"
    assert calls["count"] == 2
    assert (tmp_path / "doc.bin").exists()


def test_get_bytes_retries_503_then_uses_cache_on_second_call(tmp_path, monkeypatch):
    monkeypatch.setattr("drifthunter.ingest.http.time.sleep", lambda _: None)
    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        if calls["count"] == 1:
            return httpx.Response(503)
        return httpx.Response(200, content=b"ok")

    client = _build_client(tmp_path, httpx.MockTransport(handler))

    first = client.get_bytes("https://example.com/doc", "doc.bin")
    assert first == b"ok"
    assert calls["count"] == 2

    second = client.get_bytes("https://example.com/doc", "doc.bin")
    assert second == b"ok"
    # No additional transport calls: served from disk cache.
    assert calls["count"] == 2
