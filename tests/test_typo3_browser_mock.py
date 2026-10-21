"""Real Chromium against the local mock TYPO3 backend (tests/mock_typo3).

These prove the automation logic (login detection, frames, tabs, RTE, upload, save,
visibility, resume, verification). They do NOT prove compatibility with a real TYPO3
installation - see tests/integration/test_live_typo3.py for that.
"""

from __future__ import annotations

import pytest

from tests.conftest import make_article, make_image
from typo3_publisher.config import Secrets
from typo3_publisher.models import PublicationStage
from typo3_publisher.typo3_browser import (
    AuthChallengeError,
    ContentElementPublisher,
    Diagnostics,
    LoginError,
    NewsPublisher,
    PublishOptions,
    Typo3Browser,
)
from typo3_publisher.verifier import PublicVerifier

pytestmark = pytest.mark.browser
NEWS = "tx_news_domain_model_news"


def open_browser(settings, selectors, run_id="test"):
    diagnostics = Diagnostics(settings.path(settings.diagnostics.directory), run_id)
    return Typo3Browser(settings, selectors, Secrets(), diagnostics)


def test_login_success_is_detected(browser_env):
    settings, selectors = browser_env
    with open_browser(settings, selectors) as browser:
        assert browser.login() == "password"
        assert browser.logged_in


def test_wrong_password_raises_login_error_and_captures_diagnostics(browser_env, monkeypatch):
    settings, selectors = browser_env
    monkeypatch.setenv("TYPO3_PASSWORD", "wrong-password")
    with open_browser(settings, selectors, "badlogin") as browser:
        with pytest.raises(LoginError) as err:
            browser.login()
        assert "did not succeed" in str(err.value)
        assert "wrong-password" not in str(err.value)
        assert any(p.suffix == ".png" for p in browser.diagnostics.files)


@pytest.mark.parametrize("challenge", ["mfa", "captcha"])
def test_mfa_and_captcha_are_detected_not_bypassed(browser_env, mock_typo3, challenge):
    settings, selectors = browser_env
    setattr(mock_typo3.state, challenge, True)
    attempts_before = mock_typo3.state.login_attempts
    with open_browser(settings, selectors) as browser, pytest.raises(AuthChallengeError) as err:
        browser.login()
    assert err.value.kind == challenge
    if challenge == "captcha":  # the bot must not even submit the form
        assert mock_typo3.state.login_attempts == attempts_before


def test_news_publish_full_workflow_and_public_verification(browser_env, mock_typo3, tmp_path):
    settings, selectors = browser_env
    article = make_article()
    article.image = make_image(tmp_path / "passkeys-pixabay-12345.png")
    checkpoints = []
    with open_browser(settings, selectors) as browser:
        browser.login()
        publisher = NewsPublisher(browser)
        pub = publisher.publish(
            article, PublishOptions(categories=["KI", "Nonexistent"]), lambda p: checkpoints.append(p.stage)
        )

    records = mock_typo3.state.records[NEWS]
    assert len(records) == 1
    record = next(iter(records.values()))
    assert pub.typo3_uid == record["uid"]
    assert record["title"] == article.content.title
    assert record["teaser"] == article.content.teaser
    assert "Abschnitt 1" in record["bodytext"]
    assert record["path_segment"] == "passkeys-einfach-erklaert"
    assert record["description"] == article.content.meta_description
    assert record["categories"] == "1"
    assert record["hidden"] == 0
    assert record["datetime"]
    ref = next(iter(record["references"].values()))
    assert ref["alternative"] == "key, lock"
    assert ref["description"] == "Bild: photographer auf Pixabay"
    assert ref["showinpreview"] == "1"
    assert checkpoints[0] == PublicationStage.STARTED
    assert PublicationStage.SAVED in checkpoints and checkpoints[-1] == PublicationStage.PUBLISHED

    result = PublicVerifier(settings.website).verify(
        pub.slug, article.content.title, article.content.body_html, image_name=article.image.file_name
    )
    assert result.ok, result.reasons
    assert result.url == f"{mock_typo3.base_url}/news/passkeys-einfach-erklaert"
    assert result.image_found


def test_record_stays_hidden_when_publish_disabled(browser_env, mock_typo3, tmp_path):
    settings, selectors = browser_env
    article = make_article()
    article.image = make_image(tmp_path / "img.png")
    with open_browser(settings, selectors) as browser:
        browser.login()
        NewsPublisher(browser).publish(article, PublishOptions(publish=False), lambda p: None)
    record = next(iter(mock_typo3.state.records[NEWS].values()))
    assert record["hidden"] == 1
    result = PublicVerifier(settings.website).check(
        f"{mock_typo3.base_url}/news/{record['path_segment']}", article.content.title, article.content.body_html
    )
    assert not result.ok and result.status_code == 404


def test_resume_after_crash_does_not_create_duplicate(browser_env, mock_typo3, tmp_path):
    """A previous run clicked save (stage STARTED) but died before recording the uid."""
    settings, selectors = browser_env
    article = make_article()
    article.image = make_image(tmp_path / "img.png")
    existing_uid = 555
    mock_typo3.state.records[NEWS][existing_uid] = {
        "uid": existing_uid,
        "pid": 5,
        "title": article.content.title,
        "hidden": 1,
        "references": {},
        "path_segment": "x",
    }
    article.publication.stage = PublicationStage.STARTED
    with open_browser(settings, selectors) as browser:
        browser.login()
        pub = NewsPublisher(browser).publish(article, PublishOptions(), lambda p: None)
    assert pub.typo3_uid == existing_uid
    assert list(mock_typo3.state.records[NEWS]) == [existing_uid]
    assert mock_typo3.state.records[NEWS][existing_uid]["hidden"] == 0
    assert mock_typo3.state.records[NEWS][existing_uid]["path_segment"] == "x"  # slug of existing record kept


def test_update_existing_record_keeps_uid_and_image(browser_env, mock_typo3, tmp_path):
    settings, selectors = browser_env
    article = make_article()
    article.image = make_image(tmp_path / "img.png")
    with open_browser(settings, selectors) as browser:
        browser.login()
        publisher = NewsPublisher(browser)
        publisher.publish(article, PublishOptions(), lambda p: None)
        article.content.title = "Passkeys einfach erklärt (aktualisiert)"
        article.content.slug = "a-new-slug-that-must-be-ignored"
        publisher.update(article, PublishOptions(), lambda p: None)
    records = mock_typo3.state.records[NEWS]
    assert len(records) == 1
    record = next(iter(records.values()))
    assert record["title"].endswith("(aktualisiert)")
    assert record["path_segment"] == "passkeys-einfach-erklaert"
    assert len(record["references"]) == 1  # image not uploaded twice
    assert len(mock_typo3.state.files) == 1


def test_validation_error_is_reported(browser_env, mock_typo3):
    settings, selectors = browser_env
    article = make_article(content=None)
    article.content.title = ""
    with open_browser(settings, selectors) as browser:
        browser.login()
        publisher = NewsPublisher(browser)
        form = publisher.open_new()
        with pytest.raises(Exception, match="rejected the form"):
            form.save()
    assert mock_typo3.state.records[NEWS] == {}


def test_dry_run_preview_never_saves(browser_env, mock_typo3):
    settings, selectors = browser_env
    with open_browser(settings, selectors) as browser:
        browser.login()
        shots = NewsPublisher(browser).preview(make_article(), PublishOptions())
    assert shots
    assert mock_typo3.state.records[NEWS] == {}
    assert mock_typo3.state.files == {}


def test_content_element_profile(browser_env, mock_typo3, tmp_path):
    settings, selectors = browser_env
    settings.typo3.profile = "content_element"
    settings.website.public_url.pattern = "{base_url}/{slug}"
    article = make_article()
    article.image = make_image(tmp_path / "img.png")
    with open_browser(settings, selectors) as browser:
        browser.login()
        pub = ContentElementPublisher(browser).publish(article, PublishOptions(), lambda p: None)
    pages = mock_typo3.state.records["pages"]
    content = mock_typo3.state.records["tt_content"]
    assert len(pages) == 1 and len(content) == 1
    page = next(iter(pages.values()))
    element = next(iter(content.values()))
    assert page["hidden"] == 0 and page["abstract"] == article.content.teaser
    assert element["pid"] == page["uid"] and element["CType"] == "textmedia"
    assert len(element["references"]) == 1
    result = PublicVerifier(settings.website).verify(pub.slug, article.content.title, article.content.body_html)
    assert result.ok, result.reasons


def test_discovery_maps_and_test_saves(browser_env, mock_typo3, tmp_path):
    from typo3_publisher.discovery import Discovery

    settings, selectors = browser_env
    with open_browser(settings, selectors) as browser:
        discovery = Discovery(browser, tmp_path / "discovery")
        report = discovery.run(test_save=True, test_upload=True)
        md, proposal = discovery.write(tmp_path / "typo3_selectors.yaml")
    assert report.login == "password"
    assert report.news_module_detected
    assert report.mapping["news.title"]["status"] == "verified"
    assert report.mapping["news.bodytext"]["status"] == "verified"
    assert report.mapping["news.media"]["status"] == "verified"
    assert report.buttons["save"]
    assert report.test_save["status"] == "ok", report.test_save
    assert report.test_save["upload"]["status"] == "ok"
    assert mock_typo3.state.records[NEWS] == {}  # test record deleted again
    assert "verified: true" in proposal.read_text()
    assert "Field mapping" in md.read_text()
