"""TYPO3 backend automation through the regular editor web interface (Playwright).

No TYPO3 extension, API, database or server access is used. The bot logs in like an
editor, opens the normal record edit forms (FormEngine), fills fields, uploads the image
through the normal "Select & upload files" control, clicks Save, toggles visibility and
clicks Save again. Rich-text fields are filled through the CKEditor instance running in
the page (the same object the editor types into) - see ``rte_mode`` for a keyboard-only
alternative.

Every step persists a checkpoint (see :class:`~typo3_publisher.models.PublicationStage`)
so that a crashed run resumes the existing record instead of creating a duplicate.
Publishing success is never inferred from the backend: :mod:`typo3_publisher.verifier`
fetches the public page afterwards.
"""

from __future__ import annotations

import html
import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode
from zoneinfo import ZoneInfo

from playwright.sync_api import Browser, BrowserContext, Frame, Locator, Page, Playwright, sync_playwright
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeout

from . import locators as L
from .config import Secrets, Settings
from .html_utils import html_to_text, normalise
from .logging_utils import get_logger, redact
from .models import Article, ImageInfo, PublicationInfo, PublicationStage, iso

log = get_logger("typo3")


class Typo3Error(RuntimeError):
    pass


class LoginError(Typo3Error):
    pass


class AuthChallengeError(LoginError):
    """MFA or CAPTCHA detected: unattended login is not possible. Never bypassed."""

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind


class FormError(Typo3Error):
    pass


class ReconciliationRequiredError(Typo3Error):
    """A previous run may have created the record, and its existence cannot be determined."""


# ============================================================================ diagnostics


class Diagnostics:
    """Screenshots and HTML dumps on failure. Files contain authenticated backend content:
    they are written to a git-ignored directory and must only be shared privately."""

    def __init__(self, directory: Path, run_id: str, screenshots: bool = True, html_dumps: bool = True):
        self.directory = directory / run_id
        self.screenshots = screenshots
        self.html_dumps = html_dumps
        self.console: list[str] = []
        self.files: list[Path] = []

    def capture(self, page: Page | None, label: str) -> list[Path]:
        if page is None:
            return []
        self.directory.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%H%M%S")
        safe = re.sub(r"[^a-zA-Z0-9_-]+", "-", label)[:60]
        written = []
        try:
            if self.screenshots:
                path = self.directory / f"{stamp}-{safe}.png"
                page.screenshot(path=str(path), full_page=True, timeout=15000)
                written.append(path)
            if self.html_dumps:
                for i, frame in enumerate(page.frames):
                    try:
                        content = frame.content()
                    except PlaywrightError:
                        continue
                    path = self.directory / f"{stamp}-{safe}-frame{i}.html"
                    path.write_text(f"<!-- url: {redact(frame.url)} -->\n" + redact(content), encoding="utf-8")
                    written.append(path)
            if self.console:
                path = self.directory / f"{stamp}-{safe}-console.log"
                path.write_text("\n".join(redact(c) for c in self.console[-500:]), encoding="utf-8")
                written.append(path)
        except Exception as exc:  # diagnostics must never mask the original error
            log.warning("could not capture diagnostics: %s", exc)
        self.files.extend(written)
        if written:
            log.info("diagnostics written to %s", self.directory)
        return written


# ============================================================================ field specs


@dataclass
class FieldSpec:
    key: str
    name: str
    kind: str = "text"  # text | textarea | rte | datetime | checkbox | visibility | categories | fal
    required: bool = False
    locator: Any = None
    fill_if_empty: bool = False
    format: str = ""

    @classmethod
    def parse(cls, key: str, data: dict[str, Any]) -> FieldSpec:
        return cls(
            key=key,
            name=data["name"],
            kind=data.get("kind", "text"),
            required=bool(data.get("required")),
            locator=data.get("locator"),
            fill_if_empty=bool(data.get("fill_if_empty")),
            format=data.get("format", ""),
        )


def parse_fields(data: dict[str, Any]) -> dict[str, FieldSpec]:
    return {k: FieldSpec.parse(k, v) for k, v in (data or {}).items()}


# ============================================================================ FormEngine


_RECORD_KEY_JS = """
(table) => {
  const prefix = `data[${table}][`;
  const els = document.querySelectorAll('[name], [data-formengine-input-name]');
  for (const el of els) {
    const n = el.getAttribute('data-formengine-input-name') || el.getAttribute('name') || '';
    if (n.startsWith(prefix)) {
      const m = n.substring(prefix.length).match(/^([^\\]]+)\\]/);
      if (m) return m[1];
    }
  }
  return null;
}
"""

_RTE_JS = """
([selector, html]) => {
  const ta = document.querySelector(selector);
  if (!ta) return 'missing';
  const scope = ta.closest('typo3-rte-ckeditor-ckeditor5') || ta.closest('.formengine-field-item')
             || ta.closest('.form-group') || ta.parentElement;
  const editable = scope ? scope.querySelector('.ck-editor__editable') : null;
  const sync = (data) => {
    ta.value = data;
    ta.dispatchEvent(new Event('input', {bubbles: true}));
    ta.dispatchEvent(new Event('change', {bubbles: true}));
  };
  if (editable && editable.ckeditorInstance) {
    const ed = editable.ckeditorInstance;
    ed.setData(html);
    if (typeof ed.updateSourceElement === 'function') { try { ed.updateSourceElement(); } catch (e) {} }
    sync(ed.getData());
    return 'ckeditor5';
  }
  if (window.CKEDITOR && window.CKEDITOR.instances) {
    for (const k in window.CKEDITOR.instances) {
      const inst = window.CKEDITOR.instances[k];
      if ((inst.element && inst.element.$ === ta) || inst.name === ta.id) {
        inst.setData(html);
        inst.updateElement();
        sync(inst.getData());
        return 'ckeditor4';
      }
    }
  }
  return ta.offsetParent !== null ? 'plain' : 'unknown';
}
"""

_TAB_PANE_JS = """
(el) => {
  const pane = el.closest('.tab-pane');
  if (!pane || pane.classList.contains('active') || pane.classList.contains('show')) return null;
  return pane.id || null;
}
"""


class FormEngine:
    """A TYPO3 record edit form ("FormEngine") for one record of ``table``."""

    def __init__(self, browser: Typo3Browser, frame: Frame, table: str):
        self.browser = browser
        self.frame = frame
        self.table = table
        self.key = self._read_key()
        if not self.key:
            raise FormError(f"edit form for {table} not found")

    # -------------------------------------------------------------------- basics
    @property
    def timeout(self) -> int:
        return self.browser.settings.typo3.browser.action_timeout_ms

    def _read_key(self) -> str | None:
        try:
            return self.frame.evaluate(_RECORD_KEY_JS, self.table)
        except PlaywrightError:
            return None

    @property
    def uid(self) -> int | None:
        return int(self.key) if self.key and self.key.isdigit() else None

    @property
    def is_new(self) -> bool:
        return self.uid is None

    def name(self, column: str, table: str | None = None, key: str | None = None) -> str:
        return f"data[{table or self.table}][{key or self.key}][{column}]"

    def _candidates(self, spec: FieldSpec, table: str | None = None, key: str | None = None) -> list[Any]:
        if spec.locator:
            return L.as_list(spec.locator)
        n = self.name(spec.name, table, key)
        if spec.kind in {"textarea", "rte"}:
            return [f'textarea[name="{n}"]', f'textarea[data-formengine-input-name="{n}"]']
        if spec.kind in {"checkbox", "visibility"}:
            return [f'input[type="checkbox"][data-formengine-input-name="{n}"]', f'input[type="checkbox"][name="{n}"]']
        return [
            f'input[data-formengine-input-name="{n}"]:not([type="hidden"])',
            f'input[name="{n}"]:not([type="hidden"])',
            f'textarea[name="{n}"]',
        ]

    def locate(
        self, spec: FieldSpec, *, visible: bool = False, table: str | None = None, key: str | None = None
    ) -> Locator | None:
        return L.try_find(self.frame, self._candidates(spec, table, key), visible=visible)

    def has_field(self, spec: FieldSpec) -> bool:
        if spec.kind == "fal":
            return self._inline_container(spec) is not None
        if spec.kind == "categories":
            return self.frame.locator(f'[name="{self.name(spec.name)}"]').count() > 0
        return self.locate(spec) is not None

    def reveal(self, loc: Locator) -> None:
        """Activate the tab that contains ``loc`` (FormEngine renders all tabs, hides inactive ones)."""
        try:
            pane_id = loc.evaluate(_TAB_PANE_JS)
        except PlaywrightError:
            pane_id = None
        if pane_id:
            trigger = L.try_find(self.frame, [f'[href="#{pane_id}"]', f'[data-bs-target="#{pane_id}"]'], visible=True)
            if trigger is None:
                raise FormError(f"cannot find the tab that shows {pane_id}")
            trigger.click()
            self.frame.locator(f"#{pane_id}").wait_for(state="visible", timeout=self.timeout)
        try:
            # Hidden source elements (e.g. the RTE textarea) cannot be scrolled to; skip them.
            if loc.is_visible():
                loc.scroll_into_view_if_needed(timeout=self.timeout)
        except PlaywrightError:
            pass

    def read_value(self, column: str, table: str | None = None, key: str | None = None) -> str | None:
        n = self.name(column, table, key)
        for sel in (f'input[type="hidden"][name="{n}"]', f'[name="{n}"]', f'[data-formengine-input-name="{n}"]'):
            loc = self.frame.locator(sel)
            try:
                if loc.count():
                    return loc.first.input_value()
            except PlaywrightError:
                continue
        return None

    def _require(self, spec: FieldSpec, loc: Locator | None) -> Locator | None:
        if loc is None and spec.required:
            raise FormError(f"required field '{spec.key}' ({spec.name}) not found in the {self.table} form")
        if loc is None:
            log.info("optional field '%s' (%s) not present; skipped", spec.key, spec.name)
        return loc

    # -------------------------------------------------------------------- setters
    def set_text(self, spec: FieldSpec, value: str, *, table: str | None = None, key: str | None = None) -> bool:
        loc = self._require(spec, self.locate(spec, table=table, key=key))
        if loc is None:
            return False
        self._ensure_editable(loc)
        self.reveal(loc)
        loc.fill(value, timeout=self.timeout)
        loc.dispatch_event("change")
        loc.evaluate("el => el.blur()")
        stored = self.read_value(spec.name, table, key)
        if stored is not None and stored.strip() != value.strip():
            message = f"field '{spec.key}' did not accept the value (stored {len(stored)} chars)"
            if spec.required:
                raise FormError(message)
            log.warning(message)
        return True

    def _ensure_editable(self, loc: Locator) -> None:
        """Fields with an inherited default (e.g. image metadata) carry an 'override' checkbox."""
        try:
            if loc.is_editable():
                return
            checkbox = loc.locator(
                "xpath=ancestor::*[contains(@class,'formengine-field-item') or contains(@class,'form-group')][1]"
                "//input[@type='checkbox']"
            )
            if checkbox.count() and not checkbox.first.is_checked():
                checkbox.first.check(timeout=self.timeout)
        except PlaywrightError:
            pass

    def set_rte(self, spec: FieldSpec, html: str, mode: str = "api") -> bool:
        loc = self._require(spec, self.locate(spec))
        if loc is None:
            return False
        self.reveal(loc)
        selector = f'textarea[name="{self.name(spec.name)}"]'
        if mode == "api":
            used = self.frame.evaluate(_RTE_JS, [selector, html])
            if used == "plain":
                loc.fill(html)
                loc.dispatch_event("change")
            elif used in {"missing", "unknown"}:
                raise FormError(f"rich-text editor for '{spec.key}' could not be addressed ({used})")
            log.info("RTE '%s' filled via %s", spec.key, used)
        else:
            editable = L.find(
                self.frame,
                [
                    f"xpath=//textarea[@name='{self.name(spec.name)}']/ancestor::*[contains(@class,'formengine-field-item')"
                    " or contains(@class,'form-group')][1]//*[@contenteditable='true']"
                ],
                timeout_ms=self.timeout,
            )
            editable.click()
            editable.press("Control+A")
            editable.press("Delete")
            paragraphs = [p for p in html_to_text(html).split("\n") if p.strip()]
            for i, paragraph in enumerate(paragraphs):
                editable.type(paragraph)
                if i < len(paragraphs) - 1:
                    editable.press("Enter")
        stored = normalise(html_to_text(self.read_value(spec.name) or ""))
        probe = normalise(html_to_text(html))[:120]
        if probe and probe[:60] not in stored:
            raise FormError(f"rich-text field '{spec.key}' does not contain the expected text after filling")
        return True

    def set_datetime_if_empty(self, spec: FieldSpec, timezone: str) -> None:
        loc = self.locate(spec)
        if loc is None:
            return
        current = (self.read_value(spec.name) or "").strip()
        if current and current != "0":
            return
        value = datetime.now(ZoneInfo(timezone)).strftime(spec.format or "%H:%M %d-%m-%Y")
        self.reveal(loc)
        loc.fill(value)
        loc.dispatch_event("change")
        loc.evaluate("el => el.blur()")

    def set_checkbox(self, spec: FieldSpec, checked: bool, *, table: str | None = None, key: str | None = None) -> bool:
        loc = self.locate(spec, table=table, key=key)
        if loc is None:
            return False
        self.reveal(loc)
        if loc.is_checked() != checked:
            loc.click(timeout=self.timeout)
        return True

    def set_visibility(self, spec: FieldSpec, visible: bool) -> None:
        """Uses the stored value of the ``hidden`` column, not the checkbox state, because
        TYPO3 often displays this checkbox inverted ("Enabled"/"Visible")."""
        loc = self.locate(spec)
        if loc is None:
            raise FormError(f"visibility field '{spec.name}' not found - cannot publish/unpublish")
        wanted = "0" if visible else "1"
        for _ in range(2):
            if (self.read_value(spec.name) or "0").strip() == wanted:
                return
            self.reveal(loc)
            loc.click(timeout=self.timeout)
            self.frame.page.wait_for_timeout(150)
        if (self.read_value(spec.name) or "").strip() != wanted:
            raise FormError("could not change the record visibility")

    def set_categories(self, spec: FieldSpec, names: list[str]) -> list[str]:
        """Select categories by label. Returns the names that could not be found."""
        missing = []
        hidden_name = self.name(spec.name)
        available = L.try_find(self.frame, [f'select[data-relatedfieldname="{hidden_name}"]'], visible=False)
        for name in names:
            before = self.read_value(spec.name) or ""
            if available is not None:
                self.reveal(available)
                labels = [o.strip() for o in available.locator("option").all_inner_texts()]
                match = next((lbl for lbl in labels if lbl.casefold() == name.casefold()), None)
                if match is None:
                    missing.append(name)
                    continue
                available.select_option(label=match)
                available.dispatch_event("change")
            else:
                container = self.frame.locator(
                    f"xpath=//*[@name='{hidden_name}']/ancestor::*[contains(@class,'formengine-field-item') "
                    "or contains(@class,'form-group')][1]"
                )
                node = L.try_find(
                    container,
                    [{"role": "treeitem", "name": name, "exact": True}, {"text": name, "exact": True}],
                    visible=False,
                )
                if node is None:
                    missing.append(name)
                    continue
                self.reveal(node)
                node.click()
            self.frame.page.wait_for_timeout(200)
            if (self.read_value(spec.name) or "") == before:
                log.warning("category '%s' click did not change the selection (already selected?)", name)
        if missing:
            log.warning("categories not found in TYPO3: %s", ", ".join(missing))
        return missing

    # -------------------------------------------------------------------- media (FAL)
    def _inline_container(self, spec: FieldSpec) -> Locator | None:
        object_id = f"data-{self.key}-{self.table}-{spec.name}"
        return L.try_find(
            self.frame,
            [
                f'[id="{object_id}"]',
                f'[data-object-group="{object_id}"]',
                f"xpath=//*[@name='{self.name(spec.name)}']/ancestor::*[contains(@class,'formengine-field-item') "
                "or contains(@class,'form-group')][1]",
            ],
            visible=False,
        )

    def reference_keys(self, ref_table: str = "sys_file_reference") -> list[str]:
        return self.frame.evaluate(
            """(table) => {
                const prefix = `data[${table}][`;
                const keys = [];
                document.querySelectorAll('[name], [data-formengine-input-name]').forEach(el => {
                  const n = el.getAttribute('data-formengine-input-name') || el.getAttribute('name') || '';
                  if (n.startsWith(prefix)) {
                    const k = n.substring(prefix.length).split(']')[0];
                    if (!keys.includes(k)) keys.push(k);
                  }
                });
                return keys;
            }""",
            ref_table,
        )

    def upload_file(self, spec: FieldSpec, path: Path, ref_table: str = "sys_file_reference") -> str:
        """Upload ``path`` with the field's "Select & upload files" button; returns the new reference key."""
        container = self._inline_container(spec)
        if container is None:
            raise FormError(f"media field '{spec.name}' not found")
        self.reveal(container)
        before = set(self.reference_keys(ref_table))
        button = L.try_find(container, self.browser.selectors["buttons"]["upload"], visible=True)
        page = self.frame.page
        upload_timeout = self.browser.settings.typo3.browser.upload_timeout_ms
        if button is not None:
            with page.expect_file_chooser(timeout=self.timeout) as chooser:
                button.click()
            chooser.value.set_files(str(path))
        else:
            file_input = L.try_find(container, ['input[type="file"]'], visible=False) or L.try_find(
                self.frame, ['input[type="file"]'], visible=False
            )
            if file_input is None:
                raise FormError("no upload control found for the media field")
            file_input.set_input_files(str(path))
        deadline = time.monotonic() + upload_timeout / 1000
        while time.monotonic() < deadline:
            self.browser.handle_upload_conflict()
            new_keys = [k for k in self.reference_keys(ref_table) if k not in before]
            if new_keys:
                return new_keys[-1]
            self._expand_last_inline(container)
            page.wait_for_timeout(500)
        raise FormError("uploaded file did not appear as a media reference in the form")

    def _expand_last_inline(self, container: Locator) -> None:
        toggles = container.locator("[data-action='toggle'], .form-irre-header-button, .t3js-toggle-inline-record")
        try:
            if toggles.count():
                last = toggles.nth(toggles.count() - 1)
                if last.get_attribute("aria-expanded") == "false":
                    last.click()
        except PlaywrightError:
            pass

    # -------------------------------------------------------------------- save
    def validation_errors(self) -> list[str]:
        sel = self.browser.selectors.get("notifications", {}).get("validation_error", ".has-error")
        try:
            return [t.strip()[:200] for t in self.frame.locator(sel).all_inner_texts() if t.strip()][:10]
        except PlaywrightError:
            return []

    def save(self) -> FormEngine:
        """Click Save and wait for the reloaded form. Returns the new FormEngine (uid known)."""
        page = self.frame.page
        save = L.find(
            self.frame, self.browser.selectors["buttons"]["save"], timeout_ms=self.timeout, what="Save button"
        )
        self.frame.evaluate("() => { window.__t3botBeforeSave = true; }")
        save.click()
        deadline = time.monotonic() + self.browser.settings.typo3.browser.navigation_timeout_ms / 1000
        while time.monotonic() < deadline:
            page.wait_for_timeout(300)
            frame = self.browser.find_form_frame(self.table)
            if frame is None:
                continue
            try:
                reloaded = frame.evaluate("() => !window.__t3botBeforeSave")
            except PlaywrightError:
                continue
            if not reloaded:
                errors = self.validation_errors()
                if errors:
                    raise FormError("TYPO3 rejected the form: " + " | ".join(errors))
                continue
            try:
                frame.wait_for_load_state("domcontentloaded", timeout=self.timeout)
            except PlaywrightTimeout:
                pass
            form = FormEngine(self.browser, frame, self.table)
            errors = self.browser.backend_errors()
            if errors:
                raise FormError("TYPO3 reported an error after saving: " + " | ".join(errors))
            if form.uid is None:
                raise FormError("record was not saved (no uid after save)")
            log.info("saved %s uid=%s", self.table, form.uid)
            return form
        raise FormError("timeout waiting for TYPO3 to save the record")


# ============================================================================ browser


class Typo3Browser:
    def __init__(self, settings: Settings, selectors: dict[str, Any], secrets: Secrets, diagnostics: Diagnostics):
        self.settings = settings
        self.selectors = selectors
        self.secrets = secrets
        self.diagnostics = diagnostics
        self.backend_url = settings.typo3.backend_url.rstrip("/")
        if not self.backend_url:
            raise Typo3Error("TYPO3_BACKEND_URL is not configured")
        self._pw: Playwright | None = None
        self.browser: Browser | None = None
        self.context: BrowserContext | None = None
        self.page: Page | None = None
        self.logged_in = False

    # -------------------------------------------------------------------- lifecycle
    def __enter__(self) -> Typo3Browser:
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
        ctx: dict[str, Any] = {
            "viewport": {"width": b.viewport_width, "height": b.viewport_height},
            "locale": b.locale,
            "accept_downloads": False,
        }
        storage = self._storage_state()
        if storage:
            ctx["storage_state"] = storage
        self.context = self.browser.new_context(**ctx)
        self.context.set_default_timeout(b.action_timeout_ms)
        self.context.set_default_navigation_timeout(b.navigation_timeout_ms)
        self.page = self.context.new_page()
        self.page.on("console", lambda m: self.diagnostics.console.append(f"[{m.type}] {m.text}"))
        self.page.on("pageerror", lambda e: self.diagnostics.console.append(f"[pageerror] {e}"))
        self.page.on("dialog", self._on_dialog)

    def _on_dialog(self, dialog) -> None:
        log.info("browser dialog (%s): %s - accepting", dialog.type, dialog.message[:200])
        try:
            dialog.accept()
        except PlaywrightError:
            pass

    def _storage_state(self) -> dict[str, Any] | None:
        """Optional pre-authenticated session (for MFA sites), created by `auth-export`."""
        raw = self.secrets.get(self.settings.typo3.storage_state_env)
        if not raw:
            return None
        import base64

        try:
            return json.loads(base64.b64decode(raw).decode("utf-8"))
        except Exception as exc:
            raise LoginError(f"{self.settings.typo3.storage_state_env} is not a valid base64 storage state") from exc

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

    @property
    def timeout(self) -> int:
        return self.settings.typo3.browser.action_timeout_ms

    def url(self, route: str, query: str = "") -> str:
        return f"{self.backend_url}/{route.lstrip('/')}" + (f"?{query}" if query else "")

    # -------------------------------------------------------------------- login
    def _first_present(self, groups: dict[str, list[str]]) -> str | None:
        assert self.page is not None
        for name, sels in groups.items():
            for frame in self.page.frames:
                for sel in sels:
                    try:
                        if frame.locator(sel).count():
                            return name
                    except PlaywrightError:
                        continue
        return None

    def auth_state(self) -> str | None:
        sel = self.selectors["login"]
        return self._first_present(
            {
                "captcha": L.as_list(sel.get("captcha")),
                "mfa": L.as_list(sel.get("mfa")),
                "success": L.as_list(sel.get("success")),
                "error": L.as_list(sel.get("error")),
            }
        )

    def login(self) -> str:
        """Log in through the normal login form. Returns 'password' or 'session'."""
        assert self.page is not None
        page = self.page
        sel = self.selectors["login"]
        page.goto(self.url(""), wait_until="domcontentloaded")
        try:  # CAPTCHA widgets are injected by scripts: let the page finish loading first
            page.wait_for_load_state("load", timeout=self.timeout)
        except PlaywrightTimeout:
            pass
        state = self.auth_state()
        if state is None and L.try_find(page, sel["username"]) is None:
            state = self._wait_auth_state(self.timeout, accept={"success", "captcha", "mfa"})
        if state == "success":
            self.logged_in = True
            log.info("TYPO3 session is already authenticated")
            return "session"
        self._raise_for_challenge(state)

        username = self.secrets.require("TYPO3_USERNAME")
        password = self.secrets.require("TYPO3_PASSWORD")
        L.find(page, sel["username"], timeout_ms=self.timeout, what="username field").fill(username)
        L.find(page, sel["password"], timeout_ms=self.timeout, what="password field").fill(password)
        L.find(page, sel["submit"], timeout_ms=self.timeout, what="login button").click()
        state = self._wait_auth_state(self.settings.typo3.browser.navigation_timeout_ms)
        self._raise_for_challenge(state)
        if state == "success":
            self.logged_in = True
            log.info("TYPO3 login succeeded")
            return "password"
        message = "TYPO3 login failed"
        if state == "error":
            texts = []
            for s in L.as_list(sel.get("error")):
                try:
                    texts += [t.strip() for t in page.locator(s).all_inner_texts() if t.strip()]
                except PlaywrightError:
                    pass
            message += ": " + (redact(" ".join(texts))[:300] or "credentials rejected")
        else:
            message += ": no backend detected after submitting the login form"
        self.diagnostics.capture(page, "login-failed")
        raise LoginError(message)

    def _wait_auth_state(self, timeout_ms: int, accept: set[str] | None = None) -> str | None:
        assert self.page is not None
        deadline = time.monotonic() + timeout_ms / 1000
        state = None
        while time.monotonic() < deadline:
            state = self.auth_state()
            if state and (accept is None or state in accept):
                return state
            self.page.wait_for_timeout(250)
        return state if (accept is None or state in (accept or set())) else None

    def _raise_for_challenge(self, state: str | None) -> None:
        if state == "mfa":
            self.diagnostics.capture(self.page, "mfa-required")
            raise AuthChallengeError(
                "mfa",
                "TYPO3 requires multi-factor authentication. Unattended login is not "
                "possible; see README 'Sites with MFA or CAPTCHA'.",
            )
        if state == "captcha":
            self.diagnostics.capture(self.page, "captcha-detected")
            raise AuthChallengeError(
                "captcha",
                "A CAPTCHA protects the TYPO3 login. The bot will not attempt to "
                "solve or bypass it; see README 'Sites with MFA or CAPTCHA'.",
            )

    def ensure_logged_in(self) -> None:
        if not self.logged_in:
            self.login()

    # -------------------------------------------------------------------- frames & navigation
    def module_frame(self) -> Frame:
        assert self.page is not None
        for spec in L.as_list(self.selectors["frames"]["module"]):
            loc = self.page.locator(spec if isinstance(spec, str) else spec.get("css", ""))
            try:
                if loc.count():
                    handle = loc.first.element_handle()
                    frame = handle.content_frame() if handle else None
                    if frame:
                        return frame
            except PlaywrightError:
                continue
        return self.page.main_frame

    def find_form_frame(self, table: str) -> Frame | None:
        assert self.page is not None
        frames = [self.module_frame(), *self.page.frames]
        for frame in frames:
            try:
                if frame.locator(f'[name^="data[{table}]["], [data-formengine-input-name^="data[{table}]["]').count():
                    return frame
            except PlaywrightError:
                continue
        return None

    def backend_errors(self) -> list[str]:
        assert self.page is not None
        texts: list[str] = []
        for frame in self.page.frames:
            for sel in L.as_list(self.selectors.get("notifications", {}).get("error", [])):
                if sel.startswith(".has-error"):
                    continue
                try:
                    texts += [t.strip()[:200] for t in frame.locator(sel).all_inner_texts() if t.strip()]
                except PlaywrightError:
                    continue
        return texts[:5]

    def handle_upload_conflict(self) -> None:
        """TYPO3 asks what to do when a file name already exists: always keep both (rename)."""
        assert self.page is not None
        for frame in self.page.frames:
            try:
                modal = frame.locator(".modal.show, typo3-backend-modal .modal")
                if not modal.count() or not modal.first.locator("select").count():
                    continue
                for select in modal.first.locator("select").all():
                    try:
                        select.select_option(value="rename")
                    except PlaywrightError:
                        pass
                btn = L.try_find(
                    modal.first, [{"css": "button.btn-primary"}, {"role": "button", "name": "re:(continue|weiter|ok)"}]
                )
                if btn:
                    btn.click()
                    log.info("resolved file-name conflict by renaming")
            except PlaywrightError:
                continue

    def run_ui_steps(self, steps: list[dict[str, Any]], variables: dict[str, Any]) -> None:
        assert self.page is not None
        for step in steps:
            scope: Page | Frame = self.page if step.get("frame", "module") == "top" else self.module_frame()
            action = step.get("action", "click")
            if action == "goto":
                self.page.goto(L.format_spec(step["url"], variables))
                continue
            target = L.find(
                scope, step["target"], timeout_ms=self.timeout, variables=variables, what=f"UI step {action}"
            )
            if action == "click":
                target.click()
            elif action == "fill":
                target.fill(str(L.format_spec(step.get("value", ""), variables)))
            elif action == "press":
                target.press(step.get("key", "Enter"))
            elif action == "wait_for":
                target.wait_for(state="visible")
            else:
                raise Typo3Error(f"unknown UI step action {action!r}")
            self.page.wait_for_timeout(int(step.get("pause_ms", 300)))

    def open_form(
        self,
        table: str,
        *,
        pid: int | None = None,
        uid: int | None = None,
        extra: str = "",
        navigation: dict[str, Any] | None = None,
        variables: dict[str, Any] | None = None,
    ) -> FormEngine:
        """Open the regular edit form for a new (pid) or existing (uid) record."""
        assert self.page is not None
        self.ensure_logged_in()
        if (pid is None) == (uid is None):
            raise ValueError("pass exactly one of pid / uid")
        param = f"edit[{table}][{uid}]=edit" if uid is not None else f"edit[{table}][{pid}]=new"
        query = quote(param, safe="=&") + (f"&{extra}" if extra else "")
        navigation = navigation or {}
        strategy = navigation.get("strategy", "direct_url")
        if strategy == "direct_url" or uid is not None:
            self.page.goto(self.url("record/edit", query), wait_until="domcontentloaded")
            frame = self._wait_form_frame(table)
            if frame is not None:
                return FormEngine(self, frame, table)
            log.warning("direct edit URL did not open a form for %s", table)
        steps = navigation.get("ui_steps") or []
        if uid is None and steps:
            self.page.goto(self.url(""), wait_until="domcontentloaded")
            self.run_ui_steps(steps, variables or {})
            frame = self._wait_form_frame(table)
            if frame is not None:
                return FormEngine(self, frame, table)
        self.diagnostics.capture(self.page, f"form-not-found-{table}")
        raise FormError(f"could not open the edit form for {table} (strategy {strategy})")

    def _wait_form_frame(self, table: str) -> Frame | None:
        assert self.page is not None
        deadline = time.monotonic() + self.settings.typo3.browser.navigation_timeout_ms / 1000
        while time.monotonic() < deadline:
            frame = self.find_form_frame(table)
            if frame is not None:
                return frame
            state = self.auth_state()
            if state in {"mfa", "captcha"}:
                self._raise_for_challenge(state)
            self.page.wait_for_timeout(300)
        return None

    def find_record_uid(self, table: str, pid: int, title: str, title_column: str = "title") -> int | None:
        """Search the List module (the same search an editor would use) for a record by title.

        Returns the uid, None if the search ran and found nothing, and raises
        ReconciliationRequiredError if the search itself could not be performed.
        """
        assert self.page is not None
        self.ensure_logged_in()
        query = urlencode({"id": pid, "table": table, "search_field": title, "search_levels": 0})
        self.page.goto(self.url("module/web/list", query), wait_until="domcontentloaded")
        deadline = time.monotonic() + self.settings.typo3.browser.navigation_timeout_ms / 1000
        while time.monotonic() < deadline:
            for frame in [self.module_frame(), *self.page.frames]:
                try:
                    table_el = frame.locator(f'[data-table="{table}"], table[data-table="{table}"]')
                    if not table_el.count() and not frame.locator(".recordlist, .t3js-recordlist").count():
                        continue
                    rows = frame.locator(f'tr[data-table="{table}"][data-uid], tr[data-uid]')
                    for i in range(rows.count()):
                        row = rows.nth(i)
                        if title_similarity_ok(row.inner_text(), title):
                            return int(row.get_attribute("data-uid") or 0) or None
                    return None
                except PlaywrightError:
                    continue
            self.page.wait_for_timeout(300)
        self.diagnostics.capture(self.page, "list-search-failed")
        raise ReconciliationRequiredError(
            f"could not search the List module for '{title}'; check TYPO3 manually for a record created by a "
            "previous run before retrying"
        )

    def view_url(self, form: FormEngine) -> str:
        btn = L.try_find(form.frame, self.selectors["buttons"].get("view"), visible=False)
        if btn is None:
            return ""
        for attr in ("href", "data-url", "data-href"):
            value = btn.get_attribute(attr)
            if value and value.startswith("http"):
                return value
        return ""

    def delete_record(self, table: str, uid: int) -> None:
        """Delete through the edit form's Delete button (used by live-test cleanup only)."""
        form = self.open_form(table, uid=uid)
        L.find(form.frame, self.selectors["buttons"]["delete"], timeout_ms=self.timeout, what="Delete button").click()
        assert self.page is not None
        for frame in [self.page.main_frame, form.frame]:
            btn = L.try_find(frame, self.selectors["buttons"]["modal_confirm"])
            if btn:
                btn.click()
                break
        self.page.wait_for_timeout(1500)


def title_similarity_ok(row_text: str, title: str) -> bool:
    return normalise(title) in normalise(row_text)


# ============================================================================ publishing profiles

Checkpoint = Callable[[PublicationInfo], None]


@dataclass
class PublishOptions:
    publish: bool = True  # False: save hidden (review in backend) without making it public
    categories: list[str] = field(default_factory=list)
    attribution_target: str = "caption"
    attribution_enabled: bool = True


def attribution_html(image: ImageInfo) -> str:
    e = html.escape
    return (
        f'<p><em>{e(image.attribution)} (<a href="{e(image.page_url, quote=True)}">Pixabay</a>, '
        f'<a href="{e(image.license_url, quote=True)}">{e(image.license_name)}</a>)</em></p>'
    )


# Default mapping of image texts to sys_file_reference columns. Placeholders:
# {caption} (Bildtext), {description} (Bildbeschreibung / alt text), {copyright} ("© user / Pixabay").
DEFAULT_REFERENCE_FIELDS = {"alternative": "{description}", "description": "{caption} {copyright}"}


def reference_values(ref_cfg: dict[str, Any], image: ImageInfo, options: PublishOptions) -> dict[str, str]:
    """Column → value for the image reference, from ``image_reference.fields`` in the selector file."""
    in_caption = options.attribution_enabled and options.attribution_target in {"caption", "both"}
    values = {
        "caption": image.caption,
        "description": image.alt_text,
        "copyright": image.attribution if in_caption else "",
    }
    mapping = ref_cfg.get("fields") or DEFAULT_REFERENCE_FIELDS
    result = {}
    for column, template in mapping.items():
        value = " ".join(str(template).format(**values).split())
        if value:
            result[column] = value
    return result


class NewsPublisher:
    """EXT:news records (tx_news_domain_model_news)."""

    profile = "news"

    def __init__(self, browser: Typo3Browser):
        self.browser = browser
        self.cfg = browser.selectors["news"]
        self.table = self.cfg.get("table", "tx_news_domain_model_news")
        self.fields = parse_fields(self.cfg.get("fields", {}))
        self.pid = int(self.cfg.get("storage_pid") or 0)
        if self.pid <= 0:
            raise Typo3Error("news.storage_pid is not configured in typo3_selectors.yaml")

    def _variables(self) -> dict[str, Any]:
        return {"storage_page_title": self.cfg.get("storage_page_title", ""), "storage_pid": self.pid}

    def open_new(self) -> FormEngine:
        return self.browser.open_form(
            self.table, pid=self.pid, navigation=self.cfg.get("navigation"), variables=self._variables()
        )

    def open_existing(self, uid: int) -> FormEngine:
        return self.browser.open_form(self.table, uid=uid)

    def _body(self, article: Article, options: PublishOptions) -> str:
        assert article.content is not None
        body = article.content.body_html
        if article.image and options.attribution_enabled and options.attribution_target in {"body", "both"}:
            body += attribution_html(article.image)
        return body

    def fill_text(self, form: FormEngine, article: Article, options: PublishOptions) -> None:
        c = article.content
        assert c is not None
        f = self.fields
        form.set_text(f["title"], c.title)
        if "teaser" in f:
            form.set_text(f["teaser"], c.teaser)
        form.set_rte(f["bodytext"], self._body(article, options), self.cfg.get("rte_mode", "api"))
        for key, value in (
            ("slug", c.slug),
            ("meta_description", c.meta_description),
            ("keywords", ", ".join(c.keywords)),
            ("seo_title", c.seo_title),
        ):
            if key in f and value:
                if key == "slug" and not form.is_new and form.read_value(f[key].name):
                    continue  # never change the URL of an existing record
                form.set_text(f[key], value)
        if "datetime" in f and f["datetime"].fill_if_empty:
            form.set_datetime_if_empty(f["datetime"], self.browser.settings.publishing.timezone)
        if "categories" in f and options.categories and form.has_field(f["categories"]):
            form.set_categories(f["categories"], options.categories)

    def attach_image(self, form: FormEngine, image: ImageInfo, options: PublishOptions) -> None:
        ref = self.cfg.get("image_reference", {})
        ref_table = ref.get("table", "sys_file_reference")
        key = form.upload_file(self.fields["media"], Path(image.local_path), ref_table)
        for column, value in reference_values(ref, image, options).items():
            form.set_text(FieldSpec(key=column, name=column), value, table=ref_table, key=key)
        for column, checked in (ref.get("checkboxes") or {}).items():
            form.set_checkbox(
                FieldSpec(key=column, name=column, kind="checkbox"), bool(checked), table=ref_table, key=key
            )

    def image_attached(self, form: FormEngine) -> bool:
        ref_table = self.cfg.get("image_reference", {}).get("table", "sys_file_reference")
        return bool(form.reference_keys(ref_table))

    def publish(self, article: Article, options: PublishOptions, checkpoint: Checkpoint) -> PublicationInfo:
        assert article.content is not None
        pub = article.publication
        pub.typo3_table = self.table
        uid = pub.typo3_uid
        if uid is None and pub.stage == PublicationStage.STARTED:
            # A previous run clicked "save" but did not record the uid. Never create blindly.
            uid = self.browser.find_record_uid(self.table, self.pid, article.content.title)
            if uid:
                log.info("found record uid=%s created by an earlier run; resuming it", uid)
                pub.typo3_uid = uid
                pub.stage = PublicationStage.SAVED
                checkpoint(pub)
        if uid is None:
            form = self.open_new()
            self.fill_text(form, article, options)
            form.set_visibility(self.fields["hidden"], False)  # stay hidden until complete
            pub.stage = PublicationStage.STARTED
            checkpoint(pub)
            form = form.save()
            pub.typo3_uid = form.uid
            pub.stage = PublicationStage.SAVED
            checkpoint(pub)
        else:
            form = self.open_existing(uid)
            self.fill_text(form, article, options)
            form = form.save()

        if article.image and not self.image_attached(form):
            self.attach_image(form, article.image, options)
            form = form.save()
        elif self.fields["media"].required and article.image is None:
            raise Typo3Error("media is required by the selector config but the article has no image")

        if options.publish:
            form.set_visibility(self.fields["hidden"], True)
            form = form.save()
            pub.stage = PublicationStage.PUBLISHED
            pub.published_at = iso()
        if "slug" in self.fields:
            pub.slug = (form.read_value(self.fields["slug"].name) or article.content.slug).strip("/")
        if self.browser.settings.website.public_url.strategy == "backend_view":
            pub.verification["backend_view_url"] = self.browser.view_url(form)
        checkpoint(pub)
        return pub

    def update(self, article: Article, options: PublishOptions, checkpoint: Checkpoint) -> PublicationInfo:
        if not article.publication.typo3_uid:
            raise Typo3Error("cannot update: TYPO3 uid of the article is unknown")
        return self.publish(article, options, checkpoint)

    def preview(self, article: Article, options: PublishOptions) -> list[str]:
        """Dry run: open the form and fill all text fields, but never save or upload."""
        uid = article.publication.typo3_uid
        form = self.open_existing(uid) if uid else self.open_new()
        self.fill_text(form, article, options)
        return [str(p) for p in self.browser.diagnostics.capture(self.browser.page, "dry-run-filled-form")]


class ContentElementPublisher:
    """Classic setup: one page per article with a text/media content element."""

    profile = "content_element"

    def __init__(self, browser: Typo3Browser):
        self.browser = browser
        self.cfg = browser.selectors["content_element"]
        self.page_table = self.cfg.get("page_table", "pages")
        self.content_table = self.cfg.get("content_table", "tt_content")
        self.page_fields = parse_fields(self.cfg.get("page_fields", {}))
        self.content_fields = parse_fields(self.cfg.get("content_fields", {}))
        self.parent_pid = int(self.cfg.get("parent_pid") or 0)
        if self.parent_pid <= 0:
            raise Typo3Error("content_element.parent_pid is not configured in typo3_selectors.yaml")

    def publish(self, article: Article, options: PublishOptions, checkpoint: Checkpoint) -> PublicationInfo:
        c = article.content
        assert c is not None
        pub = article.publication
        pub.typo3_table = self.content_table
        pf, cf = self.page_fields, self.content_fields
        page_uid = pub.typo3_page_uid
        if page_uid is None and pub.stage == PublicationStage.STARTED:
            page_uid = self.browser.find_record_uid(self.page_table, self.parent_pid, c.title)
            if page_uid:
                pub.typo3_page_uid = page_uid
                checkpoint(pub)

        if page_uid is None:
            form = self.browser.open_form(self.page_table, pid=self.parent_pid)
            self._fill_page(form, article, options)
            form.set_visibility(pf["hidden"], False)
            pub.stage = PublicationStage.STARTED
            checkpoint(pub)
            form = form.save()
            pub.typo3_page_uid = page_uid = form.uid
            checkpoint(pub)
        else:
            form = self.browser.open_form(self.page_table, uid=page_uid)
            self._fill_page(form, article, options)
            form.save()

        if pub.typo3_uid is None:
            form = self.browser.open_form(self.content_table, pid=page_uid, extra=self.cfg.get("content_defaults", ""))
        else:
            form = self.browser.open_form(self.content_table, uid=pub.typo3_uid)
        form.set_text(cf["header"], c.title)
        body = c.body_html
        if article.image and options.attribution_enabled and options.attribution_target in {"body", "both"}:
            body += attribution_html(article.image)
        form.set_rte(cf["bodytext"], body, self.cfg.get("rte_mode", "api"))
        form = form.save()
        pub.typo3_uid = form.uid
        pub.stage = PublicationStage.SAVED
        checkpoint(pub)

        ref = self.cfg.get("image_reference", {})
        ref_table = ref.get("table", "sys_file_reference")
        if article.image and not form.reference_keys(ref_table):
            key = form.upload_file(cf["media"], Path(article.image.local_path), ref_table)
            for column, value in reference_values(ref, article.image, options).items():
                form.set_text(FieldSpec(key=column, name=column), value, table=ref_table, key=key)
            form = form.save()
        if "hidden" in cf:
            form.set_visibility(cf["hidden"], True)
            form = form.save()

        page_form = self.browser.open_form(self.page_table, uid=page_uid)
        if options.publish:
            page_form.set_visibility(pf["hidden"], True)
            page_form = page_form.save()
            pub.stage = PublicationStage.PUBLISHED
            pub.published_at = iso()
        if "slug" in pf:
            pub.slug = (page_form.read_value(pf["slug"].name) or c.slug).strip("/")
        checkpoint(pub)
        return pub

    def _fill_page(self, form: FormEngine, article: Article, options: PublishOptions) -> None:
        c = article.content
        assert c is not None
        pf = self.page_fields
        form.set_text(pf["title"], c.title)
        for key, value in (
            ("seo_title", c.seo_title),
            ("meta_description", c.meta_description),
            ("keywords", ", ".join(c.keywords)),
            ("teaser", c.teaser),
        ):
            if key in pf and value:
                form.set_text(pf[key], value)
        if "categories" in pf and options.categories and form.has_field(pf["categories"]):
            form.set_categories(pf["categories"], options.categories)

    def update(self, article: Article, options: PublishOptions, checkpoint: Checkpoint) -> PublicationInfo:
        if not article.publication.typo3_page_uid:
            raise Typo3Error("cannot update: TYPO3 page uid of the article is unknown")
        return self.publish(article, options, checkpoint)

    def preview(self, article: Article, options: PublishOptions) -> list[str]:
        """Dry run: fill the page form without saving (the content form needs a saved page)."""
        uid = article.publication.typo3_page_uid
        form = (
            self.browser.open_form(self.page_table, uid=uid)
            if uid
            else self.browser.open_form(self.page_table, pid=self.parent_pid)
        )
        self._fill_page(form, article, options)
        return [str(p) for p in self.browser.diagnostics.capture(self.browser.page, "dry-run-filled-form")]


def create_publisher(browser: Typo3Browser) -> NewsPublisher | ContentElementPublisher:
    if browser.settings.typo3.profile == "content_element":
        return ContentElementPublisher(browser)
    return NewsPublisher(browser)
