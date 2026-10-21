# TYPO3 Publisher Bot

An autonomous publishing bot for an existing TYPO3 website. It plans topics, writes articles
with an LLM, runs them through an independent editorial review loop, finds a licence-checked
Pixabay image, and publishes through the **normal TYPO3 editor backend in a headless browser**
(Playwright). It then checks that the article really is publicly visible. GitHub Actions runs
everything on a schedule.

You don't need server, database, SSH, extension or API access. The bot logs in and clicks
like a human editor.

> **Verification status.** The code is complete and tested against a **mock** TYPO3 backend
> (real Chromium, real HTTP). It has **not** been run against your TYPO3 installation yet: every
> TYPO3 setup differs, and only discovery mode plus the live integration tests can prove that it
> works there. See [docs/VERIFICATION_STATUS.md](docs/VERIFICATION_STATUS.md).
> Publishing is **disabled by default** (`publishing.enabled: false`).

---

## Contents

1. [How it works](#how-it-works)
2. [Create the GitHub repository](#1-create-the-github-repository)
3. [Install dependencies](#2-install-dependencies)
4. [Configure GitHub Secrets](#3-configure-github-secrets)
5. [Customize the editorial prompt](#4-customize-the-editorial-prompt)
6. [Configure topics](#5-configure-topics)
7. [Discover the TYPO3 publishing workflow](#6-discover-the-typo3-publishing-workflow)
8. [Test login and article creation](#7-test-login-and-article-creation)
9. [Run a dry run](#8-run-a-dry-run)
10. [Enable scheduled publication](#9-enable-scheduled-publication)
11. [Monitor failures](#10-monitor-failures)
12. [Disable automatic publication](#11-disable-automatic-publication)
13. [Update existing articles](#12-update-existing-articles)
14. [Security](#security), [Reliability](#reliability-and-duplicate-prevention), [Limitations](#known-limitations)

---

## How it works

```
Slot due? (Europe/Vienna) ─► claim slot in state ─► resume failed publication / approved backlog / new article
                                                                     │
 Topic planner ─► optional research (approved feeds) ─► Writer (LLM) ─► Editor (separate LLM call + deterministic checks)
                                                                     ▲                │ below threshold
                                                                     └── Revision ◄───┘ (max N cycles, else rejected)
 Approved ─► Pixabay search + licence filters ─► Playwright: login ─► new record form ─► fill title/teaser/body/SEO/categories
          ─► Save (record hidden) ─► upload image + alt/attribution ─► Save ─► make visible ─► Save
          ─► fetch the PUBLIC page anonymously and compare title + content ─► registry: PUBLISHED + URL
```

| Module | Responsibility |
|---|---|
| `src/typo3_publisher/typo3_browser.py` | Login (MFA/CAPTCHA detection), backend frames, FormEngine fields, tabs, CKEditor, file upload, save, visibility, list search, diagnostics. Profiles: `NewsPublisher` (EXT:news) and `ContentElementPublisher` (page + text/media element) |
| `discovery.py` | Setup mode: maps and **tests** selectors on your backend, writes a report and a selector proposal |
| `content_generator.py` | Writer prompts from `config/editorial_prompt.md`, plus non-negotiable rules against fabrication |
| `editorial_reviewer.py` | Deterministic checks + LLM editor scores, write → review → revise loop |
| `research.py` | Optional research from approved RSS/Atom feeds; evidence snippets are checked verbatim |
| `image_finder.py` | Pixabay search, people/brand/size/orientation filters, relevance threshold, de-duplication, download; caption + description written from the actual image, copyright line |
| `verifier.py` | Anonymous public-page verification (title similarity + content overlap, retries) |
| `scheduler.py` / `topic_planner.py` | DST-correct slots, catch-up rules, weekly topic quotas, backlog, editorial calendar |
| `state_manager.py` | Article registry as JSON on a dedicated `bot-state` git branch, lease lock (compare-and-swap via fast-forward push) |
| `article_manager.py` | Orchestration, checkpoints, retries, update workflow, version history |
| `main.py` | CLI (`python -m typo3_publisher --help`) |

Repository layout:

```
.github/workflows/  ci.yml publish.yml review.yml update.yml setup.yml
.github/actions/setup-bot/            shared Python + Chromium setup
config/  settings.yaml topics.yaml editorial_prompt.md review_rubric.md typo3_selectors.yaml
src/typo3_publisher/                 the bot
tests/                               unit tests, browser tests against tests/mock_typo3, tests/integration (live)
data/                                local state (development only; Actions uses the bot-state branch)
docs/VERIFICATION_STATUS.md
```

---

## 1. Create the GitHub repository

1. Create a **private** repository (recommended: the run summaries and the state branch
   contain your unpublished drafts) and push this code to its default branch.
2. Under **Settings → Actions → General → Workflow permissions**, select *Read and write
   permissions*. The bot pushes its state to the `bot-state` branch. If branch rules protect all
   branches, exempt `bot-state`.
3. Failure issues use the label `bot-failure`; GitHub creates it on the first failure.

## 2. Install dependencies

Python 3.12+:

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt && pip install -e . --no-deps
python -m playwright install --with-deps chromium
cp .env.example .env        # fill in for local runs; never commit .env
pytest -q                   # unit tests + Chromium against the mock TYPO3 backend
```

In GitHub Actions the composite action `.github/actions/setup-bot` does the same thing, with caching.

## 3. Configure GitHub Secrets

**Settings → Secrets and variables → Actions → New repository secret**:

| Secret | Value |
|---|---|
| `TYPO3_BACKEND_URL` | Backend base URL including `/typo3`, e.g. `https://www.example.org/typo3` |
| `TYPO3_USERNAME` | A dedicated **editor** account (not admin), limited to the news storage folder / article pages and the upload folder |
| `TYPO3_PASSWORD` | Its password |
| `GROQ_API_KEY` | Groq API key (https://console.groq.com/keys), used with `llm.provider: groq`, the default |
| `OPENAI_API_KEY` | *only with `llm.provider: openai`*: OpenAI API key |
| `PIXABAY_API_KEY` | Pixabay API key (https://pixabay.com/api/docs/) |
| `WEBSITE_BASE_URL` | Public site URL, e.g. `https://www.example.org` |
| `NOTIFY_WEBHOOK_URL` | *optional* Slack/Teams/generic webhook |
| `DIAGNOSTICS_PASSPHRASE` | *optional* encrypts failure screenshots before upload (otherwise none are uploaded) |
| `TYPO3_STORAGE_STATE` | *optional, MFA sites only*, see [MFA or CAPTCHA](#sites-with-mfa-or-captcha) |

**LLM provider.** The bot calls an OpenAI-compatible chat-completions API. The default is
**Groq** (`llm.provider: groq`, key in `GROQ_API_KEY`) with these models:
`openai/gpt-oss-120b` for writing and reviewing, and `qwen/qwen3.8-27b` for image captions, because
it is the only Groq model that accepts images. That model is still a *preview* model, so check
https://console.groq.com/docs/models now and then and change the names in `config/settings.yaml` if
Groq retires one. If a vision model disappears, captions fall back to the tag-based texts.
To use OpenAI instead, set `llm.provider: openai`, OpenAI model names, and the `OPENAI_API_KEY` secret.

Repository **variables** (not secrets) control operation without code changes:
`BOT_PAUSED=true` (stop publishing), `BOT_PUBLISHING_ENABLED=true|false`, `BOT_UPDATES_ENABLED=true|false`.

**Least privilege for the TYPO3 account.** Grant it only: the module(s) it needs (List and/or
Page), DB mounts limited to the news storage folder (or the article parent page), the tables
`tx_news_domain_model_news`/`sys_file_reference` (or `pages`/`tt_content`), the needed fields, and
a file mount for the upload folder. Don't use an admin account.

## 4. Customize the editorial prompt

`config/editorial_prompt.md` is the complete writing brief: audience, voice, tone, style,
length, headline structure, introduction, sections, SEO rules, facts and sources, topics to
avoid, and language. Edit the Markdown; no code changes are needed. The placeholders
`{{language}}`, `{{min_words}}`, `{{max_words}}` and `{{site_name}}` are filled from
`config/settings.yaml`.

`config/review_rubric.md` tells the editor how to score. Thresholds, criterion weights, the cycle
limit, word counts and forbidden terms live under `editorial:` in `settings.yaml`.

**Facts.** Some rules are always enforced in code, whatever the prompt says:

- No invented facts, quotes or sources. Without research material the writer must produce
  evergreen explanations with no statistics, quotes or claims about recent events.
- With `research.enabled: true`, facts may only come from documents fetched from your
  `approved_domains` feeds. Every claim must cite one of those URLs and give an evidence snippet,
  and **the code checks that the snippet appears verbatim in the fetched source**.
- Links, sources, quotations and percentages that don't appear in the research material are
  **critical issues** that block publication, no matter what scores the LLM gives.
- The LLM editor is *not* independent fact verification. The deterministic checks only prove
  that a claim is grounded in an approved source. They don't prove the source is right.

### Image caption, description and copyright

For every selected Pixabay image the bot produces three texts:

| Text | How it is made | Default TYPO3 column |
|---|---|---|
| **Caption** (Bildtext) | A vision call: the downloaded image itself is sent to the model together with the article title. The caption starts with "Symbolbild:", because a stock photo never shows the people or events of the article | `description` of the image relation, followed by the copyright |
| **Description** (Bildbeschreibung) | Same call: a factual description of what is visible, used as alternative text for screen readers | `alternative` |
| **Copyright** | From the Pixabay uploader: `© {user} / Pixabay` (`images.attribution.template`) | appended to the caption (`images.attribution.target: caption`), or a line under the article (`body`), or both |

- `images.text.mode: tags` skips the model and builds caption and description from the Pixabay
  tags (`caption_fallback`, `description_fallback`).
- If the vision call fails, the tag-based texts are used. The article is never blocked by this step.
- Which column receives which text is set in `config/typo3_selectors.yaml` → `image_reference.fields`.
  Placeholders are `{caption}`, `{description}` and `{copyright}`. For example, add
  `title: "{caption}"` if your templates render the image title as the caption.
- The model in `images.text.model` (default: `llm.writer_model`) must accept image input.

## 5. Configure topics

`config/topics.yaml`:

```yaml
topics:
  - name: Technology
    priority: high          # high | medium | low (weight 3/2/1)
    articles_per_week: 3
    keywords: [software, cloud]
    categories: [Technik]   # TYPO3 category titles to select
backlog:                    # optional future ideas, each used once, before the rotation
  - title: "Passkeys statt Passwörter"
    topic: Technology
```

- The planner picks the topic with the largest unmet weekly quota (ISO week, Europe/Vienna),
  weighted by priority. Ties go to the least recently used topic. Once every quota is met, it
  publishes nothing more that week.
- Manual ideas: run `python -m typo3_publisher add-idea "Title" --topic Technology`, or set the
  `idea` input of the *Publish* / *Generate and review* workflows.
- Editorial calendar: `python -m typo3_publisher plan --days 14`.
- Duplicate avoidance: recent titles are given to the writer, and titles/content that are too
  similar to existing articles are rejected.

## 6. Discover the TYPO3 publishing workflow

TYPO3 installations differ, so map yours first:

1. Pick the profile in `config/settings.yaml` → `typo3.profile`:
   - `news`: EXT:news records (`tx_news_domain_model_news`), the most common setup;
   - `content_element`: one new page per article with a text/media content element.
2. In `config/typo3_selectors.yaml`, set `news.storage_pid` (the uid of the sysfolder that holds
   news, shown as `id=` in the backend URL) or `content_element.parent_pid`.
3. Run discovery, either locally or via **Actions → TYPO3 setup → action: discover**:

   ```bash
   python -m typo3_publisher discover            # read-only: login, menu, form inventory, selector tests
   python -m typo3_publisher discover --headed   # watch it in a browser window
   ```

   Discovery logs in, lists the module menu, opens the **new record** form (nothing is saved),
   inventories every field per tab (column, type, RTE/upload detection), and tests each configured
   field and the Save button against the live markup. It writes:
   - `artifacts/discovery/<run>/report.md` and `report.json`, plus private screenshots;
   - `config/typo3_selectors.discovered.yaml`, a copy of your selector file with
     `verified_fields`. A field only counts as `verified` if it actually matched.
4. If the report lists *Manual assistance needed* (for example a different column name for the
   body text, or a missing upload control), adjust `typo3_selectors.yaml` (field `name:` or an
   explicit `locator:`). The syntax is documented at the top of the file. Repeat until nothing is
   left.
5. Prove the save workflow: `discover --test-save [--test-upload]` creates a **hidden** test
   record, saves it, checks the uid, optionally uploads a tiny PNG, and deletes the record again.
   (Uploaded test files stay in fileadmin.) The proposal file is only marked `verified: true` after
   this succeeds.

## 7. Test login and article creation

```bash
python -m typo3_publisher check-login                         # or Actions → TYPO3 setup → check-login
TYPO3_LIVE_TESTS=1 pytest tests/integration -m live -s        # login + fill form, no save
TYPO3_LIVE_TESTS=1 TYPO3_LIVE_ALLOW_SAVE=1 pytest tests/integration -m live -s     # hidden record, deleted again
TYPO3_LIVE_TESTS=1 TYPO3_LIVE_ALLOW_SAVE=1 TYPO3_LIVE_ALLOW_PUBLISH=1 pytest tests/integration -m live -s
```

The last command publishes a test article, verifies its **public** page, and deletes it. It is
the only test that proves end-to-end publishing on your site. Live tests never run unless you set
these variables explicitly.

## 8. Run a dry run

```bash
python -m typo3_publisher run --mode full --dry-run              # real LLM + Pixabay, TYPO3 form filled but never saved
python -m typo3_publisher run --mode full --dry-run --fake-llm   # offline placeholder text, no API costs
```

or **Actions → Publish → Run workflow** with *dry run* ticked (the default for manual runs).
A dry run works on a throw-away copy of the state, uploads nothing to TYPO3, never clicks Save,
and leaves a screenshot of the filled form in `artifacts/diagnostics/`.

Other modes: `generate-only`, `review-only`, `prepare` (generate + review),
`publish-approved [--article-id ID]`, `scheduled`, `update`. Check the registry with
`python -m typo3_publisher status`.

## 9. Enable scheduled publication

1. Finish steps 6 and 7, including a successful live publish test.
2. Set `publishing.enabled: true` in `config/settings.yaml`, or the repository variable
   `BOT_PUBLISHING_ENABLED=true`.
3. Configure the slots (default Mon–Fri 08:00, 12:00, 17:00 Europe/Vienna):

   ```yaml
   publishing:
     timezone: Europe/Vienna
     slots:
       - days: [mon, tue, wed, thu, fri]
         times: ["08:00", "12:00", "17:00"]
     slot_grace_minutes: 150
     catch_up: latest_only
   ```

4. If you change the times, update the cron line in `.github/workflows/publish.yml` with
   `python -m typo3_publisher print-cron`.

**Why the cron fires so often.** GitHub cron is UTC-only, may start late, and sometimes runs
twice. The workflow wakes twice an hour around every slot, in both winter and summer time. The
bot then checks in **local Vienna time** whether a slot is due (between the slot time and the
grace period), **claims it in the state branch** before doing any work, and never serves a
claimed slot again. With `catch_up: latest_only`, a run that wakes up after several missed slots
publishes once, not once per missed slot. All bot workflows also share one concurrency group,
`typo3-publisher-state`.

Optional human approval: with `publishing.auto_publish: false`, articles are generated and
reviewed but wait for **Actions → Publish → mode publish-approved**.

GitHub disables scheduled workflows after 60 days without repository activity. Commits to
`bot-state` normally count as activity; if the schedule gets disabled, re-enable it in the
Actions tab.

## 10. Monitor failures

- **Run summary**: each run writes what it generated, approved, rejected, published (with URLs)
  and what failed to the job summary.
- **Issues**: failed runs open or update an issue labelled `bot-failure` with a link to the run.
- **Webhook**: set `NOTIFY_WEBHOOK_URL` for failure, rejection and publication messages.
- **Exit codes**: 1 failure, 2 configuration error, 3 human action needed (MFA/CAPTCHA, unclear
  state after a crash), 4 skipped because another run holds the lock.
- **Diagnostics**: on browser failures, screenshots, HTML dumps (redacted) and the console log go
  to `artifacts/diagnostics/`. In Actions they are uploaded **only encrypted**, and only if
  `DIAGNOSTICS_PASSPHRASE` is set. To decrypt:
  `openssl enc -d -aes-256-cbc -pbkdf2 -in diagnostics.tgz.enc -out d.tgz -pass pass:...`
- **State**: browse the `bot-state` branch. `articles/<id>.json` holds every draft, revision,
  review score, image licence, TYPO3 uid, public URL, error and version. `runs/` holds run
  summaries and `slots.json` the served slots. Locally: `python -m typo3_publisher status`.

## 11. Disable automatic publication

Any of the following works. The fastest is a repository variable, which needs no commit:

- set the repository variable `BOT_PAUSED=true`;
- set `publishing.enabled: false` (the default);
- disable the *Publish* workflow under **Actions → Publish → ⋯ → Disable workflow**.

Articles already approved stay in the registry and are published once you re-enable publishing.

## 12. Update existing articles

Updating published articles is off by default and only runs when you turn it on explicitly
(`updates.enabled: true` or variable `BOT_UPDATES_ENABLED=true`).
**Actions → Update published articles** (or `run --mode update [--article-id ID]`):

1. selects the article given, or the oldest published article whose last check is older than
   `min_age_days`;
2. asks the editor model whether an update is needed (`update_if_score_below`);
3. writes an improved draft that keeps verified claims and **all existing links**, and keeps the
   slug/URL;
4. runs the full editorial loop (the update is rejected if a link is lost or a check fails);
5. stores the previous content as a version (`versions`, up to `max_versions_kept`);
6. opens the **existing** record through its stored uid (`edit[...][uid]=edit`), never a new
   one, then saves, republishes and verifies the public page again.

---

## Security

- Credentials exist only as GitHub Actions secrets or a local `.env` file, which is git-ignored.
- Every log line passes through a redaction filter. Registered secret values, cookies (including
  `be_typo_user`), bearer tokens and `key=`/`token=` query parameters are masked. The bot never
  logs passwords or cookies.
- Playwright traces are not recorded, because they contain cookies. Screenshots and HTML dumps
  stay private: they are git-ignored and only uploaded encrypted.
- Use a least-privilege editor account (see step 3).
- The bot **does not try to bypass** CAPTCHA or MFA and never solves challenges. It detects them
  and stops with exit code 3.

### Sites with MFA or CAPTCHA

- **CAPTCHA on the backend login**: unattended login isn't possible. Ask the site administrator
  for a policy decision, for example an IP allow-list for the runner or a self-hosted runner.
  The bot will not work around it.
- **MFA**: if your policy allows it, a person can sign in interactively and hand the session to
  the bot:

  ```bash
  python -m typo3_publisher auth-export --output typo3.storage-state.json   # opens a browser; log in incl. MFA
  base64 -w0 typo3.storage-state.json | gh secret set TYPO3_STORAGE_STATE
  rm typo3.storage-state.json
  ```

  The session lasts only as long as the TYPO3 backend session timeout (often 8 hours) and is
  ended by logouts or IP checks. That makes it suitable for supervised runs, **not** for
  unattended scheduling. When it expires, runs stop with exit code 3. If your security policy
  forbids exporting sessions, don't use this.

## Reliability and duplicate prevention

- **Checkpoints.** The article state is committed to `bot-state` before every side effect:
  `STARTED` just before Save on a new record, `SAVED` with the TYPO3 uid, `PUBLISHED`, then
  `VERIFIED`. A run that crashes leaves a resumable record:
  - uid known: the bot reopens that record (`edit`) and continues; it never creates a new one;
  - `STARTED` without uid: the bot searches the List module for the exact title. If it finds the
    record it adopts it; if the search can't run it **stops and asks for manual reconciliation**
    (exit 3) rather than risk a duplicate.
- **Retries** use exponential backoff with jitter for LLM, HTTP, git and transient browser
  errors. Rejected logins are **not** retried, to avoid account lockouts. Failed publications are
  retried in later runs up to `max_publish_attempts`, without generating a new article.
- **Verification.** Saving doesn't count as publishing. The public page is fetched without
  cookies and has to show the expected title (similarity ≥ 0.85) and body (≥ 50 % of 5-word
  shingles). If it doesn't, the article is marked `publish_failed`, and the next run only
  re-verifies before it touches TYPO3 again.
- **Concurrency.** A GitHub concurrency group plus a lease lock in the state branch: acquiring it
  is a fast-forward push, so two runs can never both hold it.
- **Images.** Each Pixabay id and file hash is used only once.

## Known limitations

- Selector defaults target TYPO3 v10–v13 FormEngine and EXT:news. Custom backend skins, custom
  record types, workspaces, or approval workflows (for example *Workspaces → send to stage*)
  need extra `ui_steps` or selector changes. Discovery tells you what doesn't match.
- Rich text is filled through the page's own CKEditor instance (`setData`) inside the browser.
  This is client-side and needs no TYPO3 API. If you need pure keyboard input, set
  `rte_mode: type`, which loses formatting.
- The tag-based Pixabay filters can't detect every identifiable person or logo. Spot-check images,
  or set `images.required: false` and add images by hand.
- Public URL detection assumes the news detail URL pattern (`website.public_url.pattern`) or a
  listing page that links to the article; adjust these for your routing.
- Mocked tests are no proof of publishing on your site. See
  [docs/VERIFICATION_STATUS.md](docs/VERIFICATION_STATUS.md).
