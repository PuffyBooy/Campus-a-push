"""Editor: automated checks + an independent LLM review, and the write/review/revise loop.

Two layers protect publication:

1. Deterministic checks (no LLM): length, forbidden terms, duplicates, links and sources
   that are not part of the research material, quotations and percentages that do not
   occur in the fetched sources, verbatim presence of every cited evidence snippet.
2. A separate LLM call acting as editor, scoring the configured criteria.

The LLM editor is *not* independent fact verification. Grounding is enforced by layer 1;
the README explains the remaining limits.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable

from .config import Settings, read_text
from .content_generator import ContentGenerator
from .html_utils import (
    PLACEHOLDER_HEADING,
    PLACEHOLDER_MARK,
    extract_links,
    html_to_text,
    jaccard,
    normalise,
    split_placeholder_section,
    title_similarity,
    word_count,
)
from .llm import LLMClient, LLMRequestTooLargeError
from .logging_utils import get_logger
from .models import Article, ArticleContent, ArticleStatus, Issue, ReviewResult, Revision
from .research import ResearchBundle
from .serialization import to_dict

log = get_logger("editor")

REVIEW_FORMAT = """
Respond with a single JSON object and nothing else:
{
  "scores": {CRITERIA},
  "critical_issues": [{"category": "factual|legal|quality|policy", "description": "..."}],
  "feedback": [{"area": "criterion name", "issue": "...", "suggestion": "concrete fix"}],
  "summary": "two sentences"
}
Scores are numbers from 0 (unacceptable) to 10 (excellent). Report a critical issue for any claim that is
not supported by the research material, any invented quotation or source, defamatory or legally risky
content, and any violation of the editorial instructions' topics to avoid.
"""

DRAFT_MODE_REVIEW = """
DRAFT MODE: the article is a draft for a human author, who adds own reporting (own interviews, survey,
photos, personal experience), real quotes from people they talked to and the final headline variants
before publishing. Therefore:
- Do NOT report missing own reporting, missing interview quotes, missing own photos or a single headline
  (instead of several proposals) as critical issues, and do not lower scores for them.
- List each of these gaps as a short, concrete to-do for the author in "author_todos" (in the article's
  language), e.g. which person to ask for a quote or what to observe for an own scene.
- Invented facts, invented quotes and invented first-person experiences or scenes stay critical issues.
Add to the JSON: "author_todos": ["..."]
"""

QUOTES_UNCHECKED_REVIEW = """
QUOTES ARE NOT CHECKED: the author verifies or replaces every quotation before publishing. Do NOT report
quotations or O-Töne (invented, altered or missing from the research material) as critical issues and do
not lower scores for them; add "Zitat prüfen: <first words of the quote>" to "author_todos" instead.
"""

PLACEHOLDER_QUOTES_REVIEW = f"""
PLACEHOLDER QUOTES: the body may end with a section "{PLACEHOLDER_HEADING}" whose items start with
"{PLACEHOLDER_MARK}". They are invented on purpose as examples for the author and are replaced before
publishing: do not report them as invented quotes and do not lower scores for them. Report a critical issue
only if a placeholder is attributed to a real or public person, an expert, teacher, official, politician,
organisation or other professional, or if quotations appear in the article text above that section.
"""

# Editor findings that are only about quotations (moved to the author's to-dos when quotes are unchecked).
_QUOTE_FINDING_RE = re.compile(r"zitat|zitier|quot|o-t[öo]n", re.I)

_QUOTE_RE = re.compile(r"[\"„“«»](?P<q>[^\"„“”«»]{20,400})[\"“”«»]")
_PERCENT_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s?(?:%|prozent|percent|per cent)", re.I)


class EditorialReviewer:
    def __init__(self, settings: Settings, llm: LLMClient, rubric: str, editorial_prompt: str):
        self.settings = settings
        self.llm = llm
        self.rubric = rubric
        checklist = settings.editorial.review_checklist_file
        self.editorial_prompt = read_text(settings, checklist) if checklist else editorial_prompt

    # ------------------------------------------------------------------ deterministic checks
    def automated_checks(
        self,
        content: ArticleContent,
        research: ResearchBundle,
        existing: Iterable[Article] = (),
        allowed_links: Iterable[str] = (),
        self_id: str = "",
    ) -> list[Issue]:
        e = self.settings.editorial
        issues: list[Issue] = []
        body_html, placeholders = split_placeholder_section(content.body_html)
        if placeholders:
            items = re.findall(r"<li[^>]*>(.*?)</li>", placeholders, re.S | re.I)
            if not (e.draft_mode and e.placeholder_quotes):
                issues.append(Issue("critical", "placeholder_not_allowed", "placeholder quotes are switched off"))
            elif not items or any(not html_to_text(i).startswith(PLACEHOLDER_MARK) for i in items):
                issues.append(
                    Issue(
                        "critical",
                        "placeholder_unmarked",
                        f"every placeholder quote must start with {PLACEHOLDER_MARK}",
                    )
                )
        words = word_count(body_html)
        low, high = e.min_words * (1 - e.word_count_tolerance), e.max_words * (1 + e.word_count_tolerance)
        if not low <= words <= high:
            issues.append(Issue("major", "word_count", f"body has {words} words; required {e.min_words}-{e.max_words}"))
        if len(content.title) > e.max_title_length:
            issues.append(Issue("major", "title_length", f"title longer than {e.max_title_length} characters"))
        if not content.teaser.strip():
            issues.append(Issue("major", "teaser_missing", "teaser/introduction is empty"))
        if not e.meta_description_min <= len(content.meta_description) <= e.meta_description_max:
            issues.append(
                Issue(
                    "minor",
                    "meta_description_length",
                    f"meta description has {len(content.meta_description)} characters",
                )
            )

        full_text = normalise(f"{content.title} {content.teaser} {html_to_text(content.body_html)}")
        for term in e.forbidden_terms:
            if normalise(term) and re.search(rf"\b{re.escape(normalise(term))}\b", full_text):
                issues.append(Issue("critical", "forbidden_term", f"contains forbidden term '{term}'"))

        research_urls = {s.url.rstrip("/") for s in research.sources}
        allowed = research_urls | {u.rstrip("/") for u in allowed_links}
        for source in content.sources:
            if source.url.rstrip("/") not in allowed:
                issues.append(
                    Issue("critical", "unknown_source", f"source {source.url} is not part of the research material")
                )
        for link in extract_links(content.body_html):
            if link.rstrip("/") not in allowed:
                issues.append(Issue("critical", "unknown_link", f"link {link} is not part of the research material"))
        for claim in content.claims:
            if claim.source_url.rstrip("/") not in research_urls and claim.source_url.rstrip("/") not in allowed:
                issues.append(
                    Issue("critical", "unsupported_claim", f"claim '{claim.claim[:80]}' cites an unknown source")
                )
            elif research.documents and not research.evidence_found(claim.source_url, claim.evidence):
                issues.append(
                    Issue(
                        "critical",
                        "evidence_not_found",
                        f"evidence for claim '{claim.claim[:80]}' was not found verbatim in the source",
                    )
                )

        research_text = normalise(" ".join(d.text for d in research.documents))
        body_text = html_to_text(body_html) + " " + content.teaser
        for match in _QUOTE_RE.finditer(body_text):
            quote = normalise(match.group("q"))
            if len(quote.split()) >= 6 and quote not in research_text:
                issues.append(
                    Issue(
                        "critical" if e.check_quotes else "minor",
                        "unsupported_quote",
                        f"quotation '{match.group('q')[:80]}' does not appear in the research material",
                    )
                )
        for match in _PERCENT_RE.finditer(body_text):
            number = normalise(match.group(1))  # "12,5" -> "12 5", same as research_text
            if not research.documents or not re.search(rf"\b{re.escape(number)}\b", research_text):
                issues.append(
                    Issue(
                        "critical",
                        "unsupported_statistic",
                        f"figure '{match.group(0)}' is not backed by the research material",
                    )
                )
        if (
            content.article_type == "factual"
            and e.require_sources_for_factual
            and not (content.sources and research.documents)
        ):
            issues.append(
                Issue("critical", "factual_without_sources", "factual article without approved research sources")
            )

        body_plain = html_to_text(content.body_html)
        for other in existing:
            if other.id == self_id or not other.content or other.status == ArticleStatus.REJECTED:
                continue
            ts = title_similarity(content.title, other.content.title)
            if ts >= e.duplicate_title_similarity:
                issues.append(
                    Issue(
                        "critical",
                        "duplicate_title",
                        f"title is too similar to '{other.content.title}' ({other.id}, {ts:.2f})",
                    )
                )
                continue
            cs = jaccard(body_plain, html_to_text(other.content.body_html))
            if cs >= e.duplicate_content_similarity:
                issues.append(
                    Issue("critical", "duplicate_content", f"content overlaps with article {other.id} ({cs:.2f})")
                )
        return issues

    # ------------------------------------------------------------------ LLM review
    def system_prompt(self) -> str:
        criteria = ", ".join(f'"{c}": 0-10' for c in self.settings.editorial.criteria_weights)
        draft = DRAFT_MODE_REVIEW if self.settings.editorial.draft_mode else ""
        if not self.settings.editorial.check_quotes:
            draft = draft.replace("invented quotes and ", "") + QUOTES_UNCHECKED_REVIEW
        if self.settings.editorial.draft_mode and self.settings.editorial.placeholder_quotes:
            draft += PLACEHOLDER_QUOTES_REVIEW
        return (
            f"{self.rubric}\n\nEDITORIAL INSTRUCTIONS GIVEN TO THE WRITER:\n<<<\n{self.editorial_prompt}\n>>>\n"
            + REVIEW_FORMAT.replace("{CRITERIA}", "{" + criteria + "}")
            + draft
        )

    def weighted_score(self, scores: dict[str, float]) -> float:
        weights = self.settings.editorial.criteria_weights
        total = sum(weights.values())
        return round(sum(weights[c] * float(scores.get(c, 0)) for c in weights) / total, 2) if total else 0.0

    def review(
        self,
        content: ArticleContent,
        research: ResearchBundle,
        iteration: int,
        existing: Iterable[Article] = (),
        allowed_links: Iterable[str] = (),
        self_id: str = "",
    ) -> ReviewResult:
        e = self.settings.editorial
        automated = self.automated_checks(content, research, existing, allowed_links, self_id)
        user = (
            f"ARTICLE TO REVIEW (iteration {iteration}):\n"
            f"{json.dumps(to_dict(content), ensure_ascii=False)}\n\n"
            f"RESEARCH MATERIAL AVAILABLE TO THE WRITER:\n{research.as_prompt()}\n\n"
            "AUTOMATED CHECK RESULTS (already verified mechanically):\n"
            + ("\n".join(f"- [{i.severity}] {i.message}" for i in automated) or "- none")
        )
        data = self.llm.complete_json(
            system=self.system_prompt(),
            user=user,
            model=self.settings.llm.editor_model,
            temperature=self.settings.llm.editor_temperature,
            purpose="review",
        )
        raw_scores = data.get("scores") or {}
        scores: dict[str, float] = {}
        for criterion in e.criteria_weights:
            try:
                scores[criterion] = max(0.0, min(10.0, float(raw_scores.get(criterion, 0))))
            except (TypeError, ValueError):
                scores[criterion] = 0.0
        critical = [
            Issue("critical", f"editor_{c.get('category', 'quality')}", str(c.get("description", "")))
            for c in (data.get("critical_issues") or [])
            if isinstance(c, dict)
        ]
        todos = [str(t) for t in (data.get("author_todos") or []) if str(t).strip()][:10]
        if split_placeholder_section(content.body_html)[1]:
            todos.insert(0, f"{PLACEHOLDER_HEADING} am Textende durch echte Zitate ersetzen oder löschen")
        if not e.check_quotes:
            quote_findings = [i for i in critical if _QUOTE_FINDING_RE.search(i.message)]
            critical = [i for i in critical if i not in quote_findings]
            todos += [f"Zitat prüfen: {i.message}" for i in quote_findings]
            todos += [f"Zitat prüfen: {i.message}" for i in automated if i.code == "unsupported_quote"]
        weighted = self.weighted_score(scores)
        blocking_auto = [i for i in automated if i.severity in {"critical", "major"}]
        passed = (
            weighted >= e.quality_threshold
            and all(v >= e.min_criterion_score for v in scores.values())
            and scores.get("factual_accuracy", 10) >= e.min_factual_score
            and not critical
            and not blocking_auto
        )
        feedback = [f for f in (data.get("feedback") or []) if isinstance(f, dict)]
        result = ReviewResult(
            iteration=iteration,
            scores=scores,
            weighted_score=weighted,
            passed=passed,
            critical_issues=critical,
            automated_issues=automated,
            feedback=feedback,
            summary=str(data.get("summary", "")),
            model=self.settings.llm.editor_model,
            author_todos=todos,
        )
        log.info(
            "review iteration %d: score=%.2f passed=%s critical=%d automated=%d",
            iteration,
            weighted,
            passed,
            len(critical),
            len(blocking_auto),
        )
        return result

    def assess_update(self, content: ArticleContent, age_days: int, research: ResearchBundle) -> dict:
        user = (
            f"The article below was published {age_days} days ago. Decide whether it needs an update "
            "(outdated information, weak structure, SEO problems, broken promises in the headline). Respond with JSON "
            '{"needs_update": true|false, "reasons": ["..."], "score": 0-10}.\n\n'
            f"ARTICLE:\n{json.dumps(to_dict(content), ensure_ascii=False)}\n\n"
            f"CURRENT RESEARCH MATERIAL:\n{research.as_prompt()}"
        )
        data = self.llm.complete_json(
            system=self.rubric,
            user=user,
            model=self.settings.llm.editor_model,
            temperature=self.settings.llm.editor_temperature,
            purpose="update_assessment",
        )
        return {
            "needs_update": bool(data.get("needs_update")),
            "reasons": [str(r) for r in data.get("reasons") or []],
            "score": float(data.get("score") or 0),
        }


def rejection_reasons(review: ReviewResult, settings: Settings) -> list[str]:
    reasons = [f"[{i.severity}] {i.message}" for i in review.critical_issues]
    reasons += [f"[{i.severity}] {i.message}" for i in review.automated_issues if i.severity in {"critical", "major"}]
    if review.weighted_score < settings.editorial.quality_threshold:
        reasons.append(f"quality score {review.weighted_score} below threshold {settings.editorial.quality_threshold}")
    low = [f"{k}={v}" for k, v in review.scores.items() if v < settings.editorial.min_criterion_score]
    if low:
        reasons.append("criteria below minimum: " + ", ".join(low))
    return reasons or ["did not pass editorial review"]


class EditorialLoop:
    """Writer → Editor → Revision → Final review, until the threshold or the cycle limit is reached."""

    def __init__(self, settings: Settings, generator: ContentGenerator, reviewer: EditorialReviewer):
        self.settings = settings
        self.generator = generator
        self.reviewer = reviewer

    def run(
        self, article: Article, research: ResearchBundle, existing: list[Article], allowed_links: Iterable[str] = ()
    ) -> Article:
        if article.content is None:
            raise ValueError("article has no content to review")
        content = article.content
        max_cycles = self.settings.editorial.max_revision_cycles
        start = len(article.reviews)
        for cycle in range(max_cycles + 1):
            iteration = start + cycle
            review = self.reviewer.review(content, research, iteration, existing, allowed_links, article.id)
            article.reviews.append(review)
            if review.passed:
                article.status = ArticleStatus.APPROVED
                article.rejected_reasons = []
                return article
            if cycle == max_cycles:
                break
            try:
                content = self.generator.revise(content, review, research)
            except LLMRequestTooLargeError as exc:
                log.warning("article %s: revision too large for the provider limit (%s)", article.id, exc)
                article.status = ArticleStatus.REJECTED
                article.rejected_reasons = [
                    *rejection_reasons(review, self.settings),
                    "revision request exceeds the provider's per-minute token limit",
                ]
                return article
            article.content = content
            article.revisions.append(
                Revision(iteration=iteration + 1, content=content, note=f"revision after review {iteration}")
            )
        article.status = ArticleStatus.REJECTED
        article.rejected_reasons = rejection_reasons(article.reviews[-1], self.settings)
        log.warning("article %s rejected: %s", article.id, "; ".join(article.rejected_reasons))
        return article
