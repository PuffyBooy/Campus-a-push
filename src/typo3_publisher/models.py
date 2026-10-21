"""Data model of an article and its lifecycle."""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any


def utcnow() -> datetime:
    return datetime.now(UTC)


def iso(dt: datetime | None = None) -> str:
    return (dt or utcnow()).astimezone(UTC).isoformat(timespec="milliseconds")


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value)


_TRANSLIT = {"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss", "Ä": "Ae", "Ö": "Oe", "Ü": "Ue"}


def slugify(text: str, max_length: int = 80) -> str:
    for src, dst in _TRANSLIT.items():
        text = text.replace(src, dst)
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    if len(text) > max_length:
        text = text[:max_length].rsplit("-", 1)[0]
    return text or "article"


def new_article_id(seed: str, now: datetime | None = None) -> str:
    now = now or utcnow()
    digest = hashlib.sha256(f"{seed}|{now.isoformat()}".encode()).hexdigest()[:8]
    return f"{now:%Y%m%d-%H%M%S}-{digest}"


class ArticleStatus(StrEnum):
    DRAFT = "draft"
    APPROVED = "approved"
    REJECTED = "rejected"
    PUBLISHING = "publishing"
    PUBLISHED = "published"
    PUBLISH_FAILED = "publish_failed"
    UPDATING = "updating"


class PublicationStage(StrEnum):
    """Checkpoints of the browser workflow, persisted so a crashed run can resume
    without creating a duplicate TYPO3 record."""

    NONE = "none"
    STARTED = "started"  # about to click "save" on a NEW record
    SAVED = "saved"  # TYPO3 record exists, uid known
    PUBLISHED = "published"  # visibility enabled and saved
    VERIFIED = "verified"  # public page confirmed


@dataclass
class Source:
    title: str
    url: str
    published: str = ""
    excerpt: str = ""


@dataclass
class Claim:
    claim: str
    source_url: str = ""
    evidence: str = ""


@dataclass
class ArticleContent:
    title: str
    teaser: str
    body_html: str
    seo_title: str = ""
    meta_description: str = ""
    keywords: list[str] = field(default_factory=list)
    slug: str = ""
    image_keywords: list[str] = field(default_factory=list)
    article_type: str = "evergreen"  # evergreen | factual | opinion
    claims: list[Claim] = field(default_factory=list)
    sources: list[Source] = field(default_factory=list)


@dataclass
class Issue:
    severity: str  # critical | major | minor
    code: str
    message: str


@dataclass
class ReviewResult:
    iteration: int
    scores: dict[str, float]
    weighted_score: float
    passed: bool
    critical_issues: list[Issue] = field(default_factory=list)
    automated_issues: list[Issue] = field(default_factory=list)
    feedback: list[dict[str, Any]] = field(default_factory=list)
    summary: str = ""
    model: str = ""
    created_at: str = field(default_factory=iso)


@dataclass
class Revision:
    iteration: int
    content: ArticleContent
    note: str = ""
    created_at: str = field(default_factory=iso)


@dataclass
class ImageInfo:
    provider: str
    provider_id: str
    page_url: str
    download_url: str
    width: int
    height: int
    tags: list[str]
    user: str
    license_name: str
    license_url: str
    attribution: str
    alt_text: str
    relevance: float
    local_path: str = ""
    sha256: str = ""
    file_name: str = ""


@dataclass
class PublicationInfo:
    stage: PublicationStage = PublicationStage.NONE
    typo3_table: str = ""
    typo3_uid: int | None = None
    typo3_page_uid: int | None = None  # content_element profile: the page holding the content
    slug: str = ""
    public_url: str = ""
    published_at: str = ""
    verified_at: str = ""
    verification: dict[str, Any] = field(default_factory=dict)
    attempts: int = 0
    last_attempt_at: str = ""
    run_id: str = ""


@dataclass
class VersionEntry:
    version: int
    created_at: str
    reason: str
    content: ArticleContent
    public_url: str = ""


@dataclass
class ErrorEntry:
    at: str
    stage: str
    message: str
    run_id: str = ""


@dataclass
class Article:
    id: str
    topic: str
    status: ArticleStatus = ArticleStatus.DRAFT
    idea: str = ""
    idea_source: str = ""  # manual | backlog | rotation
    created_at: str = field(default_factory=iso)
    updated_at: str = field(default_factory=iso)
    content: ArticleContent | None = None
    revisions: list[Revision] = field(default_factory=list)
    reviews: list[ReviewResult] = field(default_factory=list)
    research_sources: list[Source] = field(default_factory=list)
    image: ImageInfo | None = None
    image_status: str = ""  # "" | found | none_suitable | not_required
    publication: PublicationInfo = field(default_factory=PublicationInfo)
    rejected_reasons: list[str] = field(default_factory=list)
    errors: list[ErrorEntry] = field(default_factory=list)
    versions: list[VersionEntry] = field(default_factory=list)
    slot_key: str = ""
    last_update_check: str = ""
    test_content: bool = False

    def touch(self) -> None:
        self.updated_at = iso()

    def add_error(self, stage: str, message: str, run_id: str = "") -> None:
        self.errors.append(ErrorEntry(at=iso(), stage=stage, message=message[:2000], run_id=run_id))
        self.errors = self.errors[-50:]
        self.touch()

    @property
    def latest_score(self) -> float | None:
        return self.reviews[-1].weighted_score if self.reviews else None
