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
persona → current time block → canonical course list → training_content (legacy, may be empty)
→ training snippets (★high first) → team members → business lines (answer / refer / paused)
→ FAQs → session-state rule → funnel rule → behavioral rules.

Registration is per-course: each course's `CourseLink` (description "Сургалтанд сууя,
бүртгүүлье") is listed under it, and the website (`business_website_url`) is the fallback
when a course has none. The clarify-course rule lives in the ★high `Сургалтад бүртгүүлэх
асуултын чиглэл` snippet, not in code. The old global `google_form_url` form was removed
2026-07-07.

**Leverage hierarchy for fixing bot behavior** (smallest to largest blast radius):
1. **Snippet** — one rule for one question. Default choice.
2. **FAQ** — when many users ask the same thing.
3. **Catalog edit** — when the data is wrong, not the wording.
4. **Persona** — only when the bot's *voice* is off, not its *facts*.

## Reply model + knowledge-gap handoff (the "honest AI" path)

- **Customer replies run on `REPLY_MODEL` via `reply_client`** (`services/__init__.py`).
  The provider is chosen once at import:
  - `GEMINI_API_KEY` (or `GOOGLE_API_KEY`) set → **Google Gemini**, default
    `gemini-2.5-flash`. Reached through Gemini's
    **OpenAI-compatible endpoint** (`GEMINI_BASE_URL`, default
    `https://generativelanguage.googleapis.com/v1beta/openai/`), so the entire reply
    path — `chat.completions`, the `defer_to_staff` function-calling tool, and the
    `openai.AuthenticationError` / `RateLimitError` types `alert_openai_failure` keys
    on — is reused unchanged. **No `google-genai` dependency**, no message/tool reshaping.
  - neither set → **OpenAI** `gpt-5.3-chat-latest` (the prior default, kept after a
    Mongolian bake-off — far better at paraphrase / Latin-script / intent than 4o-mini).
  - `REPLY_MODEL` env var overrides the default for either provider.
  - **THINKING GOTCHA (`GEMINI_REASONING_EFFORT`, default `none`):** Gemini 2.5/3.x
    are *thinking* models and `max_tokens` caps thinking+reply COMBINED on the compat
    endpoint — so unmanaged thinking eats the whole budget and returns an EMPTY reply
    (`finish_reason=length`, 0 completion tokens). `none` disables thinking and is the
    reason `gemini-2.5-flash` is the default. **Pro / 3.x REJECT `none`** and can't
    disable thinking, so to run **`gemini-3.1-pro-preview`** (needs BILLING — free tier
    = limit 0) set all three: `REPLY_MODEL=gemini-3.1-pro-preview`,
    `GEMINI_REASONING_EFFORT=low`, `REPLY_MAX_TOKENS=2048` (headroom for the ~1K
    thinking budget + the reply). The code auto-skips `none` on non-Flash models so a
    stale default can't brick a Pro deploy. Prod runs Pro via these env vars; the code
    default stays on safe Flash.
  - **Background jobs** (topic classifier, page comments, FAQ clustering) ALWAYS stay
    on OpenAI `gpt-4o-mini` via `client` — cheap, high-volume. So `OPENAI_API_KEY` is
    required even on a Gemini reply deployment.
  - **PARAM GOTCHA (provider-aware → `REPLY_MAX_TOKENS_PARAM`):** OpenAI gpt-5.x
    chat models reject `temperature` + `max_tokens` → use `max_completion_tokens`;
    Gemini's OpenAI-compat endpoint uses the classic `max_tokens`. We never send an
    explicit `temperature`, so the default-temperature requirement holds for both.
- **When the bot lacks the info, it hands off instead of inventing.** In normal mode the
  prompt injects `KNOWLEDGE_GAP_HANDOFF_RULE` and `generate_bot_response` offers the
  **`defer_to_staff` tool** (OpenAI function calling — reliable, unlike a literal text
  marker, which chat models strip). On a tool call, `generate_bot_response` re-emits the
  internal `HANDOFF_MARKER` prefix; the webhook's `extract_handoff_marker` strips it (never
  shown to the user) and fires the existing `trigger_handoff` (Work Tasks + Telegram). The
  tool's `reply_to_customer` carries the ETA + "anything else?" message.
  - It still answers what it CAN (courses/prices/schedule/FAQ); only genuinely missing
    info → defer. It also defers rather than invent a link/password it lacks.
  - Fix a recurring handoff by adding a **Snippet** with the answer (per the leverage
    hierarchy above) — then the bot answers it itself next time.

## Latent / legacy fields — DO NOT trust without checking

These look real but were retired. Don't read or write them in new code; don't add UI back for them.

| Field | Status |
|---|---|
| `GeneralSetting.center_phone` | Removed in v2 cleanup. Use `main_office_phone`. |
| `GeneralSetting.center_address` | Removed in v2 cleanup. Use `main_office_address`. |
| `GeneralSetting.mute_duration_hours` | UI removed; `trigger_handoff` no longer reads it. Bot mutes on `take_over_chat` OR an untagged Messenger echo (human-agent reply — see "Human-takeover auto-mute"). |
| `GeneralSetting.training_content` | UI removed; prompt builder still reads for backward compat. Won't be set going forward. |
| `GeneralSetting.google_form_url` | Removed 2026-07-07 (getter, env var, admin input, prompt block all deleted). Registration is per-course (`CourseLink`) with `business_website_url` as fallback; the clarify-course rule lives in the `Сургалтад бүртгүүлэх асуултын чиглэл` snippet. Any stale DB row is inert. |

When removing a UI for a setting: drop the form input, but leave the underlying setter/getter as a one-release no-op in case rollback is needed.

## Background tasks (off by default)

Loops gated by an env var AND `WORKER_ROLE in (worker, all)`:

- `ENABLE_POLLING=true` → `polling_task` — Facebook Page auto-commenting
- `ENABLE_NUDGE=true` → `nudge_task` — silent-lead follow-ups
- `ENABLE_CHAT_CLUSTERING=true` → `cluster_task` — weekly FAQ-cluster regeneration
- `ENABLE_TOKEN_CHECK` (**defaults ON**) → `token_health_task` — every `TOKEN_CHECK_INTERVAL_HOURS` (6) pings the Graph API and Telegram-alerts staff if the Page token is expired/invalid (the OAuthException 190/463 that silently kills the bot). Cheap one-call check; no-op without Telegram configured.

Production deploy uses ONE worker dyno with `WORKER_ROLE=worker` and N web dynos with `WORKER_ROLE=web`. Don't run loops on web — they'd N-multiply.

## Webhook is async (ACK fast, reply in the background)

`routes/webhook.py:webhook()` does only the FAST work synchronously — verify signature, handle echoes, dedupe by `mid`, persist the inbound row, rate-limit, funnel/name/phone capture, mute gate — then hands the SLOW work to `enqueue_background(process_inbound_reply, ...)` and returns `200` immediately. `process_inbound_reply` (same file) builds the prompt, calls OpenAI, sends the reply (with a typing indicator), and fires handoffs. **Why:** a fast 200 stops Facebook retrying the delivery on cold starts / slow model hops — the root of the phantom rate-limit. When editing reply behaviour, edit `process_inbound_reply`, NOT the webhook loop. `enqueue_background` runs **inline under TESTING** (deterministic + in-memory SQLite is per-connection). A background failure = no reply, logged, no crash, no FB retry.

## Test conventions

```powershell
python -m pytest -q                # full suite
python -m pytest tests/test_X.py -v --tb=short  # single file, verbose
```

Current count: 109 tests. They must all pass before any commit.

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

- `services/__init__.py:trigger_handoff` used to auto-mute the bot via `mute_duration_hours`. That's been removed — DO NOT add it back; the bot keeps advisory-replying after a *handoff* until either staff `take_over_chat` OR the human-takeover auto-mute fires (see below).
- **Human-takeover auto-mute:** every bot-sent Messenger message carries `metadata=BOT_ECHO_TAG` (`services/__init__.py:send_facebook_message`). The webhook handles `message_echoes`: an echo WITHOUT that tag = a human agent replied in the FB Page inbox → `routes/webhook.py:human_takeover_pause` sets `bot_muted_until = now + HUMAN_TAKEOVER_MUTE_MINUTES` (30, env-tunable). Echoes must `continue` before the inbound pipeline (sender is the Page, recipient the customer). Requires the `message_echoes` webhook field enabled in the Meta dashboard — without it FB never sends echoes and this path is inert. NEVER drop the metadata tag: an untagged bot echo would make the bot mute *itself* on every reply.
- **Webhook idempotency / "rate-limited on the first message":** Facebook delivers at-least-once and RETRIES the same event (same `message.mid`) when the webhook is slow — a Render cold start after weeks idle, or a slow OpenAI hop (the reply is still generated *synchronously* before we return 200). Each retry used to re-enter the handler and increment the per-sender rate limiter, so a single first message could trip "5 msgs / 60s" and emit the throttle text — and the bot replied twice. Fix: `Message.mid` is a UNIQUE idempotency key; `routes/webhook.py` persists the inbound row keyed by `mid` and drops a retry on `IntegrityError` BEFORE the rate limiter and before generate/send. Do NOT move the rate-limit check above this claim, and keep the claim committed before the OpenAI call. SQLite can't `ADD COLUMN ... UNIQUE`, so `ensure_schema()` adds `message.mid` then `CREATE UNIQUE INDEX` separately (multiple NULLs allowed → bot/legacy rows are fine). The webhook now also ACKs 200 immediately and generates the reply in the background (see "Webhook is async"), so FB largely stops retrying in the first place — the `mid` dedup is the belt-and-suspenders guarantee.
- `seed_default_magic_links` historically missed a `GeneralSetting` import — it's fixed, but any new helper that touches `GeneralSetting.query` must import it from `models`.
- `classification_lookback_days` controller lives at the top of `work_tasks.html` (outside any tab) so it's visible from the default Hot Prospects landing. Don't move it back into a single tab.
- **SQLite WAL + busy_timeout in `app.py:88`** is load-bearing — without it, 2 gunicorn workers + admin polling deadlock on SQLite locks. Do not remove the `@event.listens_for(Engine, "connect")` block.
- **`get_facebook_user_info` returns HTTP 400 at Standard Access** — Meta restricted PSID profile lookups. The bot now asks customers for their name on first contact (`services/_prompt.py:481+` injects the rule; `routes/webhook.py:107+` captures the reply via `extract_name_from_reply`). When App Review for "Business Asset User Profile Access" lands, the existing API call will start succeeding and the ask-for-name path silently becomes redundant. Don't remove either path until BAUPA is approved AND re-tested in prod.
- **`import netrc` at the top of `services/__init__.py` is load-bearing** (added PR #11, 2026-07-06) — do NOT remove it. `requests` does a LAZY `from netrc import ...` inside `get_netrc_auth()` during `prepare_request`. On a *cold* gunicorn worker, a first-contact webhook (`get_facebook_user_info` → `requests.get`, which runs *synchronously* in the webhook) racing a background reply thread that is also importing can deadlock on CPython's import lock → 60s `WORKER TIMEOUT` → `POST /webhook 500` → that customer gets no reply (the log's `SIGKILL! Perhaps out of memory?` is misleading — it's a timeout). Eager-importing netrc at startup warms `sys.modules` so the lazy import is a no-op that never takes the lock mid-request; protects every `requests` call, not just that path. Intermittent — only cold workers + first-contact senders; returning users skip `get_facebook_user_info` entirely.

- **Staff-action notes:** dropping a hot prospect or a lead **requires** a reason
  (the note modal in `work_tasks.html`); resolving an issue takes an **optional**
  note. The reason is stored on `FacebookUser.notes` (or `AdminIssue.notes` for
  issues) AND the full text goes into the audit-log `detail` — the durable copy
  that survives `cleanup_old_records`' 60-day purge. The **Орхисон** tab lists
  dropped users with their reason and a **Сэргээх** (restore → `status='new'`)
  button. `update_lead_status` enforces the required note only when
  `status == 'dropped'`.

## Telemetry shortcuts — first place to look when user says "bot is broken"

Check these in order BEFORE reading code:

1. **Customers getting the canned apology** ("Уучлаарай, түр зуурын саатал...") → the **reply provider** is failing, NOT Facebook (apology delivered = Send API fine). Render logs query `Error generating response` shows the exception; `insufficient_quota` = account out of credit (2026-06-11 outage) → instant recovery on top-up. **Which provider?** If `GEMINI_API_KEY` is set, replies are Gemini → fix the key/quota at Google AI Studio (`aistudio.google.com`); otherwise OpenAI → platform.openai.com → Billing. The deduplicated Telegram alert (`alert_openai_failure`, cooldown `OPENAI_ALERT_COOLDOWN_HOURS`=6) names the active provider (`REPLY_PROVIDER_LABEL`) and the right billing page. Note: a Gemini `429` quota error may not match the `insufficient_quota` test, so it can degrade quietly to the apology without paging — Gemini *auth* (401) errors still alert.
2. **Render logs** (`r=1h`, query `Send API`) → `OAuthException code:190` = bad FB token (`subcode:463` = expired; `subcode:460` = FB account password changed / session invalidated). Regen via Graph API Explorer — **and it MUST be a Page token, not the default User token.** The Explorer defaults "User or Page" to **User Token**; copy that and every send fails `GraphMethodException code:100 subcode:33 "Object with ID 'me' does not exist"` (the bot sends via `me/messages`, so `me` must resolve to the Page). Fix: set "User or Page" → **Page Access Tokens → Magic Financial Group**, extend to long-lived, then update `FACEBOOK_ACCESS_TOKEN` on the **magicbot** service. Verify in the Render shell: `python -c "import os,requests;print(requests.get('https://graph.facebook.com/v18.0/me',params={'access_token':os.environ['FACEBOOK_ACCESS_TOKEN']}).text)"` → must return `Magic Financial Group`, not a person's name. Full walk-through: `Facebook Page Access Token хэрхэн авах тухай дэлгэрэнгүй заавар.md` §5.
3. **Render logs** (`r=1h`, query `database is locked`) → SQLite contention. Confirm `app.py:88` WAL/busy_timeout block is intact.
4. **Render logs** (`r=1h`, query `FB user profile`) → `status=400 GraphMethodException` = the BAUPA restriction, not a bug.
5. **Render Events page** — a bad deploy is the easiest explanation when nothing else fits.

The production service is `magicbot` at <https://dashboard.render.com/web/srv-d81j1p9j2pic73fbsnv0>, under `My project / Production`. (A sibling `gmcbot` service used to sit under the `GMC` env — **deleted 2026-07-06**. Before it was deleted, an evening was lost updating *its* env vars while prod `magicbot` stayed broken. Whenever you change a token/env var, confirm it landed on `magicbot` by checking its Events page for a fresh "Environment updated / Deploy" entry — if there's none, the change went to the wrong place.)

## When seeding or migrating data

- Use **UPSERT by stable key** (URL for `*Link` rows, title for `TrainingSnippet`). Re-running should refresh content, not duplicate rows.
- One-shot description updates (e.g. rewording Magic Finance product description): check the current value matches a `KNOWN_DEFAULTS` set before overwriting, so admin edits survive a re-seed.
- Removing a thing → set `is_active=False` and set a `status_note`, don't `.query.delete()`. Audit history matters.
  - **Exception:** the operator-triggered `cleanup_old_records` (`routes/admin/work_tasks.py`, admin-only button on the Leads tab) DOES hard-delete by design — chat messages older than `CLEANUP_RETENTION_DAYS` (60) and terminally-closed leads (converted/dropped) untouched that long, plus their dependents. `FacebookUser` has no DB cascade, so it deletes `Message`/`AdminIssue`/`ConversationTopic` rows explicitly. This is the one sanctioned hard-delete path.

## Mongolian voice for new content

- Chat-facing `description` columns (ProductLink / ServiceLink / CourseLink): imperative form. `Аудит хийлгэе`, `Тайлан гаргуулая`, `MS Office лиценз авая`. Not `... захиалгын форм`.
- Admin labels: natural Mongolian, untranslated terms (`Persona`, `Snippet`) are OK when used in pairs (`Ботын дүр (Persona)`).
- New admin manual sections always include: a "Юу", "Хэзээ ашиглах вэ?", and a ✓/✗ example pair.

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
