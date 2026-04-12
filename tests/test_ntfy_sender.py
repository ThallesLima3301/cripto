"""Tests for `crypto_monitor.notifications.ntfy.send_ntfy`.

These tests never touch the real network. We pass a fake `http_post`
callable and a no-op `sleeper` so retry backoff is instant.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from crypto_monitor.notifications.ntfy import (
    REASON_HTTP_ERROR,
    REASON_MISSING_TOPIC,
    REASON_NETWORK_ERROR,
    REASON_SENT,
    send_ntfy,
)


@dataclass
class FakeResponse:
    status_code: int


class RecordingPost:
    """Callable that records every POST and returns a scripted response."""

    def __init__(self, responses: list[Any]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def __call__(
        self,
        url: str,
        *,
        data: bytes,
        headers: dict[str, str],
        timeout: int,
    ) -> Any:
        self.calls.append(
            {"url": url, "data": data, "headers": dict(headers), "timeout": timeout}
        )
        nxt = self._responses.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt


def _no_sleep(_: float) -> None:
    return None


# ---------- missing topic ----------

def test_missing_topic_returns_missing_topic(ntfy_settings_missing_topic):
    result = send_ntfy(
        ntfy_settings_missing_topic,
        "title",
        "body",
        http_post=RecordingPost([FakeResponse(200)]),
        sleeper=_no_sleep,
    )
    assert result.sent is False
    assert result.reason == REASON_MISSING_TOPIC


# ---------- happy path ----------

def test_successful_send_2xx(ntfy_settings):
    post = RecordingPost([FakeResponse(200)])
    result = send_ntfy(
        ntfy_settings,
        "BTCUSDT STRONG 72",
        "signal body",
        priority="high",
        tags=("fire",),
        http_post=post,
        sleeper=_no_sleep,
    )

    assert result.sent is True
    assert result.reason == REASON_SENT
    assert result.status_code == 200
    assert len(post.calls) == 1

    call = post.calls[0]
    assert call["url"] == "https://ntfy.example.test/test-topic"
    assert call["data"] == b"signal body"
    assert call["headers"]["Title"] == "BTCUSDT STRONG 72"
    assert call["headers"]["Priority"] == "high"
    # Default tags (crypto, v1) merged with explicit tag (fire), dedup'd and ordered.
    assert call["headers"]["Tags"] == "crypto,v1,fire"


def test_tags_are_deduplicated_against_defaults(ntfy_settings):
    post = RecordingPost([FakeResponse(200)])
    send_ntfy(
        ntfy_settings,
        "t",
        "b",
        tags=("crypto", "v1", "extra"),  # first two are already defaults
        http_post=post,
        sleeper=_no_sleep,
    )
    assert post.calls[0]["headers"]["Tags"] == "crypto,v1,extra"


# ---------- http errors ----------

def test_4xx_is_permanent_error_no_retry(ntfy_settings):
    post = RecordingPost([FakeResponse(403)])
    result = send_ntfy(
        ntfy_settings,
        "t",
        "b",
        http_post=post,
        sleeper=_no_sleep,
    )
    assert result.sent is False
    assert result.reason == REASON_HTTP_ERROR
    assert result.status_code == 403
    # 4xx must not trigger a retry.
    assert len(post.calls) == 1


def test_5xx_retries_then_succeeds(ntfy_settings):
    post = RecordingPost([FakeResponse(503), FakeResponse(200)])
    sleeps: list[float] = []
    result = send_ntfy(
        ntfy_settings,
        "t",
        "b",
        http_post=post,
        sleeper=sleeps.append,
    )
    assert result.sent is True
    assert result.reason == REASON_SENT
    assert len(post.calls) == 2
    # Exponential backoff: first retry waits 1s.
    assert sleeps == [1.0]


def test_5xx_exhausts_retries_and_reports_http_error(ntfy_settings):
    # max_retries=2 → 3 attempts, all 500.
    post = RecordingPost([FakeResponse(500), FakeResponse(500), FakeResponse(500)])
    sleeps: list[float] = []
    result = send_ntfy(
        ntfy_settings,
        "t",
        "b",
        http_post=post,
        sleeper=sleeps.append,
    )
    assert result.sent is False
    assert result.reason == REASON_HTTP_ERROR
    assert result.status_code == 500
    assert len(post.calls) == 3
    # Exponential: 1s, 2s between the three attempts.
    assert sleeps == [1.0, 2.0]


# ---------- network errors ----------

def test_network_error_retries_then_succeeds(ntfy_settings):
    post = RecordingPost([ConnectionError("boom"), FakeResponse(200)])
    result = send_ntfy(
        ntfy_settings,
        "t",
        "b",
        http_post=post,
        sleeper=_no_sleep,
    )
    assert result.sent is True
    assert result.reason == REASON_SENT
    assert len(post.calls) == 2


def test_network_error_exhausts_retries(ntfy_settings):
    post = RecordingPost(
        [
            ConnectionError("boom1"),
            ConnectionError("boom2"),
            ConnectionError("boom3"),
        ]
    )
    sleeps: list[float] = []
    result = send_ntfy(
        ntfy_settings,
        "t",
        "b",
        http_post=post,
        sleeper=sleeps.append,
    )
    assert result.sent is False
    assert result.reason == REASON_NETWORK_ERROR
    assert len(post.calls) == 3
    assert sleeps == [1.0, 2.0]
    assert result.error is not None
    assert "boom3" in result.error
