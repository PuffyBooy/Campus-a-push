"""Optional research stage using approved external sources.

Facts in factual articles must come from documents fetched here, not from the
model's memory. The fetched text is kept so that the reviewer can check *mechanically*
that every evidence snippet the writer cites literally appears in the cited source.
That check proves grounding in the source, not that the source itself is correct; the
README documents this limitation.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

import requests

from .config import ResearchSettings, RetryPolicy
from .html_utils import ParsedPage, domain_allowed, normalise
from .logging_utils import get_logger
from .models import Source
from .retry import retry_call

log = get_logger("research")


@dataclass
class ResearchDocument:
    source: Source
    text: str


@dataclass
class ResearchBundle:
    documents: list[ResearchDocument] = field(default_factory=list)

    @property
    def sources(self) -> list[Source]:
        return [d.source for d in self.documents]

    def by_url(self, url: str) -> ResearchDocument | None:
        target = url.rstrip("/")
        return next((d for d in self.documents if d.source.url.rstrip("/") == target), None)

    def evidence_found(self, url: str, evidence: str) -> bool:
        doc = self.by_url(url)
        snippet = normalise(evidence)
        return bool(doc and snippet and len(snippet.split()) >= 4 and snippet in normalise(doc.text))

    def as_prompt(self) -> str:
        if not self.documents:
            return "NO RESEARCH MATERIAL AVAILABLE. Do not state specific facts, numbers, dates, names or quotes."
        parts = []
        for i, doc in enumerate(self.documents, 1):
            parts.append(
                f"[SOURCE {i}] {doc.source.title}\nURL: {doc.source.url}\nPublished: "
                f"{doc.source.published or 'unknown'}\n---\n{doc.text}\n"
            )
        return "\n".join(parts)


_NS = {"atom": "http://www.w3.org/2005/Atom"}
_RSS1 = "{http://purl.org/rss/1.0/}"  # RSS 1.0 / RDF feeds, e.g. rss.orf.at/news.xml


def article_text(page: ParsedPage) -> str:
    """Main text of a news page: navigation, buttons and teasers are short lines, sentences are not."""
    return "\n".join(line for line in page.text.splitlines() if len(line.split()) >= 8)


def parse_feed(xml_text: str) -> list[dict[str, str]]:
    root = ET.fromstring(xml_text)
    items = []
    for ns in ("", _RSS1):  # RSS 2.0, RSS 1.0
        for item in root.iter(f"{ns}item"):
            items.append(
                {
                    "title": (item.findtext(f"{ns}title") or "").strip(),
                    "url": (item.findtext(f"{ns}link") or "").strip(),
                    "summary": ParsedPage(item.findtext(f"{ns}description") or "").text,
                    "published": (
                        item.findtext("pubDate") or item.findtext("{http://purl.org/dc/elements/1.1/}date") or ""
                    ).strip(),
                }
            )
    for entry in root.iter("{http://www.w3.org/2005/Atom}entry"):
        link = entry.find("atom:link[@rel='alternate']", _NS)
        if link is None:  # Element truthiness depends on children, so compare with None
            link = entry.find("atom:link", _NS)
        items.append(
            {
                "title": (entry.findtext("atom:title", default="", namespaces=_NS)).strip(),
                "url": link.get("href", "") if link is not None else "",
                "summary": ParsedPage(entry.findtext("atom:summary", default="", namespaces=_NS)).text,
                "published": (entry.findtext("atom:updated", default="", namespaces=_NS)).strip(),
            }
        )
    return items


class Researcher:
    def __init__(self, settings: ResearchSettings, retry: RetryPolicy, session: requests.Session | None = None):
        self.settings = settings
        self.retry = retry
        self.session = session or requests.Session()
        self.session.headers["User-Agent"] = settings.user_agent

    def _get(self, url: str) -> str:
        if not domain_allowed(url, self.settings.approved_domains):
            raise ValueError(f"domain of {url} is not in research.approved_domains")

        def call() -> str:
            resp = self.session.get(url, timeout=self.settings.timeout_seconds)
            resp.raise_for_status()
            return resp.text

        return retry_call(call, self.retry, retry_on=(requests.RequestException,), description=f"GET {url}")

    def research(self, topic: str, idea: str, keywords: list[str]) -> ResearchBundle:
        if not self.settings.enabled:
            return ResearchBundle()
        terms = {normalise(t) for t in [topic, *keywords, *idea.split()] if len(normalise(t)) > 3}
        candidates: list[tuple[int, dict[str, str]]] = []
        for feed_url in self.settings.feeds:
            try:
                items = parse_feed(self._get(feed_url))
            except Exception as exc:
                log.warning("research feed %s unavailable: %s", feed_url, exc)
                continue
            for item in items:
                if not item["url"] or not domain_allowed(item["url"], self.settings.approved_domains):
                    continue
                haystack = normalise(f"{item['title']} {item['summary']}")
                hits = sum(1 for t in terms if t in haystack)
                if hits >= self.settings.min_keyword_matches:
                    candidates.append((hits, item))
        candidates.sort(key=lambda c: c[0], reverse=True)
        bundle = ResearchBundle()
        seen = set()
        for _, item in candidates:
            if len(bundle.documents) >= self.settings.max_sources or item["url"] in seen:
                continue
            seen.add(item["url"])
            try:
                page = ParsedPage(self._get(item["url"]))
            except Exception as exc:
                log.warning("research source %s unavailable: %s", item["url"], exc)
                continue
            text = article_text(page)[: self.settings.max_chars_per_source]
            if len(text.split()) < 80:
                continue
            bundle.documents.append(
                ResearchDocument(
                    source=Source(
                        title=item["title"] or page.title,
                        url=item["url"],
                        published=item["published"],
                        excerpt=text[:300],
                    ),
                    text=text,
                )
            )
        log.info("research collected %d source(s) for topic %s", len(bundle.documents), topic)
        return bundle
