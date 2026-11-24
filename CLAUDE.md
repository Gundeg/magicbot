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

Built fresh per request in `services/_prompt.py:build_system_blocks`, as TWO halves that
`generate_bot_response` sends as two `system` text blocks:
- **Stable (prompt-cached, `cache_control: ephemeral`):** persona → canonical course list →
  company identity + contact (КОМПАНИЙН ТАНИЛЦУУЛГА: `center_name`, `center_description`,
  head office address/phone, website, email, office hours) → ЕРӨНХИЙ МЭДЭЭЛЭЛ
  (`training_content`: locations, branches, business lines — the group spans audit, tax
  consulting, cloud and the training centre in UB, Darkhan and Erdenet, so the bot must know
  all of it) → training snippets (★high first) → team members → business lines (answer /
  refer / paused, each with its own address/phone/email) → services catalog → FAQs →
  facts-source rule (`FACTS_SOURCE_RULE`: company facts ONLY from the data above; earlier
  turns, automation/staff messages and the summary may be stale — data wins) →
  professional-accuracy rule → sales playbook → language-quality rule → registration rule
  (when configured) → ЧУХАЛ ДҮРМҮҮД 1-10. **Never hard-code a commercial fact (price,
  installment partner, licence term, duration, perk) in rule text** — it belongs in the
  admin data (Ерөнхий мэдээлэл, course descriptions, FAQ, snippets), where admins can see
  and fix it; `tests/test_facts_grounding.py` guards this. Identical for every customer
  and minute; an admin edit (persona, snippet, FAQ, catalog) just starts a new cache entry.
- **Volatile (after the breakpoint):** current time block (minute-granular — NEVER move it
  into the stable half) → name / customer-state (lead, prior summary) → funnel rule →
  handoff-mode rule → session rule (ЭНЭ ЯРИАНЫ ТӨЛӨВ) → a one-line brevity reminder.
`build_system_prompt` returns the two joined for tests/scripts. `REPLY_CACHE_TTL` (`5m`
default / `1h`) picks the cache lifetime; `usage.cache_read_input_tokens` in the `reply
usage` log line shows hits. Mongolian Cyrillic is ~1 token per character, so the stable
half alone is ~14K tokens — caching it is the single biggest cost lever.

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

- **Every model call runs on Claude (Anthropic) through ONE `client`**
  (`services/__init__.py`, `anthropic.Anthropic`). `ANTHROPIC_API_KEY` is the only model
  credential (OpenAI + Gemini were retired 2026-11-02). The SDK does NOT fail at
  construction without a key — the module logs a loud boot warning instead, and the
  dashboard's "Холболтын төлөв" card shows the key status.
  - **Customer replies** → `REPLY_MODEL`, default **`claude-opus-5-5`**. Mongolian
    paraphrase / Latin-script / indirect intent is where the big tier earns its cost
    (same lesson as the earlier OpenAI/Gemini bake-offs). `REPLY_MODEL=claude-sonnet-5-5`
    trades some quality for ~half the cost.
  - **Background jobs** (topic classifier, page comments, FAQ clustering) →
    `BACKGROUND_MODEL`, default **`claude-haiku-5-5`** — cheap, high-volume.
  - **THINKING / EFFORT GOTCHA:** current Claude models think *adaptively* and Opus 5.5
    REJECTS turning thinking off (`thinking: {type: "disabled"}` → 400). Never send a
    `thinking` param; depth is `output_config.effort`. Replies use `REPLY_EFFORT`
    (default `low` — fast chat; raise to `medium` if replies feel shallow). Classifier
    and comments run at `low`, clustering at `medium`.
  - **`max_tokens` caps thinking + reply COMBINED.** `REPLY_MAX_TOKENS` default is 2048
    (it was 500 on the OpenAI path). Too tight → the model stops mid-thinking with zero
    text (`stop_reason=max_tokens`); `generate_bot_response` logs `Empty reply from …`
    and sends the canned apology rather than an empty Messenger send.
  - **Never send sampling params** (`temperature` / `top_p` / `top_k`) — current models 400.
  - **Message shaping (`_claude_messages`):** the prompt travels in `system`; the
    Messages API rejects whitespace-only text and requires the first message to be
    `user`, so empty turns and a leading assistant turn from the last-10 window are
    dropped before the call.
  - **Structured outputs:** the classifier and the clusterer pass
    `output_config.format = {type: json_schema, schema}` (`TOPIC_TAGS_FORMAT` /
    `CLUSTERS_FORMAT`) so the JSON always parses — the replacement for the old
    `response_format=json_object`.
- **Handoff is the model's decision, in three categories.** In normal mode the prompt
  injects `KNOWLEDGE_GAP_HANDOFF_RULE` and `generate_bot_response` offers the
  **`defer_to_staff` tool** (Messages API tool use — reliable, unlike a literal text
  marker, which chat models strip; `tool_choice` stays on the default `auto` because Opus
  5.5 rejects a forced tool). The tool takes `category` ∈ `knowledge_gap` /
  `wants_human` / `complaint`, `reply_to_customer` (ETA + "anything else?") and a
  one-line `reason`. On a `tool_use` block the reply is prefixed with
  `[HANDOFF:category|reason]` (`format_handoff_marker`); the webhook's `extract_handoff`
  strips it (never shown to the user) and fires `trigger_handoff` with
  `бот: <Mongolian label> — <reason>` so Work Tasks + Telegram show WHY.
  - It is told NOT to defer for anything it can sell or answer: prices, schedule,
    catalog services (give the link), registration intent (link + phone options), a
    captured phone number (that is a lead — `is_lead` is passed into the prompt), or
    objections. Fix a recurring knowledge-gap handoff by adding a **Snippet** with the
    answer (per the leverage hierarchy above).
  - **Removed 2026-11-03 — do NOT reintroduce:** the scan of the bot's own reply for
    phrases like "манай ажилтан удахгүй холбогдоно" (`bot_response_implies_handoff`) and
    the "customer mentioned a refer business line" trigger. Both paged staff on every
    lead and every registration offer. The keyword pre-scan (`should_handoff`,
    `HandoffKeyword`, `handoff_sensitivity`) followed on 2026-11-04 — it paged staff on
    ordinary sentences ("ажилтан хэдэн цагт ирдэг вэ") and pre-empted the model.
    `handoff_just_triggered` plumbing remains but nothing sets it any more.
- **Customer state + cross-session memory in the prompt.** `process_inbound_reply` passes
  `is_lead`, `ask_name` and `prior_summary` to `generate_bot_response` →
  `build_system_prompt`. The name question ("Та өөрийгөө хэн гэдэг…") is asked ONLY once
  the customer is a lead (phone captured) — before that every discovery reply ended with
  two questions. `FacebookUser.bot_summary` is a 2-3 line Mongolian summary of the
  earlier conversation written by `refresh_user_summary` (BACKGROUND_MODEL) when a
  customer returns after a gap (`session_state in ('gap','returning')`), injected as
  "ӨМНӨХ ЯРИАНЫ ХУРААНГУЙ" and shown in the admin conversation side panel.

## Bot working schedule + step-in (2026-11-16)

`services/_schedule.py`, one `GeneralSetting` row `bot_schedule` (JSON), edited on Ботын
удирдлага → Ботын тохиргоо → **Ботын ажлын цаг**. It holds **staff hours** per weekday (start,
end, one lunch break; UB time, end-exclusive) — the bot replies whenever staff are NOT on duty.
Gundeg's default: staff Mon–Fri 09–13 + 14–18, so the bot covers 18:00–09:00, 13:00–14:00 and
the weekend. `mode`: `schedule` | `always_on` (holidays) | `off`.
- **Gate:** `webhook()` checks `bot_should_reply()` right after the mute gate; in staff hours the
  inbound is saved, funnel/phone capture runs, the classifier runs, but no reply is enqueued.
- **Step-in:** `step_in_sweep` (60 s) answers a customer whose FIRST unanswered message inside the
  last N+60 minutes is ≥ N minutes old (default 15), unless muted or a staff reply came after it.
  The window bound is load-bearing: without it, days-old unanswered history (takeover chats,
  'off' holidays) made the bot jump in seconds after a new message. Staff replies are not
  stored as messages, so `human_takeover_pause` stamps `FacebookUser.last_staff_reply_at` from
  the untagged echo — and only EXTENDS `bot_muted_until`, never shortens a Хариуцах takeover.
  The reply goes through `process_inbound_reply(..., waited_minutes=...)`, which adds an
  apologise-for-the-wait line to the volatile prompt. Claimed with an atomic UPDATE on
  `bot_step_in_at` (safe even if two processes sweep); a normal bot-hours reply stamps the same
  column before it is queued (`_mark_reply_in_flight`), so at a 13:00/18:00 handover the sweep
  can't answer the same wait twice. The sweep never steps in when every unanswered customer
  message is a closing one (`is_closing_message`) — it used to answer a "thank you" with an
  apology for the wait once the takeover mute lapsed.
- **One source for hours:** `_is_off_hours()`, the off-hours handoff text ("манай ажилтан маргааш
  09:00 цагаас тантай холбогдоно") and the prompt's "Ажлын цаг" line all read the schedule. The
  prompt's clock and weekday come from `ub_now()` (a UTC weekday was wrong 00:00–08:00 UB).
- **`always_on` = holiday / staff meeting:** staff count as unavailable (`staff_available`), so
  handoffs are OFF-HOURS and the bot says "манай ажилтан боломжтой болмогц тантай холбогдоно"
  instead of promising 10–30 minutes; a volatile prompt line tells the model the same.
- **Dashboard:** Холболтын төлөв → "Бот одоо" tells staff (who can't open bot settings) whether
  it is their shift.
- **Tests** default to `always_on` (autouse fixture in `conftest.py`) so no test depends on the
  wall clock; schedule tests save a row and freeze `services._schedule._utcnow`.

## Гүйцэтгэл — performance page (2026-11-16)

`/admin/performance` (admin + super_admin; registration staff must never see colleagues'
numbers), computed by `services/_performance.py:performance_report(days)`. Definitions that
the numbers depend on — change them only together with `tests/test_performance.py`:
- A **wait** starts at a customer message whose previous message (nudges ignored) isn't the
  customer's; its response is the next staff message or bot message with `kind != 'nudge'`.
  A wait begun before the window is never counted.
- **Team** numbers cover waits that start in staff hours (today's schedule) and after the first
  stored `staff` message; buckets staff / step-in / bot / unanswered sum to the waits.
  Excluded: closing-only waits (`is_closing_message`: "Баярлалаа", "за", "ок", emoji — NOT
  "тийм", which is often a yes needing follow-up) and unanswered waits younger than the step-in
  minutes (still in progress). The bot's reply-time median counts only waits begun while the bot
  was on duty.
- `Message.kind` marks bot messages that aren't ordinary replies (`'step_in'`, `'nudge'`);
  `FacebookUser.lead_at` is stamped once at the three lead paths (phone capture, bot
  registration, Hot-Prospect promote) — recorded only from 2026-11-16.
- Per-staff numbers come from the audit log (`AuditEntry.actor_id`) and the matching detail
  strings ("Work Tasks-аас шийдсэн", "→ resolved/contacted/converted/dropped") — renaming
  those strings in `work_tasks.py` silently zeroes the page.
- "Snippet болгох" links to `bot_management_training?snippet_title=&snippet_body=`; the
  training page opens the pre-filled modal on `window` load.

## Bot-driven registration (GreenSoft form)

`services/_registration.py`. The centre's registration form is a GreenSoft (call24) form —
`https://forms.greensoft.mn/1955/form/5` — feeding their CRM; Gundeg has no access to the
sheet behind it. When `REGISTRATION_FORM_URL` is set, the model gets the
`submit_registration` tool and `REGISTRATION_RULE` (cached stable half): on "бүртгүүлье" it
says "Би таны бүртгэлийг одоо үүсгэе", collects овог/нэр, утас, имэйл (mandatory — the
contract and online-lesson access are emailed; the bot explains that), сургалт, ээлж (салбар
only outside UB), confirms, then calls the tool. `generate_bot_response` turns the call into a
hidden `[REGISTER:<base64 json>]` marker; `routes/webhook.py:complete_registration` POSTs the
fields to the form exactly as its widget does (multipart, widget field names from `/formapi`,
honeypot `formdesc` empty, `_gs_form_token` a few seconds in the past), marks the customer a
lead with the fields in `notes`, pings Telegram, and sends the model's confirmation only when
the form answered `status: ok` + `logid` — otherwise `REGISTRATION_FALLBACK_REPLY` (staff will
finish it). Verified live 2026-11-03 (test entry logid 362). Unset `REGISTRATION_FORM_URL` =
old behaviour (link + phone). Select option VALUES come from the live definition (labels are
matched, so the form owner can add options); the "Magic Finance программын сургалт" option
currently has an EMPTY value on the form itself — tell the form owner.

## Latent / legacy fields — DO NOT trust without checking

These look real but were retired. Don't read or write them in new code; don't add UI back for them.

| Field | Status |
|---|---|
| `GeneralSetting.center_phone` | Removed in v2 cleanup. Use `main_office_phone`. |
| `GeneralSetting.center_address` | Removed in v2 cleanup. Use `main_office_address`. |
| `GeneralSetting.mute_duration_hours` | UI and getter removed (2026-11-04). Bot mutes on `take_over_chat` OR an untagged Messenger echo (human-agent reply — see "Human-takeover auto-mute"). |
| `GeneralSetting.training_content` | **Live again (2026-11-04)** as Бизнесийн удирдлага → Ерөнхий мэдээлэл (locations, branches, history, business lines). Read by `get_training_content` — the DB row wins, an empty row omits the prompt section (no placeholder); `TRAINING_CONTENT` env is the fallback for a never-saved deployment. |
| `GeneralSetting.training_comment` / `celebration_comment` / `default_comment` / `handoff_message` / `bot_welcome` | Dropped from the Bot Settings form 2026-11-04 — were saved but never read (auto-comments, handoff replies and greetings are model-written). Old rows inert. |
| `GeneralSetting.handoff_sensitivity` + `HandoffKeyword` table | Keyword handoff retired 2026-11-04 (`should_handoff`, the Handover tab, `/admin/handoff-keywords`, the seeder — all gone). Handoff is the model's `defer_to_staff` decision only. Table kept, unread. |
| `GeneralSetting.office_hours_start` / `office_hours_end` | Replaced 2026-11-16 by `bot_schedule` (JSON, `services/_schedule.py`). Form inputs gone; nothing reads the old rows. |
| `GeneralSetting.google_form_url` | Removed 2026-07-07 (getter, env var, admin input, prompt block all deleted). Registration is per-course (`CourseLink`) with `business_website_url` as fallback; the clarify-course rule lives in the `Сургалтад бүртгүүлэх асуултын чиглэл` snippet. Any stale DB row is inert. |

When removing a UI for a setting: drop the form input, but leave the underlying setter/getter as a one-release no-op in case rollback is needed.

## Background tasks (off by default)

Loops gated by an env var AND `WORKER_ROLE in (worker, all)`:

- `ENABLE_POLLING=true` → `polling_task` — Facebook Page auto-commenting
- `ENABLE_NUDGE=true` → `nudge_task` — silent-lead follow-ups
- `ENABLE_CHAT_CLUSTERING=true` → `cluster_task` — weekly FAQ-cluster regeneration
- `ENABLE_TOKEN_CHECK` (**defaults ON**) → `token_health_task` — every `TOKEN_CHECK_INTERVAL_HOURS` (6) pings the Graph API and Telegram-alerts staff if the Page token is expired/invalid (the OAuthException 190/463 that silently kills the bot). Cheap one-call check; no-op without Telegram configured. It is the **backstop**, not the primary pager — `alert_facebook_token_failure` fires from the send path on the first failed message (see below).

- **Always on with the loops** → `step_in_task` (`routes/webhook.py`) — every 60 s, `step_in_sweep` answers a customer staff left waiting during staff hours (see "Bot working schedule"). Not env-gated; the admin switch governs it.

**What prod actually runs (checked 2026-11-16 in the Render dashboard):** ONE web service, start command `gunicorn app:app --workers 2 --timeout 60`, with **`WORKER_ROLE=worker`** set in its environment — there is no separate worker service (the SQLite file lives on the web service's disk, which another service can't share). **Observed in the boot log of the 2026-11-16 deploy:** the app (and so every background-loop thread) loads ONCE, in the gunicorn **master** — `Starting gunicorn` and `Step-in sweeper started` both log pid 39, while the request workers are pids 64/65 and log no loop start. So loops currently run once, in the master, not per worker. Why the master imports the app is not established — so still write loop work as if it could run twice: claim with an atomic DB update first (`_claim_step_in`, the alert cooldown rows), never "check, then act". The Procfile's web/worker split is the intended design, not the deployed one.

## Webhook is async (ACK fast, reply in the background)

`routes/webhook.py:webhook()` does only the FAST work synchronously — verify signature, handle echoes, dedupe by `mid`, persist the inbound row, rate-limit, funnel/name/phone capture, mute gate — then hands the SLOW work to `enqueue_background(process_inbound_reply, ...)` and returns `200` immediately. `process_inbound_reply` (same file) builds the prompt, calls Claude, sends the reply (with a typing indicator), and fires handoffs. **Why:** a fast 200 stops Facebook retrying the delivery on cold starts / slow model hops — the root of the phantom rate-limit. When editing reply behaviour, edit `process_inbound_reply`, NOT the webhook loop. `enqueue_background` runs **inline under TESTING** (deterministic + in-memory SQLite is per-connection). A background failure = no reply, logged, no crash, no FB retry.

## Test conventions

```powershell
python -m pytest -q                # full suite
python -m pytest tests/test_X.py -v --tb=short  # single file, verbose
```

Current count: 276 tests. They must all pass before any commit.

**Reply-quality eval (costs money, opt-in):** `python scripts/eval_replies.py --run
[--persona file.txt]` runs 6 fixed customer scenarios through the real prompt + Claude and
scores length / filler particles / registration link / handoff-only-when-needed / no
price dump. Run it before AND after any persona, snippet or prompt-rule change — paste
the live persona from the admin UI into a file for `--persona` so you eval what prod
runs. Without `--run` it only lists the scenarios.

Gotchas the test suite learned the hard way:
- `db.session` is shared across tests. Mutating User/GeneralSetting between tests requires `db.session.remove()` and/or `expire_all()`.
- `flask-limiter` stays on even when `RATELIMIT_ENABLED=False` is set in Flask config — disable it directly in fixtures: `from extensions import limiter; limiter.enabled = False`.
- The `client` fixture is per-test; cookies don't leak between tests. Session-scoped state (`db.session` identity map) does — see above.
- The `_clear_setting_cache` autouse fixture in conftest handles `g._setting_cache` for you and `g._login_user` (Flask-Login's cached user — a detached `User` from an earlier test otherwise raises DetachedInstanceError on the next request).

## CSRF — no manual header needed

`templates/base.html` lines ~494–518 install a fetch interceptor that auto-adds `X-CSRFToken` on every non-GET same-origin request. JS code in templates can `fetch(url, { method: 'POST', body: ... })` without thinking about CSRF.

The Facebook webhook is the only POST exempt — `@csrf.exempt` in `routes/webhook.py`.

## Common admin paths (sidebar labels in Mongolian)

| Sidebar | Route |
|---|---|
| Хяналтын самбар | `/admin/dashboard` |
| Гүйцэтгэл | `/admin/performance?days=7\|30` (admin+ only — per-staff numbers) |
| Ажлын даалгавар | `/admin/work-tasks` (default tab: `hot_prospects`) |
| Мессежийн түүх | `/admin/logs` |
| Бизнесийн удирдлага | `/business-management/general` (admin+) |
| Ботын удирдлага | `/bot-management/training` (admin+; tabs: AI сургалт · FAQ · Ботын тохиргоо) |
| Гарын авлага | `/admin/train-ai-guide` (staff+) |
| Систем | `/admin/admins` (admin+) |

## Bug surfaces seen recently — be careful here

- **"The bot sent wrong/old info" may not be the bot (2026-11-24).** Meta Business Suite →
  Inbox → Automations can message customers too; magicbot never replies to comments (it
  only sends to a PSID, never a `comment_id`). A private reply labelled **"Automated
  response"** under "You are responding to a user comment" is a Meta comment-to-message
  automation — 29 of them (one per keyword spelling, spring texts promising an April class
  and old price lists) were replaced by ONE evergreen automation "Мэдээлэл авах — чатаар
  хариулна (2026-11)" that only invites the customer to ask (keywords are case-insensitive
  "contains", max 10 per automation); the old ones are switched off, not deleted. Their
  echoes reach the webhook untagged, so they count as staff replies (30-min mute + stored as
  `sender='staff'`) for customers already in the DB — the echo log line now prints
  `app_id` to learn how to tell an automation from a person before changing that.

- `services/__init__.py:trigger_handoff` used to auto-mute the bot via `mute_duration_hours`. That's been removed — DO NOT add it back; the bot keeps advisory-replying after a *handoff* until either staff `take_over_chat` OR the human-takeover auto-mute fires (see below).
- **Human-takeover auto-mute:** every bot-sent Messenger message carries `metadata=BOT_ECHO_TAG` (`services/__init__.py:send_facebook_message`). The webhook handles `message_echoes`: an echo WITHOUT that tag = a human agent replied in the FB Page inbox → `routes/webhook.py:human_takeover_pause` sets `bot_muted_until = now + HUMAN_TAKEOVER_MUTE_MINUTES` (30, env-tunable). Echoes must `continue` before the inbound pipeline (sender is the Page, recipient the customer). Requires the `message_echoes` webhook field enabled in the Meta dashboard — without it FB never sends echoes and this path is inert. NEVER drop the metadata tag: an untagged bot echo would make the bot mute *itself* on every reply. Since 2026-11-16 the untagged echo is also SAVED as `Message(sender='staff')` (`store_staff_reply`, deduped by `mid`, attachments → `[хавсралт]`): the bot's history sees staff turns as the Page's side (`assistant`), the summary labels them "Ажилтан", the conversation view shows a yellow "Ажилтан" bubble. Which staff member sent it is unknowable (echoes don't say).
- **Webhook idempotency / "rate-limited on the first message":** Facebook delivers at-least-once and RETRIES the same event (same `message.mid`) when the webhook is slow — a Render cold start after weeks idle, or a slow model hop (the reply is still generated *synchronously* before we return 200). Each retry used to re-enter the handler and increment the per-sender rate limiter, so a single first message could trip "5 msgs / 60s" and emit the throttle text — and the bot replied twice. Fix: `Message.mid` is a UNIQUE idempotency key; `routes/webhook.py` persists the inbound row keyed by `mid` and drops a retry on `IntegrityError` BEFORE the rate limiter and before generate/send. Do NOT move the rate-limit check above this claim, and keep the claim committed before the model call. SQLite can't `ADD COLUMN ... UNIQUE`, so `ensure_schema()` adds `message.mid` then `CREATE UNIQUE INDEX` separately (multiple NULLs allowed → bot/legacy rows are fine). The webhook now also ACKs 200 immediately and generates the reply in the background (see "Webhook is async"), so FB largely stops retrying in the first place — the `mid` dedup is the belt-and-suspenders guarantee.
- `seed_default_magic_links` historically missed a `GeneralSetting` import — it's fixed, but any new helper that touches `GeneralSetting.query` must import it from `models`.
- `classification_lookback_days` controller lives at the top of `work_tasks.html` (outside any tab) so it's visible from the default Hot Prospects landing. Don't move it back into a single tab.
- **SQLite WAL + busy_timeout in `app.py:88`** is load-bearing — without it, 2 gunicorn workers + admin polling deadlock on SQLite locks. Do not remove the `@event.listens_for(Engine, "connect")` block.
- **`get_facebook_user_info` returns HTTP 400 at Standard Access** — Meta restricted PSID profile lookups. The bot now asks customers for their name on first contact (`services/_prompt.py:481+` injects the rule; `routes/webhook.py:107+` captures the reply via `extract_name_from_reply`). When App Review for "Business Asset User Profile Access" lands, the existing API call will start succeeding and the ask-for-name path silently becomes redundant. Don't remove either path until BAUPA is approved AND re-tested in prod.
- **A dead Page token pages staff from the send path, not just the health check** (added 2026-09-07 after a 3-day silent outage). `send_facebook_message` calls `alert_facebook_token_failure(raw_body)` on every non-200; `_is_facebook_token_error` pages only on a genuinely dead credential — code `190` (expired 463 / password-changed 460), `102`, and `100/33` (a USER token deployed where a Page token belongs). It deliberately does NOT page on permission errors (`10`, `200`) or per-conversation messaging-window errors, because an alert that cries wolf gets ignored. Cooldown `FB_TOKEN_ALERT_COOLDOWN_HOURS` (6), claimed by committing the `GeneralSetting` row BEFORE sending — a dead token fails every message on every worker at once, and without the pre-claim that storm pages staff hundreds of times. Same pattern as `alert_model_failure`; separate state key. `token_health_task` routes through the SAME function (via `report_token_health`), so one outage pages staff once, not once per detection path — and only genuinely ambiguous failures (Graph unreachable, a timeout) get the hedged "might not be able to reply" wording. **What actually failed on 2026-09-04 was the response, not the detection:** Telegram was configured and ~10 identical alerts arrived over three days, but the old health-check text hedged ("байж магадгүй") and had no cooldown, and the token expired at 22:23 Friday Ulaanbaatar time — the start of a weekend. The durable fix was the non-expiring Page token, not faster alerting. See OPERATIONS.md §4–§5.
- **Undelivered replies survive in the DB.** `process_inbound_reply` commits the bot `Message` row *before* `send_facebook_message`. So during a send-path outage the history in Мессежийн түүх shows what each customer should have received; the bot never resends it. Useful for manual follow-up after any token outage.
- **`import netrc` at the top of `services/__init__.py` is load-bearing** (added PR #11, 2026-07-06) — do NOT remove it. `requests` does a LAZY `from netrc import ...` inside `get_netrc_auth()` during `prepare_request`. On a *cold* gunicorn worker, a first-contact webhook (`get_facebook_user_info` → `requests.get`, which runs *synchronously* in the webhook) racing a background reply thread that is also importing can deadlock on CPython's import lock → 60s `WORKER TIMEOUT` → `POST /webhook 500` → that customer gets no reply (the log's `SIGKILL! Perhaps out of memory?` is misleading — it's a timeout). Eager-importing netrc at startup warms `sys.modules` so the lazy import is a no-op that never takes the lock mid-request; protects every `requests` call, not just that path. Intermittent — only cold workers + first-contact senders; returning users skip `get_facebook_user_info` entirely.

- **Staff-action notes:** dropping a hot prospect or a lead **requires** a reason
  (the note modal in `work_tasks.html`); resolving an issue takes an **optional**
  note. The reason is stored on `FacebookUser.notes` (or `AdminIssue.notes` for
  issues) AND the full text goes into the audit-log `detail` — the durable copy
  that survives `cleanup_old_records`' 60-day purge. The **Орхисон** tab lists
  dropped users with their reason and a **Сэргээх** (restore → `status='new'`)
  button. `update_lead_status` enforces the required note only when
  `status == 'dropped'`.

## Worker-timeout diagnostic (2026-11-16)

Intermittent `[CRITICAL] WORKER TIMEOUT` → `SIGKILL! Perhaps out of memory?` one second later
(4× on 2026-11-16, some before that day's deploys; no "database is locked" logged; one was an
echo for a customer NOT in the DB, i.e. no write at all). Cause NOT found yet. `gunicorn.conf.py`
(auto-loaded from the working directory) arms `faulthandler.dump_traceback_later` per request and
cancels it when the request ends: a request stuck past `REQUEST_STACK_DUMP_SECONDS` (45) writes
every thread's stack to stderr. After the next timeout, search the Render logs for
`most recent call first` and fix the frame it shows — don't guess. Boot log proof the file loaded:
`Request stack-dump watchdog on (45s) pid=…` once per worker.

## Telemetry shortcuts — first place to look when user says "bot is broken"

**Step 0 — run `python scripts/diagnose.py` in the Render shell of `magicbot`.**
It is read-only and collapses the whole list below into one command: env vars +
which reply provider is live, FB token reachable *and actually a Page token*
(it asks `me` and asserts the id is `FACEBOOK_PAGE_ID` — a User token reads the
Page by id, so only that comparison catches the trap), Page webhook
subscription (`messages` / `message_echoes`), a **live** call to the deployed
`REPLY_MODEL` (separates retired-preview-model 404 / quota 429 / bad key 401 /
the empty-reply thinking trap), and how long ago the last inbound and outbound
messages were — which alone splits "Facebook isn't delivering" from "the reply
or send path is broken". Full description: OPERATIONS.md §0.

If it is all green, the outage is per-conversation (mute / takeover / handoff),
not global. Otherwise, the manual checks:

1. **Customers getting the canned apology** ("Уучлаарай, түр зуурын саатал...") → the **Claude API** is failing, NOT Facebook (apology delivered = Send API fine). Render logs query `Error generating response` shows the exception: `You have reached your specified API usage limits` (400) = the Console's monthly spend limit → raise it at console.anthropic.com → Settings → Limits, or the bot stays down until the 1st of next month (the 2026-11-12 → 11-16 outage: 4 days, 110 customers got the apology, and no alert fired because `_is_account_error` didn't know this wording); `credit balance is too low` (400) = account out of prepaid credit → instant recovery on top-up at console.anthropic.com → Plans & Billing (the 2026-06-11 outage was this failure mode on the old provider); `authentication_error` (401) = bad/revoked `ANTHROPIC_API_KEY`. The deduplicated Telegram alert (`alert_model_failure`, cooldown `MODEL_ALERT_COOLDOWN_HOURS`=6) fires on exactly those account-level errors; a `429` rate limit or `529` overloaded degrades to the apology WITHOUT paging (transient, self-heals). Also scan the boot log for `ANTHROPIC_API_KEY is not set`, and query `Empty reply from` — that is thinking exhausting `REPLY_MAX_TOKENS`, fix by raising it.
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
