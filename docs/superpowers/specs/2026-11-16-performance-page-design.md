# Гүйцэтгэл — bot + staff performance page — Design

- **Date:** 2026-11-16 · **Branch:** `feat/performance-page`
- **Status:** Approved by Gundeg in chat ("Yes, build it"; visibility "Admins only")

## Intent

Gundeg asked for a "bot and staff working performance dashboard" and accepted
the idea of a list of questions the bot could not answer, with a one-click way
to teach the bot the answer. The page answers: is the bot doing its job, are
staff answering customers in their hours, who on the team is doing the
follow-up work, what does the bot still not know, and when do customers write.

## Approaches (picked: 1)

1. **A new admin-only page, server-rendered, no chart library** — same look as
   Хяналтын самбар (cards, `health-row`), numbers computed in a pure-ish
   `services/_performance.py`; a CSS-opacity table for the heat-map.
2. Extra cards on Хяналтын самбар — that page is already dense and is visible
   to registration staff, who must not see colleagues' numbers.
3. Chart.js trend charts — prettier, but a CDN dependency and daily series the
   decisions above don't need yet.

## Goals

Route `GET /admin/performance?days=7|30` (default 7), `@admin_required`
(admin + super_admin). Sidebar link "Гүйцэтгэл" in Өдөр тутмын ажил, admins only.
All windows are `[now - days, now]` in UTC; display times in UB (UTC+8).

### 1. Бот
- Хариулсан харилцагч — distinct customers with a bot REPLY in the window
  (`sender='bot'` and `kind` not `'nudge'`).
- Хариултын дундаж хугацаа — MEDIAN seconds from a wait start (below) to the
  bot's reply, over waits answered by a normal (non-step-in) bot reply.
- Шинэ лид — `FacebookUser.lead_at` in window (NEW column, stamped where
  `is_lead` becomes True: phone capture, bot registration, Hot-Prospect
  promote). Counts only from the deploy on; the page says so.
- Бүртгэл — `FacebookUser.registered_at` in window.
- Ажилтанд шилжүүлсэн — `AdminIssue(issue_type='handoff')` created in window,
  per category, parsed from content `[бот: <label>( — <reason>)?]...` via
  `HANDOFF_CATEGORY_LABELS`; anything else counts as "бусад".
- Ажилтны оронд хариулсан — bot messages with `Message.kind == 'step_in'`
  (NEW nullable column `kind`: `'step_in'` written by `process_inbound_reply`
  when `waited_minutes > 0`; `'nudge'` written by `nudge_pending_leads`, whose
  follow-ups are not replies; NULL = an ordinary reply).

### 2. Ажилтны баг (Page inbox)
- A **wait** starts at a customer message whose previous message in that
  conversation is not the customer's (or there is none). Its **response** is
  the first later `staff` message or bot message whose `kind` is not `'nudge'`.
- Only waits that start in staff hours (current schedule, `staff_on_duty` at
  the wait's UB time) and after the first ever `staff` message (staff replies
  are only recorded since 2026-11-16) count here.
- Show four buckets that sum to the waits: answered by staff (n, %), by
  step-in, by an ordinary bot reply (e.g. holiday `always_on` mode, or a reply
  landing right after a shift starts), still unanswered. Plus median staff
  first-reply minutes and waited ≥ N minutes (N = current step-in minutes),
  unanswered included. Staff hours = `staff_on_duty` (mode ignored).
- Empty state: "Ажилтны хариуны мэдээлэл хуримтлагдаж байна".

### 3. Ажилтан тус бүр (admin-panel actions)
One row per User (all roles), from `AuditEntry.actor_id` in the window:
- Шийдсэн handoff — `issue.status_change` whose detail starts with
  "Work Tasks-аас шийдсэн" or contains "→ resolved".
- Дундаж шийдэх хугацаа — mean minutes `resolved_at - created_at` over handoff
  `AdminIssue`s with `updated_by_id` = user and `resolved_at` in window.
- Хариуцсан — `bot.take_over`.
- Холбогдсон — `lead.mark_contacted` + `lead.status_change` "→ contacted".
- Бүртгүүлсэн — `lead.status_change` "→ converted".
- Орхисон — `lead.drop` + `lead.status_change` "→ dropped".
A footnote: inbox replies can't be attributed to a person (Facebook echoes
don't say who), so reply speed is team-level only.

### 4. Ботын мэдэхгүй байсан асуултууд
`knowledge_gap` handoffs in the window, grouped by normalised reason
(lower-case, trimmed); newest group first; max 30 groups. Each: reason, count,
last seen (UB), one sample customer message, link to the conversation, and
**Snippet болгох** → `bot_management_training?snippet_title=<reason>&snippet_body=Асуулт: <message>\nХариулт: `.
`training.html` reads those params on `window.load` and opens the snippet
modal pre-filled (bootstrap is loaded after page content — never at init).

### 5. Хэзээ их бичдэг вэ
7 × 24 table (UB weekday × hour) of customer messages in the window, cell
shade ∝ count; cells inside current staff hours get an outline.

## Non-goals
Per-person inbox reply speed; trend charts; CSV export; historical schedules
(waits are judged against today's schedule); token cost (only in logs).

## Anti-goals
- Registration staff seeing colleagues' numbers (admin-only route and link).
- A slow page: one pass over the window's messages, no per-row queries.
- Misleading zeros for data recorded only from today (`lead_at`,
  staff replies) — label them.

## Builder's calls
- `services/_performance.py`: `performance_report(days, now=None)` → dict with
  `bot`, `team`, `staff`, `questions`, `heatmap`, `since`.
- Two nullable columns via `ensure_schema`: `facebook_user.lead_at`,
  `message.kind`.
- "Counting since" dates are derived, not hard-coded: first `staff` message
  for section 2, `MIN(lead_at)` for Шинэ лид; no data → empty-state text.
- Builder check (2026-11-16) raised the nudge marker, the third bucket and the
  since-dates — all settled above as builder's calls.

## Testing
Unit tests on seeded data for every metric's definition (wait detection,
staff-hours filter, medians, category parsing, audit mapping, grouping,
heat-map UB bucketing); route: admin 200 with each section, registration
staff refused, `days` other than 7/30 falls back to 7; training page carries
the prefill hook; `lead_at` stamped at all three lead paths;
`kind` = 'step_in' on step-in replies and 'nudge' on nudges only.
