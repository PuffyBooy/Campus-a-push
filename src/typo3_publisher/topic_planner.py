"""Editorial planning: which topic / idea is written next."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from .config import TopicConfig, TopicsFile
from .models import Article, ArticleStatus, parse_iso
from .scheduler import Slot
from .state_manager import StateStore

PRIORITY_WEIGHT = {"high": 3.0, "medium": 2.0, "low": 1.0}
_COUNTED = {
    ArticleStatus.DRAFT,
    ArticleStatus.APPROVED,
    ArticleStatus.PUBLISHING,
    ArticleStatus.PUBLISHED,
    ArticleStatus.PUBLISH_FAILED,
    ArticleStatus.UPDATING,
}


@dataclass
class Assignment:
    topic: str
    idea: str = ""
    notes: str = ""
    source: str = "rotation"  # manual | backlog | config_backlog | rotation
    backlog_id: str = ""


def config_backlog_key(title: str) -> str:
    return hashlib.sha256(title.strip().lower().encode()).hexdigest()[:16]


class TopicPlanner:
    def __init__(self, topics: TopicsFile, store: StateStore, timezone: str):
        self.topics = topics
        self.store = store
        self.tz = ZoneInfo(timezone)

    def topic(self, name: str) -> TopicConfig | None:
        return next((t for t in self.topics.topics if t.name.lower() == name.lower()), None)

    def _week_key(self, dt: datetime) -> tuple[int, int]:
        iso_cal = dt.astimezone(self.tz).isocalendar()
        return iso_cal.year, iso_cal.week

    def weekly_counts(self, now: datetime, articles: list[Article] | None = None) -> dict[str, int]:
        week = self._week_key(now)
        counts = {t.name: 0 for t in self.topics.topics}
        for article in articles if articles is not None else self.store.list_articles():
            created = parse_iso(article.created_at)
            if article.status in _COUNTED and created and self._week_key(created) == week:
                counts[article.topic] = counts.get(article.topic, 0) + 1
        return counts

    def next_assignment(
        self, now: datetime, *, manual_idea: str = "", manual_topic: str = "", articles: list[Article] | None = None
    ) -> Assignment | None:
        if manual_idea:
            return Assignment(topic=manual_topic or self._guess_topic(manual_idea), idea=manual_idea, source="manual")

        for item in self.store.backlog():
            if item.get("status") == "pending":
                return Assignment(
                    topic=item.get("topic") or self._guess_topic(item["title"]),
                    idea=item["title"],
                    notes=item.get("notes", ""),
                    source="backlog",
                    backlog_id=item["id"],
                )

        used = self.store.used_config_backlog()
        for item in self.topics.backlog:
            if config_backlog_key(item.title) not in used:
                return Assignment(
                    topic=item.topic or self._guess_topic(item.title),
                    idea=item.title,
                    notes=item.notes,
                    source="config_backlog",
                    backlog_id=config_backlog_key(item.title),
                )

        if manual_topic:
            return Assignment(topic=manual_topic, source="manual")
        return self._rotation(now, articles)

    def _rotation(self, now: datetime, articles: list[Article] | None) -> Assignment | None:
        articles = articles if articles is not None else self.store.list_articles()
        counts = self.weekly_counts(now, articles)
        last_used: dict[str, str] = {}
        for a in articles:
            last_used[a.topic] = max(last_used.get(a.topic, ""), a.created_at)
        best: tuple[float, str, TopicConfig] | None = None
        for topic in self.topics.topics:
            if topic.articles_per_week <= 0:
                continue
            deficit = topic.articles_per_week - counts.get(topic.name, 0)
            if deficit <= 0:
                continue
            score = (deficit / topic.articles_per_week) * PRIORITY_WEIGHT.get(topic.priority, 1.0)
            # tie-break: least recently used first (empty string sorts first)
            key = (score, _invert(last_used.get(topic.name, "")), topic)
            if best is None or key[:2] > best[:2]:
                best = key
        return Assignment(topic=best[2].name) if best else None

    def _guess_topic(self, text: str) -> str:
        lowered = text.lower()
        for topic in self.topics.topics:
            terms = [topic.name.lower(), *[k.lower() for k in topic.keywords]]
            if any(term in lowered for term in terms):
                return topic.name
        return self.topics.topics[0].name if self.topics.topics else "General"

    def consume(self, assignment: Assignment, article_id: str) -> None:
        if assignment.source == "backlog":
            self.store.mark_backlog_item(assignment.backlog_id, "used", article_id)
        elif assignment.source == "config_backlog":
            self.store.mark_config_backlog_used(assignment.backlog_id)

    def calendar(self, now: datetime, slots: list[Slot]) -> list[dict]:
        """Simulated editorial calendar for upcoming slots (topic rotation only)."""
        simulated = list(self.store.list_articles())
        plan = []
        for slot in slots:
            assignment = self._rotation(slot.local_start, simulated)
            plan.append(
                {
                    "slot": slot.key,
                    "local_time": slot.local_start.isoformat(),
                    "topic": assignment.topic if assignment else None,
                }
            )
            if assignment:
                simulated.append(
                    Article(id=f"sim-{slot.key}", topic=assignment.topic, created_at=slot.local_start.isoformat())
                )
        return plan


def _invert(value: str) -> str:
    """Make older timestamps compare greater (so they win in a max())."""
    return "".join(chr(0x10FFFF - ord(c)) for c in value) if value else "\U0010ffff"
