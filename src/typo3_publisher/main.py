"""Command line interface.

    python -m typo3_publisher run --mode scheduled         # what the cron workflow runs
    python -m typo3_publisher run --mode full --dry-run    # whole pipeline, nothing saved in TYPO3
    python -m typo3_publisher discover [--test-save]       # map/verify the TYPO3 backend
    python -m typo3_publisher check-login
    python -m typo3_publisher --help

Exit codes: 0 success / nothing to do, 1 failure, 2 configuration error,
3 human action needed (MFA/CAPTCHA, manual reconciliation), 4 state locked by another run.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .article_manager import ArticleManager, PublishingDisabledError, RunSummary
from .config import (
    ConfigError,
    MissingSecretError,
    Secrets,
    Settings,
    load_selectors,
    load_settings,
    load_topics,
    read_text,
)
from .image_finder import ImageFinder, PixabayClient
from .llm import FakeLLM, LLMClient, api_key_env, create_llm
from .logging_utils import get_logger, redact, register_secret, setup_logging
from .models import ArticleStatus, iso
from .notifier import Notifier, write_step_summary
from .research import Researcher
from .retry import retry_call
from .scheduler import suggest_cron, upcoming_slots
from .serialization import to_dict
from .state_manager import LockHeldError, StateError, StateStore, create_sync
from .topic_planner import TopicPlanner
from .verifier import PublicVerifier

log = get_logger("main")

MODES = ["scheduled", "full", "generate-only", "review-only", "prepare", "publish-approved", "update"]


def load_dotenv(path: Path) -> None:
    """Minimal .env support for local runs (never overrides real environment variables)."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def make_run_id() -> str:
    if gh := os.environ.get("GITHUB_RUN_ID"):
        return f"gh-{gh}-{os.environ.get('GITHUB_RUN_ATTEMPT', '1')}"
    return f"local-{datetime.now(UTC):%Y%m%dT%H%M%S}-{os.getpid()}"


class LazyLLM:
    """Creates the real client on first use, so modes without LLM calls need no API key."""

    def __init__(self, settings: Settings, secrets: Secrets, fake: bool):
        self.settings, self.secrets, self.fake = settings, secrets, fake
        self._client: LLMClient | None = (
            FakeLLM(language=settings.website.language, words=settings.editorial.min_words + 50) if fake else None
        )

    def complete_json(self, **kwargs: Any) -> dict[str, Any]:
        if self._client is None:
            key = self.secrets.get(api_key_env(self.settings.llm))
            register_secret(key)  # also covers a custom llm.api_key_env
            self._client = create_llm(self.settings.llm, self.settings.retries.llm, key)
        return self._client.complete_json(**kwargs)

    @property
    def is_fake(self) -> bool:
        return self.fake or self.settings.llm.provider == "fake"


def site_browser(settings: Settings, selectors: dict[str, Any], secrets: Secrets, diagnostics: Any) -> Any:
    """The TYPO3 backend browser, or the campus a author pages for profile campus_frontend."""
    if settings.typo3.profile == "campus_frontend":
        from .campus_frontend import CampusBrowser

        return CampusBrowser(settings, selectors, secrets, diagnostics)
    from .typo3_browser import Typo3Browser

    return Typo3Browser(settings, selectors, secrets, diagnostics)


def browser_session_factory(settings: Settings, secrets: Secrets, run_id: str):
    from playwright.sync_api import Error as PlaywrightError

    from .typo3_browser import Diagnostics, LoginError, create_publisher

    @contextlib.contextmanager
    def factory() -> Iterator[Any]:
        selectors = load_selectors(settings)
        diagnostics = Diagnostics(
            settings.path(settings.diagnostics.directory),
            run_id,
            settings.diagnostics.screenshots,
            settings.diagnostics.html_dumps,
        )
        with site_browser(settings, selectors, secrets, diagnostics) as browser:
            # Only transient browser/network errors are retried: repeating rejected logins could lock the account.
            retry_call(
                browser.login,
                settings.retries.browser,
                retry_on=(PlaywrightError,),
                give_up_on=(LoginError,),
                description="login",
            )
            yield create_publisher(browser)

    return factory


def build_manager(
    settings: Settings, args: argparse.Namespace, secrets: Secrets, run_id: str, store: StateStore, sync
) -> ArticleManager:
    llm = LazyLLM(settings, secrets, fake=args.fake_llm)
    if llm.is_fake:
        settings.llm.provider = "fake"
    retry = settings.retries.http
    image_finder = None
    if settings.images.provider == "pixabay" and (key := secrets.get("PIXABAY_API_KEY")):
        image_finder = ImageFinder(
            settings.images, PixabayClient(key, retry), store, settings.path(settings.images.download_dir), retry
        )
    elif settings.images.required:
        log.warning("PIXABAY_API_KEY is not set: articles cannot be published because images.required is true")
    return ArticleManager(
        settings=settings,
        topics=load_topics(settings.base_dir / "config"),
        store=store,
        sync=sync,
        llm=llm,
        editorial_prompt=read_text(settings, settings.editorial.prompt_file),
        review_rubric=read_text(settings, settings.editorial.review_rubric_file),
        researcher=Researcher(settings.research, retry),
        image_finder=image_finder,
        verifier=PublicVerifier(settings.website),
        publisher_factory=browser_session_factory(settings, secrets, run_id),
        notifier=Notifier(settings.notifications, secrets),
        run_id=run_id,
        dry_run=args.dry_run,
    )


def parse_now(value: str | None) -> datetime:
    if not value:
        return datetime.now(UTC)
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        raise ConfigError("--now must include a timezone offset, e.g. 2026-03-30T08:05:00+02:00")
    return dt


def write_run_result(store: StateStore, summary: RunSummary) -> None:
    """For the control panel (site/): the run summary and the articles it touched, also on dry runs.
    The workflows pass BOT_RESULT_FILE and keep the file in the bot-runs branch."""
    path = os.environ.get("BOT_RESULT_FILE")
    if not path:
        return
    ids = [*summary.generated, *summary.approved, *summary.rejected, *(d.get("id", "") for d in summary.drafts)]
    articles = []
    for article_id in dict.fromkeys(i for i in ids if i):
        try:
            data = to_dict(store.load_article(article_id))
        except StateError:
            continue
        data.pop("revisions", None)
        data["reviews"] = data.get("reviews", [])[-1:]
        articles.append(data)
    result = {"summary": to_dict(summary), "articles": articles}
    Path(path).write_text(redact(json.dumps(result, ensure_ascii=False, indent=1)), encoding="utf-8")


# ============================================================================ commands


def cmd_run(settings: Settings, args: argparse.Namespace) -> int:
    secrets = Secrets()
    run_id = make_run_id()
    sync = create_sync(settings.state, settings.retries.git, settings.base_dir, dry_run=args.dry_run)
    sync.prepare()
    store = StateStore(sync.root)
    summary = RunSummary(run_id=run_id, mode=args.mode, dry_run=args.dry_run)
    now = parse_now(args.now)
    try:
        sync.acquire_lock(run_id)
    except LockHeldError as exc:
        log.warning("%s", exc)
        summary.notes.append(f"skipped: {exc}")
        write_step_summary(summary.markdown())
        return 4
    exit_code = 0
    try:
        manager = build_manager(settings, args, secrets, run_id, store, sync)
        if args.mode == "scheduled":
            manager.run_scheduled(summary, now, force=args.force)
        elif args.mode == "full":
            manager.run_full(summary, now, idea=args.idea, topic=args.topic)
        elif args.mode == "generate-only":
            manager.run_generate_only(summary, now, args.count, args.idea, args.topic)
        elif args.mode == "review-only":
            manager.run_review_only(summary, args.article_id)
        elif args.mode == "prepare":
            manager.run_prepare(summary, now, args.idea, args.topic)
        elif args.mode == "publish-approved":
            manager.run_publish_approved(summary, args.limit, args.article_id)
        elif args.mode == "update":
            manager.run_update(summary, now, args.article_id)
    except PublishingDisabledError as exc:
        summary.notes.append(str(exc))
        log.warning("%s", exc)
    except (ConfigError, MissingSecretError) as exc:
        summary.failures.append({"stage": "configuration", "error": str(exc)})
        exit_code = 2
    except Exception as exc:
        from .typo3_browser import AuthChallengeError

        if isinstance(exc, AuthChallengeError):
            summary.needs_human = True
            exit_code = 3
        else:
            log.exception("run failed")
            summary.failures.append({"stage": "run", "error": redact(str(exc))})
    finally:
        summary.finished_at = iso()
        if not args.dry_run:
            store.record_run(to_dict(summary))
            try:
                sync.commit(f"Run {run_id} summary")
            except Exception as exc:
                log.error("could not persist run summary: %s", exc)
        sync.release_lock(run_id)
        write_step_summary(summary.markdown())
        write_run_result(store, summary)
        print(summary.markdown())
    if exit_code:
        return exit_code
    if summary.needs_human:
        return 3
    return 0 if summary.ok else 1


def _browser(settings: Settings, label: str, headed: bool):
    from .typo3_browser import Diagnostics

    if headed:
        settings.typo3.browser.headless = False
    diagnostics = Diagnostics(
        settings.path(settings.diagnostics.directory),
        f"{label}-{make_run_id()}",
        settings.diagnostics.screenshots,
        settings.diagnostics.html_dumps,
    )
    return site_browser(settings, load_selectors(settings), Secrets(), diagnostics)


def cmd_check_login(settings: Settings, args: argparse.Namespace) -> int:
    from .typo3_browser import AuthChallengeError, LoginError

    with _browser(settings, "check-login", args.headed) as browser:
        try:
            how = browser.login()
        except AuthChallengeError as exc:
            print(f"LOGIN BLOCKED ({exc.kind}): {exc}")
            return 3
        except LoginError as exc:
            print(f"LOGIN FAILED: {exc}")
            return 1
        print(f"LOGIN OK via {how} (verified by the page markup after login)")
        return 0


def cmd_discover(settings: Settings, args: argparse.Namespace) -> int:
    if settings.typo3.profile == "campus_frontend":
        return _check_campus_form(settings, args)
    from .discovery import Discovery

    out = settings.path("artifacts/discovery") / make_run_id()
    with _browser(settings, "discovery", args.headed) as browser:
        discovery = Discovery(browser, out)
        report = discovery.run(test_save=args.test_save, test_upload=args.test_upload)
        md, proposal = discovery.write(settings.path(settings.typo3.selectors_file), overwrite=args.write)
    print(md.read_text(encoding="utf-8"))
    print(f"\nReport: {md}\nSelector proposal: {proposal}")
    if report.auth_challenge:
        return 3
    return 0 if report.all_required_verified and not report.manual_assistance else 1


def _check_campus_form(settings: Settings, args: argparse.Namespace) -> int:
    """campus_frontend: log in, open the article form and check every field the bot uses (sends nothing)."""
    from .typo3_browser import AuthChallengeError, LoginError

    with _browser(settings, "campus-check", args.headed) as browser:
        try:
            report = browser.check_form()
        except AuthChallengeError as exc:
            print(f"LOGIN BLOCKED ({exc.kind}): {exc}")
            return 3
        except LoginError as exc:
            print(f"LOGIN FAILED: {exc}")
            return 1
    lines = [
        "### campus a article form",
        "",
        f"- Login: {report['login']}",
        f"- Website id of the form: `{report['site_id']}` (WEBSITE_ID matches)",
        f"- Role: `{report['role']}`",
        "",
        "| Field | Found |",
        "|---|---|",
        *[f"| {key} | {'yes' if found else '**NO**'} |" for key, found in report["fields"].items()],
        "",
    ]
    for key, opt in report["options"].items():
        state = "found" if opt["found"] else f"**not found** (available: {', '.join(opt['available'])})"
        lines.append(f"- {key} `{opt['default']}`: {state}")
    lines.append("")
    lines.append(("All fields found." if report["ok"] else "Some fields are missing.") + " Nothing was sent.")
    md = "\n".join(lines) + "\n"
    out = settings.path("artifacts/discovery") / make_run_id()
    out.mkdir(parents=True, exist_ok=True)
    (out / "report.md").write_text(md, encoding="utf-8")
    print(md)
    return 0 if report["ok"] else 1


def cmd_auth_export(settings: Settings, args: argparse.Namespace) -> int:
    """Interactive: a human logs in (incl. MFA) in a visible browser; the session is exported."""
    if settings.typo3.profile == "campus_frontend":
        print("auth-export is only needed for TYPO3 backends with MFA; campus_frontend logs in with e-mail/password.")
        return 2
    settings.typo3.browser.headless = False
    with _browser(settings, "auth-export", headed=True) as browser:
        assert browser.page is not None and browser.context is not None
        browser.page.goto(browser.url(""))
        print("Log in in the opened browser window (complete MFA if asked). Waiting up to 5 minutes...")
        deadline = time.monotonic() + 300
        while time.monotonic() < deadline:
            if browser.auth_state() == "success":
                break
            browser.page.wait_for_timeout(1000)
        else:
            print("Timed out waiting for a successful login.")
            return 1
        state = browser.context.storage_state()
    out = Path(args.output)
    out.write_text(json.dumps(state), encoding="utf-8")
    os.chmod(out, 0o600)
    print(f"Session written to {out} (contains authentication cookies - keep it private, delete after use).")
    print(
        "Store it as the GitHub secret TYPO3_STORAGE_STATE with:  base64 -w0 "
        + str(out)
        + " | gh secret set TYPO3_STORAGE_STATE"
    )
    print("The session expires according to the TYPO3 backend session timeout; repeat when the bot reports MFA.")
    return 0


def cmd_verify_url(settings: Settings, args: argparse.Namespace) -> int:
    verifier = PublicVerifier(settings.website)
    body = ""
    title = args.title
    if args.article_id:
        sync = create_sync(settings.state, settings.retries.git, settings.base_dir, dry_run=True)
        article = StateStore(sync.root).load_article(args.article_id)
        assert article.content is not None
        title, body = article.content.title, article.content.body_html
    result = verifier.check(args.url, title, body or f"<p>{title}</p>")
    print(json.dumps(result.as_dict(), indent=2))
    return 0 if result.ok else 1


def _mutating_store(settings: Settings) -> tuple[Any, StateStore, str]:
    sync = create_sync(settings.state, settings.retries.git, settings.base_dir, dry_run=False)
    sync.prepare()
    run_id = make_run_id()
    sync.acquire_lock(run_id)
    return sync, StateStore(sync.root), run_id


def cmd_add_idea(settings: Settings, args: argparse.Namespace) -> int:
    sync, store, run_id = _mutating_store(settings)
    try:
        item = store.add_backlog_item(args.title, args.topic, args.notes)
        sync.commit(f"Add idea {item['id']}")
        print(json.dumps(item, indent=2, ensure_ascii=False))
    finally:
        sync.release_lock(run_id)
    return 0


def _read_store(settings: Settings) -> StateStore:
    if settings.state.backend == "git_branch":
        sync = create_sync(settings.state, settings.retries.git, settings.base_dir, dry_run=False)
        sync.prepare()
        return StateStore(sync.root)
    return StateStore(settings.path(settings.state.local_dir))


def cmd_status(settings: Settings, args: argparse.Namespace) -> int:
    store = _read_store(settings)
    articles = store.list_articles()
    counts: dict[str, int] = {}
    for a in articles:
        counts[a.status] = counts.get(a.status, 0) + 1
    print(f"Articles: {len(articles)}  " + "  ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    for a in articles[-args.limit :]:
        title = a.content.title if a.content else "-"
        score = f"{a.latest_score:.1f}" if a.latest_score is not None else "-"
        print(f"{a.id}  {a.status:<15} score={score:<5} {a.topic:<22} {title[:60]}  {a.publication.public_url}")
    failed = [a for a in articles if a.status in {ArticleStatus.PUBLISH_FAILED, ArticleStatus.PUBLISHING}]
    for a in failed:
        print(f"! {a.id}: {a.errors[-1].message if a.errors else 'incomplete'}")
    return 0


def cmd_plan(settings: Settings, args: argparse.Namespace) -> int:
    store = _read_store(settings)
    planner = TopicPlanner(load_topics(settings.base_dir / "config"), store, settings.publishing.timezone)
    now = parse_now(args.now)
    plan = planner.calendar(now, upcoming_slots(now, settings.publishing, args.days))
    for entry in plan:
        print(f"{entry['local_time'][:16]}  {entry['topic'] or '(quota fulfilled - no article)'}")
    if args.write and settings.state.backend == "local":
        store.write_json("editorial_calendar.json", plan)
    return 0


def cmd_print_cron(settings: Settings, args: argparse.Namespace) -> int:
    for expr in suggest_cron(settings.publishing):
        print(expr)
    return 0


# ============================================================================ entry point


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="typo3_publisher", description="Autonomous TYPO3 publishing bot")
    parser.add_argument("--config-dir", default=os.environ.get("BOT_CONFIG_DIR", "config"))
    parser.add_argument("--log-json", action="store_true", help="structured JSON logs")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="run the pipeline")
    run.add_argument("--mode", choices=MODES, default="scheduled")
    run.add_argument(
        "--dry-run", action="store_true", help="no TYPO3 save/upload, state changes go to a throw-away copy"
    )
    run.add_argument("--fake-llm", action="store_true", help="offline placeholder content (testing only)")
    run.add_argument("--force", action="store_true", help="scheduled mode: run even if no slot is due")
    run.add_argument("--idea", default="", help="manually supplied article idea")
    run.add_argument("--topic", default="", help="topic for --idea or to force a topic")
    run.add_argument("--article-id", default="")
    run.add_argument("--count", type=int, default=1)
    run.add_argument("--limit", type=int, default=1)
    run.add_argument("--now", default=None, help="override current time (ISO 8601 with offset), for testing")
    run.set_defaults(func=cmd_run)

    for name, func, helptext in (
        ("check-login", cmd_check_login, "test the login"),
        ("discover", cmd_discover, "inspect and verify the TYPO3 backend / the campus a article form"),
    ):
        p = sub.add_parser(name, help=helptext)
        p.add_argument("--headed", action="store_true", help="show the browser window")
        p.set_defaults(func=func)
        if name == "discover":
            p.add_argument("--test-save", action="store_true", help="create, save and delete a hidden test record")
            p.add_argument("--test-upload", action="store_true", help="with --test-save: also test the file upload")
            p.add_argument("--write", action="store_true", help="overwrite the selector file instead of a proposal")

    p = sub.add_parser("auth-export", help="interactive login (MFA) and session export")
    p.add_argument("--output", default="typo3.storage-state.json")
    p.set_defaults(func=cmd_auth_export)

    p = sub.add_parser("verify-url", help="check a public article URL")
    p.add_argument("--url", required=True)
    p.add_argument("--title", default="")
    p.add_argument("--article-id", default="")
    p.set_defaults(func=cmd_verify_url)

    p = sub.add_parser("add-idea", help="add an article idea to the backlog")
    p.add_argument("title")
    p.add_argument("--topic", default="")
    p.add_argument("--notes", default="")
    p.set_defaults(func=cmd_add_idea)

    p = sub.add_parser("status", help="show the article registry")
    p.add_argument("--limit", type=int, default=30)
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("plan", help="show the editorial calendar")
    p.add_argument("--days", type=int, default=14)
    p.add_argument("--now", default=None)
    p.add_argument("--write", action="store_true")
    p.set_defaults(func=cmd_plan)

    p = sub.add_parser("print-cron", help="UTC cron expressions covering the configured local slots")
    p.set_defaults(func=cmd_print_cron)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging(json_logs=args.log_json or None)
    config_dir = Path(args.config_dir)
    load_dotenv(config_dir.resolve().parent / ".env")
    try:
        settings = load_settings(config_dir)
        Secrets()  # registers secret values with the log redactor
        return args.func(settings, args)
    except (ConfigError, MissingSecretError) as exc:
        log.error("configuration error: %s", exc)
        return 2


if __name__ == "__main__":
    sys.exit(main())
