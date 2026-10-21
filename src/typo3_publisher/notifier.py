"""Notifications: optional webhook (Slack/Teams/generic) and the GitHub job summary.

Failure issues are opened by the workflow itself (actions/github-script), so a crash of
the Python process is reported too.
"""

from __future__ import annotations

import os
from typing import Any

import requests

from .config import NotificationSettings, Secrets
from .logging_utils import get_logger, redact

log = get_logger("notify")


class Notifier:
    def __init__(self, settings: NotificationSettings, secrets: Secrets, session: requests.Session | None = None):
        self.settings = settings
        self.webhook = secrets.get(settings.webhook_env)
        self.session = session or requests.Session()
        self.sent: list[dict[str, Any]] = []

    def notify(self, event: str, title: str, details: str = "") -> None:
        if event not in self.settings.notify_on:
            return
        text = redact(f"[typo3-publisher] {event.upper()}: {title}\n{details}".strip())
        self.sent.append({"event": event, "text": text})
        if not self.webhook:
            return
        try:
            # "text" works for Slack and Microsoft Teams incoming webhooks and most generic receivers.
            self.session.post(self.webhook, json={"text": text}, timeout=15).raise_for_status()
        except requests.RequestException as exc:
            log.warning("webhook notification failed: %s", redact(str(exc)))


def write_step_summary(markdown: str) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(redact(markdown) + "\n")
