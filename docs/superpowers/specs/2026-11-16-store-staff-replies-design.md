# Store staff Page-inbox replies as messages — Design (short)

- **Date:** 2026-11-16 · **Branch:** `feat/store-staff-replies`
- **Status:** Idea accepted by Gundeg ("I accept your ideas"; "let's continue")

## Intent

When staff answer a customer in the Facebook Page inbox, that reply exists only
in Facebook. The bot's 10-message memory and the admin conversation view never
see it, so after the takeover pause the bot can repeat or contradict staff, and
the coming performance dashboard has no data on staff replies.

## Approaches

1. **Store each untagged echo as a `Message` with `sender='staff'`** (picked) —
   one table, every existing view/query keeps working, dedupe by `mid`.
2. A separate `StaffReply` table — cleaner separation, but every consumer
   (bot history, summary, conversation view, logs, dashboard) needs a join.
3. Fetch the Page conversation from the Graph API on demand — no storage, but a
   network call per reply and it needs `pages_messaging` read scope.

## Design

- `routes/webhook.py` echo branch: an untagged echo (human agent) → besides the
  existing `human_takeover_pause`, `store_staff_reply(psid, text, mid)` saves
  `Message(sender='staff')`. Attachment-only echoes store `[хавсралт]`. A retry
  of the same echo (`mid` UNIQUE) is dropped. No customer row → nothing saved.
  The bot's own tagged echoes stay unsaved (already stored when sent).
- Bot reply history: staff turns go in as `assistant` (the customer saw them
  from the Page) — unchanged mapping, now with real content.
- Cross-session summary labels staff lines `Ажилтан`.
- Admin views: conversation bubble labelled "Ажилтан" (Page side, distinct
  colour); Мессежийн түүх badge + filter option "Staff".

## Non-goals

Which staff member replied (Facebook echoes don't say); back-filling replies
sent before the deploy; the dashboard itself (next).
