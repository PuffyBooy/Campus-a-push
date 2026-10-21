"""Pixabay image discovery, licence-aware filtering and download.

Pixabay content is published under the Pixabay Content License
(https://pixabay.com/service/license-summary/). It is *not* unrestricted: depicted
people, trademarks/logos and private property may need additional permission, and
images must not be used in a way that suggests endorsement. This module therefore

* searches photos only, with SafeSearch, landscape orientation and a minimum width,
* rejects images whose tags indicate people or brands (configurable lists),
* never reuses an image (provider id and SHA-256 are tracked in the state),
* refuses to pick an image below the relevance threshold - no unrelated images,
* downloads the file (Pixabay forbids permanent hotlinking) and stores attribution.

Tag-based filtering cannot detect every identifiable person or logo; the README asks
editors to spot-check images, and ``images.required`` decides what happens if none fits.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests

from .config import ImageSettings, RetryPolicy
from .html_utils import normalise
from .logging_utils import get_logger
from .models import ArticleContent, ImageInfo, slugify
from .retry import retry_call
from .state_manager import StateStore

log = get_logger("images")

PIXABAY_API = "https://pixabay.com/api/"
LICENSE_NAME = "Pixabay Content License"
LICENSE_URL = "https://pixabay.com/service/license-summary/"
_STOPWORDS = {"the", "and", "for", "with", "der", "die", "das", "und", "mit", "von", "a", "an", "of", "in"}


class ImageError(RuntimeError):
    pass


@dataclass
class Candidate:
    hit: dict[str, Any]
    relevance: float
    rejected: list[str]


class PixabayClient:
    def __init__(self, api_key: str, retry: RetryPolicy, session: requests.Session | None = None):
        self.api_key = api_key
        self.retry = retry
        self.session = session or requests.Session()

    def search(self, query: str, settings: ImageSettings) -> list[dict[str, Any]]:
        params = {
            "key": self.api_key,
            "q": query[:100],
            "lang": settings.search_language,
            "image_type": "photo",
            "orientation": "horizontal",
            "min_width": settings.min_width,
            "safesearch": "true" if settings.safesearch else "false",
            "per_page": max(3, min(200, settings.per_page)),
            "order": "popular",
        }

        def call() -> list[dict[str, Any]]:
            resp = self.session.get(PIXABAY_API, params=params, timeout=30)
            if resp.status_code == 429:
                raise requests.HTTPError("Pixabay rate limit reached (429)")
            resp.raise_for_status()
            return resp.json().get("hits", [])

        return retry_call(call, self.retry, retry_on=(requests.RequestException,), description="Pixabay search")


def _tokens(text: str) -> set[str]:
    return {t for t in normalise(text).split() if len(t) > 2 and t not in _STOPWORDS}


def hit_tags(hit: dict[str, Any]) -> list[str]:
    return [t.strip().lower() for t in str(hit.get("tags", "")).split(",") if t.strip()]


class ImageFinder:
    def __init__(
        self,
        settings: ImageSettings,
        client: PixabayClient,
        store: StateStore,
        download_dir: Path,
        retry: RetryPolicy,
        session: requests.Session | None = None,
    ):
        self.settings = settings
        self.client = client
        self.store = store
        self.download_dir = download_dir
        self.retry = retry
        self.session = session or requests.Session()

    def queries(self, content: ArticleContent, topic: str) -> list[str]:
        queries: list[str] = []
        kws = [k for k in content.image_keywords if k.strip()]
        if kws:
            queries.append(" ".join(kws[:3]))
            queries.extend(kws[:3])
        queries.append(topic)
        seen, unique = set(), []
        for q in queries:
            key = normalise(q)
            if key and key not in seen:
                seen.add(key)
                unique.append(q)
        return unique[: self.settings.max_queries]

    def rejection_reasons(self, hit: dict[str, Any]) -> list[str]:
        reasons = []
        tags = hit_tags(hit)
        tag_words = set()
        for tag in tags:
            tag_words.update(re.split(r"[\s\-]+", tag))
        if self.settings.exclude_people:
            people = sorted(tag_words & {p.lower() for p in self.settings.people_tags})
            if people:
                reasons.append(f"may show identifiable people ({', '.join(people)})")
        blocked = [b for b in self.settings.blocked_terms if b.lower() in tag_words or b.lower() in tags]
        if blocked:
            reasons.append(f"may show trademarks/brands ({', '.join(blocked)})")
        width, height = int(hit.get("imageWidth") or 0), int(hit.get("imageHeight") or 0)
        if width < self.settings.min_width:
            reasons.append(f"too small ({width}px)")
        if height <= 0 or width / height < self.settings.min_aspect_ratio:
            reasons.append("not a landscape header format")
        if hit.get("type") not in (None, "photo"):
            reasons.append(f"type {hit.get('type')} is not a photo")
        if self.store.image_is_used("pixabay", str(hit.get("id"))):
            reasons.append("already used")
        if not (hit.get("largeImageURL") or hit.get("webformatURL")):
            reasons.append("no download URL")
        return reasons

    def relevance(self, hit: dict[str, Any], content: ArticleContent, topic: str) -> float:
        wanted = set()
        for kw in content.image_keywords:
            wanted |= _tokens(kw)
        if not wanted:
            wanted = _tokens(topic)
        if not wanted:
            return 0.0
        tag_tokens = set()
        for tag in hit_tags(hit):
            tag_tokens |= _tokens(tag)
        matched = {w for w in wanted if any(w == t or (len(w) > 4 and (w in t or t in w)) for t in tag_tokens)}
        return round(len(matched) / len(wanted), 3)

    def select(self, content: ArticleContent, topic: str) -> Candidate | None:
        best: Candidate | None = None
        seen_ids = set()
        for query in self.queries(content, topic):
            try:
                hits = self.client.search(query, self.settings)
            except Exception as exc:
                log.warning("image search '%s' failed: %s", query, exc)
                continue
            for hit in hits:
                if hit.get("id") in seen_ids:
                    continue
                seen_ids.add(hit.get("id"))
                cand = Candidate(
                    hit=hit, relevance=self.relevance(hit, content, topic), rejected=self.rejection_reasons(hit)
                )
                if cand.rejected or cand.relevance < self.settings.min_relevance:
                    continue
                popularity = (hit.get("likes", 0) or 0) + (hit.get("downloads", 0) or 0) / 100
                if best is None or (cand.relevance, popularity) > (
                    best.relevance,
                    (best.hit.get("likes", 0) or 0) + (best.hit.get("downloads", 0) or 0) / 100,
                ):
                    best = cand
        if best is None:
            log.warning("no suitable image found for '%s'", content.title)
        return best

    def download(self, hit: dict[str, Any], slug: str) -> tuple[Path, str]:
        url = hit.get("largeImageURL") or hit.get("webformatURL")
        max_bytes = int(self.settings.max_file_mb * 1024 * 1024)

        def call() -> bytes:
            resp = self.session.get(url, timeout=60)
            resp.raise_for_status()
            if len(resp.content) > max_bytes:
                raise ImageError("image exceeds images.max_file_mb")
            return resp.content

        data = retry_call(call, self.retry, retry_on=(requests.RequestException,), description="image download")
        if data[:3] == b"\xff\xd8\xff":
            ext = "jpg"
        elif data[:8] == b"\x89PNG\r\n\x1a\n":
            ext = "png"
        else:
            raise ImageError("downloaded file is not a JPEG or PNG image")
        sha = hashlib.sha256(data).hexdigest()
        self.download_dir.mkdir(parents=True, exist_ok=True)
        path = self.download_dir / f"{slugify(slug, 60)}-pixabay-{hit.get('id')}.{ext}"
        path.write_bytes(data)
        return path, sha

    def find(self, content: ArticleContent, topic: str) -> ImageInfo | None:
        candidate = self.select(content, topic)
        if candidate is None:
            return None
        hit = candidate.hit
        path, sha = self.download(hit, content.slug or content.title)
        if self.store.image_is_used("pixabay", str(hit.get("id")), sha):
            log.warning("downloaded image %s is a duplicate of an already used image", hit.get("id"))
            path.unlink(missing_ok=True)
            return None
        tags = hit_tags(hit)
        user = str(hit.get("user") or "Pixabay")
        attribution = self.settings.attribution.template.format(
            user=user, page_url=hit.get("pageURL", ""), license=LICENSE_NAME
        )
        alt = self.settings.alt_template.format(tags=", ".join(tags[:4]), title=content.title)
        return ImageInfo(
            provider="pixabay",
            provider_id=str(hit.get("id")),
            page_url=str(hit.get("pageURL", "")),
            download_url=str(hit.get("largeImageURL") or hit.get("webformatURL")),
            width=int(hit.get("imageWidth") or 0),
            height=int(hit.get("imageHeight") or 0),
            tags=tags,
            user=user,
            license_name=LICENSE_NAME,
            license_url=LICENSE_URL,
            attribution=attribution,
            alt_text=alt[:250],
            relevance=candidate.relevance,
            local_path=str(path),
            sha256=sha,
            file_name=path.name,
        )
