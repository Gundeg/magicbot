# CLAUDE.md — Magic Bot project instructions for Claude

This file is loaded automatically when Claude works on `magicbot/`. It captures
project conventions and gotchas so you don't re-discover them every session.
Update this file when you find yourself explaining the same thing twice.

## Project at a glance

Flask + SQLAlchemy + Bootstrap 5 admin panel for a Facebook Messenger bot.
Multi-tenant ready (env-driven branding) but currently deployed as Magic
Financial Group's bot. Mongolian-language UI. Auto-deploys to **Render** on
push to `master` — commit = deploy.

GitHub: <https://github.com/Gundeg/magicbot>

## File map (skip exploration; jump straight here)

| Path | What lives here |
|---|---|
| `app.py` | Flask app construction, `init_db()`, background-thread gating |
| `extensions.py` | `db`, `login_manager`, `csrf`, `migrate`, `limiter` |
| `auth.py` | `staff_required`, `admin_required`, `super_admin_required` |
| `models.py` | All SQLAlchemy models — single source of truth for schema |
| `services/__init__.py` | Bot reply pipeline, `trigger_handoff`, `take_over_chat`, classifier |
| `services/_prompt.py` | `build_system_prompt()` — assembles the bot's per-request memory |
| `services/_seed.py` | `ensure_schema()` (legacy migration), `lint_training_data()`, `seed_*()` |
| `routes/admin/business.py` | `/business-management/*` — catalog editor |
| `routes/admin/bot.py` | `/bot-management/*` — persona, snippets, FAQs, settings |
| `routes/admin/work_tasks.py` | Daily ops queue, dashboard, classify, handoff poll |
| `routes/admin/system.py` | Admin users, audit log, defaults reseed, docs, train-ai guide |
| `routes/admin/auth.py` | Login / logout |
| `routes/webhook.py` | Facebook webhook (POST/GET) |
| `templates/` | Top-level pages + `bot/`, `business/`, `system/` partials |
| `tests/` | pytest, in-memory SQLite, see `conftest.py` |

## Roles (3-tier — pay attention)

- `super_admin` — only role that can edit persona or manage other admins
- `admin` — catalog + bot training + system tabs
- `registration_staff` — daily ops only (dashboard, work_tasks, logs, conversation, train-ai guide)

Decorators (in `auth.py`):
- `@staff_required` — accepts all 3 roles
- `@admin_required` — admin + super_admin only
- `@super_admin_required` — super_admin only

The last super_admin cannot be demoted or self-demote; the toggle handler enforces.

## System prompt assembly (the bot's "brain")

Built fresh per request in `services/_prompt.py:build_system_prompt`. Sources in order:
persona → current time block → **company contact block** (website / email / office hours, when set) →
canonical course list with per-course CourseLinks → training_content (legacy, may be empty)
→ training snippets (★high first) → team members → business lines + Products + ProductLinks →
**Service catalog + ServiceLinks** (the "ҮЙЛЧИЛГЭЭНИЙ КАТАЛОГ" block) →
FAQs → registration-link block → session-state rule → funnel rule →
HANDOFF_JUST_TRIGGERED_RULE (for the one turn after handoff fires) **OR** HANDOFF_ADVISORY_RULE
(while a handoff is open) → sales-behaviour A–F → LANGUAGE_QUALITY_RULE → ЧУХАЛ ДҮРМҮҮД 1–10.

Shared link rendering: `_format_links_block(owner, indent)` is the single helper used by all
three entry formatters (Product / Service / Course). When adding a new linkable model, reuse it.

**Bot-knowledge gaps that have been closed** (don't re-introduce):
- Services + ServiceLinks were previously invisible to the bot — closed by `_format_services()`.
- CourseLinks were previously invisible — closed by `_format_links_block(c, indent='  ')` in `_format_course_entry`.
- `business_website_url`, `center_email`, `office_hours_start/end` weren't surfaced — closed by `_format_company_contact_block()`.

**Bot-knowledge gaps still open** (intentional, not bugs):
- Bot cannot browse the web. To give it deeper knowledge of services / processes, admins must paste content into Service `description`, FAQ answers, or ★high Training Snippets. A future "Knowledge Base" feature was scoped and deferred — user picked Option A (use existing fields).
- ЧУХАЛ ДҮРМҮҮД are code-only — not admin-editable. See [[bot-rules-location]] memory entry.
- `ConversationTopic` (per-user classified topics) isn't fed back into the prompt for personalization.

**Leverage hierarchy for fixing bot behavior** (smallest to largest blast radius):
1. **Snippet** — one rule for one question. Default choice.
2. **FAQ** — when many users ask the same thing.
3. **Catalog edit** — when the data is wrong, not the wording.
4. **Persona** — only when the bot's *voice* is off, not its *facts*.
5. **Code (`services/_prompt.py` or `__init__.py` constants)** — only when the override needs to ship to every customer and snippet wording can't reach it.

## Latent / legacy fields — DO NOT trust without checking

These look real but were retired. Don't read or write them in new code; don't add UI back for them.

| Field | Status |
|---|---|
| `GeneralSetting.center_phone` | Removed in v2 cleanup. Use `main_office_phone`. |
| `GeneralSetting.center_address` | Removed in v2 cleanup. Use `main_office_address`. |
| `GeneralSetting.mute_duration_hours` | UI removed; `trigger_handoff` no longer reads it. Bot mutes only on `take_over_chat`. |
| `GeneralSetting.training_content` | UI removed; prompt builder still reads for backward compat. Won't be set going forward. |

When removing a UI for a setting: drop the form input, but leave the underlying setter/getter as a one-release no-op in case rollback is needed.

## Background tasks (off by default)

Three optional loops, each gated by an env var AND `WORKER_ROLE in (worker, all)`:

- `ENABLE_POLLING=true` → `polling_task` — Facebook Page auto-commenting
- `ENABLE_NUDGE=true` → `nudge_task` — silent-lead follow-ups
- `ENABLE_CHAT_CLUSTERING=true` → `cluster_task` — weekly FAQ-cluster regeneration

Production deploy uses ONE worker dyno with `WORKER_ROLE=worker` and N web dynos with `WORKER_ROLE=web`. Don't run loops on web — they'd N-multiply.

## Test conventions

```powershell
python -m pytest -q                # full suite
python -m pytest tests/test_X.py -v --tb=short  # single file, verbose
```

Current count: 62 tests. They must all pass before any commit.

Gotchas the test suite learned the hard way:
- `db.session` is shared across tests. Mutating User/GeneralSetting between tests requires `db.session.remove()` and/or `expire_all()`.
- `flask-limiter` stays on even when `RATELIMIT_ENABLED=False` is set in Flask config — disable it directly in fixtures: `from extensions import limiter; limiter.enabled = False`.
- The `client` fixture is per-test; cookies don't leak between tests. Session-scoped state (`db.session` identity map) does — see above.
- The `_clear_setting_cache` autouse fixture in conftest handles `g._setting_cache` for you.

## CSRF — no manual header needed

`templates/base.html` lines ~494–518 install a fetch interceptor that auto-adds `X-CSRFToken` on every non-GET same-origin request. JS code in templates can `fetch(url, { method: 'POST', body: ... })` without thinking about CSRF.

The Facebook webhook is the only POST exempt — `@csrf.exempt` in `routes/webhook.py`.

## Common admin paths (sidebar labels in Mongolian)

| Sidebar | Route |
|---|---|
| Хяналтын самбар | `/admin/dashboard` |
| Ажлын даалгавар | `/admin/work-tasks` (default tab: `hot_prospects`) |
| Мессежийн түүх | `/admin/logs` |
| Бизнесийн удирдлага | `/business-management/general` (admin+) |
| Ботын удирдлага | `/bot-management/settings` (admin+) |
| Гарын авлага | `/admin/train-ai-guide` (staff+) |
| Систем | `/admin/admins` (admin+) |

## Bug surfaces seen recently — be careful here

- `services/__init__.py:trigger_handoff` used to auto-mute the bot via `mute_duration_hours`. That's been removed — DO NOT add it back; the bot must keep advisory-replying after handover until staff `take_over_chat`.
- `seed_default_magic_links` historically missed a `GeneralSetting` import — it's fixed, but any new helper that touches `GeneralSetting.query` must import it from `models`.
- `classification_lookback_days` controller lives at the top of `work_tasks.html` (outside any tab) so it's visible from the default Hot Prospects landing. Don't move it back into a single tab.
- **"Bot is silent" is most often a Facebook plumbing issue, not a code issue.** When the user reports silence and the admin panel shows zero muted users, START by checking Render logs for `POST /webhook` entries from Facebook (admin polling traffic shows up as `/admin/api/handoff-poll`, ignore that). If there are no POSTs, the Facebook ↔ Render link is broken — see the [[fb-webhook-silence]] memory recipe. Don't read code first.
- **The webhook trigger now falls through to LLM generation** instead of sending a static handoff message. The webhook sets `handoff_just_triggered=True` and continues to `generate_bot_response`; the prompt builder injects `HANDOFF_JUST_TRIGGERED_RULE` which overrides advisory mode for that one turn. Static `HANDOFF_USER_REPLY` / `HANDOFF_USER_REPLY_OFF_HOURS` are now only used as the OpenAI-failure fallback inside `generate_bot_response`.

## When seeding or migrating data

- Use **UPSERT by stable key** (URL for `*Link` rows, title for `TrainingSnippet`). Re-running should refresh content, not duplicate rows.
- One-shot description updates (e.g. rewording Magic Finance product description): check the current value matches a `KNOWN_DEFAULTS` set before overwriting, so admin edits survive a re-seed.
- Removing a thing → set `is_active=False` and set a `status_note`, don't `.query.delete()`. Audit history matters.

## Mongolian voice for new content

- Chat-facing `description` columns (ProductLink / ServiceLink / CourseLink): imperative form. `Аудит хийлгэе`, `Тайлан гаргуулая`, `MS Office лиценз авая`. Not `... захиалгын форм`.
- Admin labels: natural Mongolian, untranslated terms (`Persona`, `Snippet`) are OK when used in pairs (`Ботын дүр (Persona)`).
- New admin manual sections always include: a "Юу", "Хэзээ ашиглах вэ?", and a ✓/✗ example pair.
- **Bot output (LLM-generated)**: must read like polished native Mongolian, not machine-translated. `LANGUAGE_QUALITY_RULE` in `services/__init__.py` is the prompt-side anchor — it carries explicit ✗/✓ pairs for the specific failures the user has flagged (genitive subjects like "Миний бэлэн байна", verbose "X-ийн боломжтой" stacks, dead noun-clause questions). When adding new bot-output examples to any prompt rule, follow the same paired-example pattern.

## Commit + push protocol

1. `git status` + `git diff` + `git log --oneline -5` in parallel.
2. Stage SPECIFIC files (`git add path/...`) — never `git add -A` or `git add .`.
3. Run `pytest -q`. If anything fails, do NOT proceed.
4. Commit with HEREDOC body, finishing with `Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>`.
5. Push only on explicit user ask (`push`, `Continue`, or end-of-batch).

Recent commit titles to mirror style:
- `Fix two reported bugs: premature handoff mute + hidden classify control`
- `Items 9 + 10: Document Нийцлийн шалгалт + polish manuals`
- `Admin cleanup batch: 8/10 of the v2 polish list`

## Things NOT to do (lessons learned)

- Don't restart the dev server unless asked — the user runs it themselves.
- Don't `mkdir` random project directories. The layout is stable.
- Don't add tests for Flask-Login or Bootstrap's built-in behavior; test only project logic.
- Don't translate the chat-facing English-loanword terms ("link", "form") — Mongolian admins recognize them.
- Don't rename `master` → `main` unless asked. Render is configured against `master`.
- Don't commit `__pycache__/`, `.env`, `magic_bot.db`, or `.pytest_cache/`.
