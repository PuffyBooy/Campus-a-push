"""Real Chromium against the mock campus a author pages (tests/mock_campus).

They prove the automation logic of profile campus_frontend (login, the four form steps,
Quill, image, tags, draft-only submit, resume). They do NOT prove compatibility with the
real website - run `python -m typo3_publisher discover` and a dry run for that.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from tests.conftest import make_article, make_content, make_image
from tests.test_article_manager import Commits
from typo3_publisher.article_manager import ArticleManager, RunSummary
from typo3_publisher.campus_frontend import CampusBrowser, CampusDraftPublisher, draft_uid, form_field
from typo3_publisher.config import Secrets, load_settings, load_topics, normalise_site_id
from typo3_publisher.llm import FakeLLM
from typo3_publisher.main import browser_session_factory
from typo3_publisher.models import ArticleStatus, PublicationStage
from typo3_publisher.notifier import Notifier
from typo3_publisher.research import Researcher
from typo3_publisher.serialization import ConfigError, to_dict
from typo3_publisher.state_manager import StateStore
from typo3_publisher.typo3_browser import (
    AuthChallengeError,
    Diagnostics,
    FormError,
    LoginError,
    PublishOptions,
)
from typo3_publisher.verifier import PublicVerifier

NOW = datetime(2026, 10, 21, 10, 5, tzinfo=UTC)  # 12:05 Europe/Vienna


def open_browser(settings, selectors, run_id="campus-test"):
    diagnostics = Diagnostics(settings.path(settings.diagnostics.directory), run_id)
    return CampusBrowser(settings, selectors, Secrets(), diagnostics)


def article_with_image(tmp_path, **kw):
    article = make_article(**kw)
    article.image = make_image(tmp_path / "passkeys-pixabay-12345.png")
    return article


# ---------------------------------------------------------------- unit


def test_form_field_reads_multipart_and_urlencoded():
    multipart = (
        b'--x\r\nContent-Disposition: form-data; name="submitAction"\r\n\r\ndraft\r\n'
        b'--x\r\nContent-Disposition: form-data; name="a[isModifiedByAi]"\r\n\r\n1\r\n--x--\r\n'
    )
    assert form_field(multipart, "submitAction") == "draft"
    assert form_field(multipart, "a[isModifiedByAi]") == "1"
    assert form_field(b"submitAction=published&x=1", "submitAction") == "published"
    assert form_field(b"user=a&pass=b", "submitAction") is None


def test_website_id_comes_from_the_environment(config_dir, monkeypatch):
    monkeypatch.setenv("WEBSITE_ID", " campus-a-college ")
    settings = load_settings(config_dir)
    assert settings.typo3.profile == "campus_frontend"
    assert settings.campus.site_id == "campus-a-college"
    assert settings.campus.resolved_base_url("") == "https://www.campus-a-college.at"
    assert settings.campus.resolved_base_url("https://example.org/") == "https://example.org"


@pytest.mark.parametrize(
    "raw",
    [
        "campus-a-college",
        '"campus-a-college"',
        "Campus-A-College",
        "campus a college",
        "https://www.campus-a-college.at/",
    ],
)
def test_website_id_spellings_are_accepted(raw):
    assert normalise_site_id(raw) == "campus-a-college"


def test_unknown_website_id_still_opens_campus_a_college(settings):
    settings.campus.site_id = "12345"
    assert settings.campus.resolved_base_url("") == "https://www.campus-a-college.at"


def test_missing_website_id_is_a_clear_error(settings):
    settings.typo3.profile = "campus_frontend"
    with pytest.raises(ConfigError, match="WEBSITE_ID"):
        CampusBrowser(settings, {"campus_frontend": {}}, Secrets({}), Diagnostics(settings.base_dir, "x"))


def test_draft_uid_from_edit_url():
    assert draft_uid("https://x/artikel/edit?tx_hirokicampus_articleedit%5Bnews%5D=4711&cHash=1") == 4711
    assert draft_uid("https://x/profil") is None


# ---------------------------------------------------------------- browser

browser = pytest.mark.browser


@browser
def test_login_success_and_failure(campus_env, monkeypatch):
    settings, selectors = campus_env
    with open_browser(settings, selectors) as b:
        assert b.login() == "password"
        assert b.login() == "session"
    monkeypatch.setenv("TYPO3_PASSWORD", "wrong-password")
    with open_browser(settings, selectors, "badlogin") as b:
        with pytest.raises(LoginError) as err:
            b.login()
        assert "Anmeldung fehlgeschlagen" in str(err.value)
        assert "wrong-password" not in str(err.value)


@browser
def test_captcha_is_not_bypassed(campus_env, mock_campus):
    settings, selectors = campus_env
    mock_campus.state.captcha = True
    with open_browser(settings, selectors) as b, pytest.raises(AuthChallengeError):
        b.login()
    assert mock_campus.state.login_attempts == 0


@browser
def test_website_id_must_match_the_form(campus_env, mock_campus):
    settings, selectors = campus_env
    mock_campus.state.site_id = "campus-a-medien"
    with (
        open_browser(settings, selectors) as b,
        pytest.raises(ConfigError, match="set WEBSITE_ID to exactly 'campus-a-medien'"),
    ):
        b.open_form()


@browser
def test_saves_a_complete_draft_marked_as_ai_assisted(campus_env, mock_campus, tmp_path):
    settings, selectors = campus_env
    article = article_with_image(tmp_path)
    checkpoints = []
    with open_browser(settings, selectors) as b:
        pub = CampusDraftPublisher(b).publish(
            article, PublishOptions(categories=["KI"]), lambda p: checkpoints.append(p.stage)
        )

    [saved] = mock_campus.state.articles
    f = saved["fields"]
    c = article.content
    assert f["submitAction"] == "draft"
    assert f["isModifiedByAi"] == "1"
    assert f["categories"] == "1"  # Fakten
    assert f["title"] == c.title
    assert f["teaser"] == c.teaser
    assert "Abschnitt 1" in f["bodytext"] and "Passkeys ersetzen" in f["bodytext"]
    assert f["alternativeTitle"] == "Passkeys erklärt"
    assert f["description"] == c.meta_description  # not the site's generated one
    assert f["resorts"] == "1" and f["regions"] == "2"  # Panorama, Österreich
    assert f["tags"] == ["24", "new_passkeys", "new_anmeldung"]  # existing "KI" + new tags
    assert f["falMedia.title"] == "Ein silberner Schlüssel steckt in einem Türschloss"
    assert f["falMedia.description"] == "Symbolbild: Ein Schlüssel als Zeichen für sichere Anmeldung"
    assert f["falMedia.copyright"] == "© photographer / Pixabay"
    assert f["falMediaConsentFirst"] and f["falMediaConsentSecond"]
    assert saved["files"]["falMedia"]["name"] == "passkeys-pixabay-12345.png"
    assert checkpoints == [PublicationStage.STARTED, PublicationStage.DRAFT_SAVED]
    assert pub.stage == PublicationStage.DRAFT_SAVED
    assert pub.verification["draft_saved_url"].endswith("/profil")


@browser
def test_opinion_articles_use_category_meinung(campus_env, mock_campus, tmp_path):
    settings, selectors = campus_env
    article = article_with_image(tmp_path, content=make_content(article_type="opinion"))
    with open_browser(settings, selectors) as b:
        CampusDraftPublisher(b).publish(article, PublishOptions(), lambda p: None)
    assert mock_campus.state.articles[0]["fields"]["categories"] == "2"


@browser
def test_publishing_is_refused_even_if_someone_clicks_it(campus_env, mock_campus, tmp_path):
    settings, selectors = campus_env
    with open_browser(settings, selectors) as b:
        b.open_form()
        b.fill(article_with_image(tmp_path), PublishOptions())
        b.page.get_by_role("button", name="Veröffentlichen").click()
        b.page.wait_for_timeout(1000)
        assert b.refused_submissions
    assert mock_campus.state.articles == []


@browser
def test_drafts_must_carry_the_ai_flag(campus_env, mock_campus, tmp_path):
    settings, selectors = campus_env
    with open_browser(settings, selectors) as b:
        b.open_form()
        b.fill(article_with_image(tmp_path), PublishOptions())
        b.page.evaluate("() => { document.getElementById('isModifiedByAi').value = '0'; }")
        with pytest.raises(FormError, match=r"refused|not sent"):
            b.save_draft()
    assert mock_campus.state.articles == []


@browser
def test_no_image_confirmation_without_people_filter(campus_env, mock_campus, tmp_path):
    settings, selectors = campus_env
    settings.images.exclude_people = False
    with open_browser(settings, selectors) as b, pytest.raises(FormError, match="confirmations"):
        CampusDraftPublisher(b).publish(article_with_image(tmp_path), PublishOptions(), lambda p: None)
    assert mock_campus.state.articles == []


@browser
def test_resume_does_not_send_the_draft_twice(campus_env, mock_campus, tmp_path):
    settings, selectors = campus_env
    article = article_with_image(tmp_path)
    mock_campus.state.articles.append({"fields": {"title": article.content.title, "submitAction": "draft"}})
    article.publication.stage = PublicationStage.STARTED  # crashed after clicking save
    with open_browser(settings, selectors) as b:
        pub = CampusDraftPublisher(b).publish(article, PublishOptions(), lambda p: None)
    assert pub.stage == PublicationStage.DRAFT_SAVED
    assert len(mock_campus.state.articles) == 1


@browser
def test_dry_run_preview_sends_nothing(campus_env, mock_campus, tmp_path):
    settings, selectors = campus_env
    with open_browser(settings, selectors) as b:
        shots = CampusDraftPublisher(b).preview(article_with_image(tmp_path), PublishOptions())
    assert shots
    assert mock_campus.state.articles == []


@browser
def test_check_form_reports_all_fields(campus_env):
    settings, selectors = campus_env
    with open_browser(settings, selectors) as b:
        report = b.check_form()
    assert report["ok"], report
    assert report["site_id"] == "campus-a-college"
    assert report["role"] == "EDITOR_IN_CHIEF"


@browser
def test_scheduled_run_saves_a_draft(campus_env, mock_campus, tmp_path, monkeypatch, config_dir):
    settings, selectors = campus_env
    settings.publishing.enabled = True
    settings.publishing.allow_test_content = True  # scripted FakeLLM content, mock site only
    settings.editorial.draft_mode = True
    monkeypatch.setattr("typo3_publisher.main.load_selectors", lambda s: selectors)
    todo = "Eine Lehrkraft zum Umstieg auf Passkeys befragen"

    def llm(purpose, system, user):
        if purpose == "review":
            assert "DRAFT MODE" in system
            return {
                "scores": dict.fromkeys(settings.editorial.criteria_weights, 9.0),
                "critical_issues": [],
                "author_todos": [todo],
            }
        return to_dict(make_content(title="Passkeys im Studium: so klappt der Umstieg", seed=1))

    class OneImage:
        def find(self, content, topic):
            return make_image(tmp_path / "img-pixabay-1.png")

    store = StateStore(tmp_path / "state")
    notifier = Notifier(settings.notifications, Secrets({}))
    manager = ArticleManager(
        settings=settings,
        topics=load_topics(config_dir),
        store=store,
        sync=Commits(store.root, settings.state),
        llm=FakeLLM(llm),
        editorial_prompt="prompt",
        review_rubric="rubric",
        researcher=Researcher(settings.research, settings.retries.http),
        image_finder=OneImage(),
        verifier=PublicVerifier(settings.website),
        publisher_factory=browser_session_factory(settings, Secrets(), "campus-e2e"),
        notifier=notifier,
        run_id="campus-e2e",
    )
    summary = RunSummary(run_id="campus-e2e", mode="scheduled", dry_run=False)
    manager.run_scheduled(summary, NOW)

    assert summary.ok, summary.failures
    assert not summary.published
    [article] = store.list_articles()
    assert article.status == ArticleStatus.DRAFT_SAVED
    assert summary.drafts == [
        {"id": article.id, "title": article.content.title, "url": f"{mock_campus.base_url}/profil", "todos": [todo]}
    ]
    assert store.get_slot(summary.slot)["status"] == "published"
    assert [n["event"] for n in notifier.sent] == ["draft_saved"]
    assert todo in notifier.sent[0]["text"]
    assert "Saved as draft" in summary.markdown() and f"To do: {todo}" in summary.markdown()
    assert len(mock_campus.state.articles) == 1

    # The saved draft is not sent again by the next run.
    again = RunSummary(run_id="campus-e2e-2", mode="full", dry_run=False)
    manager.run_publish_approved(again)
    assert len(mock_campus.state.articles) == 1
