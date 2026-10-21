"""Configuration loading.

All behaviour is driven by files in ``config/``:

* ``settings.yaml``         central settings (this module)
* ``topics.yaml``           topics, quotas and backlog
* ``editorial_prompt.md``   writing instructions
* ``review_rubric.md``      editor instructions
* ``typo3_selectors.yaml``  site-specific TYPO3 backend selectors

Secrets never live in these files; they are read from environment variables
(GitHub Actions secrets) through :class:`Secrets`.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .serialization import ConfigError, from_dict

# --------------------------------------------------------------------------- settings.yaml


@dataclass
class PublicUrlSettings:
    # pattern | listing | pattern_then_listing | backend_view
    strategy: str = "pattern_then_listing"
    pattern: str = "{base_url}/news/{slug}"
    listing_url: str = ""


@dataclass
class VerificationSettings:
    attempts: int = 6
    initial_delay_seconds: float = 10.0
    backoff_factor: float = 2.0
    max_delay_seconds: float = 120.0
    timeout_seconds: float = 30.0
    min_title_similarity: float = 0.85
    min_content_overlap: float = 0.5
    user_agent: str = "Mozilla/5.0 (compatible; typo3-publisher-verifier/1.0)"


@dataclass
class WebsiteSettings:
    base_url: str = ""
    name: str = ""
    language: str = "de"
    public_url: PublicUrlSettings = field(default_factory=PublicUrlSettings)
    verification: VerificationSettings = field(default_factory=VerificationSettings)


@dataclass
class BrowserSettings:
    headless: bool = True
    executable_path: str = ""
    navigation_timeout_ms: int = 45000
    action_timeout_ms: int = 15000
    upload_timeout_ms: int = 120000
    slow_mo_ms: int = 0
    viewport_width: int = 1600
    viewport_height: int = 1000
    locale: str = "de-AT"


@dataclass
class Typo3Settings:
    backend_url: str = ""
    profile: str = "news"  # news | content_element | campus_frontend
    selectors_file: str = "config/typo3_selectors.yaml"
    storage_state_env: str = "TYPO3_STORAGE_STATE"
    browser: BrowserSettings = field(default_factory=BrowserSettings)


# Known site identifiers (the hidden #currentSiteIdentifier field of the campus a article form).
CAMPUS_SITES = {"campus-a-college": "https://www.campus-a-college.at"}


@dataclass
class CampusSettings:
    """Profile ``campus_frontend``: the public author pages of campus a (no TYPO3 backend).

    The bot only ever saves articles as drafts ("Als Entwurf speichern") and marks them as
    AI-assisted; the author reviews and publishes them on the website.
    """

    site_id: str = ""  # WEBSITE_ID overrides, e.g. campus-a-college
    base_url: str = ""  # empty: website.base_url, else the known URL of site_id
    login_path: str = "/login"
    create_path: str = "/artikel/create"
    profile_path: str = "/profil"
    opinion_types: list[str] = field(default_factory=lambda: ["opinion"])  # article_type -> "Meinung"
    default_resort: str = "Panorama"
    resort_by_topic: dict[str, str] = field(default_factory=dict)
    default_region: str = "Österreich"
    region_by_topic: dict[str, str] = field(default_factory=dict)
    max_tags: int = 5
    create_missing_tags: bool = True
    # The two image confirmations of the form. Ticked only when true AND the image is from
    # Pixabay (free licence) with people excluded - otherwise the draft is saved without image.
    confirm_image_rights: bool = True
    # Third-party hosts not needed to fill in the form (consent banner, ads, statistics).
    block_hosts: list[str] = field(
        default_factory=lambda: [
            "consent.cookiebot.com",
            "consentcdn.cookiebot.com",
            "stats.campus-a.at",
            "securepubads.g.doubleclick.net",
            "www.googletagmanager.com",
            "pagead2.googlesyndication.com",
        ]
    )

    def resolved_base_url(self, website_base_url: str) -> str:
        return (self.base_url or website_base_url or CAMPUS_SITES.get(self.site_id, "")).rstrip("/")


@dataclass
class LLMSettings:
    provider: str = "openai"  # openai | groq | fake (offline testing only)
    base_url: str = ""  # empty: provider default (groq: https://api.groq.com/openai/v1)
    api_key_env: str = ""  # empty: OPENAI_API_KEY (openai) / GROQ_API_KEY (groq)
    writer_model: str = "gpt-4.1"
    editor_model: str = "gpt-4.1"
    writer_temperature: float | None = 0.7
    editor_temperature: float | None = 0.2
    max_output_tokens: int = 8000
    timeout_seconds: float = 180.0


@dataclass
class EditorialSettings:
    prompt_file: str = "config/editorial_prompt.md"
    review_rubric_file: str = "config/review_rubric.md"
    quality_threshold: float = 8.0
    min_criterion_score: float = 6.0
    min_factual_score: float = 7.0
    max_revision_cycles: int = 3
    criteria_weights: dict[str, float] = field(
        default_factory=lambda: {
            "factual_accuracy": 3.0,
            "readability": 1.0,
            "structure": 1.0,
            "grammar": 1.0,
            "seo": 1.0,
            "relevance": 1.5,
            "originality": 1.0,
            "clarity": 1.0,
            "instruction_adherence": 1.5,
        }
    )
    min_words: int = 600
    max_words: int = 1200
    word_count_tolerance: float = 0.1
    max_title_length: int = 90
    meta_description_min: int = 110
    meta_description_max: int = 165
    forbidden_terms: list[str] = field(default_factory=list)
    require_sources_for_factual: bool = True
    duplicate_title_similarity: float = 0.82
    duplicate_content_similarity: float = 0.45
    recent_titles_context: int = 40


@dataclass
class ResearchSettings:
    enabled: bool = False
    approved_domains: list[str] = field(default_factory=list)
    feeds: list[str] = field(default_factory=list)
    max_sources: int = 5
    max_chars_per_source: int = 6000
    min_keyword_matches: int = 1
    timeout_seconds: float = 20.0
    user_agent: str = "Mozilla/5.0 (compatible; typo3-publisher-research/1.0)"


@dataclass
class AttributionSettings:
    """The copyright line of the image (Pixabay uploader)."""

    enabled: bool = True
    template: str = "© {user} / Pixabay"
    target: str = "caption"  # caption (image fields) | body | both


@dataclass
class ImageTextSettings:
    """Caption (Bildtext) and description/alt text (Bildbeschreibung) of the image."""

    mode: str = "vision"  # vision: the LLM looks at the downloaded image | tags: templates from Pixabay tags
    model: str = ""  # empty: llm.writer_model (must accept image input in vision mode)
    caption_max_chars: int = 200
    description_max_chars: int = 250
    caption_fallback: str = "Symbolbild: {tags}"
    description_fallback: str = "{tags}"
    max_image_mb: float = 4.0  # larger files are not sent to the vision model (fallback is used)


@dataclass
class ImageSettings:
    provider: str = "pixabay"
    required: bool = True
    search_language: str = "en"
    min_width: int = 1200
    min_aspect_ratio: float = 1.3
    min_relevance: float = 0.34
    per_page: int = 50
    max_queries: int = 4
    safesearch: bool = True
    exclude_people: bool = True
    people_tags: list[str] = field(
        default_factory=lambda: [
            "people",
            "person",
            "woman",
            "man",
            "girl",
            "boy",
            "child",
            "children",
            "kid",
            "portrait",
            "face",
            "model",
            "businessman",
            "businesswoman",
            "lady",
            "baby",
            "teen",
            "teenager",
            "couple",
            "family",
            "selfie",
            "human",
            "crowd",
            "men",
            "women",
        ]
    )
    blocked_terms: list[str] = field(
        default_factory=lambda: [
            "logo",
            "brand",
            "trademark",
            "apple",
            "iphone",
            "macbook",
            "samsung",
            "google",
            "microsoft",
            "coca-cola",
            "nike",
            "adidas",
            "facebook",
            "instagram",
            "twitter",
            "tesla",
            "mcdonalds",
        ]
    )
    download_dir: str = "artifacts/images"
    max_file_mb: float = 15.0
    text: ImageTextSettings = field(default_factory=ImageTextSettings)
    attribution: AttributionSettings = field(default_factory=AttributionSettings)


@dataclass
class SlotSettings:
    days: list[str] = field(default_factory=lambda: ["mon", "tue", "wed", "thu", "fri"])
    times: list[str] = field(default_factory=lambda: ["08:00", "12:00", "17:00"])


@dataclass
class PublishingSettings:
    enabled: bool = False
    auto_publish: bool = True
    timezone: str = "Europe/Vienna"
    slots: list[SlotSettings] = field(default_factory=lambda: [SlotSettings()])
    slot_grace_minutes: int = 150
    catch_up: str = "latest_only"  # latest_only | none
    max_articles_per_run: int = 1
    max_publish_attempts: int = 3
    max_generation_attempts: int = 2
    prefer_approved_backlog: bool = True
    default_categories: list[str] = field(default_factory=list)
    categories_by_topic: dict[str, list[str]] = field(default_factory=dict)
    allow_test_content: bool = False


@dataclass
class UpdateSettings:
    enabled: bool = False
    min_age_days: int = 30
    max_updates_per_run: int = 1
    update_if_score_below: float = 7.5
    preserve_links: bool = True
    max_versions_kept: int = 20


@dataclass
class StateSettings:
    backend: str = "local"  # local | git_branch
    local_dir: str = "data"
    git_branch: str = "bot-state"
    git_dir: str = ".bot-state"
    git_remote: str = "origin"
    lock_ttl_minutes: int = 120
    commit_author_name: str = "typo3-publisher-bot"
    commit_author_email: str = "typo3-publisher-bot@users.noreply.github.com"


@dataclass
class RetryPolicy:
    attempts: int = 3
    base_delay_seconds: float = 2.0
    max_delay_seconds: float = 30.0


@dataclass
class RetrySettings:
    llm: RetryPolicy = field(default_factory=lambda: RetryPolicy(attempts=4))
    http: RetryPolicy = field(default_factory=RetryPolicy)
    browser: RetryPolicy = field(default_factory=lambda: RetryPolicy(attempts=2, base_delay_seconds=5))
    git: RetryPolicy = field(default_factory=lambda: RetryPolicy(attempts=5, base_delay_seconds=2))


@dataclass
class DiagnosticsSettings:
    directory: str = "artifacts/diagnostics"
    screenshots: bool = True
    html_dumps: bool = True


@dataclass
class NotificationSettings:
    webhook_env: str = "NOTIFY_WEBHOOK_URL"
    notify_on: list[str] = field(default_factory=lambda: ["failure", "rejected", "published", "draft_saved"])


@dataclass
class Settings:
    website: WebsiteSettings = field(default_factory=WebsiteSettings)
    typo3: Typo3Settings = field(default_factory=Typo3Settings)
    llm: LLMSettings = field(default_factory=LLMSettings)
    editorial: EditorialSettings = field(default_factory=EditorialSettings)
    research: ResearchSettings = field(default_factory=ResearchSettings)
    images: ImageSettings = field(default_factory=ImageSettings)
    publishing: PublishingSettings = field(default_factory=PublishingSettings)
    updates: UpdateSettings = field(default_factory=UpdateSettings)
    state: StateSettings = field(default_factory=StateSettings)
    retries: RetrySettings = field(default_factory=RetrySettings)
    diagnostics: DiagnosticsSettings = field(default_factory=DiagnosticsSettings)
    notifications: NotificationSettings = field(default_factory=NotificationSettings)
    campus: CampusSettings = field(default_factory=CampusSettings)
    # Not read from YAML: repository root used to resolve relative paths.
    base_dir: Path = field(default_factory=Path.cwd)

    def path(self, relative: str) -> Path:
        p = Path(relative)
        return p if p.is_absolute() else self.base_dir / p


# --------------------------------------------------------------------------- topics.yaml


@dataclass
class TopicConfig:
    name: str
    priority: str = "medium"  # high | medium | low
    articles_per_week: int = 1
    description: str = ""
    keywords: list[str] = field(default_factory=list)
    categories: list[str] = field(default_factory=list)


@dataclass
class BacklogItem:
    title: str
    topic: str = ""
    notes: str = ""


@dataclass
class TopicsFile:
    topics: list[TopicConfig] = field(default_factory=list)
    backlog: list[BacklogItem] = field(default_factory=list)


# --------------------------------------------------------------------------- loading


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise ConfigError(f"configuration file not found: {path}")
    with path.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: top level must be a mapping")
    return data


_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


def _env_bool(name: str) -> bool | None:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return None
    value = raw.strip().lower()
    if value in _TRUE:
        return True
    if value in _FALSE:
        return False
    raise ConfigError(f"environment variable {name} must be true/false, got {raw!r}")


def apply_env_overrides(settings: Settings) -> Settings:
    """Values that are environment specific (and partly secret) come from the environment."""
    if url := os.environ.get("TYPO3_BACKEND_URL"):
        settings.typo3.backend_url = url
    if url := os.environ.get("WEBSITE_BASE_URL"):
        settings.website.base_url = url
    if site_id := os.environ.get("WEBSITE_ID"):
        settings.campus.site_id = site_id.strip()
    if (enabled := _env_bool("BOT_PUBLISHING_ENABLED")) is not None:
        settings.publishing.enabled = enabled
    if (paused := _env_bool("BOT_PAUSED")) is not None and paused:
        settings.publishing.enabled = False
    if (updates := _env_bool("BOT_UPDATES_ENABLED")) is not None:
        settings.updates.enabled = updates
    if backend := os.environ.get("BOT_STATE_BACKEND"):
        settings.state.backend = backend
    if exe := os.environ.get("CHROMIUM_EXECUTABLE"):
        settings.typo3.browser.executable_path = exe
    if (headless := _env_bool("BOT_HEADLESS")) is not None:
        settings.typo3.browser.headless = headless
    return settings


def validate(settings: Settings) -> None:
    if settings.typo3.profile not in {"news", "content_element", "campus_frontend"}:
        raise ConfigError("typo3.profile must be 'news', 'content_element' or 'campus_frontend'")
    if settings.state.backend not in {"local", "git_branch"}:
        raise ConfigError("state.backend must be 'local' or 'git_branch'")
    if settings.images.attribution.target not in {"caption", "body", "both"}:
        raise ConfigError("images.attribution.target must be caption, body or both")
    if settings.llm.provider not in {"openai", "groq", "fake"}:
        raise ConfigError("llm.provider must be 'openai', 'groq' or 'fake'")
    if settings.images.text.mode not in {"vision", "tags"}:
        raise ConfigError("images.text.mode must be 'vision' or 'tags'")
    if settings.publishing.catch_up not in {"latest_only", "none"}:
        raise ConfigError("publishing.catch_up must be 'latest_only' or 'none'")
    if settings.website.public_url.strategy not in {"pattern", "listing", "pattern_then_listing", "backend_view"}:
        raise ConfigError("website.public_url.strategy is invalid")
    if settings.editorial.min_words >= settings.editorial.max_words:
        raise ConfigError("editorial.min_words must be smaller than editorial.max_words")
    if settings.editorial.max_revision_cycles < 0:
        raise ConfigError("editorial.max_revision_cycles must be >= 0")
    if not set(settings.editorial.criteria_weights):
        raise ConfigError("editorial.criteria_weights must not be empty")


def load_settings(config_dir: Path | str = "config", *, base_dir: Path | None = None) -> Settings:
    config_dir = Path(config_dir)
    base = base_dir or (config_dir.resolve().parent)
    settings = from_dict(Settings, _read_yaml(config_dir / "settings.yaml"), path="settings")
    settings.base_dir = base
    apply_env_overrides(settings)
    validate(settings)
    return settings


def load_topics(config_dir: Path | str = "config") -> TopicsFile:
    topics = from_dict(TopicsFile, _read_yaml(Path(config_dir) / "topics.yaml"), path="topics")
    names = [t.name for t in topics.topics]
    if len(names) != len(set(names)):
        raise ConfigError("topics.yaml: topic names must be unique")
    for t in topics.topics:
        if t.priority not in {"high", "medium", "low"}:
            raise ConfigError(f"topics.yaml: topic {t.name!r} has invalid priority {t.priority!r}")
        if t.articles_per_week < 0:
            raise ConfigError(f"topics.yaml: topic {t.name!r} has negative articles_per_week")
    return topics


def load_selectors(settings: Settings) -> dict[str, Any]:
    data = _read_yaml(settings.path(settings.typo3.selectors_file))
    backend = () if settings.typo3.profile == "campus_frontend" else ("login", "frames", "buttons")
    for key in (*backend, settings.typo3.profile):
        if key not in data:
            raise ConfigError(f"{settings.typo3.selectors_file}: missing section '{key}'")
    return data


def read_text(settings: Settings, relative: str) -> str:
    path = settings.path(relative)
    if not path.exists():
        raise ConfigError(f"file not found: {path}")
    return path.read_text(encoding="utf-8")


# --------------------------------------------------------------------------- secrets


class MissingSecretError(RuntimeError):
    pass


SECRET_NAMES = (
    "TYPO3_USERNAME",
    "TYPO3_PASSWORD",
    "OPENAI_API_KEY",
    "GROQ_API_KEY",
    "PIXABAY_API_KEY",
    "NOTIFY_WEBHOOK_URL",
    "TYPO3_STORAGE_STATE",
    "DIAGNOSTICS_PASSPHRASE",
)

# Profile campus_frontend: TYPO3_USERNAME is the e-mail address of the campus a account.


class Secrets:
    """Reads secrets from the environment. Values are registered with the log redactor."""

    def __init__(self, environ: dict[str, str] | None = None):
        self._env = environ if environ is not None else os.environ
        from .logging_utils import register_secret

        for name in SECRET_NAMES:
            if value := self._env.get(name):
                register_secret(value)

    def get(self, name: str) -> str | None:
        value = self._env.get(name)
        return value or None

    def require(self, name: str) -> str:
        value = self.get(name)
        if not value:
            raise MissingSecretError(
                f"required secret {name} is not set (configure it as a GitHub Actions secret or in .env)"
            )
        return value
