"""Phase 6 demonstration against the mock backend:

Topic selection → generation → editorial review → revision → image → TYPO3 browser login →
record creation → image upload → save → publish → public verification → history update.

Only the LLM and Pixabay are faked; browser automation, state, scheduling and verification
are the real implementations. Still a mock TYPO3 - not proof for the live site.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from tests.conftest import make_content, make_image
from tests.test_article_manager import Commits
from typo3_publisher.article_manager import ArticleManager, RunSummary
from typo3_publisher.config import Secrets, load_topics
from typo3_publisher.llm import FakeLLM
from typo3_publisher.main import browser_session_factory, main
from typo3_publisher.models import ArticleStatus, PublicationStage
from typo3_publisher.notifier import Notifier
from typo3_publisher.research import Researcher
from typo3_publisher.serialization import to_dict
from typo3_publisher.state_manager import StateStore
from typo3_publisher.verifier import PublicVerifier

pytestmark = pytest.mark.browser
NEWS = "tx_news_domain_model_news"
NOW = datetime(2026, 10, 21, 10, 5, tzinfo=UTC)  # 12:05 Europe/Vienna


class OneImage:
    def __init__(self, tmp_path):
        self.tmp_path = tmp_path

    def find(self, content, topic):
        return make_image(self.tmp_path / f"{content.slug}-pixabay-12345.png")


def test_scheduled_run_publishes_through_the_browser(browser_env, mock_typo3, tmp_path, monkeypatch, config_dir):
    settings, selectors = browser_env
    settings.publishing.enabled = True
    settings.publishing.allow_test_content = True  # scripted FakeLLM content, mock site only
    monkeypatch.setattr("typo3_publisher.main.load_selectors", lambda s: selectors)

    reviews = iter([6.5, 9.0])  # first draft fails, revision passes

    def llm(purpose, system, user):
        if purpose == "review":
            return {
                "scores": dict.fromkeys(settings.editorial.criteria_weights, next(reviews)),
                "critical_issues": [],
                "feedback": [{"area": "structure", "issue": "x", "suggestion": "y"}],
            }
        return to_dict(
            make_content(
                title="Passkeys im Studium: so klappt der Umstieg",
                seed=1 if purpose == "write" else 2,
                slug="passkeys-im-studium",
            )
        )

    store = StateStore(tmp_path / "state")
    manager = ArticleManager(
        settings=settings,
        topics=load_topics(config_dir),
        store=store,
        sync=Commits(store.root, settings.state),
        llm=FakeLLM(llm),
        editorial_prompt="prompt",
        review_rubric="rubric",
        researcher=Researcher(settings.research, settings.retries.http),
        image_finder=OneImage(tmp_path),
        verifier=PublicVerifier(settings.website),
        publisher_factory=browser_session_factory(settings, Secrets(), "e2e"),
        notifier=Notifier(settings.notifications, Secrets({})),
        run_id="e2e",
    )
    summary = RunSummary(run_id="e2e", mode="scheduled", dry_run=False)
    manager.run_scheduled(summary, NOW)

    assert summary.ok, summary.failures
    [article] = store.list_articles()
    assert article.status == ArticleStatus.PUBLISHED
    assert article.publication.stage == PublicationStage.VERIFIED
    assert article.publication.public_url == f"{mock_typo3.base_url}/news/passkeys-im-studium"
    assert [r.passed for r in article.reviews] == [False, True]
    record = mock_typo3.state.records[NEWS][article.publication.typo3_uid]
    assert record["hidden"] == 0 and len(record["references"]) == 1
    assert store.get_slot(summary.slot)["status"] == "published"

    # The same slot is not served again by a delayed duplicate run.
    again = RunSummary(run_id="e2e-2", mode="scheduled", dry_run=False)
    manager.run_scheduled(again, NOW.replace(minute=40))
    assert not again.published and len(mock_typo3.state.records[NEWS]) == 1


def test_cli_dry_run_fills_form_but_saves_nothing(browser_env, mock_typo3, config_dir, monkeypatch, capsys):
    settings, _selectors = browser_env
    text = (config_dir / "settings.yaml").read_text()
    text = text.replace("  required: true", "  required: false")  # no Pixabay key in this test
    text = text.replace("  profile: campus_frontend", "  profile: news")
    (config_dir / "settings.yaml").write_text(text)
    sel = (config_dir / "typo3_selectors.yaml").read_text().replace("storage_pid: 0", "storage_pid: 5")
    (config_dir / "typo3_selectors.yaml").write_text(sel)
    monkeypatch.setenv("TYPO3_BACKEND_URL", settings.typo3.backend_url)
    monkeypatch.setenv("WEBSITE_BASE_URL", mock_typo3.base_url)
    if settings.typo3.browser.executable_path:
        monkeypatch.setenv("CHROMIUM_EXECUTABLE", settings.typo3.browser.executable_path)

    result_file = config_dir.parent / "bot-result.json"
    monkeypatch.setenv("BOT_RESULT_FILE", str(result_file))

    code = main(["--config-dir", str(config_dir), "run", "--mode", "full", "--dry-run", "--fake-llm"])
    out = capsys.readouterr().out
    assert code == 0, out
    # The control panel shows dry-run articles from this file (kept in the bot-runs branch).
    result = json.loads(result_file.read_text(encoding="utf-8"))
    assert result["summary"]["dry_run"] is True
    assert [a["id"] for a in result["articles"]] == result["summary"]["generated"]
    assert result["articles"][0]["content"]["title"] and "revisions" not in result["articles"][0]
    assert "dry run: filled the TYPO3 form" in out
    assert mock_typo3.state.records[NEWS] == {}
    assert not (config_dir.parent / "data" / "articles").exists() or not any(
        (config_dir.parent / "data" / "articles").iterdir()
    )
