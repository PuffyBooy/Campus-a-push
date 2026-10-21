"""Pipeline orchestration:

Topic selection → research → generation → editorial review/revision → image search →
TYPO3 browser login → record creation → image upload → save → publish → public
verification → history update.

Every state change is persisted (and, in GitHub Actions, committed to the state branch)
before the next side effect, so a run can crash at any point without losing work or
publishing twice.
"""

from __future__ import annotations

import contextlib
import copy
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from .config import Settings, TopicsFile
from .content_generator import ContentGenerator
from .editorial_reviewer import EditorialLoop, EditorialReviewer
from .html_utils import extract_links
from .image_finder import ImageFinder, ImageTextWriter
from .llm import FakeLLM, LLMClient
from .logging_utils import get_logger
from .models import (
    Article,
    ArticleStatus,
    PublicationInfo,
    PublicationStage,
    Revision,
    VersionEntry,
    iso,
    new_article_id,
    parse_iso,
)
from .notifier import Notifier
from .research import ResearchBundle, ResearchDocument, Researcher
from .scheduler import select_slot
from .serialization import from_dict, to_dict
from .state_manager import StateStore, StateSync
from .topic_planner import Assignment, TopicPlanner
from .typo3_browser import AuthChallengeError, PublishOptions, ReconciliationRequiredError
from .verifier import PublicVerifier

log = get_logger("manager")


class PublisherSession(Protocol):
    def publish(
        self, article: Article, options: PublishOptions, checkpoint: Callable[[PublicationInfo], None]
    ) -> PublicationInfo: ...

    def update(
        self, article: Article, options: PublishOptions, checkpoint: Callable[[PublicationInfo], None]
    ) -> PublicationInfo: ...

    def preview(self, article: Article, options: PublishOptions) -> list[str]: ...


PublisherFactory = Callable[[], contextlib.AbstractContextManager[PublisherSession]]


class PublishingDisabledError(RuntimeError):
    pass


@dataclass
class RunSummary:
    run_id: str
    mode: str
    dry_run: bool
    started_at: str = field(default_factory=iso)
    finished_at: str = ""
    slot: str = ""
    generated: list[str] = field(default_factory=list)
    approved: list[str] = field(default_factory=list)
    rejected: list[str] = field(default_factory=list)
    published: list[dict[str, Any]] = field(default_factory=list)
    drafts: list[dict[str, Any]] = field(default_factory=list)  # campus_frontend: saved, not published
    updated: list[dict[str, Any]] = field(default_factory=list)
    failures: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    needs_human: bool = False

    @property
    def ok(self) -> bool:
        return not self.failures

    def markdown(self) -> str:
        lines = [f"### TYPO3 publisher run `{self.run_id}` ({self.mode}{', dry run' if self.dry_run else ''})", ""]
        if self.slot:
            lines.append(f"- Slot: `{self.slot}`")
        for label, items in (("Generated", self.generated), ("Approved", self.approved), ("Rejected", self.rejected)):
            if items:
                lines.append(f"- {label}: {', '.join(items)}")
        for p in self.published:
            lines.append(f"- Published **{p['title']}** → {p.get('url', '')}")
        for d in self.drafts:
            lines.append(f"- Saved as draft **{d['title']}** → {d.get('url', '')} (review and publish it yourself)")
            lines += [f"  - To do: {t}" for t in d.get("todos", [])]
        for u in self.updated:
            lines.append(f"- Updated **{u['title']}** → {u.get('url', '')}")
        for f in self.failures:
            lines.append(f"- ❌ {f.get('article', '')} [{f['stage']}]: {f['error']}")
        lines += [f"- {n}" for n in self.notes]
        return "\n".join(lines) + "\n"


class ArticleManager:
    def __init__(
        self,
        *,
        settings: Settings,
        topics: TopicsFile,
        store: StateStore,
        sync: StateSync,
        llm: LLMClient,
        editorial_prompt: str,
        review_rubric: str,
        researcher: Researcher,
        image_finder: ImageFinder | None,
        verifier: PublicVerifier,
        publisher_factory: PublisherFactory,
        notifier: Notifier,
        run_id: str,
        dry_run: bool = False,
    ):
        self.settings = settings
        self.topics = topics
        self.store = store
        self.sync = sync
        self.llm = llm
        self.generator = ContentGenerator(settings, llm, editorial_prompt)
        self.reviewer = EditorialReviewer(settings, llm, review_rubric, editorial_prompt)
        self.loop = EditorialLoop(settings, self.generator, self.reviewer)
        self.researcher = researcher
        self.image_finder = image_finder
        self.verifier = verifier
        self.publisher_factory = publisher_factory
        self.notifier = notifier
        self.image_text = ImageTextWriter(settings.images, llm, settings.llm.writer_model, settings.website.language)
        self.run_id = run_id
        self.dry_run = dry_run
        self.planner = TopicPlanner(topics, store, settings.publishing.timezone)

    # ------------------------------------------------------------------ persistence
    def checkpoint(self, article: Article, message: str) -> None:
        self.store.save_article(article)
        self.sync.commit(f"{message} ({article.id})")

    def _fail(self, summary: RunSummary, article: Article | None, stage: str, exc: BaseException | str) -> None:
        message = str(exc)
        summary.failures.append({"article": article.id if article else "", "stage": stage, "error": message})
        if article is not None:
            article.add_error(stage, message, self.run_id)
            self.checkpoint(article, f"error in {stage}")
        self.notifier.notify("failure", f"{stage} failed" + (f" for {article.id}" if article else ""), message)

    # ------------------------------------------------------------------ research
    def _research(self, topic: str, idea: str) -> ResearchBundle:
        cfg = self.planner.topic(topic)
        return self.researcher.research(topic, idea, cfg.keywords if cfg else [])

    def _save_research(self, article: Article, bundle: ResearchBundle) -> None:
        self.store.save_research(article.id, [to_dict(d) for d in bundle.documents])

    def _load_research(self, article: Article) -> ResearchBundle:
        return ResearchBundle(
            [from_dict(ResearchDocument, d, strict=False) for d in self.store.load_research(article.id)]
        )

    # ------------------------------------------------------------------ stages
    def generate(self, assignment: Assignment, summary: RunSummary) -> Article:
        research = self._research(assignment.topic, assignment.idea)
        recent = [
            a.content.title for a in self.store.list_articles() if a.content and a.status != ArticleStatus.REJECTED
        ][-self.settings.editorial.recent_titles_context :]
        content = self.generator.generate(
            self.planner.topic(assignment.topic), assignment.topic, assignment.idea, assignment.notes, research, recent
        )
        article = Article(
            id=new_article_id(f"{assignment.topic}|{content.title}"),
            topic=assignment.topic,
            idea=assignment.idea,
            idea_source=assignment.source,
            content=content,
            research_sources=research.sources,
            revisions=[Revision(iteration=0, content=content, note="first draft")],
            test_content=isinstance(self.llm, FakeLLM) or bool(getattr(self.llm, "is_fake", False)),
        )
        self._save_research(article, research)
        self.planner.consume(assignment, article.id)
        self.checkpoint(article, "Generate draft")
        summary.generated.append(article.id)
        log.info("generated draft %s: %s", article.id, content.title)
        return article

    def review(self, article: Article, summary: RunSummary, research: ResearchBundle | None = None) -> Article:
        research = research if research is not None else self._load_research(article)
        existing = [a for a in self.store.list_articles() if a.id != article.id]
        self.loop.run(article, research, existing)
        self.checkpoint(article, f"Editorial review: {article.status}")
        if article.status == ArticleStatus.APPROVED:
            summary.approved.append(article.id)
        else:
            summary.rejected.append(article.id)
            self.notifier.notify("rejected", f"article {article.id} rejected", "\n".join(article.rejected_reasons))
        return article

    def prepare_image(self, article: Article) -> bool:
        """Make sure a suitable image file is available locally. False = cannot publish (yet)."""
        assert article.content is not None
        img = article.image
        if img and img.local_path and Path(img.local_path).exists():
            return self._ensure_image_text(article)
        if self.image_finder is None:
            article.image_status = "not_required"
            return not self.settings.images.required
        if img:  # selected in an earlier run; the runner's disk is gone - download again
            try:
                path, sha = self.image_finder.download(
                    {"id": img.provider_id, "largeImageURL": img.download_url},
                    article.content.slug or article.content.title,
                )
                if not img.sha256 or sha == img.sha256:
                    img.local_path = str(path)
                    img.file_name = path.name
                    return self._ensure_image_text(article)
            except Exception as exc:
                log.warning("re-download of image %s failed (%s); searching again", img.provider_id, exc)
            article.image = None
        found = self.image_finder.find(article.content, article.topic)
        if found is None:
            article.image_status = "none_suitable"
            return not self.settings.images.required
        article.image = found
        article.image_status = "found"
        if not self.dry_run:
            self.store.register_image(found.provider, found.provider_id, found.sha256, article.id)
        return self._ensure_image_text(article)

    def _ensure_image_text(self, article: Article) -> bool:
        """Caption (Bildtext) and description (Bildbeschreibung) are written once per image."""
        img = article.image
        assert img is not None and article.content is not None
        wants_vision = self.settings.images.text.mode == "vision" and img.text_source != "vision"
        if wants_vision or not img.caption or not img.alt_text:
            self.image_text.describe(img, article.content)
        return True

    def _options(self, article: Article, publish: bool = True) -> PublishOptions:
        topic_cfg = self.planner.topic(article.topic)
        p = self.settings.publishing
        categories = (
            p.categories_by_topic.get(article.topic)
            or (topic_cfg.categories if topic_cfg else [])
            or p.default_categories
        )
        att = self.settings.images.attribution
        return PublishOptions(
            publish=publish, categories=list(categories), attribution_target=att.target, attribution_enabled=att.enabled
        )

    def publish(self, article: Article, summary: RunSummary) -> bool:
        """Create/resume the TYPO3 record, publish it and verify the public page."""
        p = self.settings.publishing
        if not self.dry_run and not p.enabled:
            raise PublishingDisabledError("publishing.enabled is false (or BOT_PAUSED is set)")
        if article.test_content and not p.allow_test_content and not self.dry_run:
            raise PublishingDisabledError(f"article {article.id} is offline test content and must not be published")
        if article.content is None:
            raise ValueError("article without content")
        pub = article.publication

        # Previously published in the backend but not verified yet: try verification first.
        if pub.stage == PublicationStage.PUBLISHED and not self.dry_run and self._verify(article, summary, quiet=True):
            return True

        try:
            if not self.prepare_image(article):
                msg = "no suitable image found and images.required is true - not publishing an unrelated image"
                article.add_error("image", msg, self.run_id)
                self.checkpoint(article, "No suitable image")
                summary.notes.append(f"{article.id}: {msg}")
                return False
        except Exception as exc:
            self._fail(summary, article, "image", exc)
            return False

        options = self._options(article)
        if self.dry_run:
            try:
                with self.publisher_factory() as session:
                    shots = session.preview(article, options)
                summary.notes.append(
                    f"dry run: filled the TYPO3 form for '{article.content.title}' without saving "
                    f"({len(shots)} diagnostic file(s))"
                )
                summary.notes += [f"To do: {t}" for t in (article.reviews[-1].author_todos if article.reviews else [])]
            except Exception as exc:
                self._fail(summary, None, "dry-run preview", exc)
            return False

        previous_status = article.status
        article.status = ArticleStatus.PUBLISHING
        pub.attempts += 1
        pub.last_attempt_at = iso()
        pub.run_id = self.run_id
        self.checkpoint(article, "Start publishing")
        try:
            with self.publisher_factory() as session:
                method = session.update if previous_status == ArticleStatus.UPDATING else session.publish
                method(article, options, lambda _p: self.checkpoint(article, f"Publish checkpoint {_p.stage}"))
        except AuthChallengeError as exc:
            article.status = ArticleStatus.PUBLISH_FAILED
            summary.needs_human = True
            self._fail(summary, article, f"login ({exc.kind})", exc)
            raise
        except ReconciliationRequiredError as exc:
            article.status = ArticleStatus.PUBLISH_FAILED
            summary.needs_human = True
            self._fail(summary, article, "reconciliation", exc)
            return False
        except Exception as exc:
            article.status = ArticleStatus.PUBLISH_FAILED
            self._fail(summary, article, "typo3", exc)
            return False
        if pub.stage == PublicationStage.DRAFT_SAVED:
            return self._draft_saved(article, summary)
        return self._verify(article, summary)

    def _draft_saved(self, article: Article, summary: RunSummary) -> bool:
        """campus_frontend: the article is a draft on the website. Nothing public to verify."""
        assert article.content is not None
        url = article.publication.verification.get("draft_saved_url", "")
        todos = article.reviews[-1].author_todos if article.reviews else []
        article.status = ArticleStatus.DRAFT_SAVED
        self.checkpoint(article, "Saved as draft on the website")
        summary.drafts.append({"id": article.id, "title": article.content.title, "url": url, "todos": todos})
        self.notifier.notify(
            "draft_saved",
            article.content.title,
            "Saved as draft (marked as AI-assisted). Review, edit and publish it yourself on the website."
            + "".join(f"\n- {t}" for t in todos),
        )
        return True

    def _verify(self, article: Article, summary: RunSummary, quiet: bool = False) -> bool:
        assert article.content is not None
        pub = article.publication
        result = self.verifier.verify(
            pub.slug or article.content.slug,
            article.content.title,
            article.content.body_html,
            image_name=article.image.file_name if article.image else "",
            backend_view_url=pub.verification.get("backend_view_url", ""),
            known_url=pub.public_url,
        )
        pub.verification = {**pub.verification, **result.as_dict()}
        if result.ok:
            pub.public_url = result.url
            pub.stage = PublicationStage.VERIFIED
            pub.verified_at = iso()
            article.status = ArticleStatus.PUBLISHED
            self.checkpoint(article, "Published and verified")
            summary.published.append({"id": article.id, "title": article.content.title, "url": result.url})
            self.notifier.notify("published", article.content.title, result.url)
            return True
        if quiet:
            return False
        article.status = ArticleStatus.PUBLISH_FAILED
        self._fail(
            summary,
            article,
            "public verification",
            "saved and published in TYPO3, but the public page could not be verified: " + "; ".join(result.reasons),
        )
        return False

    # ------------------------------------------------------------------ modes
    def retryable(self) -> list[Article]:
        limit = self.settings.publishing.max_publish_attempts
        return [
            a
            for a in self.store.list_articles(ArticleStatus.PUBLISHING, ArticleStatus.PUBLISH_FAILED)
            if a.publication.attempts < limit
        ]

    def approved_queue(self) -> list[Article]:
        return self.store.list_articles(ArticleStatus.APPROVED)

    def run_generate_only(
        self, summary: RunSummary, now: datetime, count: int = 1, idea: str = "", topic: str = ""
    ) -> list[Article]:
        articles = []
        for _ in range(count):
            assignment = self.planner.next_assignment(now, manual_idea=idea, manual_topic=topic)
            if assignment is None:
                summary.notes.append("weekly topic quotas are fulfilled; nothing to generate")
                break
            articles.append(self.generate(assignment, summary))
            idea = ""
        return articles

    def run_review_only(self, summary: RunSummary, article_id: str = "") -> None:
        drafts = [self.store.load_article(article_id)] if article_id else self.store.list_articles(ArticleStatus.DRAFT)
        if not drafts:
            summary.notes.append("no drafts waiting for review")
        for article in drafts:
            self.review(article, summary)

    def run_prepare(self, summary: RunSummary, now: datetime, idea: str = "", topic: str = "") -> Article | None:
        """Generate and review until one article is approved (bounded by max_generation_attempts).

        A draft that a previous run generated but did not review (e.g. it stopped at an LLM rate
        limit) is reviewed first instead of writing a new one."""
        pending = (
            [] if idea else [a for a in self.store.list_articles(ArticleStatus.DRAFT) if self.planner.topic(a.topic)]
        )
        for _ in range(max(1, self.settings.publishing.max_generation_attempts)):
            if pending:
                log.info("reviewing pending draft %s from an earlier run", pending[0].id)
                article = self.review(pending.pop(0), summary)
                if article.status == ArticleStatus.APPROVED:
                    return article
                continue
            generated = self.run_generate_only(summary, now, 1, idea, topic)
            if not generated:
                return None
            article = self.review(generated[0], summary)
            if article.status == ArticleStatus.APPROVED:
                return article
            idea = ""
        return None

    def run_publish_approved(self, summary: RunSummary, limit: int = 1, article_id: str = "") -> None:
        if article_id:
            queue = [self.store.load_article(article_id)]
            if queue[0].status not in {ArticleStatus.APPROVED, ArticleStatus.PUBLISH_FAILED, ArticleStatus.PUBLISHING}:
                raise ValueError(
                    f"article {article_id} has status {queue[0].status}; only approved or failed "
                    "articles can be published"
                )
        else:
            queue = (self.retryable() + self.approved_queue())[:limit]
        if not queue:
            summary.notes.append("no approved articles waiting")
        for article in queue[:limit]:
            self.publish(article, summary)

    def run_full(self, summary: RunSummary, now: datetime, idea: str = "", topic: str = "") -> bool:
        """One publication: resume a failed one, else publish the approved backlog, else write a new one."""
        p = self.settings.publishing
        for article in self.retryable():
            log.info("resuming incomplete publication %s (attempt %d)", article.id, article.publication.attempts + 1)
            if self.publish(article, summary):
                return True
        if p.prefer_approved_backlog and not idea:
            for article in self.approved_queue():
                if p.auto_publish or self.dry_run:
                    return self.publish(article, summary)
        article = self.run_prepare(summary, now, idea, topic)
        if article is None:
            return False
        if not p.auto_publish and not self.dry_run:
            summary.notes.append(f"{article.id} approved; auto_publish is off - run mode publish-approved to publish")
            return False
        return self.publish(article, summary)

    def run_scheduled(self, summary: RunSummary, now: datetime, force: bool = False) -> None:
        p = self.settings.publishing
        if not p.enabled and not self.dry_run:
            summary.notes.append("publishing is disabled (publishing.enabled=false or BOT_PAUSED) - nothing to do")
            return
        slot, skipped = select_slot(now, p, self.store.slots())
        for s in skipped:
            self.store.set_slot(s.key, status="skipped", run_id=self.run_id)
        if slot is None and not force:
            summary.notes.append("no publishing slot is due (or it was already served)")
            if skipped:
                self.sync.commit("Mark skipped slots")
            return
        key = slot.key if slot else f"forced-{iso()}"
        summary.slot = key
        self.store.set_slot(key, status="claimed", run_id=self.run_id)
        self.sync.commit(f"Claim slot {key}")
        published_before, drafts_before = len(summary.published), len(summary.drafts)
        try:
            self.run_full(summary, now)
        finally:
            ids = [x["id"] for x in summary.published[published_before:] + summary.drafts[drafts_before:]]
            done = bool(ids)
            self.store.set_slot(
                key, status="published" if done else "attempted", run_id=self.run_id, article_ids=ids, finished_at=iso()
            )
            self.sync.commit(f"Slot {key} {'served' if done else 'attempted'}")

    def run_update(self, summary: RunSummary, now: datetime, article_id: str = "") -> None:
        u = self.settings.updates
        if not u.enabled:
            raise PublishingDisabledError("updates.enabled is false - the update workflow must be enabled explicitly")
        if article_id:
            candidates = [self.store.load_article(article_id)]
        else:
            cutoff = now - timedelta(days=u.min_age_days)
            candidates = [
                a
                for a in self.store.list_articles(ArticleStatus.PUBLISHED)
                if (parse_iso(a.publication.published_at) or now) <= cutoff
                and (parse_iso(a.last_update_check) or datetime.min.replace(tzinfo=now.tzinfo)) <= cutoff
            ]
            candidates.sort(key=lambda a: a.last_update_check or a.publication.published_at)
        if not candidates:
            summary.notes.append("no published article is due for an update check")
        for article in candidates[: u.max_updates_per_run]:
            self.update_article(article, summary, now)

    def update_article(self, article: Article, summary: RunSummary, now: datetime) -> None:
        if article.status != ArticleStatus.PUBLISHED or article.content is None:
            raise ValueError(f"article {article.id} is not published; only published articles can be updated")
        research = self._research(article.topic, article.content.title)
        age = (now - (parse_iso(article.publication.published_at) or now)).days
        assessment = self.reviewer.assess_update(article.content, age, research)
        article.last_update_check = iso()
        if not assessment["needs_update"] and assessment["score"] >= self.settings.updates.update_if_score_below:
            self.checkpoint(article, "Update check: no update needed")
            summary.notes.append(f"{article.id}: no update needed (score {assessment['score']})")
            return
        old = article.content
        links = extract_links(old.body_html) if self.settings.updates.preserve_links else []
        new = self.generator.improve_existing(old, assessment["reasons"] or ["general improvement"], research, links)
        new.slug = old.slug  # never change the public URL
        candidate = copy.deepcopy(article)
        candidate.content = new
        candidate.reviews = []
        prior_sources = ResearchBundle(research.documents + self._load_research(article).documents)
        existing = [a for a in self.store.list_articles() if a.id != article.id]
        self.loop.run(candidate, prior_sources, existing, allowed_links=links)
        missing = [link for link in links if link not in candidate.content.body_html]
        if candidate.status != ArticleStatus.APPROVED or missing:
            reasons = candidate.rejected_reasons + [f"existing link removed: {m}" for m in missing]
            article.add_error("update-review", "; ".join(reasons), self.run_id)
            self.checkpoint(article, "Update rejected")
            summary.rejected.append(article.id)
            return
        article.versions.append(
            VersionEntry(
                version=len(article.versions) + 1,
                created_at=iso(),
                reason="; ".join(assessment["reasons"])[:500],
                content=old,
                public_url=article.publication.public_url,
            )
        )
        article.versions = article.versions[-self.settings.updates.max_versions_kept :]
        article.content = candidate.content
        article.reviews += candidate.reviews
        article.revisions += candidate.revisions[len(article.revisions) :]
        article.status = ArticleStatus.UPDATING
        article.publication.stage = PublicationStage.SAVED
        self.checkpoint(article, "Update approved")
        before = len(summary.published)
        self.publish(article, summary)
        for entry in summary.published[before:]:
            summary.updated.append(summary.published.pop(summary.published.index(entry)))


@contextlib.contextmanager
def no_publisher() -> Iterator[PublisherSession]:
    raise RuntimeError("no TYPO3 publisher configured")
    yield  # pragma: no cover
