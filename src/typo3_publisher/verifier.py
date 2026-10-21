"""Public verification: the article only counts as published when the public page,
fetched anonymously (no backend cookies), shows the expected title and content."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urljoin

import requests

from .config import VerificationSettings, WebsiteSettings
from .html_utils import ParsedPage, containment, html_to_text, title_similarity
from .logging_utils import get_logger
from .retry import sleep

log = get_logger("verify")


@dataclass
class VerificationResult:
    ok: bool
    url: str
    status_code: int | None = None
    title_similarity: float = 0.0
    content_overlap: float = 0.0
    image_found: bool | None = None
    reasons: list[str] = field(default_factory=list)
    attempts: int = 0

    def as_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


class PublicVerifier:
    def __init__(self, website: WebsiteSettings, session: requests.Session | None = None):
        self.website = website
        self.settings: VerificationSettings = website.verification
        self.session = session or requests.Session()
        self.session.headers.update(
            {"User-Agent": self.settings.user_agent, "Cache-Control": "no-cache", "Pragma": "no-cache"}
        )

    def candidate_urls(self, slug: str, title: str, backend_view_url: str = "") -> list[str]:
        cfg = self.website.public_url
        base = self.website.base_url.rstrip("/")
        urls = []
        if cfg.strategy == "backend_view" and backend_view_url:
            urls.append(backend_view_url)
        if cfg.strategy in {"pattern", "pattern_then_listing"} and slug:
            urls.append(cfg.pattern.format(base_url=base, slug=slug.strip("/")))
        if cfg.strategy in {"listing", "pattern_then_listing"} and cfg.listing_url:
            found = self.find_in_listing(cfg.listing_url.format(base_url=base), title)
            if found:
                urls.append(found)
        return list(dict.fromkeys(urls))

    def find_in_listing(self, listing_url: str, title: str) -> str | None:
        try:
            resp = self.session.get(listing_url, timeout=self.settings.timeout_seconds)
        except requests.RequestException as exc:
            log.warning("listing page %s unavailable: %s", listing_url, exc)
            return None
        if resp.status_code != 200:
            return None
        best = (0.0, None)
        for href, text in ParsedPage(resp.text).links:
            score = title_similarity(text, title)
            if score > best[0]:
                best = (score, href)
        if best[1] and best[0] >= self.settings.min_title_similarity:
            return urljoin(listing_url, best[1])
        return None

    def check(self, url: str, title: str, body_html: str, image_name: str = "") -> VerificationResult:
        result = VerificationResult(ok=False, url=url)
        try:
            resp = self.session.get(url, timeout=self.settings.timeout_seconds, allow_redirects=True)
        except requests.RequestException as exc:
            result.reasons.append(f"request failed: {exc}")
            return result
        result.status_code = resp.status_code
        if resp.status_code != 200:
            result.reasons.append(f"HTTP {resp.status_code}")
            return result
        page = ParsedPage(resp.text)
        candidates = [*page.h1, page.title, page.meta.get("og:title", "")]
        result.title_similarity = max((title_similarity(c, title) for c in candidates if c), default=0.0)
        # Titles often carry a site suffix ("Headline | Site"): accept containment.
        if title.casefold() in page.title.casefold():
            result.title_similarity = max(result.title_similarity, 1.0)
        result.content_overlap = round(containment(html_to_text(body_html), page.text), 3)
        if image_name:
            stem = image_name.rsplit(".", 1)[0]
            result.image_found = stem[:40] in resp.text
        if result.title_similarity < self.settings.min_title_similarity:
            result.reasons.append(f"title similarity {result.title_similarity:.2f} below threshold")
        if result.content_overlap < self.settings.min_content_overlap:
            result.reasons.append(f"content overlap {result.content_overlap:.2f} below threshold")
        result.ok = not result.reasons
        return result

    def verify(
        self,
        slug: str,
        title: str,
        body_html: str,
        image_name: str = "",
        backend_view_url: str = "",
        known_url: str = "",
    ) -> VerificationResult:
        """Poll until the page is public (caches, indexing) or attempts are exhausted."""
        delay = self.settings.initial_delay_seconds
        last = VerificationResult(ok=False, url=known_url, reasons=["no candidate URL found"])
        for attempt in range(1, self.settings.attempts + 1):
            urls = [known_url] if known_url else []
            urls += [u for u in self.candidate_urls(slug, title, backend_view_url) if u not in urls]
            for url in urls:
                result = self.check(url, title, body_html, image_name)
                result.attempts = attempt
                if result.ok:
                    log.info("public verification succeeded: %s", url)
                    return result
                last = result
            last.attempts = attempt
            if attempt < self.settings.attempts:
                log.info("public page not verified yet (%s); retrying in %.0fs", "; ".join(last.reasons), delay)
                sleep(delay)
                delay = min(self.settings.max_delay_seconds, delay * self.settings.backoff_factor)
        return last
