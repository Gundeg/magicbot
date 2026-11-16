# Bot working schedule + staff step-in safety net — Design

- **Date:** 2026-11-16
- **Branch:** `feat/bot-schedule`
- **Status:** Approved by Gundeg in chat ("i accept your suggestion … go ahead")

## Intent

Staff answer Messenger themselves during office hours; the bot covers the rest.
Gundeg's schedule: the bot works **18:00–09:00**, **13:00–14:00** (lunch) and
**all of Saturday and Sunday** — i.e. staff are on duty Mon–Fri 09:00–13:00 and
14:00–18:00. It must be adjustable from the admin panel later.

Safety net (Gundeg: "make the bot step in switch on/off with response time"):
during staff hours, if no staff member answers a customer within N minutes, the
bot answers. Switchable, N adjustable, default ON at 15 minutes.

## Approaches (picked: 1)

1. **Staff hours per weekday; the bot covers everything else.** Per day: staff
   working yes/no, start/end, one lunch break. The same hours drive what the bot
   promises customers ("staff reply at 09:00") — one source of truth.
2. Free-form bot time ranges per day — most flexible, but 18:00–09:00 must be
   typed as two ranges, and staff hours (for the ETA wording) would still need a
   second, contradictory setting.
3. One set of hours for all weekdays + a weekend switch — smallest, cannot
   express a staff Saturday morning or a short Friday later.

## Goals

- `GeneralSetting` key `bot_schedule` (JSON) holds:
  - `mode`: `schedule` | `always_on` | `off` (holidays, staff meetings).
  - `days`: 7 entries, Monday=0 (Python `weekday()`), each
    `{staff, start, end, break_start, break_end}`, times `HH:MM` Ulaanbaatar
    time (UTC+8, no DST). Break optional (both empty).
  - `step_in`: `{enabled, minutes}`; minutes 1–240.
- Defaults (no row saved yet) = Gundeg's schedule, `mode=schedule`, step-in ON
  at 15 min — so the deploy delivers what was asked without a manual step.
- **Bot off-duty** (staff hours, or `mode=off`): the inbound message is saved
  and the fast path (dedupe, rate limit, funnel/name/phone capture, mute gate)
  runs as today, but no reply is generated. The topic classifier still runs so
  Work Tasks stays current.
- **Step-in** (only `mode=schedule` + enabled): a background sweep every 60 s
  picks customers whose latest stored message is theirs, aged between N and N+60
  minutes, not muted, with no staff reply since it (`last_staff_reply_at`), and
  not already stepped-in for it. The bot then replies through the normal
  `process_inbound_reply` path, with one volatile-prompt line telling it the
  customer waited N minutes so it apologises briefly for the wait.
- Office hours become the schedule: `_is_off_hours()`, the off-hours handoff
  wording and the prompt's "Ажлын цаг" line all read the schedule. The off-hours
  reply names the next staff time ("өнөөдөр 14:00 цагаас", "маргааш 09:00
  цагаас", "Даваа гарагт 09:00 цагаас").
- Admin UI: a "Ботын ажлын цаг" card on Ботын удирдлага → Ботын тохиргоо (mode,
  7-day table, step-in switch + minutes, live "Одоо: …" status). The dashboard's
  Холболтын төлөв card shows the bot's current state for staff (who cannot open
  bot settings).

## Non-goals

- Holiday calendar (the `off` / `always_on` mode covers holidays by hand).
- Multiple breaks per day, per-staff shifts.
- Saving staff Page-Inbox replies as messages, and the performance dashboard —
  the next two items, separately designed.

## Anti-goals (failure even if it "works")

- The bot talks over staff: the takeover mute and a recent staff reply must
  always win over the schedule and the sweep.
- The bot answers twice: production runs the background loops in BOTH gunicorn
  processes (`WORKER_ROLE=worker` on the web service, `--workers 2`), so each
  step-in is claimed atomically:
  `UPDATE facebook_user SET bot_step_in_at=:now WHERE id=:id AND
  (bot_step_in_at IS NULL OR bot_step_in_at < :msg_time)` — rowcount 1 wins.
- A late, odd reply: messages older than N+60 minutes are never stepped in.

## Constraints

- Staff replies are not stored as messages; the staff echo (`human_takeover_pause`)
  already fires per reply — it now also stamps `FacebookUser.last_staff_reply_at`.
  The 30-minute takeover mute alone is not enough: with N > 30 a staff reply at
  minute 5 would have expired by minute N.
- Two new nullable columns on `facebook_user` (`bot_step_in_at`,
  `last_staff_reply_at`), added by `ensure_schema()` on boot.
- `office_hours_start` / `office_hours_end` form inputs are removed; old rows
  stay inert (CLAUDE.md latent-field table).
- The prompt's hours line lives in the cached stable half; it changes only when
  the schedule is saved.

## Builder's calls (mine, stated)

- New module `services/_schedule.py` (pure schedule math + validation + sweep).
- Times validated server-side: `start < end`, break inside the shift with
  `break_start < break_end`; invalid → 400 with a Mongolian message.
- Sweep loop started with the other loops (`_run_background_loops`), not
  env-gated — the admin switch governs it.

## Testing

Schedule math (weekday, lunch, night, weekend, bounds), validation, webhook gate
per mode, step-in selection / mute / staff-reply / claim / age window, the echo
stamping `last_staff_reply_at`, settings save (valid + invalid), prompt and
off-hours wording. All existing tests keep passing.
