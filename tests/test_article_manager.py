"""Pipeline orchestration with a mocked TYPO3 publisher, LLM, image finder and verifier."""

from __future__ import annotations

import contextlib
from datetime import UTC, datetime

import pytest

from tests.conftest import make_content, make_image
from typo3_publisher.article_manager import ArticleManager, PublishingDisabledError, RunSummary
from typo3_publisher.config import Secrets, load_topics
from typo3_publisher.llm import FakeLLM
from typo3_publisher.models import ArticleStatus, PublicationStage
from typo3_publisher.notifier import Notifier
from typo3_publisher.research import Researcher
from typo3_publisher.serialization import to_dict
from typo3_publisher.state_manager import LocalSync
from typo3_publisher.typo3_browser import Typo3Error
from typo3_publisher.verifier import VerificationResult

NOW = datetime(2026, 10, 21, 6, 5, tzinfo=UTC)  # Wednesday 08:05 Europe/Vienna (CEST)


class FakePublisher:
    """In-memory stand-in for the browser session. Records calls; can fail on demand."""

    def __init__(self):
        self.records: dict[int, dict] = {}
        self.calls: list[str] = []
        self.fail_with: Exception | None = None
        self.fail_after_save = False
        self.next_uid = 1

    def publish(self, article, options, checkpoint):
        self.calls.append(f"publish:{article.id}")
        pub = article.publication
        if self.fail_with and not self.fail_after_save:
            raise self.fail_with
        if pub.typo3_uid is None:
            pub.stage = PublicationStage.STARTED
            checkpoint(pub)
            pub.typo3_uid = self.next_uid
            self.next_uid += 1
            pub.stage = PublicationStage.SAVED
            checkpoint(pub)
        self.records[pub.typo3_uid] = {
            "title": article.content.title,
            "slug": article.content.slug,
            "hidden": not options.publish,
        }
        if self.fail_with and self.fail_after_save:
            err, self.fail_with = self.fail_with, None
            raise err
        if options.publish:
            pub.stage = PublicationStage.PUBLISHED
        pub.slug = self.records[pub.typo3_uid]["slug"]
        checkpoint(pub)
        return pub

    def update(self, article, options, checkpoint):
        self.calls.append(f"update:{article.id}")
        return self.publish(article, options, checkpoint)

    def preview(self, article, options):
        self.calls.append(f"preview:{article.id}")
        return ["shot.png"]


class FakeVerifier:
    def __init__(self, publisher: FakePublisher, ok: bool = True):
        self.publisher = publisher
        self.ok = ok

    def verify(self, slug, title, body_html, image_name="", backend_view_url="", known_url=""):
        visible = any(r["slug"] == slug and not r["hidden"] for r in self.publisher.records.values())
        ok = self.ok and visible
        return VerificationResult(
            ok=ok,
            url=f"https://example.org/news/{slug}",
            status_code=200 if ok else 404,
            title_similarity=1.0 if ok else 0,
            reasons=[] if ok else ["HTTP 404"],
        )


class FakeImageFinder:
    def __init__(self, tmp_path, found=True):
        self.tmp_path = tmp_path
        self.found = found
        self.calls = 0

    def find(self, content, topic):
        self.calls += 1
        return make_image(self.tmp_path / f"img-{self.calls}.png") if self.found else None

    def download(self, hit, slug):
        raise RuntimeError("expired")


class Commits(LocalSync):
    def __init__(self, root, settings):
        super().__init__(root, settings)
        self.messages: list[str] = []

    def commit(self, message):
        self.messages.append(message)


TITLES = [
    "Passkeys statt Passwörter im Studienalltag",
    "Wie Cloud-Speicher Daten synchronisiert",
    "Backups richtig planen: die 3-2-1-Regel",
    "Was ein Router im WLAN eigentlich tut",
    "Maschinelles Lernen ohne Mathematik erklärt",
    "Barrierefreie Präsentationen gestalten",
    "Digitale Notizen sinnvoll organisieren",
    "Open-Source-Software im Hörsaal",
]


def article_llm(titles=None, offset: int = 0):
    """FakeLLM writing distinct articles and approving them."""
    it = iter(titles or TITLES[offset:])
    counter = iter(range(offset, 1000))

    def respond(purpose, system, user):
        if purpose in {"write", "revise", "update"}:
            data = to_dict(make_content(title=next(it), seed=next(counter)))
            data["slug"] = ""
            return data
        return FakeLLM().complete_json(system=system, user=user, model="x", temperature=0, purpose=purpose)

    return FakeLLM(respond)


@pytest.fixture
def env(settings, store, tmp_path, config_dir):
    settings.publishing.enabled = True
    settings.publishing.allow_test_content = True  # scripted FakeLLM content counts as test content
    publisher = FakePublisher()

    @contextlib.contextmanager
    def factory():
        yield publisher

    def build(llm=None, image_finder=None, verifier_ok=True, dry_run=False):
        return ArticleManager(
            settings=settings,
            topics=load_topics(config_dir),
            store=store,
            sync=Commits(store.root, settings.state),
            llm=llm or article_llm(),
            editorial_prompt=(config_dir / "editorial_prompt.md").read_text(),
            review_rubric=(config_dir / "review_rubric.md").read_text(),
            researcher=Researcher(settings.research, settings.retries.http),
            image_finder=image_finder if image_finder is not None else FakeImageFinder(tmp_path),
            verifier=FakeVerifier(publisher, verifier_ok),
            publisher_factory=factory,
            notifier=Notifier(settings.notifications, Secrets({})),
            run_id="run-1",
            dry_run=dry_run,
        )

    return build, publisher


def test_full_pipeline_publishes_and_records_history(env, store):
    build, _publisher = env
    manager = build()
    summary = RunSummary(run_id="run-1", mode="full", dry_run=False)
    assert manager.run_full(summary, NOW)
    [article] = store.list_articles()
    assert article.status == ArticleStatus.PUBLISHED
    assert article.publication.stage == PublicationStage.VERIFIED
    assert article.publication.public_url.startswith("https://example.org/news/")
    assert article.publication.typo3_uid == 1
    assert article.reviews and article.reviews[-1].passed
    assert article.image and store.image_is_used("pixabay", "12345")
    assert summary.published[0]["id"] == article.id
    assert any("Start publishing" in m for m in manager.sync.messages)


def test_scheduled_slot_is_served_only_once(env, store):
    build, publisher = env
    s1 = RunSummary(run_id="run-1", mode="scheduled", dry_run=False)
    build().run_scheduled(s1, NOW)
    assert s1.slot.startswith("2026-10-21T08:00")
    assert len(s1.published) == 1
    # A delayed duplicate run 20 minutes later must not publish again.
    s2 = RunSummary(run_id="run-2", mode="scheduled", dry_run=False)
    build().run_scheduled(s2, NOW.replace(minute=25))
    assert not s2.published and "no publishing slot is due" in s2.notes[0]
    assert len(publisher.records) == 1
    assert store.get_slot(s1.slot)["status"] == "published"


def test_no_slot_due_outside_window(env):
    build, publisher = env
    summary = RunSummary(run_id="r", mode="scheduled", dry_run=False)
    build().run_scheduled(summary, datetime(2026, 10, 24, 10, 0, tzinfo=UTC))  # Saturday
    assert not summary.published and not publisher.calls


def test_publishing_disabled_does_nothing(env, settings):
    build, publisher = env
    settings.publishing.enabled = False
    summary = RunSummary(run_id="r", mode="scheduled", dry_run=False)
    build().run_scheduled(summary, NOW)
    assert "disabled" in summary.notes[0]
    assert not publisher.calls


def test_failure_is_preserved_and_retry_resumes_same_record(env, store):
    build, publisher = env
    publisher.fail_with = Typo3Error("network dropped after save")
    publisher.fail_after_save = True
    summary = RunSummary(run_id="r1", mode="full", dry_run=False)
    assert not build().run_full(summary, NOW)
    [article] = store.list_articles()
    assert article.status == ArticleStatus.PUBLISH_FAILED
    assert article.publication.typo3_uid == 1
    assert article.errors[-1].stage == "typo3"
    assert summary.failures

    summary2 = RunSummary(run_id="r2", mode="full", dry_run=False)
    assert build().run_full(summary2, NOW)
    articles = store.list_articles()
    assert len(articles) == 1, "retry must not generate a new article"
    assert articles[0].status == ArticleStatus.PUBLISHED
    assert list(publisher.records) == [1], "retry must reuse the TYPO3 record"
    assert articles[0].publication.attempts == 2


def test_publish_attempts_are_limited(env, store, settings):
    build, publisher = env
    settings.publishing.max_publish_attempts = 2
    publisher.fail_with = Typo3Error("always broken")
    for i in range(3):
        build().run_full(RunSummary(run_id=f"r{i}", mode="full", dry_run=False), NOW)
    failed = store.list_articles(ArticleStatus.PUBLISH_FAILED)
    assert failed and all(a.publication.attempts <= 2 for a in failed)


def test_verification_failure_is_not_reported_as_published(env, store):
    build, publisher = env
    summary = RunSummary(run_id="r", mode="full", dry_run=False)
    assert not build(verifier_ok=False).run_full(summary, NOW)
    [article] = store.list_articles()
    assert article.status == ArticleStatus.PUBLISH_FAILED
    assert article.publication.stage == PublicationStage.PUBLISHED
    assert "could not be verified" in summary.failures[0]["error"]
    # Next run: verification succeeds without touching TYPO3 again.
    calls = len(publisher.calls)
    assert build().run_full(RunSummary(run_id="r2", mode="full", dry_run=False), NOW)
    assert len(publisher.calls) == calls


def test_no_suitable_image_blocks_publication(env, store, tmp_path):
    build, publisher = env
    summary = RunSummary(run_id="r", mode="full", dry_run=False)
    build(image_finder=FakeImageFinder(tmp_path, found=False)).run_full(summary, NOW)
    [article] = store.list_articles()
    assert article.status == ArticleStatus.APPROVED
    assert article.image is None and not publisher.calls
    assert "unrelated image" in summary.notes[0]


def test_rejected_article_is_not_published(env, store, settings):
    build, publisher = env

    def respond(purpose, system, user):
        if purpose == "review":
            return {
                "scores": {k: 3 for k in settings.editorial.criteria_weights},
                "critical_issues": [{"category": "factual", "description": "invented statistic"}],
                "feedback": [],
            }
        data = to_dict(make_content())
        return data

    summary = RunSummary(run_id="r", mode="full", dry_run=False)
    assert not build(llm=FakeLLM(respond)).run_full(summary, NOW)
    articles = store.list_articles()
    assert articles and all(a.status == ArticleStatus.REJECTED for a in articles)
    assert any("invented statistic" in r for r in articles[0].rejected_reasons)
    assert not publisher.calls


def test_fake_llm_content_is_never_published(env, store, settings):
    build, publisher = env
    settings.editorial.min_words = 100
    settings.publishing.allow_test_content = False
    summary = RunSummary(run_id="r", mode="full", dry_run=False)
    with pytest.raises(PublishingDisabledError, match="test content"):
        build(llm=FakeLLM(words=200)).run_full(summary, NOW)
    assert not publisher.calls


def test_dry_run_only_previews(env, store):
    build, publisher = env
    summary = RunSummary(run_id="r", mode="full", dry_run=True)
    build(dry_run=True).run_full(summary, NOW)
    assert publisher.calls == [f"preview:{store.list_articles()[0].id}"]
    assert not publisher.records


def test_generate_and_review_only_modes(env, store):
    build, _publisher = env
    manager = build()
    summary = RunSummary(run_id="r", mode="generate-only", dry_run=False)
    manager.run_generate_only(summary, NOW, count=2)
    assert [a.status for a in store.list_articles()] == [ArticleStatus.DRAFT] * 2
    manager.run_review_only(summary)
    assert [a.status for a in store.list_articles()] == [ArticleStatus.APPROVED] * 2
    manager.run_publish_approved(summary, limit=1)
    assert len(store.list_articles(ArticleStatus.PUBLISHED)) == 1


def test_pending_draft_from_an_aborted_run_is_reviewed_not_rewritten(env, store):
    build, _publisher = env
    manager = build()
    summary = RunSummary(run_id="r", mode="generate-only", dry_run=False)
    [draft] = manager.run_generate_only(summary, NOW)  # the run then stopped (e.g. LLM rate limit)
    full = RunSummary(run_id="r2", mode="full", dry_run=False)
    assert build().run_full(full, NOW)
    assert [a.id for a in store.list_articles()] == [draft.id]
    assert full.published[0]["id"] == draft.id and not full.generated


def test_manual_idea_and_backlog(env, store):
    build, _ = env
    store.add_backlog_item("Backlog idea about cloud storage", "Technology")
    manager = build()
    summary = RunSummary(run_id="r", mode="generate-only", dry_run=False)
    manager.run_generate_only(summary, NOW, idea="Manual idea", topic="Digital Trends")
    manager.run_generate_only(summary, NOW)
    first, second = store.list_articles()
    assert (first.idea, first.idea_source, first.topic) == ("Manual idea", "manual", "Digital Trends")
    assert (second.idea_source, second.idea) == ("backlog", "Backlog idea about cloud storage")
    assert store.backlog()[0]["status"] == "used"


def test_update_requires_explicit_enable(env, settings):
    build, _ = env
    with pytest.raises(PublishingDisabledError):
        build().run_update(RunSummary(run_id="r", mode="update", dry_run=False), NOW)


def test_update_workflow_versions_and_keeps_slug(env, store, settings):
    build, publisher = env
    manager = build()
    manager.run_full(RunSummary(run_id="r1", mode="full", dry_run=False), NOW)
    [article] = store.list_articles()
    old_title, old_slug = article.content.title, article.publication.slug
    settings.updates.enabled = True
    summary = RunSummary(run_id="r2", mode="update", dry_run=False)
    build(llm=article_llm(offset=5)).run_update(summary, NOW, article_id=article.id)
    updated = store.load_article(article.id)
    assert updated.status == ArticleStatus.PUBLISHED
    assert updated.versions[0].content.title == old_title
    assert updated.content.title != old_title
    assert updated.content.slug == old_slug
    assert len(publisher.records) == 1, "update must not create a duplicate record"
    assert summary.updated and not summary.published
    assert f"update:{article.id}" in publisher.calls


def test_image_texts_are_generated_before_publishing(env, store, tmp_path):
    build, _publisher = env

    class TagOnlyFinder(FakeImageFinder):
        def find(self, content, topic):
            img = super().find(content, topic)
            img.caption, img.alt_text, img.text_source = "Symbolbild: key, lock", "key, lock", "tags"
            return img

    manager = build(image_finder=TagOnlyFinder(tmp_path))
    assert manager.run_full(RunSummary(run_id="r", mode="full", dry_run=False), NOW)
    [article] = store.list_articles()
    assert article.image.text_source == "vision"
    assert article.image.caption == "[TEST] Symbolbild"
    assert article.image.alt_text == "[TEST] Platzhalter-Bildbeschreibung"
    vision_calls = [c for c in manager.llm.calls if c["purpose"] == "image_text"]
    assert len(vision_calls) == 1 and vision_calls[0]["images"][0].startswith("data:image/png;base64,")
