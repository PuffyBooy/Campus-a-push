"""Image selection, public verification, topic planning, config, secrets redaction."""

import logging
from datetime import UTC, datetime

import pytest

from tests.conftest import make_article, make_content
from typo3_publisher.config import ConfigError, ImageSettings, RetryPolicy, Secrets, load_settings, load_topics
from typo3_publisher.discovery import tiny_png
from typo3_publisher.image_finder import ImageError, ImageFinder
from typo3_publisher.logging_utils import RedactingFilter, redact, register_secret
from typo3_publisher.models import ArticleStatus
from typo3_publisher.topic_planner import TopicPlanner
from typo3_publisher.verifier import PublicVerifier

# ------------------------------------------------------------------ fakes


class Resp:
    def __init__(self, status=200, text="", content=b"", json_data=None):
        self.status_code, self.text, self.content, self._json = status, text, content, json_data

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests

            raise requests.HTTPError(f"HTTP {self.status_code}")

    def json(self):
        return self._json


class FakeSession:
    def __init__(self, routes):
        self.routes = routes
        self.headers = {}
        self.requested = []

    def get(self, url, **kw):
        self.requested.append(url)
        return self.routes.get(url, Resp(404))


class FakePixabay:
    def __init__(self, hits):
        self.hits = hits
        self.queries = []

    def search(self, query, settings):
        self.queries.append(query)
        return self.hits


def hit(i, tags, w=1920, h=1080, **kw):
    return {
        "id": i,
        "tags": tags,
        "imageWidth": w,
        "imageHeight": h,
        "type": "photo",
        "user": "u",
        "pageURL": f"https://pixabay.com/photos/{i}/",
        "largeImageURL": f"https://cdn.example/{i}.png",
        "likes": kw.get("likes", 1),
        "downloads": 10,
    }


def finder(store, tmp_path, hits, routes=None):
    session = FakeSession(routes or {})
    return ImageFinder(
        ImageSettings(), FakePixabay(hits), store, tmp_path / "img", RetryPolicy(attempts=1), session=session
    )


# ------------------------------------------------------------------ images


def test_image_filters_people_brands_size_orientation_and_used(store, tmp_path):
    store.register_image("pixabay", "6", "x", "old")
    f = finder(store, tmp_path, [])
    assert "may show identifiable people" in f.rejection_reasons(hit(1, "laptop, woman, office"))[0]
    assert "trademarks" in f.rejection_reasons(hit(2, "iphone, desk"))[0]
    assert any("too small" in r for r in f.rejection_reasons(hit(3, "key", w=800, h=500)))
    assert any("landscape" in r for r in f.rejection_reasons(hit(4, "key", w=1200, h=1600)))
    assert f.rejection_reasons(hit(5, "key, lock, security")) == []
    assert "already used" in f.rejection_reasons(hit(6, "key"))


def test_image_selects_relevant_and_downloads(store, tmp_path):
    png = tiny_png()
    hits = [hit(10, "beach, sea, sand", likes=999), hit(11, "key, lock, security"), hit(12, "lock, door")]
    f = finder(store, tmp_path, hits, {"https://cdn.example/11.png": Resp(content=png)})
    content = make_content(image_keywords=["key lock", "security"])
    image = f.find(content, "Technology")
    assert image.provider_id == "11" and image.relevance == 1.0
    assert image.license_name == "Pixabay Content License"
    assert image.attribution == "© u / Pixabay"
    assert image.caption == "Symbolbild: key, lock, security" and image.text_source == "tags"
    assert (tmp_path / "img" / image.file_name).read_bytes() == png


def test_no_unrelated_image(store, tmp_path):
    f = finder(store, tmp_path, [hit(10, "beach, sea, sand"), hit(11, "cat, animal")])
    assert f.find(make_content(image_keywords=["quantum computer"]), "Technology") is None


def test_download_rejects_non_images(store, tmp_path):
    f = finder(store, tmp_path, [], {"https://cdn.example/1.png": Resp(content=b"<html>")})
    with pytest.raises(ImageError):
        f.download(hit(1, "x"), "slug")


# ------------------------------------------------------------------ caption / description / copyright


def stored_image(tmp_path, content=b""):
    from tests.conftest import make_image

    img = make_image(tmp_path / "key.png")
    if content:
        (tmp_path / "key.png").write_bytes(content)
    img.caption, img.alt_text, img.text_source = "", "", ""
    return img


def test_vision_caption_and_description(tmp_path):
    from typo3_publisher.image_finder import ImageTextWriter
    from typo3_publisher.llm import FakeLLM

    def respond(purpose, system, user):
        assert purpose == "image_text"
        assert "Describe ONLY what is actually visible" in system and "Language: de" in system
        return {
            "caption": "Symbolbild: Ein Schlüssel steckt im Schloss einer Holztür, " + "sehr " * 80,
            "description": "Nahaufnahme eines Messingschlüssels in einem alten Türschloss.",
        }

    llm = FakeLLM(respond)
    img = ImageTextWriter(ImageSettings(), llm, "gpt-test", "de").describe(stored_image(tmp_path), make_content())
    assert img.text_source == "vision"
    assert img.alt_text == "Nahaufnahme eines Messingschlüssels in einem alten Türschloss."
    assert img.caption.startswith("Symbolbild: Ein Schlüssel") and len(img.caption) <= 200
    assert img.caption.endswith("…")
    call = llm.calls[0]
    assert call["model"] == "gpt-test"
    assert call["images"][0].startswith("data:image/png;base64,")


def test_caption_falls_back_to_tags_on_failure(tmp_path):
    from typo3_publisher.image_finder import ImageTextWriter
    from typo3_publisher.llm import FakeLLM, LLMError

    def broken(purpose, system, user):
        raise LLMError("model does not support images")

    img = ImageTextWriter(ImageSettings(), FakeLLM(broken), "m", "de").describe(stored_image(tmp_path), make_content())
    assert (img.caption, img.alt_text, img.text_source) == ("Symbolbild: key, lock", "key, lock", "tags")

    empty = ImageTextWriter(ImageSettings(), FakeLLM(lambda *a: {"caption": "", "description": "x"}), "m", "de")
    assert empty.describe(stored_image(tmp_path), make_content()).text_source == "tags"


def test_tags_mode_and_oversized_images_skip_the_model(tmp_path):
    from typo3_publisher.config import ImageTextSettings
    from typo3_publisher.image_finder import ImageTextWriter
    from typo3_publisher.llm import FakeLLM

    llm = FakeLLM()
    settings = ImageSettings(text=ImageTextSettings(mode="tags"))
    assert ImageTextWriter(settings, llm, "m", "de").describe(stored_image(tmp_path), make_content()).caption
    settings = ImageSettings(text=ImageTextSettings(max_image_mb=0.000001))
    ImageTextWriter(settings, llm, "m", "de").describe(stored_image(tmp_path), make_content())
    assert llm.calls == []


def test_reference_values_mapping_and_copyright_target(tmp_path):
    from tests.conftest import make_image
    from typo3_publisher.typo3_browser import PublishOptions, attribution_html, reference_values

    img = make_image(tmp_path / "a.png")
    caption_mode = reference_values({}, img, PublishOptions(attribution_target="caption"))
    assert caption_mode == {
        "alternative": "Ein silberner Schlüssel steckt in einem Türschloss",
        "description": "Symbolbild: Ein Schlüssel als Zeichen für sichere Anmeldung © photographer / Pixabay",
    }
    body_mode = reference_values({}, img, PublishOptions(attribution_target="body"))
    assert body_mode["description"] == "Symbolbild: Ein Schlüssel als Zeichen für sichere Anmeldung"
    custom = reference_values({"fields": {"title": "{caption}", "description": "{copyright}"}}, img, PublishOptions())
    assert custom == {"title": img.caption, "description": "© photographer / Pixabay"}
    img.attribution = "© <b>x</b> / Pixabay"
    assert "&lt;b&gt;" in attribution_html(img)


# ------------------------------------------------------------------ verifier


def page(title, body):
    return f"<html><head><title>{title} | Site</title></head><body><nav>Menu</nav><h1>{title}</h1>{body}</body></html>"


def test_verifier_checks_title_and_content(settings):
    c = make_content()
    url = "https://example.org/news/passkeys-einfach-erklaert"
    v = PublicVerifier(settings.website, FakeSession({url: Resp(text=page(c.title, c.body_html))}))
    result = v.verify(c.slug, c.title, c.body_html)
    assert result.ok and result.url == url and result.content_overlap > 0.9

    wrong = PublicVerifier(settings.website, FakeSession({url: Resp(text=page(c.title, "<p>Other text</p>"))}))
    result = wrong.verify(c.slug, c.title, c.body_html)
    assert not result.ok and "content overlap" in result.reasons[0]
    assert result.attempts == settings.website.verification.attempts


def test_verifier_finds_url_on_listing_page(settings):
    c = make_content()
    listing = (
        '<ul><li><a href="/news/other">Something else</a></li><li><a href="/news/real-slug">'
        + c.title
        + "</a></li></ul>"
    )
    routes = {
        "https://example.org/news": Resp(text=listing),
        "https://example.org/news/real-slug": Resp(text=page(c.title, c.body_html)),
    }
    result = PublicVerifier(settings.website, FakeSession(routes)).verify("wrong-slug", c.title, c.body_html)
    assert result.ok and result.url == "https://example.org/news/real-slug"


# ------------------------------------------------------------------ topic planner


def test_topic_rotation_respects_quota_and_priority(store, config_dir):
    topics = load_topics(config_dir)
    planner = TopicPlanner(topics, store, "Europe/Vienna")
    now = datetime(2026, 10, 21, 8, tzinfo=UTC)
    assert planner.next_assignment(now).topic == "Technology"  # high priority, largest quota
    for i in range(3):
        store.save_article(
            make_article(f"t{i}", topic="Technology", status=ArticleStatus.PUBLISHED, created_at=now.isoformat())
        )
    assert planner.next_assignment(now).topic == "Artificial Intelligence"
    for i, t in enumerate(["Artificial Intelligence"] * 2 + ["Digital Trends"] * 2):
        store.save_article(make_article(f"x{i}", topic=t, status=ArticleStatus.PUBLISHED, created_at=now.isoformat()))
    assert planner.next_assignment(now) is None  # all weekly quotas fulfilled
    rejected = make_article(
        "r",
        topic="Technology",
        status=ArticleStatus.REJECTED,
        created_at=datetime(2026, 10, 28, 8, tzinfo=UTC).isoformat(),
    )
    store.save_article(rejected)
    # New week: Technology and AI have equal weighted deficits; the least recently used topic wins.
    assert planner.next_assignment(datetime(2026, 10, 28, 8, tzinfo=UTC)).topic == "Artificial Intelligence"


def test_config_backlog_is_used_once(store, config_dir):
    (config_dir / "topics.yaml").write_text(
        "topics:\n  - name: Technology\n    priority: high\n    articles_per_week: 1\n"
        "backlog:\n  - title: A backlog idea\n    topic: Technology\n"
    )
    planner = TopicPlanner(load_topics(config_dir), store, "Europe/Vienna")
    first = planner.next_assignment(datetime(2026, 10, 21, tzinfo=UTC))
    assert first.source == "config_backlog"
    planner.consume(first, "a1")
    assert planner.next_assignment(datetime(2026, 10, 21, tzinfo=UTC)).source == "rotation"


# ------------------------------------------------------------------ config & secrets


def test_shipped_config_is_valid_and_safe_by_default(config_dir):
    s = load_settings(config_dir)
    assert s.publishing.enabled is False
    assert s.updates.enabled is False
    assert s.publishing.allow_test_content is False
    assert s.publishing.timezone == "Europe/Vienna"


def test_unknown_config_key_is_rejected(config_dir):
    text = (config_dir / "settings.yaml").read_text().replace("  quality_threshold:", "  quality_treshold:")
    (config_dir / "settings.yaml").write_text(text)
    with pytest.raises(ConfigError, match="quality_treshold"):
        load_settings(config_dir)


def test_env_overrides(config_dir, monkeypatch):
    monkeypatch.setenv("BOT_PUBLISHING_ENABLED", "true")
    monkeypatch.setenv("TYPO3_BACKEND_URL", "https://x.example/typo3")
    assert load_settings(config_dir).publishing.enabled is True
    monkeypatch.setenv("BOT_PAUSED", "1")
    s = load_settings(config_dir)
    assert s.publishing.enabled is False and s.typo3.backend_url == "https://x.example/typo3"


def test_secrets_are_redacted_from_logs(caplog):
    Secrets({"TYPO3_PASSWORD": "Sup3r-Secret!", "OPENAI_API_KEY": "sk-abcdefghijklmnopqrstuv"})
    assert "Sup3r-Secret!" not in redact("login with Sup3r-Secret! failed")
    assert "abc123def" not in redact("Cookie: be_typo_user=abc123def; path=/")
    assert redact("be_typo_user=abc123def") == "be_typo_user=***"
    assert "abc" not in redact("https://pixabay.com/api/?key=abc&q=x")
    record = logging.LogRecord("x", logging.INFO, __file__, 1, "token %s", ("sk-abcdefghijklmnopqrstuv",), None)
    RedactingFilter().filter(record)
    assert "abcdefghijklmnop" not in record.getMessage()
    register_secret("ab")  # too short to redact safely
    assert redact("abc") == "abc"
