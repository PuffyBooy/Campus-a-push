from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from typo3_publisher import retry as retry_module
from typo3_publisher import verifier as verifier_module
from typo3_publisher.config import Settings, SlotSettings, load_selectors, load_settings
from typo3_publisher.models import Article, ArticleContent, ImageInfo
from typo3_publisher.state_manager import StateStore

REPO = Path(__file__).resolve().parent.parent

ENV_VARS = [
    "WEBSITE_ID",
    "TYPO3_BACKEND_URL",
    "WEBSITE_BASE_URL",
    "BOT_PUBLISHING_ENABLED",
    "BOT_PAUSED",
    "BOT_UPDATES_ENABLED",
    "BOT_STATE_BACKEND",
    "BOT_HEADLESS",
    "TYPO3_USERNAME",
    "TYPO3_PASSWORD",
    "TYPO3_STORAGE_STATE",
    "OPENAI_API_KEY",
    "GROQ_API_KEY",
    "MISTRAL_API_KEY",
    "PIXABAY_API_KEY",
    "NOTIFY_WEBHOOK_URL",
]


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    for name in ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(retry_module, "sleep", lambda s: None)
    monkeypatch.setattr(verifier_module, "sleep", lambda s: None)
    import typo3_publisher.state_manager as sm

    monkeypatch.setattr(sm, "sleep", lambda s: None)


@pytest.fixture
def config_dir(tmp_path) -> Path:
    target = tmp_path / "config"
    shutil.copytree(REPO / "config", target)
    # Tests use a fixed topic set, independent of the site's editorial plan in config/topics.yaml.
    shutil.copy(REPO / "tests" / "fixtures" / "topics.yaml", target / "topics.yaml")
    return target


@pytest.fixture
def settings(config_dir) -> Settings:
    s = load_settings(config_dir)
    s.website.base_url = "https://example.org"
    s.website.verification.initial_delay_seconds = 0
    s.website.verification.attempts = 2
    # Fixed values the tests rely on, independent of the live configuration.
    s.research.enabled = False  # no network access in tests
    s.publishing.slots = [SlotSettings(times=["08:00", "12:00", "17:00"])]
    s.editorial.min_words, s.editorial.max_words = 600, 1200
    s.editorial.draft_mode = False
    s.editorial.quality_threshold = 8.0
    s.editorial.check_quotes, s.editorial.placeholder_quotes = True, False
    return s


@pytest.fixture
def store(tmp_path) -> StateStore:
    return StateStore(tmp_path / "state")


VOCAB = (
    "cloud server netzwerk speicher daten backup software update browser tastatur bildschirm drucker kamera akku "
    "ladegerät router kabel funk sensor roboter modell algorithmus lernen training prüfung studium bibliothek "
    "vorlesung seminar campus mensa kalender projekt team zukunft trend"
).split()


def make_content(
    title: str = "Passkeys einfach erklärt", words: int = 650, seed: int | None = None, **kw
) -> ArticleContent:
    paragraph = (
        "Passkeys ersetzen klassische Passwörter durch ein Schlüsselpaar, das auf dem Gerät erzeugt wird "
        "und die Anmeldung sicherer sowie bequemer macht. "
    )
    if seed is not None:  # distinct, non-duplicate text per seed
        import random

        rnd = random.Random(seed)
        paragraph = " ".join(rnd.choice(VOCAB) for _ in range(20)) + ". "
    body = ""
    count = 0
    i = 0
    while count < words:
        i += 1
        body += f"<h2>Abschnitt {i}</h2><p>{paragraph * 3}</p>"
        count += len(paragraph.split()) * 3 + 2
    defaults = dict(
        title=title,
        teaser="Was Passkeys sind und warum sie Passwörter ablösen.",
        body_html=body,
        seo_title="Passkeys erklärt",
        meta_description="Passkeys einfach erklärt: wie die Anmeldung ohne Passwort funktioniert und welche Vorteile "
        "sie im Alltag bringt.",
        keywords=["passkeys", "anmeldung"],
        slug="passkeys-einfach-erklaert",
        image_keywords=["key", "lock"],
    )
    defaults.update(kw)
    return ArticleContent(**defaults)


def make_image(path: Path) -> ImageInfo:
    from typo3_publisher.discovery import tiny_png

    path.write_bytes(tiny_png(80, 40))
    return ImageInfo(
        provider="pixabay",
        provider_id="12345",
        page_url="https://pixabay.com/photos/key-12345/",
        download_url="https://cdn.pixabay.com/photo/key.png",
        width=1920,
        height=1080,
        tags=["key", "lock"],
        user="photographer",
        license_name="Pixabay Content License",
        license_url="https://pixabay.com/service/license-summary/",
        attribution="© photographer / Pixabay",
        alt_text="Ein silberner Schlüssel steckt in einem Türschloss",
        relevance=1.0,
        local_path=str(path),
        sha256="abc",
        file_name=path.name,
        caption="Symbolbild: Ein Schlüssel als Zeichen für sichere Anmeldung",
        text_source="vision",
    )


def make_article(article_id: str = "20260101-000000-aaaaaaaa", **kw) -> Article:
    content = kw.pop("content", None) or make_content()
    return Article(id=article_id, topic=kw.pop("topic", "Technology"), content=content, **kw)


# ---------------------------------------------------------------- browser fixtures


def _chromium_executable() -> str | None:
    if exe := os.environ.get("CHROMIUM_EXECUTABLE"):
        return exe
    default = Path("/opt/pw-browsers/chromium")
    return str(default) if default.exists() else None


@pytest.fixture(scope="session")
def chromium_executable() -> str | None:
    from playwright.sync_api import sync_playwright

    exe = _chromium_executable()
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(**({"executable_path": exe} if exe else {}))
            browser.close()
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"Chromium not available for browser tests: {exc}")
    return exe


@pytest.fixture
def mock_typo3():
    from tests.mock_typo3.server import MockTypo3

    with MockTypo3() as mock:
        yield mock


@pytest.fixture
def browser_env(settings, mock_typo3, chromium_executable, monkeypatch):
    """Settings + selectors + secrets pointing at the mock backend."""
    settings.typo3.profile = "news"
    monkeypatch.setenv("TYPO3_USERNAME", mock_typo3.state.username)
    monkeypatch.setenv("TYPO3_PASSWORD", mock_typo3.state.password)
    settings.typo3.backend_url = f"{mock_typo3.base_url}/typo3"
    settings.website.base_url = mock_typo3.base_url
    settings.typo3.browser.executable_path = chromium_executable or ""
    settings.typo3.browser.navigation_timeout_ms = 10000
    settings.typo3.browser.action_timeout_ms = 5000
    settings.typo3.browser.upload_timeout_ms = 10000
    settings.diagnostics.directory = str(settings.base_dir / "artifacts" / "diagnostics")
    selectors = load_selectors(settings)
    selectors["news"]["storage_pid"] = 5
    selectors["content_element"]["parent_pid"] = 7
    return settings, selectors


@pytest.fixture
def mock_campus():
    from tests.mock_campus.server import MockCampus

    with MockCampus() as mock:
        yield mock


@pytest.fixture
def campus_env(settings, mock_campus, chromium_executable, monkeypatch):
    """Settings + selectors + secrets pointing at the mock campus a author pages."""
    monkeypatch.setenv("TYPO3_USERNAME", mock_campus.state.username)
    monkeypatch.setenv("TYPO3_PASSWORD", mock_campus.state.password)
    settings.typo3.profile = "campus_frontend"
    settings.campus.site_id = "campus-a-college"
    settings.website.base_url = mock_campus.base_url
    settings.campus.block_hosts = []
    settings.typo3.browser.executable_path = chromium_executable or ""
    settings.typo3.browser.navigation_timeout_ms = 10000
    settings.typo3.browser.action_timeout_ms = 5000
    settings.typo3.browser.upload_timeout_ms = 10000
    settings.diagnostics.directory = str(settings.base_dir / "artifacts" / "diagnostics")
    return settings, load_selectors(settings)
