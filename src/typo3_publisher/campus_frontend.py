"""campus a author pages (profile ``campus_frontend``): no TYPO3 backend, no TYPO3 login.

The bot logs in with the normal author account on ``<site>/login``, opens "Beitrag
publizieren" (``/artikel/create``) and goes through the four steps of the form like an
author does:

1. Kategorie (Fakten / Meinung)
2. Titel, Vorspann, Story (Quill editor)
3. Artikelfoto with Bildbeschreibung, Bildtext and Copyright
4. Datum, Ressort, Region, Tags

It then clicks **"Als Entwurf speichern"**, never "Veröffentlichen", and marks the article
as AI-assisted (``isModifiedByAi=1``, the same flag the site's own AI "Hugo" sets). A request
guard aborts every form submission that is not a draft or not marked as AI-assisted, so the
bot cannot publish even if the page changes. The author reads, edits and publishes the
draft on the website.

The site identifier (``#currentSiteIdentifier`` in the form) must match ``WEBSITE_ID``.
"""

from __future__ import annotations

import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse
from zoneinfo import ZoneInfo

from playwright.sync_api import Browser, BrowserContext, Page, Playwright, Route, sync_playwright
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeout

from . import locators as L
from .config import Secrets, Settings
from .html_utils import html_to_text, normalise
from .logging_utils import get_logger, redact
from .models import Article, ImageInfo, PublicationInfo, PublicationStage, iso
from .serialization import ConfigError
from .typo3_browser import (
    AuthChallengeError,
    Checkpoint,
    Diagnostics,
    FormError,
    LoginError,
    PublishOptions,
    ReconciliationRequiredError,
    Typo3Error,
    attribution_html,
)

log = get_logger("campus")

CATEGORY_FACTS = 1
CATEGORY_OPINION = 2
DRAFT_TABLE = "campus_frontend_draft"

# Field length limits of the article form (maxlength / JustValidate rules).
MAX_TITLE = 130
MAX_TEASER = 500
MAX_SEO_TITLE = 60
MAX_META_DESCRIPTION = 155
MAX_IMAGE_TEXT = 255

_STORY_JS = """
([selector, html]) => {
  const el = document.querySelector(selector);
  if (!el || !window.Quill) return null;
  const quill = window.Quill.find(el);
  if (!quill || !quill.clipboard) return null;
  quill.clipboard.dangerouslyPasteHTML(html);
  return quill.getText();
}
"""

_SET_VALUE_JS = """
([selector, value]) => {
  const el = document.querySelector(selector);
  if (!el) return false;
  el.value = value;
  el.dispatchEvent(new Event('input', {bubbles: true}));
  el.dispatchEvent(new Event('change', {bubbles: true}));
  return true;
}
"""


def form_field(body: bytes, name: str) -> str | None:
    """Value of field ``name`` in a multipart or urlencoded form body (None if absent)."""
    marker = f'name="{name}"'.encode()
    if marker in body:
        m = re.search(re.escape(marker) + rb"\r\n\r\n([^\r\n]*)", body)
        return m.group(1).decode("utf-8", "replace") if m else ""
    try:
        values = parse_qs(body.decode("utf-8"), keep_blank_values=True).get(name)
    except UnicodeDecodeError:
        return None
    return values[0] if values else None


class CampusBrowser:
    """Playwright session on the public campus a website, logged in as the author."""

    def __init__(self, settings: Settings, selectors: dict[str, Any], secrets: Secrets, diagnostics: Diagnostics):
        self.settings = settings
        self.campus = settings.campus
        self.cfg = selectors["campus_frontend"]
        self.secrets = secrets
        self.diagnostics = diagnostics
        if not self.campus.site_id:
            raise ConfigError("WEBSITE_ID is not set (the site identifier of the article form, e.g. campus-a-college)")
        self.base_url = self.campus.resolved_base_url(settings.website.base_url)
        if not self.base_url:
            raise ConfigError(f"WEBSITE_BASE_URL is not set and the website id {self.campus.site_id!r} is unknown")
        self._pw: Playwright | None = None
        self.browser: Browser | None = None
        self.context: BrowserContext | None = None
        self.page: Page | None = None
        self.logged_in = False
        self.refused_submissions: list[str] = []

    # -------------------------------------------------------------------- lifecycle
    def __enter__(self) -> CampusBrowser:
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if exc is not None:
            self.diagnostics.capture(self.page, f"error-{exc_type.__name__}")
        self.close()

    def start(self) -> None:
        b = self.settings.typo3.browser
        self._pw = sync_playwright().start()
        launch: dict[str, Any] = {"headless": b.headless, "slow_mo": b.slow_mo_ms}
        if b.executable_path:
            launch["executable_path"] = b.executable_path
        self.browser = self._pw.chromium.launch(**launch)
        self.context = self.browser.new_context(
            viewport={"width": b.viewport_width, "height": b.viewport_height},
            locale=b.locale,
            accept_downloads=False,
        )
        self.context.set_default_timeout(b.action_timeout_ms)
        self.context.set_default_navigation_timeout(b.navigation_timeout_ms)
        self.context.route("**/*", self._route)
        self.page = self.context.new_page()
        self.page.on("console", lambda m: self.diagnostics.console.append(f"[{m.type}] {m.text}"))
        self.page.on("pageerror", lambda e: self.diagnostics.console.append(f"[pageerror] {e}"))
        self.page.on("dialog", self._on_dialog)

    def close(self) -> None:
        for closer in (self.context, self.browser):
            try:
                if closer:
                    closer.close()
            except PlaywrightError:
                pass
        if self._pw:
            self._pw.stop()
        self._pw = self.browser = self.context = self.page = None

    def _on_dialog(self, dialog) -> None:
        log.info("browser dialog (%s): %s - dismissing", dialog.type, dialog.message[:200])
        try:
            dialog.dismiss()
        except PlaywrightError:
            pass

    def _route(self, route: Route) -> None:
        request = route.request
        host = urlparse(request.url).hostname or ""
        if any(host == h or host.endswith("." + h) for h in self.campus.block_hosts):
            route.abort()
            return
        if request.method == "POST":
            body = request.post_data_buffer or b""
            action = form_field(body, "submitAction")
            if action is not None:
                ai_flag = form_field(body, "tx_hirokicampus_articlecreate[newNews][isModifiedByAi]")
                if action != "draft" or ai_flag != "1":
                    # Safety net: the bot only ever saves drafts that are marked as AI-assisted.
                    self.refused_submissions.append(f"submitAction={action!r}, isModifiedByAi={ai_flag!r}")
                    log.error("refused to send an article form with submitAction=%r isModifiedByAi=%r", action, ai_flag)
                    route.abort()
                    return
        route.continue_()

    @property
    def timeout(self) -> int:
        return self.settings.typo3.browser.action_timeout_ms

    @property
    def nav_timeout(self) -> int:
        return self.settings.typo3.browser.navigation_timeout_ms

    def url(self, path: str) -> str:
        return f"{self.base_url}/{path.lstrip('/')}"

    def _settle(self) -> None:
        assert self.page is not None
        try:
            self.page.wait_for_load_state("load", timeout=self.timeout)
        except PlaywrightTimeout:
            pass
        decline = L.try_find(self.page, self.cfg.get("consent_decline"))
        if decline is not None:
            try:
                decline.click()
                log.info("cookie banner: declined optional cookies")
            except PlaywrightError:
                pass

    # -------------------------------------------------------------------- login
    def auth_state(self) -> str | None:
        assert self.page is not None
        sel = self.cfg["login"]
        for state in ("captcha", "success"):
            if L.try_find(self.page, sel.get(state), visible=False) is not None:
                return state
        return None

    def login(self) -> str:
        """Log in with the author account on the login page. Returns 'password' or 'session'."""
        assert self.page is not None
        page = self.page
        sel = self.cfg["login"]
        page.goto(self.url(self.campus.login_path), wait_until="domcontentloaded")
        self._settle()
        state = self.auth_state()
        if state == "success":
            self.logged_in = True
            return "session"
        self._raise_for_captcha(state)
        username = self.secrets.require("TYPO3_USERNAME")
        password = self.secrets.require("TYPO3_PASSWORD")
        L.find(page, sel["username"], timeout_ms=self.timeout, what="e-mail field").fill(username)
        L.find(page, sel["password"], timeout_ms=self.timeout, what="password field").fill(password)
        submit = L.find(page, sel["submit"], timeout_ms=self.timeout, what="login button")
        try:
            with page.expect_navigation(wait_until="domcontentloaded", timeout=self.nav_timeout):
                submit.click()
        except PlaywrightTimeout:
            pass
        deadline = time.monotonic() + self.timeout / 1000
        while time.monotonic() < deadline:
            state = self.auth_state()
            if state in {"success", "captcha"}:
                break
            page.wait_for_timeout(250)
        self._raise_for_captcha(state)
        if state == "success":
            self.logged_in = True
            log.info("logged in on %s", self.base_url)
            return "password"
        texts = []
        for s in L.as_list(sel.get("error")):
            try:
                texts += [t.strip() for t in page.locator(s).all_inner_texts() if t.strip()]
            except PlaywrightError:
                pass
        self.diagnostics.capture(page, "login-failed")
        raise LoginError("login did not succeed: " + (redact(" ".join(texts))[:300] or "credentials rejected"))

    def _raise_for_captcha(self, state: str | None) -> None:
        if state == "captcha":
            self.diagnostics.capture(self.page, "captcha-detected")
            raise AuthChallengeError("captcha", "A CAPTCHA protects the login. The bot will not try to solve it.")

    def ensure_logged_in(self) -> None:
        if not self.logged_in:
            self.login()

    # -------------------------------------------------------------------- the article form
    def _f(self, key: str) -> Any:
        return self.cfg["form"][key]

    def open_form(self) -> None:
        assert self.page is not None
        self.ensure_logged_in()
        self.page.goto(self.url(self.campus.create_path), wait_until="domcontentloaded")
        self._settle()
        if L.try_find(self.page, self._f("root"), visible=False) is None:
            if L.try_find(self.page, self.cfg["login"]["password"]) is not None:
                raise LoginError("the article form redirected to the login page (session expired?)")
            self.diagnostics.capture(self.page, "article-form-not-found")
            raise FormError(f"article form not found on {self.campus.create_path}")
        L.find(self.page, self._f("steps")[1], timeout_ms=self.nav_timeout, what="step 1 of the article form")
        site = self._value(self._f("site_id"))
        if site != self.campus.site_id:
            raise Typo3Error(
                f"website id mismatch: the form reports {site!r} but WEBSITE_ID is {self.campus.site_id!r}"
            )

    def _value(self, selector: str) -> str:
        assert self.page is not None
        loc = self.page.locator(selector)
        return loc.first.input_value() if loc.count() else ""

    def _set_value(self, selector: str, value: str) -> None:
        """Hidden inputs (AI flag, SEO fields) are set like the page's own scripts do."""
        assert self.page is not None
        if not self.page.evaluate(_SET_VALUE_JS, [selector, value]):
            raise FormError(f"form field {selector} not found")

    def _fill(self, selector: str, value: str) -> None:
        assert self.page is not None
        loc = L.find(self.page, selector, timeout_ms=self.timeout, what=selector)
        loc.fill(value)
        loc.dispatch_event("change")
        if loc.input_value().strip() != value.strip():
            raise FormError(f"form field {selector} did not accept the value")

    def validation_errors(self) -> list[str]:
        assert self.page is not None
        loc = self.page.locator(self._f("errors"))
        texts = []
        for i in range(min(loc.count(), 20)):
            try:
                if loc.nth(i).is_visible() and (t := loc.nth(i).inner_text().strip()):
                    texts.append(t[:200])
            except PlaywrightError:
                continue
        return texts

    def _next(self, step: int) -> None:
        """Click "Weiter" and wait until ``step`` is shown (the page validates the current step)."""
        assert self.page is not None
        L.find(self.page, self._f("next_button"), timeout_ms=self.timeout, what="Weiter button").click()
        try:
            L.find(self.page, self._f("steps")[step], timeout_ms=self.timeout, what=f"step {step}")
        except L.LocatorNotFoundError as exc:
            errors = self.validation_errors()
            self.diagnostics.capture(self.page, f"step-{step}-not-reached")
            raise FormError(f"the form did not continue to step {step}: " + (" | ".join(errors) or str(exc))) from exc

    def category(self, article: Article) -> int:
        assert article.content is not None
        return CATEGORY_OPINION if article.content.article_type in self.campus.opinion_types else CATEGORY_FACTS

    def fill(self, article: Article, options: PublishOptions) -> None:
        """Steps 1-4 of the article form. Nothing is sent to the website."""
        assert self.page is not None and article.content is not None
        c = article.content
        page = self.page

        # Step 1: category
        page.locator(self._f("category_radio").format(value=self.category(article))).check()
        self._next(2)

        # Step 2: text. The meta description is filled here so the page does not generate its own.
        self._fill(self._f("title"), c.title[:MAX_TITLE])
        self._fill(self._f("teaser"), c.teaser[:MAX_TEASER])
        self._set_story(self._body(article, options))
        self._set_value(self._f("meta_description"), c.meta_description[:MAX_META_DESCRIPTION])
        self._set_value(self._f("ai_flag"), "1")
        self._next(3)

        # Step 3: image
        if article.image:
            self._attach_image(article.image)
        self._next(4)

        # Step 4: date, Ressort, Region, Tags, SEO title
        if not self._value(self._f("date")):
            today = datetime.now(ZoneInfo(self.settings.publishing.timezone)).strftime("%Y-%m-%d")
            self._fill(self._f("date"), today)
        self._select(self._f("resort"), self.campus.resort_by_topic.get(article.topic, self.campus.default_resort))
        self._select(self._f("region"), self.campus.region_by_topic.get(article.topic, self.campus.default_region))
        self._set_tags([*options.categories, *c.keywords])
        self._set_value(self._f("seo_title"), (c.seo_title or c.title)[:MAX_SEO_TITLE])
        self._set_value(self._f("ai_flag"), "1")

    def _body(self, article: Article, options: PublishOptions) -> str:
        assert article.content is not None
        body = article.content.body_html
        if article.image and options.attribution_enabled and options.attribution_target in {"body", "both"}:
            body += attribution_html(article.image)
        return body

    def _set_story(self, html: str) -> None:
        assert self.page is not None
        text = self.page.evaluate(_STORY_JS, [self._f("story"), html])
        if text is None:
            raise FormError("the story editor (Quill) could not be addressed")
        probe = normalise(html_to_text(html))[:60]
        if probe and probe not in normalise(text):
            raise FormError("the story editor does not contain the expected text after filling")

    def image_confirmable(self, image: ImageInfo) -> bool:
        """The form requires two confirmations for an image. They are only given when they are
        true for the image: free Pixabay licence, and no identifiable people (exclude_people)."""
        return self.campus.confirm_image_rights and image.provider == "pixabay" and self.settings.images.exclude_people

    def _attach_image(self, image: ImageInfo) -> None:
        assert self.page is not None
        if not self.image_confirmable(image):
            raise FormError(
                "the image confirmations of the form cannot be given automatically for this image "
                "(campus.confirm_image_rights, Pixabay licence and images.exclude_people are required)"
            )
        page = self.page
        page.set_input_files(self._f("image_input"), str(Path(image.local_path)))
        flag = self._f("image_flag")
        deadline = time.monotonic() + self.settings.typo3.browser.upload_timeout_ms / 1000
        while self._value(flag) != "1":
            if time.monotonic() > deadline:
                raise FormError("the image was not accepted by the form (no crop area appeared)")
            page.wait_for_timeout(200)
        self._fill(self._f("image_alt"), image.alt_text[:MAX_IMAGE_TEXT])
        self._fill(self._f("image_caption"), image.caption[:MAX_IMAGE_TEXT])
        copyright_line = image.attribution or f"© {image.user} / Pixabay"
        self._fill(self._f("image_copyright"), copyright_line[:MAX_IMAGE_TEXT])
        for key in ("consent_rights", "consent_people"):
            page.locator(self._f(key)).check()

    def _select(self, selector: str, label: str) -> None:
        assert self.page is not None
        loc = L.find(self.page, selector, timeout_ms=self.timeout, what=selector)
        options = [o.strip() for o in loc.locator("option").all_inner_texts()]
        match = next((o for o in options if o.casefold() == label.casefold()), None)
        if match is None:
            raise FormError(f"{selector}: option {label!r} not found (available: {', '.join(options[1:])})")
        loc.select_option(label=match)

    def _set_tags(self, names: list[str]) -> list[str]:
        """Pick tags in the site's tag widget: an existing tag with the same name, otherwise a new one."""
        assert self.page is not None
        page = self.page
        wanted: list[str] = []
        for name in (n.strip() for n in names):
            if len(name) >= 2 and name.casefold() not in {w.casefold() for w in wanted}:
                wanted.append(name)
        listbox = page.locator(self._f("tags_listbox"))
        selected: list[str] = []
        for name in wanted:
            if len(selected) >= self.campus.max_tags:
                break
            search = L.try_find(page, self._f("tags_search"))
            if search is None:
                L.find(page, self._f("tags_open"), timeout_ms=self.timeout, what="tags field").click()
                search = L.find(page, self._f("tags_search"), timeout_ms=self.timeout, what="tag search")
            search.fill(name)
            page.wait_for_timeout(150)
            exact = listbox.locator("li").filter(has_text=re.compile(rf"^\s*{re.escape(name)}\s*$", re.I))
            create = listbox.locator("li").filter(has_text=re.compile(r"Tag erstellen", re.I))
            if exact.count() and exact.first.is_visible():
                exact.first.click()
            elif self.campus.create_missing_tags and create.count() and create.first.is_visible():
                create.first.click()
            else:
                search.fill("")
                continue
            selected.append(name)
        search = L.try_find(page, self._f("tags_search"))
        if search is not None:
            search.press("Escape")
        count = page.eval_on_selector(self._f("tags_select"), "s => s.selectedOptions.length")
        if not count:
            raise FormError("no tag could be selected (Tags are required for Fakten and Meinung)")
        log.info("tags: %s", ", ".join(selected))
        return selected

    def save_draft(self) -> str:
        """Click "Als Entwurf speichern" and wait for the website to accept it. Returns the new URL."""
        assert self.page is not None
        page = self.page
        button = L.find(page, self._f("draft_button"), timeout_ms=self.timeout, what="'Als Entwurf speichern'")
        try:
            with page.expect_navigation(wait_until="domcontentloaded", timeout=self.nav_timeout):
                button.click()
        except PlaywrightError as exc:  # timeout, or the request guard aborted the submission
            if self.refused_submissions:
                raise FormError("refused to submit the article form: " + "; ".join(self.refused_submissions)) from exc
            errors = self.validation_errors()
            self.diagnostics.capture(page, "draft-not-saved")
            reason = " | ".join(errors) or "no response from the website"
            raise FormError(f"the draft was not sent: {reason}") from exc
        if self.refused_submissions:
            raise FormError("refused to submit the article form: " + "; ".join(self.refused_submissions))
        self._settle()
        still_on_form = urlparse(page.url).path.rstrip("/") == self.campus.create_path.rstrip("/")
        if still_on_form and L.try_find(page, self._f("root"), visible=False) is not None:
            errors = self.validation_errors()
            self.diagnostics.capture(page, "draft-rejected")
            raise FormError("the website did not accept the draft: " + (" | ".join(errors) or "form shown again"))
        log.info("draft saved (now at %s)", page.url)
        return page.url

    def find_existing_draft(self, title: str) -> bool:
        """Is an article with this title listed on the author's profile? Used after a crash
        between sending the draft and recording it, so the draft is not sent twice."""
        assert self.page is not None
        self.ensure_logged_in()
        response = self.page.goto(self.url(f"{self.campus.profile_path}?fetchNews=1"), wait_until="domcontentloaded")
        if response is None or not response.ok:
            raise ReconciliationRequiredError(
                f"could not read the profile page to check whether '{title}' was already saved; "
                "check the drafts on the website before retrying"
            )
        return normalise(title) in normalise(self.page.inner_text("body"))

    def check_form(self) -> dict[str, Any]:
        """Setup check: login, open the form, confirm the website id and every field the bot uses."""
        assert self.page is not None
        report: dict[str, Any] = {"login": self.login()}
        self.open_form()
        report["site_id"] = self._value(self._f("site_id"))
        report["role"] = self._value(self._f("role"))
        fields = {}
        for key in (
            "category_radio",
            "title",
            "teaser",
            "story",
            "ai_flag",
            "meta_description",
            "image_input",
            "image_flag",
            "image_alt",
            "image_caption",
            "image_copyright",
            "consent_rights",
            "consent_people",
            "date",
            "resort",
            "region",
            "tags_select",
            "seo_title",
            "draft_button",
        ):
            spec = self._f(key)
            if key == "category_radio":
                spec = spec.format(value=CATEGORY_FACTS)
            fields[key] = L.try_find(self.page, spec, visible=False) is not None
        report["fields"] = fields
        options = {}
        for key, wanted in (("resort", self.campus.default_resort), ("region", self.campus.default_region)):
            labels = [o.strip() for o in self.page.locator(self._f(key)).locator("option").all_inner_texts()]
            options[key] = {"default": wanted, "found": wanted in labels, "available": labels[1:]}
        report["options"] = options
        report["ok"] = all(fields.values()) and all(o["found"] for o in options.values())
        return report


def draft_uid(url: str) -> int | None:
    """The news uid if the website redirects to an edit URL such as ...[news]=123."""
    m = re.search(r"\[news\]=(\d+)", unquote(url))
    return int(m.group(1)) if m else None


class CampusDraftPublisher:
    """Saves approved articles as drafts on the campus a website. Never publishes."""

    profile = "campus_frontend"

    def __init__(self, browser: CampusBrowser):
        self.browser = browser

    def publish(self, article: Article, options: PublishOptions, checkpoint: Checkpoint) -> PublicationInfo:
        assert article.content is not None
        pub = article.publication
        pub.typo3_table = DRAFT_TABLE
        if pub.stage == PublicationStage.DRAFT_SAVED:
            return pub
        if pub.stage == PublicationStage.STARTED and self.browser.find_existing_draft(article.content.title):
            # A previous run sent the form but died before recording it.
            log.info("draft '%s' already exists on the website; not sending it again", article.content.title)
            pub.stage = PublicationStage.DRAFT_SAVED
            checkpoint(pub)
            return pub
        self.browser.open_form()
        self.browser.fill(article, options)
        pub.stage = PublicationStage.STARTED
        checkpoint(pub)
        url = self.browser.save_draft()
        pub.stage = PublicationStage.DRAFT_SAVED
        pub.typo3_uid = draft_uid(url)
        pub.verification = {**pub.verification, "draft_saved_url": url, "draft_saved_at": iso()}
        checkpoint(pub)
        return pub

    def update(self, article: Article, options: PublishOptions, checkpoint: Checkpoint) -> PublicationInfo:
        raise Typo3Error("profile campus_frontend does not change existing articles; edit them on the website")

    def preview(self, article: Article, options: PublishOptions) -> list[str]:
        """Dry run: fill in all four steps, but never send the form."""
        self.browser.open_form()
        self.browser.fill(article, options)
        return [str(p) for p in self.browser.diagnostics.capture(self.browser.page, "dry-run-filled-form")]
