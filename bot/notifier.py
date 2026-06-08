"""
Phone push notifications via ntfy.sh.

ntfy is a free, no-account push service: pick a unique topic name, install the
ntfy app on your phone, subscribe to that topic, and anything POSTed to
`https://ntfy.sh/<topic>` shows up as a push notification.

Set `NTFY_TOPIC` in .env to enable. If unset, this module is a graceful no-op
so the executor works fine without push.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Iterable

import httpx

log = logging.getLogger("bot.notifier")


@dataclass(frozen=True)
class NotifierConfig:
    """Resolved notifier configuration.

    `topic` is the only required field; everything else has sensible defaults.
    """

    topic: str | None
    server: str = "https://ntfy.sh"
    default_priority: int = 3  # 1=min … 5=urgent; 3 is the ntfy default.

    @property
    def enabled(self) -> bool:
        return bool(self.topic)


def load_config() -> NotifierConfig:
    return NotifierConfig(
        topic=(os.environ.get("NTFY_TOPIC") or "").strip() or None,
        server=(os.environ.get("NTFY_SERVER") or "https://ntfy.sh").rstrip("/"),
    )


class Notifier:
    """Thin wrapper that POSTs to ntfy.sh, or no-ops if disabled."""

    def __init__(self, config: NotifierConfig | None = None):
        self.config = config or load_config()
        # Short timeout: notifications must never block the executor's hot path.
        # If ntfy is slow, drop the notification rather than backlog signals.
        self._client = httpx.Client(timeout=3.0) if self.config.enabled else None

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    def __enter__(self) -> "Notifier":
        return self

    def __exit__(self, *_a) -> None:
        self.close()

    def push(
        self,
        title: str,
        body: str,
        *,
        priority: int | None = None,
        tags: Iterable[str] = (),
        click_url: str | None = None,
    ) -> bool:
        """
        Send a push notification.

        Returns True on success, False on failure (network error, ntfy 4xx/5xx,
        or notifier disabled). Never raises.
        """
        if not self.config.enabled or self._client is None:
            return False

        url = f"{self.config.server}/{self.config.topic}"
        headers: dict[str, str] = {
            "Title": _encode_header(title),
            "Priority": str(priority or self.config.default_priority),
        }
        tag_str = ",".join(tags)
        if tag_str:
            headers["Tags"] = tag_str
        if click_url:
            headers["Click"] = click_url

        try:
            r = self._client.post(url, content=body.encode("utf-8"), headers=headers)
            r.raise_for_status()
            return True
        except httpx.HTTPError as e:
            log.warning("ntfy push failed: %s", e)
            return False


def _encode_header(s: str) -> str:
    """ntfy headers must be ASCII; replace non-ASCII chars with '?'."""
    return s.encode("ascii", errors="replace").decode("ascii")
