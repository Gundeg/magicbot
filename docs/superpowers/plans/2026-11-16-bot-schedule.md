# Bot Working Schedule + Step-in Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The bot stays quiet while staff are on shift, covers nights / lunch / weekends, and steps in when staff leave a customer unanswered for N minutes — all set from the admin panel.

**Architecture:** A pure schedule module (`services/_schedule.py`) owns the JSON setting, validation, UB-time math and Mongolian wording. The webhook consults it before enqueuing a reply; a 60 s background sweep in `routes/webhook.py` re-uses `process_inbound_reply` for step-ins, claimed atomically per customer. Office hours everywhere read the schedule.

**Tech Stack:** Flask, SQLAlchemy (SQLite in prod), Jinja + Bootstrap 5, pytest.

**Spec:** `docs/superpowers/specs/2026-11-16-bot-schedule-design.md`

## Global Constraints

- Setting key `bot_schedule`; `mode` ∈ `schedule | always_on | off`; `days[0]` = Monday; times `HH:MM`, Ulaanbaatar = UTC+8, no DST.
- Defaults: Mon–Fri staff 09:00–18:00, break 13:00–14:00; Sat/Sun no staff; `mode=schedule`; step-in enabled, 15 min. Minutes range 1–240.
- Step-in window: message age in [N, N+60] minutes. Never step in when muted or when `last_staff_reply_at >= message.created_at`.
- All customer/admin copy in polished Mongolian; code/comments English.
- 168 existing tests stay green. Tests default the bot to `always_on` (autouse fixture) so they never depend on the wall clock.

## Review Focus

1. Shift boundaries — 09:00 staff, 13:00 bot, 14:00 staff, 18:00 bot (end-exclusive). Task 1 boundary test.
2. UB vs UTC day — 23:30 UTC Sunday is 07:30 UB Monday; weekday must come from UB time. Task 1 test.
3. Corrupt / partial `bot_schedule` row — fall back to defaults, webhook never crashes. Task 1 test.
4. Several customer messages while waiting — exactly one step-in reply. Task 3 test.
5. Staff ON with missing/inverted times, or a break outside the shift — rejected with 400, nothing saved. Task 4 test.

---

### Task 1: Schedule module

**Files:** Create `services/_schedule.py`; Test `tests/test_bot_schedule.py`

**Interfaces — Produces:**
- `SCHEDULE_KEY = 'bot_schedule'`, `DEFAULT_SCHEDULE: dict`
- `ub_now() -> datetime` (naive, UB local = `utcnow() + 8h`)
- `validate_schedule(data) -> tuple[dict | None, str | None]` — (clean, Mongolian error)
- `get_bot_schedule() -> dict` — DB row validated, else `deepcopy(DEFAULT_SCHEDULE)`
- `staff_on_duty(schedule, at) -> bool` (`at` = UB-local datetime)
- `bot_should_reply(at=None, schedule=None) -> bool` — `always_on` → True, `off` → False, else `not staff_on_duty`
- `next_staff_start(schedule, at) -> datetime | None` — next instant staff come on duty (search ≤ 8 days)
- `describe_staff_hours(schedule) -> str` — groups consecutive identical days: `"Даваа–Баасан 09:00–18:00 (цайны цаг 13:00–14:00); Бямба, Ням — амралтын өдөр"`
- `describe_next_staff_time(at=None, schedule=None) -> str` — `"өнөөдөр 14:00 цагаас"` / `"маргааш 09:00 цагаас"` / `"Даваа гарагт 09:00 цагаас"`; `''` when no staff day exists
- `bot_status(at=None, schedule=None) -> dict` — `{'replying': bool, 'label': str}` for the UI

- [ ] **Step 1: Failing tests** — `test_default_matches_gundegs_schedule` (Mon 10:00 staff; Mon 13:30 bot; Mon 18:00 bot; Mon 08:59 bot; Sat 11:00 bot); `test_boundaries_end_exclusive` (09:00 staff, 13:00 bot, 14:00 staff, 18:00 bot); `test_ub_now_shifts_day` (monkeypatched utcnow 2026-11-15 23:30 Sun → UB Mon 07:30); `test_modes` (always_on during staff hours → True; off at Sat night → False); `test_validate_rejects` (end ≤ start; break outside shift; one break field only; minutes 0 / 241; bad `HH:MM`; unknown mode) each returns `(None, msg)` with non-empty msg; `test_validate_accepts_no_break`; `test_corrupt_row_falls_back` (row value `'{not json'` and `'{"mode":"x"}'` → defaults); `test_describe_staff_hours_default` equals the string above; `test_next_staff_time_wording` (Mon 13:20 → "өнөөдөр 14:00 цагаас", Mon 19:00 → "маргааш 09:00 цагаас", Fri 19:00 → "Даваа гарагт 09:00 цагаас", Sat 10:00 → "Даваа гарагт 09:00 цагаас").
- [ ] **Step 2:** `python -m pytest tests/test_bot_schedule.py -q` → FAIL (module missing).
- [ ] **Step 3:** Implement. Compare times as minutes-since-midnight; a staff day is on duty when `start ≤ t < end` and not `break_start ≤ t < break_end`. `validate_schedule` returns a fresh normalised dict (all 7 days present, minutes int).
- [ ] **Step 4:** Tests PASS.
- [ ] **Step 5:** Commit `services/_schedule.py tests/test_bot_schedule.py`.

### Task 2: Office hours read the schedule; step-in wait line in the prompt

**Files:** Modify `services/__init__.py` (`_is_off_hours`, `HANDOFF_USER_REPLY_OFF_HOURS`, `trigger_handoff`, `_static_handoff_reply`, `generate_bot_response`), `services/_prompt.py` (`_format_company_contact_block`, `build_system_blocks`); Test `tests/test_bot_schedule.py`

**Interfaces — Consumes:** Task 1 `bot_should_reply`, `staff_on_duty`, `describe_staff_hours`, `describe_next_staff_time`, `get_bot_schedule`, `ub_now`. **Produces:** `generate_bot_response(..., waited_minutes=0)`, `build_system_blocks(..., waited_minutes=0)`.

- [ ] **Step 1: Failing tests** — `test_off_hours_follows_schedule` (`_is_off_hours()` False at Mon 10:00 UB, True at Mon 13:30); `test_off_hours_handoff_reply_names_next_staff_time` (`_static_handoff_reply()` at Mon 19:00 contains "маргааш 09:00 цагаас", no `{start}`); `test_prompt_lists_staff_hours` (`build_system_prompt()` contains `describe_staff_hours(DEFAULT_SCHEDULE)`); `test_waited_line_only_on_step_in` (`build_system_blocks(waited_minutes=15)[1]` contains "15 минут", `waited_minutes=0` does not).
- [ ] **Step 2:** FAIL.
- [ ] **Step 3:** Implement. `_is_off_hours()` = `not staff_on_duty(get_bot_schedule(), ub_now())`. Off-hours copy: `"Таны мессежийг хүлээж авлаа 🌙 Одоогоор ажлын цаг биш тул манай ажилтан {when} тантай холбогдоно. Та энэ хооронд асуулт асууж, надтай үргэлжлүүлэн ярилцаж болно."` with `{when}` = `describe_next_staff_time()` (fallback `"ажлын цагт"`). Contact block line: `"Ажлын цаг (Улаанбаатарын цагаар): {describe_staff_hours}. Энэ цагийн гадуур бичсэн хэрэглэгчид ажилтан хэзээ хариулахыг зөөлөн хэлж болно."` Volatile line when `waited_minutes > 0`: `"АНХААР: Энэ хэрэглэгч ажилтны хариуг {N} минут хүлээсэн. Хүлээлгэсэнд нэг өгүүлбэрээр товч уучлал гуйгаад асуултад нь шууд хариул."`
- [ ] **Step 4:** Targeted + full suite PASS.
- [ ] **Step 5:** Commit.

### Task 3: Webhook gate, staff-reply stamp, step-in sweep

**Files:** Modify `models.py` (FacebookUser `bot_step_in_at`, `last_staff_reply_at`), `services/_seed.py` (`ensure_schema` facebook_user block), `routes/webhook.py`, `app.py` (start loop), `tests/conftest.py` (autouse always_on); Test `tests/test_bot_schedule_webhook.py`

**Interfaces — Consumes:** Task 1 `bot_should_reply`, `get_bot_schedule`; Task 2 `waited_minutes`. **Produces:** `process_inbound_reply(fb_user_id, message_text, session_state, waited_minutes=0)`, `step_in_sweep(now=None) -> list[int]` (UTC `now`; returns stepped-in user ids), `step_in_task(app)` (60 s loop, never raises).

- [ ] **Step 1: Failing tests** (monkeypatch `services._schedule.ub_now`, `wh.generate_bot_response`, `wh.send_facebook_message`, `wh.check_rate_limit`; save explicit `bot_schedule` rows): `test_staff_hours_inbound_saved_but_no_reply`; `test_bot_hours_inbound_gets_reply`; `test_mode_off_never_replies`; `test_staff_echo_stamps_last_staff_reply_at`; `test_step_in_replies_after_n_minutes` (user msg 16 min old, staff hours → one bot Message, prompt got `waited_minutes=15`); `test_step_in_skips_when_staff_replied` (`last_staff_reply_at` after the msg); `test_step_in_skips_muted`; `test_step_in_skips_too_young_and_too_old` (10 min, 80 min); `test_step_in_disabled_or_mode_not_schedule`; `test_step_in_once_for_burst` (3 msgs, sweep twice → 1 reply); `test_step_in_claim_is_atomic` (second claim UPDATE for same msg → rowcount 0).
- [ ] **Step 2:** FAIL.
- [ ] **Step 3:** Implement. Webhook: after the mute gate, `if not bot_should_reply(): enqueue_background(classify_user_topics, fb_user.id); continue`. `human_takeover_pause` also sets `last_staff_reply_at = utcnow()`. Sweep: returns `[]` unless `mode == 'schedule'` and step-in enabled; candidates = each user's latest Message is `sender='user'` with `created_at` in `[now-N-60, now-N]`, not muted, staff stamp older than the message; claim with
  `UPDATE facebook_user SET bot_step_in_at=:now WHERE id=:id AND (bot_step_in_at IS NULL OR bot_step_in_at < :msg_at)` (commit; rowcount 1 wins); then `process_inbound_reply(id, msg.content, classify_session(prev_msg_at), waited_minutes=N)`. Loop: sleep 60 s, `app.app_context()`, log + swallow errors. `app.py`: start `step_in_task` inside `_run_background_loops`. Conftest autouse: `monkeypatch.setitem(_schedule.DEFAULT_SCHEDULE, 'mode', 'always_on')` and remove any `bot_schedule` row after each test.
- [ ] **Step 4:** Targeted + full suite PASS.
- [ ] **Step 5:** Commit.

### Task 4: Admin settings card, dashboard status, docs

**Files:** Modify `routes/admin/bot.py`, `templates/bot/settings.html`, `routes/admin/work_tasks.py` (dashboard `health`), `templates/dashboard.html`, `tests/test_admin_cleanup.py`, `CLAUDE.md`; Test `tests/test_bot_schedule_admin.py`

**Interfaces — Consumes:** Task 1 `validate_schedule`, `get_bot_schedule`, `bot_status`, `SCHEDULE_KEY`.

- [ ] **Step 1: Failing tests** — `test_save_valid_schedule` (POST `/bot-management/settings` JSON `{bot_schedule: {...}}` → 200, row stored normalised); `test_save_invalid_schedule_400_nothing_saved`; `test_settings_page_renders_schedule_card` (GET contains "Ботын ажлын цаг" and 7 day rows); `test_dashboard_shows_bot_status` (GET `/admin/dashboard` contains the `bot_status()['label']`); update `test_admin_cleanup` — `office_hours_*` no longer in `BOT_SETTINGS_KEYS`, `bot_schedule` is.
- [ ] **Step 2:** FAIL.
- [ ] **Step 3:** Implement. `BOT_SETTINGS_KEYS` drops `office_hours_start/end`, adds `bot_schedule`; the POST validates `bot_schedule` first and returns `{'success': False, 'error': msg}, 400` before writing anything; stores `json.dumps(clean, ensure_ascii=False)`. Card replaces the hours inputs: mode radios (Хуваарийн дагуу / Үргэлж ажиллана / Унтраах), 7-row table (Ажилтан ✓, эхлэх, дуусах, цай эхлэх, цай дуусах — `type=time`), step-in switch + minutes, live status line from `bot_status()`. Dashboard health row "Бот одоо" with the label. CLAUDE.md: schedule in prompt section, latent table row for `office_hours_*`, background-task entry for the sweep, test count.
- [ ] **Step 4:** Full suite PASS; render the settings page locally once and look at it.
- [ ] **Step 5:** Commit.
