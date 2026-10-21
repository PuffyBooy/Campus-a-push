"""Opt-in tests against the REAL TYPO3 backend. They are the only tests that prove the bot
works with your installation; everything else runs against a mock.

Levels (each requires the previous one):

    TYPO3_LIVE_TESTS=1            login + open and fill the new-record form (nothing is saved)
    TYPO3_LIVE_ALLOW_SAVE=1       save a HIDDEN test record (+ optional image upload) and delete it again
    TYPO3_LIVE_ALLOW_PUBLISH=1    publish a test article, verify the public page, then hide and delete it

Required: TYPO3_BACKEND_URL, TYPO3_USERNAME, TYPO3_PASSWORD, WEBSITE_BASE_URL and a configured
config/typo3_selectors.yaml (storage pid). Run:  pytest tests/integration -m live -s
Publishing test content is visible to the public for the duration of the test - use a staging site
or a hidden storage folder if possible. Uploaded test images remain in fileadmin and must be removed
manually (the bot never deletes files).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tests.conftest import make_article, make_content
from typo3_publisher.config import Secrets, load_selectors, load_settings
from typo3_publisher.discovery import tiny_png
from typo3_publisher.models import ImageInfo, iso
from typo3_publisher.typo3_browser import Diagnostics, PublishOptions, Typo3Browser, create_publisher
from typo3_publisher.verifier import PublicVerifier

REPO = Path(__file__).resolve().parents[2]
# Captured at import time, before the autouse fixture in tests/conftest.py clears credentials.
_original_env = dict(os.environ)
pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(os.environ.get("TYPO3_LIVE_TESTS") != "1", reason="set TYPO3_LIVE_TESTS=1 to run live tests"),
]


@pytest.fixture
def live(monkeypatch):
    # The autouse fixture in conftest clears these; restore them from the real environment.
    for name in ("TYPO3_BACKEND_URL", "TYPO3_USERNAME", "TYPO3_PASSWORD", "WEBSITE_BASE_URL", "TYPO3_STORAGE_STATE"):
        if value := _original_env.get(name):
            monkeypatch.setenv(name, value)
    settings = load_settings(REPO / "config")
    if not settings.typo3.backend_url:
        pytest.skip("TYPO3_BACKEND_URL not set")
    selectors = load_selectors(settings)
    diagnostics = Diagnostics(settings.path(settings.diagnostics.directory), f"live-{iso()[:19]}")
    with Typo3Browser(settings, selectors, Secrets(), diagnostics) as browser:
        browser.login()
        yield settings, browser


def test_live_login_and_fill_form_without_saving(live):
    _settings, browser = live
    publisher = create_publisher(browser)
    shots = publisher.preview(
        make_article(content=make_content(title=f"[typo3-bot live test {iso()}]")), PublishOptions()
    )
    assert shots


@pytest.mark.skipif(os.environ.get("TYPO3_LIVE_ALLOW_SAVE") != "1", reason="set TYPO3_LIVE_ALLOW_SAVE=1")
def test_live_save_hidden_record_and_delete(live, tmp_path):
    settings, browser = live
    publisher = create_publisher(browser)
    article = make_article(content=make_content(title=f"[typo3-bot live test {iso()}] safe to delete"))
    if os.environ.get("TYPO3_LIVE_TEST_UPLOAD") == "1":
        article.image = _test_image(tmp_path)
    pub = publisher.publish(article, PublishOptions(publish=False), lambda p: None)
    try:
        assert pub.typo3_uid
        table = pub.typo3_table if settings.typo3.profile == "news" else "pages"
        uid = pub.typo3_uid if settings.typo3.profile == "news" else pub.typo3_page_uid
        form = browser.open_form(table, uid=uid)
        assert form.read_value("hidden") == "1"
    finally:
        _cleanup(settings, browser, pub)


@pytest.mark.skipif(os.environ.get("TYPO3_LIVE_ALLOW_PUBLISH") != "1", reason="set TYPO3_LIVE_ALLOW_PUBLISH=1")
def test_live_publish_verify_and_delete(live, tmp_path):
    settings, browser = live
    publisher = create_publisher(browser)
    article = make_article(content=make_content(title=f"typo3-bot live test {iso()[:16]}"))
    article.image = _test_image(tmp_path)
    pub = publisher.publish(article, PublishOptions(), lambda p: None)
    try:
        result = PublicVerifier(settings.website).verify(
            pub.slug, article.content.title, article.content.body_html, image_name=article.image.file_name
        )
        assert result.ok, f"public verification failed: {result.reasons} ({result.url})"
    finally:
        _cleanup(settings, browser, pub)


def _test_image(tmp_path: Path) -> ImageInfo:
    path = tmp_path / f"typo3-bot-live-test-{iso()[:19].replace(':', '')}.png"
    path.write_bytes(tiny_png(1200, 700))
    return ImageInfo(
        provider="test",
        provider_id="0",
        page_url="https://example.org",
        download_url="",
        width=1200,
        height=700,
        tags=["test"],
        user="test",
        license_name="test",
        license_url="https://example.org",
        attribution="Test image",
        alt_text="Test image",
        relevance=1.0,
        local_path=str(path),
        file_name=path.name,
    )


def _cleanup(settings, browser, pub) -> None:
    if settings.typo3.profile == "news" and pub.typo3_uid:
        browser.delete_record(pub.typo3_table, pub.typo3_uid)
    elif pub.typo3_page_uid:
        browser.delete_record("pages", pub.typo3_page_uid)
