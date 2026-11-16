# Гүйцэтгэл (Performance Page) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** An admin-only page showing bot results, team inbox response in staff hours, per-staff admin actions, the bot's unanswered questions (with "Snippet болгох"), and a busy-hours heat-map, over 7 or 30 days.

**Architecture:** Two new nullable columns record what the data can't infer (`facebook_user.lead_at`, `message.kind`). A pure-ish `services/_performance.py` computes the whole report in one pass over the window's messages plus a few aggregate queries; a server-rendered template shows it. The training page pre-fills the snippet modal from query params.

**Tech Stack:** Flask, SQLAlchemy (SQLite), Jinja + Bootstrap 5, pytest.

**Spec:** `docs/superpowers/specs/2026-11-16-performance-page-design.md`

## Global Constraints

- Route `GET /admin/performance?days=7|30`, default 7, anything else → 7; `@login_required @admin_required`.
- `Message.kind`: `'step_in'` | `'nudge'` | NULL. Replies = bot messages with kind ≠ 'nudge'.
- Windows in UTC `[now-days, now]`; displayed times and heat-map buckets in UB (UTC+8, `services._schedule.UB_OFFSET`).
- Mongolian UI copy; English code. 230 existing tests stay green.
- Never call `bootstrap.*` at page-script init (base.html loads it after content) — only on `window.load` / in handlers.

## Review Focus

1. A conversation whose first message in the window is a bot/staff reply to a customer message from BEFORE the window — must not create a wait or a negative/huge delay.
2. Two customers interleaved in time — waits must be computed per conversation, never across customers.
3. A staff-hours wait at 08:59 UB vs 09:00 UB — bucketed by the wait's UB time (09:00 counts).
4. Handoff content not in the `[бот: …]` shape (older/manual issues) — counted as "бусад", never crashes.
5. A registration_staff user opening `/admin/performance` — refused, and no sidebar link.

---

### Task 1: Data markers (`lead_at`, `kind`)

**Files:** Modify `models.py`, `services/_seed.py`, `routes/webhook.py` (phone capture, `complete_registration`, `process_inbound_reply`), `routes/admin/work_tasks.py` (`promote_to_lead`), `services/__init__.py` (`nudge_pending_leads`); Test `tests/test_performance.py`

**Interfaces — Produces:** `FacebookUser.lead_at: DateTime | None`; `Message.kind: String(20) | None`.

- [ ] **Step 1: Failing tests** — `test_phone_capture_stamps_lead_at` (webhook inbound with a phone → `lead_at` set); `test_promote_to_lead_stamps_lead_at`; `test_registration_stamps_lead_at` (`complete_registration` with the form stubbed ok); `test_step_in_reply_is_kind_step_in` (`process_inbound_reply(..., waited_minutes=16)` → stored bot msg `kind == 'step_in'`; a normal call → `kind is None`); `test_nudge_is_kind_nudge` (`nudge_pending_leads` with send stubbed → stored msg `kind == 'nudge'`).
- [ ] **Step 2:** FAIL. **Step 3:** implement (only set `lead_at` when it is None). **Step 4:** PASS + full suite. **Step 5:** commit.

### Task 2: Report computation

**Files:** Create `services/_performance.py`; Test `tests/test_performance.py`

**Interfaces — Consumes:** Task 1 columns; `services._schedule.get_bot_schedule`, `staff_on_duty`, `UB_OFFSET`; `services.HANDOFF_CATEGORY_LABELS`. **Produces:** `performance_report(days: int, now: datetime | None = None) -> dict` with keys:
- `since` (UTC datetime), `days`
- `bot`: `people`, `replies`, `median_reply_seconds` (int | None), `new_leads`, `leads_since` (UTC | None), `registrations`, `handoffs` (dict label→n incl. 'бусад'), `step_ins`
- `team`: `waits`, `staff`, `step_in`, `bot`, `unanswered`, `staff_pct` (int | None), `median_staff_minutes` (int | None), `long_waits`, `threshold_minutes`, `tracking_since` (UTC | None)
- `staff`: list of `{username, role, resolved, avg_resolution_minutes, takeovers, contacted, converted, dropped}` ordered by username
- `questions`: list (≤ 30) of `{reason, count, last_seen_ub, sample, user_id}` newest group first
- `heatmap`: `{'grid': 7×24 ints, 'max': int, 'staff_cells': set of (weekday, hour)}`

- [ ] **Step 1: Failing tests** (frozen `now`, seeded rows, cleaned up): `test_waits_are_per_conversation` (Review Focus 2); `test_reply_to_pre_window_message_is_not_a_wait` (RF 1); `test_staff_hours_buckets_sum_to_waits` (staff / step-in / bot / unanswered with one of each, `staff_pct` 25); `test_wait_at_0900_is_staff_hours` (RF 3); `test_team_ignores_waits_before_first_staff_message`; `test_median_staff_minutes`; `test_long_waits_include_unanswered`; `test_bot_metrics` (people/replies exclude nudges, median reply seconds, step_ins, registrations, new_leads); `test_handoff_categories_parsed_and_other` (RF 4); `test_staff_rows_from_audit` (resolved via both detail shapes, takeovers, contacted/converted/dropped, avg resolution minutes); `test_questions_grouped_by_reason`; `test_heatmap_buckets_in_ub_time` (23:30 UTC Sun → Mon 07); `test_empty_db_has_no_crash`.
- [ ] **Step 2:** FAIL. **Step 3:** implement — load the window's messages plus each conversation's last message before the window (to know who spoke last), sort by (user, created_at, id), walk once. **Step 4:** PASS + full suite. **Step 5:** commit.

### Task 3: Page, sidebar, snippet pre-fill, docs

**Files:** Modify `routes/admin/work_tasks.py` (new `performance` view) or create `routes/admin/performance.py` and import it in `routes/admin/__init__.py`; create `templates/performance.html`; modify `templates/base.html` (sidebar), `templates/training.html` (pre-fill), `CLAUDE.md`; Test `tests/test_performance.py`

**Interfaces — Consumes:** Task 2 `performance_report`.

- [ ] **Step 1: Failing tests** — `test_admin_sees_all_sections` (200; contains "Бот", "Ажилтны баг", "Ажилтан тус бүр", "мэдэхгүй байсан асуултууд", "Хэзээ их бичдэг"); `test_days_param_falls_back_to_7` (`?days=99` → "7 хоног" active); `test_registration_staff_refused` (RF 5: not 200, and dashboard HTML has no `/admin/performance` link for them); `test_admin_sidebar_has_link`; `test_snippet_button_links_to_training_prefill` (question row → href with `snippet_title=`); `test_training_page_has_prefill_hook` (training HTML contains `snippet_title` param reader inside a `load` listener).
- [ ] **Step 2:** FAIL. **Step 3:** implement; check the page once in a browser on a throwaway local server. **Step 4:** PASS + full suite. **Step 5:** commit.
