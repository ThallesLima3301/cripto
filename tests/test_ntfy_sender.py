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


# ---------- RFC 2047 header encoding ----------

def test_ascii_title_passes_through_unchanged(ntfy_settings):
    """A pure-ASCII title is not wrapped — keeps backward compat for
    every existing call site (ntfy-test, buy alerts with friendly
    names like ``BTC``, the test fixtures elsewhere)."""
    post = RecordingPost([FakeResponse(200)])
    send_ntfy(
        ntfy_settings,
        "BTCUSDT STRONG 72",
        "signal body",
        http_post=post,
        sleeper=_no_sleep,
    )
    assert post.calls[0]["headers"]["Title"] == "BTCUSDT STRONG 72"


def test_non_ascii_title_is_rfc2047_encoded(ntfy_settings):
    """The weekly title carries an em-dash (U+2014); accent-rich
    Portuguese decision phrases on the buy side carry diacritics +
    emoji. Both must transit as RFC-2047 base64 so ``requests``
    accepts them and ntfy decodes them on the phone."""
    import base64
    post = RecordingPost([FakeResponse(200)])
    title = "Resumo semanal — 04/04 a 11/04"
    send_ntfy(
        ntfy_settings, title, "body",
        http_post=post, sleeper=_no_sleep,
    )
    sent_title = post.calls[0]["headers"]["Title"]
    # Header is now ASCII-only (this would have raised UnicodeEncodeError
    # under the old code path against a real HTTP layer).
    sent_title.encode("ascii")
    # Format: =?utf-8?B?<base64>?=
    assert sent_title.startswith("=?utf-8?B?")
    assert sent_title.endswith("?=")
    encoded = sent_title.removeprefix("=?utf-8?B?").removesuffix("?=")
    assert base64.b64decode(encoded).decode("utf-8") == title


def test_emoji_title_is_rfc2047_encoded(ntfy_settings):
    """The buy-side decision phrase ``🟡 Vale observar — BTC`` was the
    other long-standing breakage. Pin it explicitly."""
    post = RecordingPost([FakeResponse(200)])
    title = "🟡 Vale observar — BTC"
    send_ntfy(
        ntfy_settings, title, "body",
        http_post=post, sleeper=_no_sleep,
    )
    sent_title = post.calls[0]["headers"]["Title"]
    sent_title.encode("ascii")
    assert sent_title.startswith("=?utf-8?B?")


def test_default_tags_with_unicode_are_encoded(ntfy_settings):
    """Defensive: even though project tags are ASCII today, a future
    user-configured tag with non-ASCII must not crash the sender."""
    from dataclasses import replace
    post = RecordingPost([FakeResponse(200)])
    settings = replace(ntfy_settings, default_tags=("café",))
    send_ntfy(
        settings, "title", "body",
        http_post=post, sleeper=_no_sleep,
    )
    sent_tags = post.calls[0]["headers"]["Tags"]
    sent_tags.encode("ascii")
    assert sent_tags.startswith("=?utf-8?B?")


def test_weekly_send_succeeds_against_a_strict_ascii_http_layer(ntfy_settings):
    """End-to-end shape of the bug: an http_post layer that mirrors
    ``requests``/``urllib3`` (refusing non-ASCII headers) used to
    surface the em-dash title as a network_error. With RFC 2047 in
    place the same strict layer accepts the headers and returns 200."""

    class StrictAsciiPost:
        def __init__(self) -> None:
            self.calls: list[dict] = []

        def __call__(self, url, *, data, headers, timeout):
            for name, value in headers.items():
                # Same check requests/urllib3 perform under the hood.
                value.encode("latin-1")
            self.calls.append({"url": url, "headers": dict(headers)})
            return FakeResponse(200)

    post = StrictAsciiPost()
    result = send_ntfy(
        ntfy_settings,
        "Resumo semanal — 04/04 a 11/04",
        "body",
        priority="default",
        tags=("weekly",),
        http_post=post,
        sleeper=_no_sleep,
    )
    assert result.sent is True
    assert result.reason == REASON_SENT
    # The Title was actually accepted by the strict layer.
    assert "Title" in post.calls[0]["headers"]
