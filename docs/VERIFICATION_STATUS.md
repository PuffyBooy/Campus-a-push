# Verification status

Last updated with the initial implementation. Three levels:

- **Implemented**: code exists.
- **Verified (mock)**: exercised by automated tests. Browser tests drive a real Chromium against
  `tests/mock_typo3/server.py`, a fake backend that reproduces the TYPO3 markup the bot relies on.
- **Verified (live)**: proven against *your* TYPO3 installation. **Nothing is live-verified yet**:
  the developer had no access to the site. Use discovery mode and `tests/integration` to get there.

| Capability | Implemented | Verified (mock) | Verified (live) |
|---|---|---|---|
| Login through the backend login form | ✅ | ✅ | ❌ run `check-login` |
| Login success/failure detection | ✅ | ✅ | ❌ |
| MFA / CAPTCHA detection (no bypass) | ✅ | ✅ | ❌ |
| Session reuse for MFA sites (`auth-export`) | ✅ | ❌ (interactive) | ❌ |
| Backend iframe / v12-style deep-link handling | ✅ | ✅ | ❌ |
| Open new-record form (direct URL) | ✅ | ✅ | ❌ run `discover` |
| Open new-record form (UI steps fallback) | ✅ | ❌ | ❌ |
| Title / teaser / SEO fields, tab switching | ✅ | ✅ | ❌ |
| Rich-text body via CKEditor 5 instance | ✅ | ✅ (CKEditor stub) | ❌ |
| Rich-text body via CKEditor 4 instance | ✅ | ❌ | ❌ |
| Image upload via "Select & upload files" + metadata | ✅ | ✅ | ❌ run `discover --test-save --test-upload` |
| File-name conflict modal (rename) | ✅ | ❌ | ❌ |
| Categories (side-by-side select) | ✅ | ✅ | ❌ |
| Categories (tree) | ✅ | ❌ | ❌ |
| Save + uid detection + validation-error detection | ✅ | ✅ | ❌ run `discover --test-save` |
| Publish via `hidden` field (inverted checkbox safe) | ✅ | ✅ | ❌ live publish test |
| Workspaces / approval workflows | ⚠️ only via custom `ui_steps` | ❌ | ❌ |
| Public page verification (title + content) | ✅ | ✅ | ❌ live publish test |
| Resume after crash without duplicate (uid / list search) | ✅ | ✅ | ❌ |
| Update existing record (same uid, slug kept) | ✅ | ✅ | ❌ |
| Content-element profile (page + tt_content) | ✅ | ✅ | ❌ |
| Discovery report + selector proposal | ✅ | ✅ | ❌ |
| Writer / editor / revision loop | ✅ | ✅ (scripted LLM) | ❌ needs `GROQ_API_KEY` |
| Groq / OpenAI client (JSON mode, invalid-JSON retry, images) | ✅ | ✅ (fake HTTP client) | ❌ needs `GROQ_API_KEY` |
| Deterministic fact-grounding checks | ✅ | ✅ | n/a |
| Research from approved feeds | ✅ | ✅ (feed parsing) | ❌ |
| Pixabay search, filters, download | ✅ | ✅ (fake API) | ❌ needs `PIXABAY_API_KEY` |
| Image caption + description (vision), copyright line | ✅ | ✅ (scripted LLM; fields written in mock form) | ❌ needs `GROQ_API_KEY` + discovery of the image fields |
| DST-correct slots, catch-up, duplicate-slot protection | ✅ | ✅ | n/a |
| Git-branch state + lease lock | ✅ | ✅ (local bare remote) | ❌ first Actions run |
| GitHub Actions workflows | ✅ | ❌ (YAML validated only) | ❌ |

## How to reach "verified (live)"

1. `Actions → TYPO3 setup → check-login`
2. `Actions → TYPO3 setup → discover`; fix the selectors until no manual assistance is listed
3. `Actions → TYPO3 setup → discover-test-save-and-upload`
4. Locally: `TYPO3_LIVE_TESTS=1 TYPO3_LIVE_ALLOW_SAVE=1 TYPO3_LIVE_ALLOW_PUBLISH=1 pytest tests/integration -m live -s`
5. `Actions → Publish` with `mode=full` and dry run **off**, once, while you watch
6. Only then set `BOT_PUBLISHING_ENABLED=true`

Update this table as items are confirmed on the real site.
