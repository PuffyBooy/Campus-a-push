"""Content generation, deterministic editorial checks and the review/revision loop."""

import pytest

from tests.conftest import make_article, make_content
from typo3_publisher.content_generator import ContentGenerator, content_from_llm, render_template
from typo3_publisher.editorial_reviewer import EditorialLoop, EditorialReviewer
from typo3_publisher.llm import FakeLLM, LLMError, parse_json_object
from typo3_publisher.models import ArticleStatus, Claim, Source
from typo3_publisher.research import ResearchBundle, ResearchDocument, parse_feed
from typo3_publisher.serialization import to_dict

SOURCE_URL = "https://www.statistik.at/news/1"
SOURCE_TEXT = (
    "Im Jahr 2025 nutzten 87 Prozent der Studierenden in Österreich täglich ein Smartphone für das "
    "Studium, wie eine Erhebung unter 4000 Personen ergab. Die Nutzung stieg damit deutlich an."
)


def research() -> ResearchBundle:
    return ResearchBundle([ResearchDocument(Source(title="Erhebung", url=SOURCE_URL), SOURCE_TEXT)])


def codes(issues):
    return {i.code for i in issues}


# ------------------------------------------------------------------ writer


def test_template_placeholders():
    assert (
        render_template("{{language}} {{ min_words }} {{unknown}}", {"language": "de", "min_words": 5})
        == "de 5 {{unknown}}"
    )


def test_content_from_llm_sanitises_and_slugifies():
    data = to_dict(make_content(title="Größer & schneller: Ein Überblick"))
    data["slug"] = ""
    data["body_html"] = '<p onclick="x()">Hi <script>alert(1)</script><a href="javascript:x">l</a></p><img src=x>'
    c = content_from_llm(data)
    assert c.slug == "groesser-schneller-ein-ueberblick"
    assert "script" not in c.body_html and "onclick" not in c.body_html and "<img" not in c.body_html
    assert c.body_html == "<p>Hi l</p>"
    with pytest.raises(LLMError):
        content_from_llm({"teaser": "x", "body_html": "<p>x</p>"})


def test_parse_json_object_variants():
    assert parse_json_object('```json\n{"a": 1}\n```') == {"a": 1}
    assert parse_json_object('Sure! {"a": 2} hope that helps') == {"a": 2}
    with pytest.raises(LLMError):
        parse_json_object("no json")


def test_generator_prompt_contains_rules_research_and_avoid_list(settings):
    llm = FakeLLM()
    gen = ContentGenerator(settings, llm, "Write in {{language}} for {{site_name}}.")
    content = gen.generate(None, "Technology", "Passkeys", "", research(), ["Old title"])
    call = llm.calls[0]
    assert "Write in de" in call["system"] and "NON-NEGOTIABLE" in call["system"]
    assert SOURCE_URL in call["user"] and "Old title" in call["user"]
    assert content.title.startswith("[TEST]")


# ------------------------------------------------------------------ automated checks


@pytest.fixture
def reviewer(settings):
    return EditorialReviewer(settings, FakeLLM(), "rubric", "instructions")


def test_clean_evergreen_article_has_no_blocking_issues(reviewer):
    issues = reviewer.automated_checks(make_content(), ResearchBundle())
    assert not [i for i in issues if i.severity in {"critical", "major"}], issues


def test_invented_source_link_quote_and_statistic_are_critical(reviewer):
    c = make_content()
    c.sources = [Source(title="Fake", url="https://made-up.example/study")]
    c.body_html += (
        '<p>Laut <a href="https://made-up.example/study">Studie</a> sind es 42 Prozent. '
        'Ein Experte sagte: "Das ist die wichtigste Entwicklung des ganzen Jahrzehnts für alle."</p>'
    )
    found = codes(reviewer.automated_checks(c, ResearchBundle()))
    assert {"unknown_source", "unknown_link", "unsupported_quote", "unsupported_statistic"} <= found


def test_claim_evidence_must_appear_verbatim(reviewer):
    good = make_content(article_type="factual", sources=[Source(title="Erhebung", url=SOURCE_URL)])
    good.claims = [
        Claim(
            "87 % nutzen täglich ein Smartphone",
            SOURCE_URL,
            "nutzten 87 Prozent der Studierenden in Österreich täglich ein Smartphone",
        )
    ]
    good.body_html += "<p>87 Prozent der Studierenden nutzen täglich ein Smartphone.</p>"
    assert not codes(reviewer.automated_checks(good, research())) & {
        "evidence_not_found",
        "unsupported_statistic",
        "unsupported_claim",
    }
    bad = make_content(article_type="factual", sources=[Source(title="Erhebung", url=SOURCE_URL)])
    bad.claims = [Claim("90 % nutzen", SOURCE_URL, "nutzten 90 Prozent der Studierenden in Österreich täglich")]
    bad.body_html += "<p>90 Prozent der Studierenden.</p>"
    found = codes(reviewer.automated_checks(bad, research()))
    assert {"evidence_not_found", "unsupported_statistic"} <= found


def test_factual_article_without_research_is_critical(reviewer):
    assert "factual_without_sources" in codes(
        reviewer.automated_checks(make_content(article_type="factual"), ResearchBundle())
    )


def test_duplicates_and_forbidden_terms(reviewer, settings):
    settings.editorial.forbidden_terms = ["Kryptowährung"]
    existing = [make_article("old", content=make_content(title="Passkeys einfach erklärt!"))]
    c = make_content(teaser="Alles über Kryptowährung.")
    found = codes(reviewer.automated_checks(c, ResearchBundle(), existing))
    assert {"duplicate_title", "forbidden_term"} <= found
    other = make_content(title="Ein völlig anderes Thema")
    assert "duplicate_content" in codes(reviewer.automated_checks(other, ResearchBundle(), existing))


def test_word_count_limits(reviewer):
    assert "word_count" in codes(reviewer.automated_checks(make_content(words=100), ResearchBundle()))


# ------------------------------------------------------------------ review loop


def scripted(scores_per_review, settings, critical_on=()):
    """FakeLLM whose n-th review gives every criterion ``scores_per_review[n]``."""
    review_no = iter(range(100))

    def respond(purpose, system, user):
        if purpose == "review":
            n = next(review_no)
            crit = [{"category": "factual", "description": "unsupported"}] if n in critical_on else []
            return {
                "scores": dict.fromkeys(settings.editorial.criteria_weights, scores_per_review[n]),
                "critical_issues": crit,
                "feedback": [{"area": "seo", "issue": "x", "suggestion": "y"}],
            }
        return to_dict(make_content(title=f"Revision {len(user)}"))

    return FakeLLM(respond)


def build_loop(settings, llm):
    gen = ContentGenerator(settings, llm, "prompt")
    return EditorialLoop(settings, gen, EditorialReviewer(settings, llm, "rubric", "prompt"))


def test_loop_revises_until_threshold(settings):
    llm = scripted([6, 7, 9], settings)
    article = make_article()
    build_loop(settings, llm).run(article, ResearchBundle(), [])
    assert article.status == ArticleStatus.APPROVED
    assert [r.passed for r in article.reviews] == [False, False, True]
    assert len(article.revisions) == 2
    revise_calls = [c for c in llm.calls if c["purpose"] == "revise"]
    assert len(revise_calls) == 2 and '"suggestion": "y"' in revise_calls[0]["user"]


def test_loop_rejects_after_max_cycles(settings):
    settings.editorial.max_revision_cycles = 2
    article = make_article()
    build_loop(settings, scripted([5, 5, 5, 5], settings)).run(article, ResearchBundle(), [])
    assert article.status == ArticleStatus.REJECTED
    assert len(article.reviews) == 3
    assert any("below threshold" in r for r in article.rejected_reasons)


def test_critical_issue_blocks_even_with_high_scores(settings):
    settings.editorial.max_revision_cycles = 0
    article = make_article()
    build_loop(settings, scripted([10], settings, critical_on={0})).run(article, ResearchBundle(), [])
    assert article.status == ArticleStatus.REJECTED
    assert any("unsupported" in r for r in article.rejected_reasons)


def test_weighted_score(settings, reviewer):
    scores = dict.fromkeys(settings.editorial.criteria_weights, 8.0)
    scores["factual_accuracy"] = 4.0
    weights = settings.editorial.criteria_weights
    expected = round((sum(weights.values()) * 8 - weights["factual_accuracy"] * 4) / sum(weights.values()), 2)
    assert reviewer.weighted_score(scores) == expected


def test_parse_rss_and_atom():
    rss = (
        "<rss><channel><item><title>A</title><link>https://a.example/1</link><description>&lt;p&gt;x&lt;/p&gt;"
        "</description></item></channel></rss>"
    )
    atom = (
        '<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>B</title>'
        '<link href="https://b.example/2"/><summary>y</summary></entry></feed>'
    )
    assert parse_feed(rss)[0]["url"] == "https://a.example/1"
    assert parse_feed(atom)[0]["url"] == "https://b.example/2"


def test_parse_rss1_rdf_feed():
    rdf = (
        '<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#" xmlns="http://purl.org/rss/1.0/" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/"><item rdf:about="https://orf.at/stories/1/">'
        "<title>C</title><link>https://orf.at/stories/1/</link><dc:date>2026-10-21T20:10:44+02:00</dc:date>"
        "</item></rdf:RDF>"
    )
    [item] = parse_feed(rdf)
    assert item["url"] == "https://orf.at/stories/1/" and item["published"].startswith("2026-10-21")


def test_article_text_drops_navigation_lines():
    from typo3_publisher.html_utils import ParsedPage
    from typo3_publisher.research import article_text

    page = ParsedPage(
        "<nav><a>Zum Inhalt</a><a>Sport</a><a>Wetter</a></nav><h1>Kurz</h1>"
        "<p>Eine neue Studie zeigt, dass Jugendliche in Österreich deutlich weniger schlafen als empfohlen.</p>"
    )
    assert article_text(page) == (
        "Eine neue Studie zeigt, dass Jugendliche in Österreich deutlich weniger schlafen als empfohlen."
    )


# ------------------------------------------------------------------ draft mode


def test_draft_mode_prompts_and_author_todos(settings):
    settings.editorial.draft_mode = True
    settings.editorial.review_checklist_file = "config/review_checklist.md"
    gen_llm = FakeLLM()
    ContentGenerator(settings, gen_llm, "Prompt").generate(None, "Wissenschaft", "", "", research(), [])
    assert "DRAFT MODE" in gen_llm.calls[0]["system"]
    assert "invent personal experiences" in gen_llm.calls[0]["system"]
    assert "at least 600" in gen_llm.calls[0]["user"]

    def respond(purpose, system, user):
        assert "DRAFT MODE" in system and "author_todos" in system
        assert "Prüfliste für campus a college" in system and "FULL WRITER PROMPT" not in system
        scores = dict.fromkeys(settings.editorial.criteria_weights, 9.0)
        return {"scores": scores, "critical_issues": [], "author_todos": ["Eine Schülerin befragen", " "]}

    reviewer = EditorialReviewer(settings, FakeLLM(respond), "rubric", "FULL WRITER PROMPT")
    result = reviewer.review(make_content(), research(), 0)
    assert result.author_todos == ["Eine Schülerin befragen"]


def test_without_draft_mode_the_editor_gets_the_full_prompt(settings):
    settings.editorial.review_checklist_file = ""

    def respond(purpose, system, user):
        assert "FULL WRITER PROMPT" in system and "DRAFT MODE" not in system
        return {"scores": dict.fromkeys(settings.editorial.criteria_weights, 9.0), "critical_issues": []}

    EditorialReviewer(settings, FakeLLM(respond), "rubric", "FULL WRITER PROMPT").review(make_content(), research(), 0)


def test_live_configuration_matches_the_campus_form():
    """Topics map to Ressorts that exist in the campus a form; research uses approved feeds only."""
    from tests.conftest import REPO
    from typo3_publisher.config import load_settings, load_topics
    from typo3_publisher.html_utils import domain_allowed

    settings = load_settings(REPO / "config")
    resorts = {"Kultur", "Panorama", "Politik", "Sport", "Wirtschaft", "Wissenschaft"}
    for topic in load_topics(REPO / "config").topics:
        assert settings.campus.resort_by_topic.get(topic.name, settings.campus.default_resort) in resorts
    r = settings.research
    assert r.enabled and r.feeds
    assert all(domain_allowed(f, r.approved_domains) for f in r.feeds)
    assert settings.editorial.draft_mode and settings.editorial.review_checklist_file


def test_revision_uses_the_compact_checklist(settings):
    from typo3_publisher.models import ReviewResult

    settings.editorial.review_checklist_file = "config/review_checklist.md"
    llm = FakeLLM()
    gen = ContentGenerator(settings, llm, "FULL WRITER PROMPT")
    gen.generate(None, "Wissenschaft", "", "", research(), [])
    gen.revise(make_content(), ReviewResult(0, {}, 5.0, False), research())
    write, revise = llm.calls
    assert "FULL WRITER PROMPT" in write["system"]
    assert "FULL WRITER PROMPT" not in revise["system"] and "Prüfliste für campus a college" in revise["system"]
    assert "NON-NEGOTIABLE" in revise["system"]


def test_revision_too_large_for_the_provider_rejects_only_this_article(settings):
    from typo3_publisher.llm import LLMRequestTooLargeError

    def respond(purpose, system, user):
        if purpose == "revise":
            raise LLMRequestTooLargeError("Request too large: Limit 8000, Requested 8129")
        return {"scores": dict.fromkeys(settings.editorial.criteria_weights, 5.0), "critical_issues": []}

    llm = FakeLLM(respond)
    gen = ContentGenerator(settings, llm, "prompt")
    loop = EditorialLoop(settings, gen, EditorialReviewer(settings, llm, "rubric", "prompt"))
    article = loop.run(make_article(), research(), [])
    assert article.status == ArticleStatus.REJECTED
    assert any("per-minute token limit" in r for r in article.rejected_reasons)


def test_writer_builds_on_one_story_when_research_is_available(settings):
    llm = FakeLLM()
    gen = ContentGenerator(settings, llm, "prompt")
    gen.generate(None, "Politik", "", "", research(), [])
    gen.generate(None, "Politik", "", "", ResearchBundle(), [])
    with_sources, without = llm.calls
    assert "ONE concrete news item" in with_sources["user"]
    assert "ONE concrete news item" not in without["user"]


def test_writer_knows_the_title_limit_and_drafts_avoid_direct_quotes(settings):
    settings.editorial.draft_mode = True
    settings.editorial.max_title_length = 90
    llm = FakeLLM()
    ContentGenerator(settings, llm, "prompt").generate(None, "Politik", "", "", research(), [])
    system = llm.calls[0]["system"]
    assert "at most 90 characters" in system and "{max_title}" not in system
    assert "No direct quotations at all" in system


def test_unchecked_quotes_become_author_todos_instead_of_blocking(settings):
    settings.editorial.draft_mode = True
    settings.editorial.check_quotes = False
    c = make_content()
    c.body_html += '<p>Eine Schülerin sagte: "Ich kann bei dieser Hitze im Klassenzimmer nicht mehr denken."</p>'

    def respond(purpose, system, user):
        assert "QUOTES ARE NOT CHECKED" in system and "invented quotes" not in system
        scores = dict.fromkeys(settings.editorial.criteria_weights, 9.0)
        return {
            "scores": scores,
            "critical_issues": [{"category": "factual", "description": "Zitat von Lena ist erfunden."}],
        }

    reviewer = EditorialReviewer(settings, FakeLLM(respond), "rubric", "instructions")
    result = reviewer.review(c, ResearchBundle(), 0)
    assert result.passed and not result.critical_issues
    assert result.author_todos[0] == "Zitat prüfen: Zitat von Lena ist erfunden."
    assert result.author_todos[1].startswith("Zitat prüfen: quotation 'Ich kann bei dieser Hitze")
