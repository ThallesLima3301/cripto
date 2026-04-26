"""HTTP sender for ntfy.sh notifications.

This module does one thing: given an `NtfySettings` and a title/body,
POST the message to the configured ntfy server and return a
`SendResult`. It has no knowledge of the database, the alert policy,
or quiet hours — those concerns live in `service.py` and `policy.py`.

Design notes
------------
* `NTFY_TOPIC` is validated at send time, not at config-load time.
  The user may run `cli init` before they've picked a topic; we want
  that to be a visible "missing topic" result at the first attempted
  send rather than a hard config-load crash.

* The HTTP layer is injected via an `http_post` callable so the tests
  do not import `requests`. Default behavior is to lazy-import
  `requests.post` on first use, which keeps `requests` out of the
  import graph for pure-policy tests.

* Retries use an injected `sleeper` callable (defaults to
  `time.sleep`). Tests pass a no-op. Retries are limited to network
  errors and 5xx responses — 4xx responses are a permanent error and
  we return immediately so we don't spam ntfy with a request it has
  already rejected.

* Backoff is **exponential**: 1s, 2s, 4s, 8s between successive
  retries. `max_retries` in config caps the number of *additional*
  attempts after the first one, so `max_retries=2` means up to 3
  POSTs total (initial + 2 retries with 1s and 2s waits between).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Callable

from crypto_monitor.config.settings import NtfySettings


logger = logging.getLogger(__name__)


# Reason codes. A closed set so callers can branch without string typos.
REASON_SENT = "sent"
REASON_MISSING_TOPIC = "missing_topic"
REASON_HTTP_ERROR = "http_error"
REASON_NETWORK_ERROR = "network_error"


@dataclass(frozen=True)
class SendResult:
    """Outcome of a `send_ntfy` call.

    `sent` is True only when the HTTP POST returned a 2xx status.
    `reason` is one of the REASON_* constants above. `status_code` is
    populated whenever the server actually responded. `error` carries
    a short human-readable message on failure.
    """
    sent: bool
    reason: str
    status_code: int | None = None
    error: str | None = None


# Type alias for the injected HTTP POST callable. It must accept
# (url, data=..., headers=..., timeout=...) and return an object with
# a `.status_code` attribute (duck-typed against `requests.Response`).
HttpPost = Callable[..., Any]


def send_ntfy(
    ntfy: NtfySettings,
    title: str,
    body: str,
    *,
    priority: str = "default",
    tags: tuple[str, ...] = (),
    http_post: HttpPost | None = None,
    sleeper: Callable[[float], None] = time.sleep,
) -> SendResult:
    """POST a notification to the configured ntfy server.

    Returns a SendResult describing the outcome. Never raises; network
    and HTTP errors are reported as a SendResult with `sent=False`.
    """
    topic = (ntfy.topic or "").strip()
    if not topic:
        return SendResult(
            sent=False,
            reason=REASON_MISSING_TOPIC,
            error="NTFY_TOPIC is not set",
        )

    if http_post is None:
        http_post = _default_http_post()

    url = f"{ntfy.server_url.rstrip('/')}/{topic}"
    # HTTP/1.1 header values are restricted to Latin-1 by ``requests`` /
    # ``urllib3``. The weekly title carries an em-dash and the buy-side
    # notification copy carries Portuguese accented characters and
    # emoji — both unencodable as Latin-1 — so we wrap any non-ASCII
    # header value in RFC 2047. ntfy decodes ``=?utf-8?B?...?=`` back
    # to the original UTF-8 string server-side.
    # See https://docs.ntfy.sh/publish/#message-headers.
    headers = {
        "Title": _encode_header_value(title),
        "Priority": priority,
    }
    # Merge default tags with per-call tags, preserving order and
    # de-duplicating so a caller that passes a tag that is also in
    # `default_tags` does not produce two copies on the phone.
    merged_tags: list[str] = []
    for tag in (*ntfy.default_tags, *tags):
        if tag and tag not in merged_tags:
            merged_tags.append(tag)
    if merged_tags:
        # Tag values are conventionally ASCII (project tag names like
        # "crypto_monitor", "weekly", "test"), but the encoder is a
        # cheap no-op on ASCII so wrapping it is defensive and free.
        headers["Tags"] = _encode_header_value(",".join(merged_tags))

    encoded_body = body.encode("utf-8")

    attempts = max(1, ntfy.max_retries + 1)
    last_error: str | None = None
    last_status: int | None = None

    for attempt in range(1, attempts + 1):
        try:
            response = http_post(
                url,
                data=encoded_body,
                headers=headers,
                timeout=ntfy.request_timeout,
            )
        except Exception as exc:  # noqa: BLE001 — network layer is intentionally broad
            last_error = f"{type(exc).__name__}: {exc}"
            last_status = None
            logger.debug(
                "ntfy network error attempt %d/%d: %s",
                attempt, attempts, last_error,
            )
            if attempt < attempts:
                sleeper(_backoff_seconds(attempt))
                continue
            return SendResult(
                sent=False,
                reason=REASON_NETWORK_ERROR,
                status_code=None,
                error=last_error,
            )

        status = getattr(response, "status_code", None)
        last_status = status
        if status is not None and 200 <= status < 300:
            return SendResult(sent=True, reason=REASON_SENT, status_code=status)

        # 4xx = permanent error, do not retry.
        if status is not None and 400 <= status < 500:
            return SendResult(
                sent=False,
                reason=REASON_HTTP_ERROR,
                status_code=status,
                error=f"ntfy returned HTTP {status}",
            )

        # 5xx or unknown — retry if we have attempts left.
        last_error = f"ntfy returned HTTP {status}"
        logger.debug(
            "ntfy server error attempt %d/%d: %s",
            attempt, attempts, last_error,
        )
        if attempt < attempts:
            sleeper(_backoff_seconds(attempt))
            continue
        return SendResult(
            sent=False,
            reason=REASON_HTTP_ERROR,
            status_code=last_status,
            error=last_error,
        )

    # Unreachable — the loop always returns — but keeps the type checker happy.
    return SendResult(
        sent=False,
        reason=REASON_NETWORK_ERROR,
        status_code=last_status,
        error=last_error or "unknown error",
    )


# ---------- internals ----------

def _backoff_seconds(attempt: int) -> float:
    """Exponential backoff: 1s, 2s, 4s, 8s between retries.

    `attempt` is 1-indexed and is the attempt that just failed, so
    `attempt=1` yields a 1-second wait before the second try.
    """
    return float(2 ** (attempt - 1))


def _default_http_post() -> HttpPost:
    """Lazy-import `requests.post`. Raising here surfaces a clear error
    if `requests` is not installed, without paying for the import on
    the pure-policy path."""
    import requests  # type: ignore[import-not-found]

    return requests.post


def _encode_header_value(value: str) -> str:
    """Return ``value`` safe to put in an HTTP/1.1 header.

    HTTP headers default to Latin-1 in ``requests`` / ``urllib3``, so
    a value like ``"Resumo semanal — 04/04 a 11/04"`` raises
    ``UnicodeEncodeError`` at request time because the em-dash is
    outside Latin-1. We wrap such values as RFC 2047 base64
    (``=?utf-8?B?<base64>?=``) — ntfy explicitly supports this
    encoding for the ``Title`` header and decodes it back to the
    original UTF-8 string on the receiving phone.

    Pure-ASCII values pass through unchanged (the common case for
    ``priority``, project tags, and ``ntfy-test`` titles), so this
    helper is a no-op for them.
    """
    try:
        value.encode("ascii")
    except UnicodeEncodeError:
        import base64
        b64 = base64.b64encode(value.encode("utf-8")).decode("ascii")
        return f"=?utf-8?B?{b64}?="
    return value
