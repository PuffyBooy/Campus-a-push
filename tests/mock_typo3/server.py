"""A small fake TYPO3 backend + frontend for browser tests.

It imitates the parts of the real markup the bot relies on: the login form
(#t3-username/#t3-password/#t3-login-submit), the backend shell with the
``list_frame`` iframe (v12-style deep-link redirect), FormEngine fields
(``data-formengine-input-name`` + hidden twin inputs, tabs as hidden panes),
a CKEditor-5-like editable exposing ``ckeditorInstance``, the "Select & upload
files" drag uploader creating ``sys_file_reference`` inline records, an inverted
"Visible" checkbox for the ``hidden`` column, the List module search and public
news/page URLs. It is NOT TYPO3 - passing tests prove the automation logic, not
compatibility with a particular TYPO3 installation.
"""

from __future__ import annotations

import base64
import html
import json
import re
import secrets
import threading
from dataclasses import dataclass, field
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urlparse

NEWS = "tx_news_domain_model_news"


def slugify(text: str) -> str:
    text = text.lower().replace("ä", "ae").replace("ö", "oe").replace("ü", "ue").replace("ß", "ss")
    return re.sub(r"[^a-z0-9]+", "-", text).strip("-") or "record"


@dataclass
class MockState:
    username: str = "editor"
    password: str = "secret-pass"
    mfa: bool = False
    captcha: bool = False
    categories: dict[str, str] = field(default_factory=lambda: {"1": "KI", "2": "Technik", "3": "Trends"})
    sessions: set[str] = field(default_factory=set)
    records: dict[str, dict[int, dict[str, Any]]] = field(
        default_factory=lambda: {NEWS: {}, "pages": {}, "tt_content": {}}
    )
    files: dict[int, dict[str, Any]] = field(default_factory=dict)
    next_uid: int = 100
    login_attempts: int = 0
    saves: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)

    def uid(self) -> int:
        with self.lock:
            self.next_uid += 1
            return self.next_uid


CSS = """
body{font-family:sans-serif} .tab-pane{display:none} .tab-pane.active{display:block}
.modal{display:none} .modal.show{display:block;position:fixed;top:20%;left:30%;background:#fff;border:1px solid #000;padding:1em}
"""

FORM_JS = r"""
document.addEventListener('click', (e) => {
  const tab = e.target.closest('[data-bs-toggle="tab"]');
  if (tab) {
    e.preventDefault();
    document.querySelectorAll('.tab-pane').forEach(p => p.classList.remove('active'));
    document.querySelectorAll('[data-bs-toggle="tab"]').forEach(t => t.classList.remove('active'));
    document.querySelector(tab.getAttribute('href')).classList.add('active');
    tab.classList.add('active');
  }
});
// FormEngine: visible field -> hidden twin on change
document.addEventListener('change', (e) => {
  const el = e.target;
  const name = el.getAttribute('data-formengine-input-name');
  if (el.matches('select[data-relatedfieldname]')) {
    const target = el.getAttribute('data-relatedfieldname');
    const hidden = document.querySelector(`input[type=hidden][name="${target}"]`);
    const sel = document.querySelector(`select[data-formengine-input-name="${target}"]`);
    Array.from(el.selectedOptions).forEach(o => {
      const ids = hidden.value ? hidden.value.split(',') : [];
      if (!ids.includes(o.value)) { ids.push(o.value); hidden.value = ids.join(','); sel.add(new Option(o.text, o.value)); }
    });
    return;
  }
  if (!name) return;
  const hidden = document.querySelector(`input[type=hidden][name="${name}"]`);
  if (!hidden) return;
  if (el.type === 'checkbox') {
    hidden.value = el.dataset.invert ? (el.checked ? '0' : '1') : (el.checked ? '1' : '0');
  } else { hidden.value = el.value; }
});
// fake CKEditor 5
document.querySelectorAll('typo3-rte-ckeditor-ckeditor5').forEach(w => {
  const ta = w.querySelector('textarea');
  const ed = w.querySelector('.ck-editor__editable');
  ed.innerHTML = ta.value;
  ed.ckeditorInstance = {
    setData(h) { ed.innerHTML = h; },
    getData() { return ed.innerHTML; },
    updateSourceElement() { ta.value = ed.innerHTML; },
  };
  ed.addEventListener('input', () => { ta.value = ed.innerHTML; });
});
// client-side validation like FormEngine
document.getElementById('EditDocumentController').addEventListener('submit', (e) => {
  const req = document.querySelectorAll('[data-required]');
  let bad = false;
  req.forEach(el => {
    const hidden = document.querySelector(`input[type=hidden][name="${el.getAttribute('data-formengine-input-name')}"]`);
    if (!(hidden ? hidden.value : el.value).trim()) { el.closest('.formengine-field-item').classList.add('has-error'); bad = true; }
  });
  if (bad) e.preventDefault();
});
// drag uploader
document.querySelectorAll('.t3js-drag-uploader').forEach(btn => {
  btn.addEventListener('click', () => {
    const input = document.createElement('input');
    input.type = 'file'; input.style.display = 'none';
    document.body.appendChild(input);
    input.addEventListener('change', () => {
      const file = input.files[0];
      const reader = new FileReader();
      reader.onload = async () => {
        const data = reader.result.split(',')[1];
        const resp = await fetch('/typo3/ajax/upload', {method: 'POST', headers: {'Content-Type': 'application/json'},
                                  body: JSON.stringify({name: file.name, data})});
        const info = await resp.json();
        const key = 'NEW' + Math.random().toString(16).slice(2, 10);
        const group = btn.closest('[data-object-group]');
        const list = group.querySelector('.inline-records');
        const p = `data[sys_file_reference][${key}]`;
        list.insertAdjacentHTML('beforeend', `
          <div class="panel form-irre-object"><div class="form-irre-header">${info.name}</div>
           <input type="hidden" name="${p}[uid_local]" value="${info.uid}">
           <div class="formengine-field-item"><label>Alternative</label>
             <input type="text" data-formengine-input-name="${p}[alternative]"><input type="hidden" name="${p}[alternative]" value=""></div>
           <div class="formengine-field-item"><label>Title</label>
             <input type="text" data-formengine-input-name="${p}[title]"><input type="hidden" name="${p}[title]" value=""></div>
           <div class="formengine-field-item"><label>Description</label><textarea name="${p}[description]"></textarea></div>
           <div class="formengine-field-item"><label>Show in views</label>
             <input type="checkbox" data-formengine-input-name="${p}[showinpreview]"><input type="hidden" name="${p}[showinpreview]" value="0"></div>
          </div>`);
        const hidden = group.querySelector('input[type=hidden][data-relations]');
        hidden.value = hidden.value ? hidden.value + ',' + key : key;
      };
      reader.readAsDataURL(file);
    });
    input.click();
  });
});
// delete with confirmation modal
document.querySelectorAll('.t3js-editform-delete-record').forEach(btn => btn.addEventListener('click', () => {
  document.getElementById('delete-modal').classList.add('show');
}));
"""


def esc(v: Any) -> str:
    return html.escape(str(v if v is not None else ""), quote=True)


class Handler(BaseHTTPRequestHandler):
    state: MockState  # injected per server

    def log_message(self, *args):  # silence
        pass

    # ------------------------------------------------------------------ helpers
    def _send(
        self,
        body: str | bytes,
        status: int = 200,
        ctype: str = "text/html; charset=utf-8",
        headers: dict[str, str] | None = None,
    ) -> None:
        data = body.encode() if isinstance(body, str) else body
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _redirect(self, location: str, cookie: str | None = None) -> None:
        headers = {"Location": location}
        if cookie:
            headers["Set-Cookie"] = cookie
        self._send("", 303, headers=headers)

    def _authed(self) -> bool:
        cookie = SimpleCookie(self.headers.get("Cookie", ""))
        token = cookie.get("be_typo_user")
        return bool(token and token.value in self.state.sessions)

    def _body(self) -> bytes:
        return self.rfile.read(int(self.headers.get("Content-Length", 0) or 0))

    def _page(self, title: str, body: str, js: str = "") -> str:
        return (
            f"<!doctype html><html><head><title>{esc(title)}</title><style>{CSS}</style></head>"
            f"<body>{body}<script>{js}</script></body></html>"
        )

    # ------------------------------------------------------------------ routing
    def do_GET(self):
        url = urlparse(self.path)
        q = parse_qs(url.query)
        path = url.path.rstrip("/") or "/"
        if path == "/typo3":
            return self._redirect("/typo3/main") if self._authed() else self._login_page()
        if path == "/typo3/login/mfa":
            return self._send(self._page("MFA", '<form id="t3-mfa"><input name="totp"><button>Verify</button></form>'))
        if path.startswith("/typo3/") and not self._authed():
            return self._redirect("/typo3/")
        if path == "/typo3/main":
            target = q.get("redirect", ["/typo3/module/dashboard?embedded=1"])[0]
            return self._send(
                self._page(
                    "TYPO3 CMS",
                    (
                        '<div class="scaffold t3js-scaffold"><nav id="modulemenu">'
                        '<a data-modulemenu-identifier="web_list" href="#">List</a>'
                        '<a data-modulemenu-identifier="web_NewsAdministration" href="#">News Administration</a></nav>'
                        f'<typo3-backend-module-router><iframe name="list_frame" src="{esc(target)}" width="1400" '
                        'height="900"></iframe></typo3-backend-module-router></div>'
                    ),
                )
            )
        if path in {"/typo3/record/edit", "/typo3/module/web/list"} and "embedded" not in q:
            # v12-style: deep links are opened inside the backend shell
            sep = "&" if url.query else ""
            return self._redirect("/typo3/main?redirect=" + quote(f"{path}?{url.query}{sep}embedded=1"))
        if path == "/typo3/module/dashboard":
            return self._send(self._page("Dashboard", "<h1>Dashboard</h1>"))
        if path == "/typo3/record/edit":
            return self._edit_form(q)
        if path == "/typo3/module/web/list":
            return self._list(q)
        if path.startswith("/fileadmin/"):
            name = unquote(path.rsplit("/", 1)[-1])
            for f in self.state.files.values():
                if f["name"] == name:
                    return self._send(f["data"], ctype="image/png")
            return self._send("not found", 404)
        return self._frontend(path)

    def do_POST(self):
        url = urlparse(self.path)
        path = url.path.rstrip("/")
        if path == "/typo3/login":
            return self._login(parse_qs(self._body().decode()))
        if not self._authed():
            return self._send("forbidden", 403)
        if path == "/typo3/ajax/upload":
            data = json.loads(self._body())
            uid = self.state.uid()
            name = data["name"]
            if any(f["name"] == name for f in self.state.files.values()):
                name = re.sub(r"(\.\w+)$", r"_01\1", name)
            self.state.files[uid] = {"name": name, "data": base64.b64decode(data["data"])}
            return self._send(json.dumps({"uid": uid, "name": name}), ctype="application/json")
        if path == "/typo3/record/commit":
            return self._commit(parse_qs(self._body().decode(), keep_blank_values=True))
        if path == "/typo3/record/delete":
            q = parse_qs(url.query)
            self.state.records[q["table"][0]].pop(int(q["uid"][0]), None)
            return self._redirect("/typo3/module/dashboard?embedded=1")
        return self._send("not found", 404)

    # ------------------------------------------------------------------ login
    def _login_page(self, error: str = "") -> None:
        captcha = '<div class="g-recaptcha" data-sitekey="x"></div>' if self.state.captcha else ""
        err = f'<div id="t3-login-error" class="alert alert-danger">{esc(error)}</div>' if error else ""
        self._send(
            self._page(
                "TYPO3 Login",
                (
                    f'{err}<form method="post" action="/typo3/login">'
                    '<input id="t3-username" name="username" type="text">'
                    '<input id="t3-password" name="p_field" type="password">'
                    f'{captcha}<button id="t3-login-submit" type="submit" name="commandLI">Login</button></form>'
                ),
            )
        )

    def _login(self, form: dict[str, list[str]]) -> None:
        self.state.login_attempts += 1
        user, pw = form.get("username", [""])[0], form.get("p_field", [""])[0]
        if user != self.state.username or pw != self.state.password:
            return self._login_page("Your login attempt did not succeed")
        if self.state.mfa:
            return self._redirect("/typo3/login/mfa")
        token = secrets.token_hex(16)
        self.state.sessions.add(token)
        self._redirect("/typo3/main", cookie=f"be_typo_user={token}; Path=/; HttpOnly")

    # ------------------------------------------------------------------ FormEngine
    def _edit_form(self, q: dict[str, list[str]]) -> None:
        params = [(k, v[0]) for k, v in q.items() if k.startswith("edit[")]
        if not params:
            return self._send("bad request", 400)
        m = re.match(r"edit\[(\w+)\]\[(\d+)\]", params[0][0])
        table, num, cmd = m.group(1), int(m.group(2)), params[0][1]
        if cmd == "new":
            key, rec = f"NEW{secrets.token_hex(4)}", {"pid": num, "hidden": 1 if table == "pages" else 0}
            for k, v in q.items():
                dm = re.match(rf"defVals\[{table}\]\[(\w+)\]", k)
                if dm:
                    rec[dm.group(1)] = v[0]
        else:
            if num not in self.state.records.get(table, {}):
                return self._send("record not found", 404)
            key, rec = str(num), self.state.records[table][num]
        self._send(self._page(f"Edit {table}", self._form_html(table, key, rec), FORM_JS))

    def _field(
        self, table: str, key: str, col: str, label: str, value: Any, kind: str = "text", required: bool = False
    ) -> str:
        n = f"data[{table}][{key}][{col}]"
        req = " data-required" if required else ""
        if kind == "textarea":
            return (
                f'<div class="formengine-field-item"><label>{label}</label>'
                f'<textarea name="{n}"{req}>{esc(value)}</textarea></div>'
            )
        if kind == "rte":
            return (
                f'<div class="formengine-field-item"><label>{label}</label><typo3-rte-ckeditor-ckeditor5>'
                f'<textarea name="{n}" style="display:none">{esc(value)}</textarea>'
                '<div class="ck-editor__editable" contenteditable="true"></div>'
                "</typo3-rte-ckeditor-ckeditor5></div>"
            )
        if kind == "visible":
            checked = " checked" if str(value) in {"0", ""} else ""
            return (
                f'<div class="formengine-field-item"><label>Visible</label>'
                f'<input type="checkbox" data-invert="1" data-formengine-input-name="{n}"{checked}>'
                f'<input type="hidden" name="{n}" value="{esc(value or 0)}"></div>'
            )
        return (
            f'<div class="formengine-field-item"><label>{label}</label>'
            f'<input type="text" class="form-control" data-formengine-input-name="{n}" value="{esc(value)}"{req}>'
            f'<input type="hidden" name="{n}" value="{esc(value)}"></div>'
        )

    def _media(self, table: str, key: str, col: str, rec: dict[str, Any]) -> str:
        n = f"data[{table}][{key}][{col}]"
        existing = "".join(
            f'<div class="panel form-irre-object"><div class="form-irre-header">{esc(self.state.files[r["uid_local"]]["name"])}</div>'
            f'<input type="hidden" name="data[sys_file_reference][{rid}][alternative]" value="{esc(r.get("alternative"))}"></div>'
            for rid, r in rec.get("references", {}).items()
        )
        group = f"data-{key}-{table}-{col}"
        return (
            f'<div class="formengine-field-item" id="{group}" data-object-group="{group}"><label>Media</label>'
            f'<input type="hidden" data-relations="1" name="{n}" value="">'
            f'<div class="inline-records">{existing}</div>'
            '<button type="button" class="btn btn-default t3js-drag-uploader">Select &amp; upload files</button></div>'
        )

    def _form_html(self, table: str, key: str, rec: dict[str, Any]) -> str:
        f = self._field
        pid = f'<input type="hidden" name="data[{table}][{key}][pid]" value="{rec.get("pid", 0)}">'
        if table == NEWS:
            cats = rec.get("categories", "")
            selected = "".join(
                f'<option value="{c}">{esc(self.state.categories[c])}</option>' for c in cats.split(",") if c
            )
            avail = "".join(f'<option value="{k}">{esc(v)}</option>' for k, v in self.state.categories.items())
            n = f"data[{table}][{key}][categories]"
            tabs = [
                (
                    "General",
                    f(table, key, "title", "Title", rec.get("title"), required=True)
                    + f(table, key, "datetime", "Date & time", rec.get("datetime"))
                    + f(table, key, "teaser", "Teaser", rec.get("teaser"), "textarea")
                    + f(table, key, "bodytext", "Text", rec.get("bodytext"), "rte"),
                ),
                ("Media", self._media(table, key, "fal_media", rec)),
                (
                    "Categories",
                    f'<div class="formengine-field-item"><label>Categories</label>'
                    f'<input type="hidden" name="{n}" value="{esc(cats)}">'
                    f'<select multiple data-formengine-input-name="{n}">{selected}</select>'
                    f'<select multiple data-relatedfieldname="{n}">{avail}</select></div>',
                ),
                ("Access", f(table, key, "hidden", "Visible", rec.get("hidden", 0), "visible")),
                (
                    "SEO",
                    f(table, key, "alternative_title", "Alternative title", rec.get("alternative_title"))
                    + f(table, key, "path_segment", "URL segment", rec.get("path_segment"))
                    + f(table, key, "description", "Description", rec.get("description"), "textarea")
                    + f(table, key, "keywords", "Keywords", rec.get("keywords"), "textarea"),
                ),
            ]
        elif table == "pages":
            tabs = [
                (
                    "General",
                    f(table, key, "title", "Page title", rec.get("title"), required=True)
                    + f(table, key, "slug", "URL segment", rec.get("slug"))
                    + f(table, key, "abstract", "Abstract", rec.get("abstract"), "textarea"),
                ),
                (
                    "SEO",
                    f(table, key, "seo_title", "SEO title", rec.get("seo_title"))
                    + f(table, key, "description", "Description", rec.get("description"), "textarea")
                    + f(table, key, "keywords", "Keywords", rec.get("keywords"), "textarea"),
                ),
                ("Access", f(table, key, "hidden", "Visible", rec.get("hidden", 1), "visible")),
            ]
        else:
            tabs = [
                (
                    "General",
                    f(table, key, "header", "Header", rec.get("header"), required=True)
                    + f(table, key, "bodytext", "Text", rec.get("bodytext"), "rte")
                    + f'<input type="hidden" name="data[{table}][{key}][CType]" value="{esc(rec.get("CType", "text"))}">',
                ),
                ("Images", self._media(table, key, "assets", rec)),
                ("Access", f(table, key, "hidden", "Visible", rec.get("hidden", 0), "visible")),
            ]
        nav = "".join(
            f'<li><a href="#tab-{i}" data-bs-toggle="tab" class="{"active" if i == 0 else ""}">{t}</a></li>'
            for i, (t, _) in enumerate(tabs)
        )
        panes = "".join(
            f'<div class="tab-pane{" active" if i == 0 else ""}" id="tab-{i}">{body}</div>'
            for i, (_, body) in enumerate(tabs)
        )
        delete = ""
        if key.isdigit():
            delete = (
                f'<button type="button" class="btn t3js-editform-delete-record">Delete</button>'
                f'<div class="modal" id="delete-modal"><form method="post" '
                f'action="/typo3/record/delete?table={table}&uid={key}">Really delete?'
                '<button class="btn btn-danger" type="submit">Delete</button></form></div>'
            )
        flash = '<div class="alert alert-success">Record saved</div>' if key.isdigit() else ""
        return (
            f'<div class="module-docheader"><button type="submit" name="_savedok" value="1" '
            f'form="EditDocumentController" class="btn btn-default">Save</button>'
            f'<a href="#" class="t3js-editform-close">Close</a>{delete}</div>{flash}'
            f'<form name="editform" id="EditDocumentController" method="post" action="/typo3/record/commit">'
            f'{pid}<ul class="nav nav-tabs">{nav}</ul><div class="tab-content">{panes}</div></form>'
        )

    def _commit(self, form: dict[str, list[str]]) -> None:
        data: dict[str, dict[str, dict[str, str]]] = {}
        for name, values in form.items():
            m = re.match(r"data\[(\w+)\]\[(\w+)\]\[(\w+)\]", name)
            if m:
                data.setdefault(m.group(1), {}).setdefault(m.group(2), {})[m.group(3)] = values[0]
        main_table = next(t for t in data if t != "sys_file_reference")
        key, values = next(iter(data[main_table].items()))
        table_records = self.state.records[main_table]
        if key.startswith("NEW"):
            uid = self.state.uid()
            rec: dict[str, Any] = {"uid": uid, "references": {}}
            table_records[uid] = rec
        else:
            uid = int(key)
            rec = table_records[uid]
        for col, value in values.items():
            if col == "pid":
                value = int(value)
            elif col == "hidden":
                value = int(value or 0)
            if col not in {"fal_media", "assets"}:
                rec[col] = value
        if main_table == NEWS and not rec.get("path_segment"):
            rec["path_segment"] = slugify(rec.get("title", ""))
        if main_table == "pages" and not rec.get("slug"):
            rec["slug"] = "/" + slugify(rec.get("title", ""))
        for ref_key, ref in data.get("sys_file_reference", {}).items():
            if ref_key.startswith("NEW") and ref.get("uid_local"):
                rec["references"][str(self.state.uid())] = {**ref, "uid_local": int(ref["uid_local"])}
        self.state.saves += 1
        self._redirect(f"/typo3/record/edit?edit[{main_table}][{uid}]=edit&embedded=1")

    def _list(self, q: dict[str, list[str]]) -> None:
        table = q.get("table", [NEWS])[0]
        pid = int(q.get("id", ["0"])[0])
        term = q.get("search_field", [""])[0].lower()
        rows = "".join(
            f'<tr data-table="{table}" data-uid="{uid}"><td>{esc(r.get("title", r.get("header", "")))}</td></tr>'
            for uid, r in self.state.records.get(table, {}).items()
            if r.get("pid") == pid and term in str(r.get("title", "")).lower()
        )
        self._send(self._page("List", f'<div class="recordlist"><table data-table="{table}">{rows}</table></div>'))

    # ------------------------------------------------------------------ frontend
    def _frontend(self, path: str) -> None:
        news = self.state.records[NEWS]
        if path == "/news":
            links = "".join(
                f'<li><a href="/news/{esc(r["path_segment"])}">{esc(r["title"])}</a></li>'
                for r in news.values()
                if not r.get("hidden")
            )
            return self._send(self._page("News", f"<h1>News</h1><ul>{links}</ul>"))
        if path.startswith("/news/"):
            slug = path[len("/news/") :]
            for r in news.values():
                if r.get("path_segment") == slug and not r.get("hidden"):
                    imgs = "".join(
                        f'<img src="/fileadmin/user_upload/{esc(self.state.files[ref["uid_local"]]["name"])}" '
                        f'alt="{esc(ref.get("alternative"))}"><figcaption>{esc(ref.get("description"))}</figcaption>'
                        for ref in r.get("references", {}).values()
                    )
                    return self._send(
                        self._page(
                            f"{r['title']} | Mock Site",
                            (
                                f'<article><h1>{esc(r["title"])}</h1><p class="teaser">{esc(r.get("teaser"))}</p>'
                                f'{imgs}<div class="body">{r.get("bodytext", "")}</div></article>'
                            ),
                        )
                    )
        for page in self.state.records["pages"].values():
            if page.get("slug") == path and not page.get("hidden"):
                content = [
                    c
                    for c in self.state.records["tt_content"].values()
                    if c.get("pid") == page["uid"] and not c.get("hidden")
                ]
                body = "".join(f"<h2>{esc(c.get('header'))}</h2>{c.get('bodytext', '')}" for c in content)
                return self._send(self._page(f"{page['title']} | Mock Site", f"<h1>{esc(page['title'])}</h1>{body}"))
        self._send(self._page("Not found", "<h1>Page not found</h1>"), 404)


class MockTypo3:
    def __init__(self, state: MockState | None = None):
        self.state = state or MockState()
        handler = type("BoundHandler", (Handler,), {"state": self.state})
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def __enter__(self) -> MockTypo3:
        self.thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


if __name__ == "__main__":  # manual exploration: python tests/mock_typo3/server.py
    import time

    with MockTypo3() as mock:
        print(f"mock TYPO3 at {mock.base_url}/typo3/  (editor / secret-pass)")
        while True:
            time.sleep(3600)
