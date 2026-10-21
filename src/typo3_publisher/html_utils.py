"""HTML sanitising, text extraction and similarity helpers (stdlib only)."""

from __future__ import annotations

import html
import re
from difflib import SequenceMatcher
from html.parser import HTMLParser
from urllib.parse import urlparse

ALLOWED_TAGS = {"p", "h2", "h3", "h4", "ul", "ol", "li", "strong", "em", "b", "i", "a", "blockquote", "br"}
VOID_TAGS = {"br"}


class _Sanitizer(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.stack: list[str] = []
        self.skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "iframe", "object"}:
            self.skip_depth += 1
            return
        if self.skip_depth or tag not in ALLOWED_TAGS:
            return
        if tag == "a":
            href = dict(attrs).get("href") or ""
            if not href.startswith(("http://", "https://")):
                return
            self.out.append(f'<a href="{html.escape(href, quote=True)}">')
        else:
            self.out.append(f"<{tag}>")
        if tag not in VOID_TAGS:
            self.stack.append(tag)

    def handle_endtag(self, tag):
        if tag in {"script", "style", "iframe", "object"}:
            self.skip_depth = max(0, self.skip_depth - 1)
            return
        if self.skip_depth or tag not in self.stack:
            return
        while self.stack:
            open_tag = self.stack.pop()
            self.out.append(f"</{open_tag}>")
            if open_tag == tag:
                break

    def handle_data(self, data):
        if not self.skip_depth:
            self.out.append(html.escape(data, quote=False))

    def result(self) -> str:
        while self.stack:
            self.out.append(f"</{self.stack.pop()}>")
        return "".join(self.out).strip()


def sanitize_html(raw: str) -> str:
    """Keep only simple editorial markup that every TYPO3 RTE configuration accepts."""
    parser = _Sanitizer()
    parser.feed(raw or "")
    parser.close()
    cleaned = parser.result()
    if cleaned and not cleaned.lstrip().startswith("<"):
        cleaned = "".join(f"<p>{p.strip()}</p>" for p in re.split(r"\n\s*\n", cleaned) if p.strip())
    return cleaned


class _TextExtractor(HTMLParser):
    BLOCK = {"p", "div", "h1", "h2", "h3", "h4", "h5", "h6", "li", "br", "section", "article", "tr", "blockquote"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.title_parts: list[str] = []
        self.h1: list[str] = []
        self.links: list[tuple[str, str]] = []
        self._skip = 0
        self._in_title = False
        self._in_h1 = False
        self._h1_buf: list[str] = []
        self._link_href: str | None = None
        self._link_buf: list[str] = []
        self.meta: dict[str, str] = {}

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in {"script", "style", "noscript", "template", "svg"}:
            self._skip += 1
        elif tag == "title":
            self._in_title = True
        elif tag == "h1":
            self._in_h1 = True
            self._h1_buf = []
        elif tag == "a":
            self._link_href = a.get("href")
            self._link_buf = []
        elif tag == "meta" and a.get("content"):
            key = (a.get("name") or a.get("property") or "").lower()
            if key:
                self.meta[key] = a["content"]
        if tag in self.BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in {"script", "style", "noscript", "template", "svg"}:
            self._skip = max(0, self._skip - 1)
        elif tag == "title":
            self._in_title = False
        elif tag == "h1":
            self._in_h1 = False
            self.h1.append(" ".join("".join(self._h1_buf).split()))
        elif tag == "a" and self._link_href is not None:
            self.links.append((self._link_href, " ".join("".join(self._link_buf).split())))
            self._link_href = None
        if tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if self._skip:
            return
        if self._in_title:
            self.title_parts.append(data)
            return
        self.parts.append(data)
        if self._in_h1:
            self._h1_buf.append(data)
        if self._link_href is not None:
            self._link_buf.append(data)


class ParsedPage:
    def __init__(self, raw_html: str):
        parser = _TextExtractor()
        parser.feed(raw_html or "")
        parser.close()
        self.title = " ".join("".join(parser.title_parts).split())
        self.h1 = [h for h in parser.h1 if h]
        self.links = parser.links
        self.meta = parser.meta
        text = "".join(parser.parts)
        self.text = "\n".join(" ".join(line.split()) for line in text.splitlines() if line.strip())


def html_to_text(raw: str) -> str:
    return ParsedPage(raw).text


def word_count(raw_html: str) -> int:
    return len(re.findall(r"\w+", html_to_text(raw_html)))


def extract_links(raw_html: str) -> list[str]:
    return [href for href, _ in ParsedPage(raw_html).links if href]


def normalise(text: str) -> str:
    text = html.unescape(text or "").casefold()
    text = text.replace("„", '"').replace("“", '"').replace("”", '"').replace("’", "'").replace("‘", "'")
    text = re.sub(r"[^\w\s]", " ", text)
    return " ".join(text.split())


def title_similarity(a: str, b: str) -> float:
    na, nb = normalise(a), normalise(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    return SequenceMatcher(None, na, nb).ratio()


def shingles(text: str, size: int = 5) -> set[tuple[str, ...]]:
    words = normalise(text).split()
    if len(words) < size:
        return {tuple(words)} if words else set()
    return {tuple(words[i : i + size]) for i in range(len(words) - size + 1)}


def containment(expected: str, actual: str, size: int = 5) -> float:
    """Share of ``expected`` shingles that appear in ``actual`` (robust to surrounding markup)."""
    exp = shingles(expected, size)
    if not exp:
        return 0.0
    return len(exp & shingles(actual, size)) / len(exp)


def jaccard(a: str, b: str, size: int = 3) -> float:
    sa, sb = shingles(a, size), shingles(b, size)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def domain_of(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def domain_allowed(url: str, approved: list[str]) -> bool:
    host = domain_of(url)
    for allowed in approved:
        allowed = allowed.lower().removeprefix("www.")
        if host == allowed or host.endswith("." + allowed):
            return True
    return False
