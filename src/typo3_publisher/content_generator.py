"""Writer: produces and revises articles according to config/editorial_prompt.md."""

from __future__ import annotations

import json
import re
from typing import Any

from .config import Settings, TopicConfig, read_text
from .html_utils import sanitize_html
from .llm import LLMClient, LLMError
from .logging_utils import get_logger
from .models import ArticleContent, Claim, ReviewResult, Source, slugify
from .research import ResearchBundle
from .serialization import to_dict

log = get_logger("writer")

# Rules that are enforced regardless of what config/editorial_prompt.md says.
NON_NEGOTIABLE_RULES = """
NON-NEGOTIABLE RULES (these override any other instruction):
1. Never invent facts, statistics, dates, names, studies, quotations or sources.
2. Every specific factual claim (numbers, dates, named organisations or people, study results) must be
   listed in "claims" with the exact URL of a provided research source and an "evidence" field that
   copies a short passage (at least 6 words) VERBATIM from that source.
3. If no research material is provided, write an evergreen, explanatory article that contains no
   specific statistics, no quotations and no claims about recent events; set "article_type" to
   "evergreen" and leave "claims" and "sources" empty.
4. Only cite URLs that appear in the research material. Never add links that are not in the research
   material.
5. Do not use direct quotations unless the quotation appears verbatim in the research material.
6. Your own knowledge is not a source. Do not describe the article as fact-checked or verified.
7. Never invent personal experiences, scenes, observations, surveys or interviews and present them as
   real (no "I saw in the canteen", no made-up classmates). Real people's statements only from the
   research material.
"""

ONE_STORY = (
    "Build the article around ONE concrete news item from the research material. Use another source only if it "
    "is about the same subject; never combine unrelated stories in one article or one headline.\n\n"
)

DRAFT_MODE_RULES = """
DRAFT MODE: this article is a draft. A human author will read it, add their own reporting (own
interviews, survey, photos, personal experience), real quotes and the final headline, and publish it.
- Write a complete, publishable factual text based on the research material; deliver exactly one title.
- Do not put placeholders, notes to the author or questions into the article text.
- Do not imitate own reporting: no invented scenes, first-person experiences or quotes.
- No direct quotations at all, no quotation marks around anyone's words: report what people said in
  indirect speech with attribution ("laut X", "sagte X dem *Standard*"). The author adds real quotes.
"""

PLACEHOLDER_QUOTES_RULES = """
- Exception to rules 1, 5 and 7, only at the very end of body_html: you may close with a section
  <h2>Platzhalter-Zitate (erfunden – vor dem Veröffentlichen durch echte Zitate ersetzen oder löschen)</h2>
  followed by a <ul> with 2-4 <li> items that show what quotes from the author's own interviews could
  sound like. Every item starts with "[PLATZHALTER]" and is attributed only to an invented, ordinary
  young person described by first name, age and role (e.g. "Lena, 16, Schülerin aus Wien"). Never
  attribute a placeholder to a real or named public person, an expert, teacher, school head, official,
  politician, organisation or any other professional. Nothing after this section.
- The article text above this section contains no quotations.
"""

OUTPUT_FORMAT = """
Respond with a single JSON object and nothing else, using exactly these keys:
{
  "title": "headline (at most {max_title} characters)",
  "teaser": "introductory paragraph / teaser (plain text, 1-3 sentences)",
  "body_html": "article body as HTML using only <h2>, <h3>, <p>, <ul>, <ol>, <li>, <strong>, <em>, <a href>, <blockquote>",
  "seo_title": "SEO title (max 60 characters)",
  "meta_description": "meta description (120-160 characters)",
  "keywords": ["keyword", "..."],
  "slug": "url-slug",
  "image_keywords": ["2-5 short ENGLISH search terms describing a suitable header photo without people or brands"],
  "article_type": "evergreen | factual | opinion",
  "claims": [{"claim": "...", "source_url": "...", "evidence": "verbatim passage from the source"}],
  "sources": [{"title": "...", "url": "..."}]
}
Do not repeat the title or teaser inside body_html.
"""


def render_template(template: str, variables: dict[str, Any]) -> str:
    def repl(match: re.Match) -> str:
        key = match.group(1).strip()
        if key in variables:
            return str(variables[key])
        log.warning("unknown placeholder {{%s}} in editorial prompt", key)
        return match.group(0)

    return re.sub(r"\{\{\s*([a-zA-Z0-9_]+)\s*\}\}", repl, template)


def content_from_llm(data: dict[str, Any]) -> ArticleContent:
    def text(key: str, required: bool = True) -> str:
        value = data.get(key, "")
        if not isinstance(value, str):
            value = str(value)
        if required and not value.strip():
            raise LLMError(f"writer response is missing '{key}'")
        return value.strip()

    def str_list(key: str) -> list[str]:
        value = data.get(key) or []
        if isinstance(value, str):
            value = [v.strip() for v in value.split(",")]
        return [str(v).strip() for v in value if str(v).strip()]

    claims = []
    for c in data.get("claims") or []:
        if isinstance(c, dict) and c.get("claim"):
            claims.append(
                Claim(
                    claim=str(c["claim"]), source_url=str(c.get("source_url", "")), evidence=str(c.get("evidence", ""))
                )
            )
    sources = []
    for s in data.get("sources") or []:
        if isinstance(s, dict) and s.get("url"):
            sources.append(Source(title=str(s.get("title", "")), url=str(s["url"])))
    title = text("title")
    article_type = text("article_type", required=False).lower() or "evergreen"
    if article_type not in {"evergreen", "factual", "opinion"}:
        article_type = "factual"
    return ArticleContent(
        title=title,
        teaser=text("teaser"),
        body_html=sanitize_html(text("body_html")),
        seo_title=text("seo_title", required=False) or title[:60],
        meta_description=text("meta_description", required=False),
        keywords=str_list("keywords"),
        slug=slugify(text("slug", required=False) or title),
        image_keywords=str_list("image_keywords"),
        article_type=article_type,
        claims=claims,
        sources=sources,
    )


class ContentGenerator:
    def __init__(self, settings: Settings, llm: LLMClient, prompt_template: str):
        self.settings = settings
        self.llm = llm
        self.prompt_template = prompt_template

    def system_prompt(self, compact: bool = False) -> str:
        """``compact``: the short review checklist instead of the full editorial prompt (revisions; the
        draft already follows the full prompt, and Groq's free tier allows only 8000 tokens per request)."""
        e = self.settings.editorial
        template = self.prompt_template
        if compact and e.review_checklist_file:
            template = read_text(self.settings, e.review_checklist_file)
        rendered = render_template(
            template,
            {
                "language": self.settings.website.language,
                "min_words": e.min_words,
                "max_words": e.max_words,
                "site_name": self.settings.website.name or self.settings.website.base_url,
            },
        )
        draft = DRAFT_MODE_RULES if e.draft_mode else ""
        if e.draft_mode and e.placeholder_quotes:
            draft += PLACEHOLDER_QUOTES_RULES
        output = OUTPUT_FORMAT.replace("{max_title}", str(e.max_title_length))
        return f"{rendered}\n\n{NON_NEGOTIABLE_RULES}{draft}\n{output}"

    def _complete(self, user: str, purpose: str, compact: bool = False) -> ArticleContent:
        data = self.llm.complete_json(
            system=self.system_prompt(compact),
            user=user,
            model=self.settings.llm.writer_model,
            temperature=self.settings.llm.writer_temperature,
            purpose=purpose,
        )
        return content_from_llm(data)

    def generate(
        self,
        topic: TopicConfig | None,
        topic_name: str,
        idea: str,
        notes: str,
        research: ResearchBundle,
        recent_titles: list[str],
    ) -> ArticleContent:
        avoid = "\n".join(f"- {t}" for t in recent_titles) or "- (none)"
        user = (
            f"TOPIC: {topic_name}\n"
            f"TOPIC DESCRIPTION: {topic.description if topic else ''}\n"
            f"TOPIC KEYWORDS: {', '.join(topic.keywords) if topic else ''}\n"
            f"ARTICLE IDEA: {idea or '(choose a fresh, specific angle within the topic)'}\n"
            f"EDITOR NOTES: {notes or '(none)'}\n"
            f"LENGTH: {self._length()}\n\n"
            f"ALREADY PUBLISHED OR PLANNED - choose a clearly different subject and angle:\n{avoid}\n\n"
            f"RESEARCH MATERIAL:\n{research.as_prompt()}\n\n"
            + (ONE_STORY if research.documents else "")
            + "Write the article now."
        )
        return self._complete(user, "write")

    def _length(self) -> str:
        e = self.settings.editorial
        target = (e.min_words + e.max_words) // 2
        return (
            f"body_html must contain at least {e.min_words} and at most {e.max_words} words (aim for about {target} "
            "words in several sections with subheadings). Shorter articles are rejected automatically."
        )

    def revise(self, content: ArticleContent, review: ReviewResult, research: ResearchBundle) -> ArticleContent:
        feedback = {
            "scores": review.scores,
            "critical_issues": [to_dict(i) for i in review.critical_issues],
            "automated_check_failures": [to_dict(i) for i in review.automated_issues],
            "feedback": review.feedback,
            "summary": review.summary,
        }
        user = (
            "Revise the article below so that it addresses ALL editor feedback. Remove any claim that cannot be "
            "supported verbatim by the research material instead of inventing support for it. Return the complete "
            f"revised article in the required JSON format.\nLENGTH: {self._length()}\n\n"
            f"CURRENT ARTICLE:\n{json.dumps(to_dict(content), ensure_ascii=False)}\n\n"
            f"EDITOR FEEDBACK:\n{json.dumps(feedback, ensure_ascii=False)}\n\n"
            f"RESEARCH MATERIAL:\n{research.as_prompt()}"
        )
        return self._complete(user, "revise", compact=True)

    def improve_existing(
        self, content: ArticleContent, reasons: list[str], research: ResearchBundle, preserve_links: list[str]
    ) -> ArticleContent:
        links = "\n".join(f"- {u}" for u in preserve_links) or "- (none)"
        user = (
            "Update the already published article below. Keep everything that is still correct, keep the existing "
            "headline unless it is misleading, keep every existing verified claim with its source, and keep all of "
            "the following links in the body:\n"
            f"{links}\n\nREASONS FOR THE UPDATE:\n"
            + "\n".join(f"- {r}" for r in reasons)
            + f"\n\nPUBLISHED ARTICLE:\n{json.dumps(to_dict(content), ensure_ascii=False)}\n\n"
            f"RESEARCH MATERIAL:\n{research.as_prompt()}\n\nReturn the complete updated article in the JSON format."
        )
        return self._complete(user, "update")
