"""Discovery / setup mode.

Opens the real backend, logs in, inspects navigation and the record edit form, and
tests every configured selector against the live markup. Results:

* ``artifacts/discovery/<run>/report.md`` + ``report.json`` + screenshots (private!)
* ``config/typo3_selectors.discovered.yaml``: the selector file with ``verified_fields``
  filled in from what actually matched. Nothing is marked verified unless it matched.

By default nothing is saved in TYPO3. ``--test-save`` additionally creates a *hidden*
test record, saves it (proving the save workflow and uid detection) and deletes it again.
"""

from __future__ import annotations

import copy
import json
import struct
import zlib
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml

from . import locators as L
from .logging_utils import get_logger
from .models import iso
from .typo3_browser import (
    AuthChallengeError,
    FieldSpec,
    FormEngine,
    LoginError,
    Typo3Browser,
    Typo3Error,
    parse_fields,
)

log = get_logger("discovery")

_INVENTORY_JS = """
(table) => {
  const out = [];
  const seen = new Set();
  const labelFor = (el) => {
    const item = el.closest('.formengine-field-item, .form-group, .form-section');
    const lbl = item ? item.querySelector('label, .form-label, .t3js-formengine-label') : null;
    return lbl ? lbl.textContent.trim().replace(/\\s+/g, ' ').slice(0, 80) : '';
  };
  const tabFor = (el) => {
    const pane = el.closest('.tab-pane');
    if (!pane || !pane.id) return '';
    const trig = document.querySelector(`[href="#${pane.id}"], [data-bs-target="#${pane.id}"]`);
    return trig ? trig.textContent.trim().replace(/\\s+/g, ' ') : pane.id;
  };
  const prefix = `data[${table}][`;
  document.querySelectorAll('[data-formengine-input-name], [name]').forEach(el => {
    const n = el.getAttribute('data-formengine-input-name') || el.getAttribute('name') || '';
    if (!n.startsWith(prefix)) return;
    const rest = n.substring(prefix.length).split('][');
    if (rest.length < 2) return;
    const column = rest[1].replace(']', '');
    if (seen.has(column) && el.type === 'hidden') return;
    let kind = el.tagName.toLowerCase() === 'textarea' ? 'textarea' : (el.type || 'text');
    if (kind === 'textarea') {
      const scope = el.closest('typo3-rte-ckeditor-ckeditor5, .formengine-field-item, .form-group');
      if (scope && (scope.querySelector('.ck-editor__editable') || scope.querySelector('.cke'))) kind = 'rte';
    }
    if (el.type === 'hidden') {
      const item = el.closest('.formengine-field-item, .form-group');
      if (item && item.querySelector('.t3js-drag-uploader, [data-object-group]')) kind = 'fal';
      else if (item && item.querySelector('[role="tree"], .svg-tree, typo3-backend-form-selecttree, select[data-relatedfieldname]')) kind = 'categories';
      else return;
    }
    if (kind === 'checkbox') kind = 'checkbox';
    seen.add(column);
    out.push({column, kind, label: labelFor(el), tab: tabFor(el)});
  });
  const tabs = Array.from(document.querySelectorAll('[data-bs-toggle="tab"], .nav-tabs a, [role="tab"]'))
    .map(t => t.textContent.trim().replace(/\\s+/g, ' ')).filter(Boolean);
  const uploads = Array.from(document.querySelectorAll('.t3js-drag-uploader, input[type="file"]')).length;
  return {fields: out, tabs: Array.from(new Set(tabs)), upload_controls: uploads};
}
"""

_MODULE_MENU_JS = """
() => {
  const items = [];
  document.querySelectorAll('[data-modulemenu-identifier], .modulemenu-action, #modulemenu a').forEach(el => {
    const id = el.getAttribute('data-modulemenu-identifier') || el.id || '';
    const text = (el.getAttribute('title') || el.textContent || '').trim().replace(/\\s+/g, ' ');
    if (id || text) items.push({id, text: text.slice(0, 60)});
  });
  return items;
}
"""


@dataclass
class DiscoveryReport:
    backend_url: str
    started_at: str = field(default_factory=iso)
    login: str = "not attempted"
    auth_challenge: str = ""
    backend_generation: str = ""
    module_menu: list[dict[str, str]] = field(default_factory=list)
    news_module_detected: bool = False
    profile: str = ""
    forms: dict[str, Any] = field(default_factory=dict)
    mapping: dict[str, dict[str, Any]] = field(default_factory=dict)
    buttons: dict[str, bool] = field(default_factory=dict)
    buttons_optional: dict[str, bool] = field(default_factory=dict)
    test_save: dict[str, Any] = field(default_factory=dict)
    manual_assistance: list[str] = field(default_factory=list)
    screenshots: list[str] = field(default_factory=list)

    @property
    def all_required_verified(self) -> bool:
        required = [m for m in self.mapping.values() if m.get("required")]
        return bool(required) and all(m["status"] == "verified" for m in required) and all(self.buttons.values())


def tiny_png(width: int = 64, height: int = 64) -> bytes:
    raw = b"".join(b"\x00" + bytes((40, 120, 200)) * width for _ in range(height))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


class Discovery:
    def __init__(self, browser: Typo3Browser, out_dir: Path):
        self.browser = browser
        self.selectors = browser.selectors
        self.profile = browser.settings.typo3.profile
        self.out_dir = out_dir
        self.report = DiscoveryReport(backend_url=browser.backend_url, profile=self.profile)

    def _shot(self, label: str) -> None:
        files = self.browser.diagnostics.capture(self.browser.page, f"discovery-{label}")
        self.report.screenshots += [str(f) for f in files if f.suffix == ".png"]

    def run(self, *, test_save: bool = False, test_upload: bool = False) -> DiscoveryReport:
        r = self.report
        try:
            r.login = self.browser.login()
        except AuthChallengeError as exc:
            r.login = "blocked"
            r.auth_challenge = exc.kind
            r.manual_assistance.append(str(exc))
            self._shot("auth-challenge")
            return r
        except LoginError as exc:
            r.login = "failed"
            r.manual_assistance.append(
                f"Login failed: {exc}. Check TYPO3_USERNAME/TYPO3_PASSWORD and the login selectors."
            )
            return r
        page = self.browser.page
        assert page is not None
        self._shot("backend")
        try:
            r.module_menu = page.evaluate(_MODULE_MENU_JS)
        except Exception:
            r.module_menu = []
        r.backend_generation = (
            "v12+ (web components)" if page.locator("typo3-backend-module-router").count() else "v9-v11 (heuristic)"
        )
        r.news_module_detected = any("news" in (m["id"] + m["text"]).lower() for m in r.module_menu)

        if self.profile == "news":
            self._inspect_news(test_save, test_upload)
        else:
            self._inspect_content_element(test_save)
        return r

    # ------------------------------------------------------------------ profile inspection
    def _inspect_news(self, test_save: bool, test_upload: bool) -> None:
        cfg = self.selectors["news"]
        table = cfg.get("table", "tx_news_domain_model_news")
        pid = int(cfg.get("storage_pid") or 0)
        if pid <= 0:
            self.report.manual_assistance.append(
                "Set news.storage_pid in config/typo3_selectors.yaml to the uid of the sysfolder that stores news "
                "records (hover the folder in the page tree or open it and read 'id=' in the URL)."
            )
            return
        if not self.report.news_module_detected:
            self.report.manual_assistance.append(
                "No news module in the backend menu. If EXT:news is not installed, use typo3.profile: content_element."
            )
        try:
            form = self.browser.open_form(
                table,
                pid=pid,
                navigation=cfg.get("navigation"),
                variables={"storage_page_title": cfg.get("storage_page_title", "")},
            )
        except Typo3Error as exc:
            self.report.manual_assistance.append(f"Could not open a new {table} form: {exc}")
            self._shot("news-form-failed")
            return
        self._inspect_form(form, parse_fields(cfg.get("fields", {})), "news")
        if test_save:
            self._test_save(form, parse_fields(cfg.get("fields", {})), table, test_upload)

    def _inspect_content_element(self, test_save: bool) -> None:
        cfg = self.selectors["content_element"]
        pid = int(cfg.get("parent_pid") or 0)
        if pid <= 0:
            self.report.manual_assistance.append("Set content_element.parent_pid in config/typo3_selectors.yaml.")
            return
        try:
            form = self.browser.open_form(cfg.get("page_table", "pages"), pid=pid)
            self._inspect_form(form, parse_fields(cfg.get("page_fields", {})), "page")
        except Typo3Error as exc:
            self.report.manual_assistance.append(f"Could not open a new page form: {exc}")
        self.report.manual_assistance.append(
            "Content element fields are verified on the first --test-save run or the live integration test, because "
            "the content form needs an existing page."
        )
        if test_save:
            self.report.test_save = {"status": "skipped", "reason": "not supported for content_element discovery"}

    def _inspect_form(self, form: FormEngine, fields: dict[str, FieldSpec], prefix: str) -> None:
        r = self.report
        inventory = form.frame.evaluate(_INVENTORY_JS, form.table)
        r.forms[form.table] = inventory
        self._shot(f"{prefix}-form")
        for key, spec in fields.items():
            entry: dict[str, Any] = {"name": spec.name, "kind": spec.kind, "required": spec.required}
            try:
                present = form.has_field(spec)
            except Exception as exc:
                present = False
                entry["error"] = str(exc)
            if present and spec.kind not in {"fal", "categories"}:
                loc = form.locate(spec)
                try:
                    if loc is not None:
                        form.reveal(loc)
                        entry["visible_after_tab_switch"] = loc.is_visible() or spec.kind == "rte"
                except Exception as exc:
                    entry["error"] = str(exc)
            entry["status"] = "verified" if present and entry.get("visible_after_tab_switch", True) else "missing"
            if entry["status"] == "missing":
                similar = [
                    f["column"] for f in inventory["fields"] if f["kind"] == spec.kind or spec.name in f["column"]
                ][:8]
                entry["candidates"] = similar
                if spec.required:
                    r.manual_assistance.append(
                        f"Required field '{key}' (column '{spec.name}') not found in {form.table}. Candidates: "
                        f"{', '.join(similar) or 'none'} - adjust the 'name' in the selector file."
                    )
            r.mapping[f"{prefix}.{key}"] = entry
        buttons = self.selectors["buttons"]
        r.buttons = {
            "save": L.try_find(form.frame, buttons["save"], visible=True) is not None,
        }
        r.buttons_optional = {
            "close": L.try_find(form.frame, buttons.get("close"), visible=True) is not None,
            "delete": L.try_find(form.frame, buttons.get("delete"), visible=False) is not None,
        }
        if not r.buttons["save"]:
            r.manual_assistance.append("Save button not found: adjust buttons.save in the selector file.")
        if inventory.get("upload_controls", 0) == 0:
            r.manual_assistance.append(
                "No file upload control detected in the form. Check the media field "
                "(news.fields.media.name) and that the editor may upload files."
            )

    def _test_save(self, form: FormEngine, fields: dict[str, FieldSpec], table: str, test_upload: bool) -> None:
        result: dict[str, Any] = {"status": "failed"}
        try:
            form.set_text(fields["title"], f"[typo3-bot discovery test {iso()}] safe to delete")
            if "bodytext" in fields:
                form.set_rte(
                    fields["bodytext"],
                    "<p>Discovery test record. It is hidden and will be deleted.</p>",
                    self.selectors["news"].get("rte_mode", "api"),
                )
            if "datetime" in fields:
                form.set_datetime_if_empty(fields["datetime"], self.browser.settings.publishing.timezone)
            form.set_visibility(fields["hidden"], False)
            saved = form.save()
            result.update(status="saved", uid=saved.uid, hidden=saved.read_value(fields["hidden"].name))
            if test_upload and "media" in fields:
                png = self.out_dir / "typo3-bot-discovery-test.png"
                png.parent.mkdir(parents=True, exist_ok=True)
                png.write_bytes(tiny_png())
                key = saved.upload_file(fields["media"], png)
                saved = saved.save()
                result["upload"] = {"status": "ok", "reference": key}
            self._shot("test-save")
            self.browser.delete_record(table, int(saved.uid or 0))
            result["deleted"] = True
            result["status"] = "ok"
        except Exception as exc:
            result["error"] = str(exc)
            self._shot("test-save-failed")
            self.report.manual_assistance.append(
                f"Test save failed: {exc}. If a hidden test record was created, delete it manually."
            )
        self.report.test_save = result

    # ------------------------------------------------------------------ output
    def write(self, selectors_path: Path, *, overwrite: bool = False) -> tuple[Path, Path]:
        r = self.report
        self.out_dir.mkdir(parents=True, exist_ok=True)
        data = asdict(r)
        (self.out_dir / "report.json").write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        md = self.out_dir / "report.md"
        md.write_text(self.markdown(), encoding="utf-8")

        proposal = copy.deepcopy(self.selectors)
        proposal["verified_fields"] = {
            k: {"status": v["status"], "checked_at": r.started_at} for k, v in r.mapping.items()
        }
        test_ok = r.test_save.get("status") == "ok" if r.test_save else None
        proposal["verified"] = bool(r.all_required_verified and test_ok)
        proposal["verified_at"] = r.started_at if proposal["verified"] else None
        proposal["discovery_note"] = (
            "verified=true requires all required fields + Save button matched AND a successful --test-save. "
            "Publishing visibility and public verification are proven only by a live publish test."
        )
        target = selectors_path if overwrite else selectors_path.with_name(selectors_path.stem + ".discovered.yaml")
        target.write_text(yaml.safe_dump(proposal, sort_keys=False, allow_unicode=True), encoding="utf-8")
        return md, target

    def markdown(self) -> str:
        r = self.report
        lines = [
            "# TYPO3 discovery report",
            "",
            f"- Backend: `{r.backend_url}`",
            f"- Started: {r.started_at}",
            f"- Login: **{r.login}**" + (f" (challenge: {r.auth_challenge})" if r.auth_challenge else ""),
            f"- Backend generation: {r.backend_generation}",
            f"- Profile: `{r.profile}`",
            f"- News module in menu: {r.news_module_detected}",
            "",
            "## Field mapping (tested against the live form)",
            "",
            "| field | column | kind | required | status |",
            "|---|---|---|---|---|",
        ]
        for key, m in r.mapping.items():
            lines.append(f"| {key} | `{m['name']}` | {m['kind']} | {m['required']} | {m['status']} |")
        lines += ["", "## Buttons", ""] + [f"- {k}: {'found' if v else 'NOT FOUND'}" for k, v in r.buttons.items()]
        if r.test_save:
            lines += ["", "## Test save", "", f"```\n{json.dumps(r.test_save, indent=2)}\n```"]
        for table, inv in r.forms.items():
            lines += [
                "",
                f"## Fields present in `{table}`",
                "",
                f"Tabs: {', '.join(inv.get('tabs', []))}",
                "",
                "| column | kind | label | tab |",
                "|---|---|---|---|",
            ]
            lines += [f"| `{f['column']}` | {f['kind']} | {f['label']} | {f['tab']} |" for f in inv.get("fields", [])]
        lines += ["", "## Manual assistance needed", ""]
        lines += [f"- {m}" for m in r.manual_assistance] or ["- none"]
        lines += ["", "Screenshots/HTML dumps contain authenticated backend content - keep them private.", ""]
        return "\n".join(lines)
