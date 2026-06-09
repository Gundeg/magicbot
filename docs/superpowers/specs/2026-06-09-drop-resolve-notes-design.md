# Staff-action notes (resolve / drop) + dropped-leads archive — Design

- **Date:** 2026-06-09
- **Branch:** `feature/drop-resolve-notes`
- **Status:** Approved (ready for implementation plan)

## Problem

When staff take a terminal action on the Work Tasks page they leave no record of
*why*. Three actions need an attached note:

1. **Resolve an issue** (`resolve_issue`) — how/why it was closed.
2. **Drop a hot prospect** (`drop_prospect`).
3. **Drop a lead** (`update_lead_status` → `dropped`).

Today only the rich `update_status` issue path captures a note; the one-click
resolve and both drop paths capture nothing. Dropped leads also vanish from every
list, so a mistaken drop is unrecoverable from the UI and the reason is lost.

## Decisions (from brainstorming)

| Question | Decision |
|---|---|
| Where does a drop note live? | **On the lead** (`FacebookUser.notes`) — visible in the conversation viewer — **and** the full reason in the durable audit log. |
| Required or optional? | **Required for the two drops; optional for resolve.** |
| How is the note entered? | **A small shared modal with a textarea** (Save disabled until text entered when required). |
| Browse dropped leads? | **Yes — add a dropped-leads archive tab** (user opted in, accepting it only shows the ~60-day pre-cleanup window). |
| Restore from archive? | **Yes — include a "Сэргээх" (Restore) button.** |

### Durability note (why the audit copy matters)

`cleanup_old_records` (`routes/admin/work_tasks.py:639`) hard-deletes
terminally-closed leads (`converted`/`dropped`) older than
`CLEANUP_RETENTION_DAYS` (60) — including their `notes` — but it **never deletes
`AuditLog`** rows. So `FacebookUser.notes` is the convenient short-term copy and
`AuditLog.detail` (a `db.Text`, no length cap) is the lasting record. The drop
handlers therefore write the **full** note into the audit `detail`, not a snippet.

## Design

### 1. Data model

- `models.py`: add `notes = db.Column(db.Text)` to `FacebookUser`.
- `services/_seed.py` `ensure_schema()`: add `'notes': 'notes TEXT'` to the
  `facebook_user` `add_columns({...})` block so the live Render DB self-migrates
  on next boot (mirrors the existing `admin_issue.notes` entry).
- `AdminIssue.notes` already exists — no change.

### 2. Capturing the note — handlers in `routes/admin/work_tasks.py`

All three read `note = (data.get('note') or '').strip()`.

- **`drop_prospect`** — if `note` empty → `return jsonify({'success': False,
  'error': 'Шалтгаан заавал бичнэ үү.'}), 400` **before** mutating. Else set
  `user.lead_status = 'dropped'`, `user.notes = note`, commit, and include the
  full note in the audit `detail`.
- **`update_lead_status`** — when `status == 'dropped'`: same required-note rule
  + `user.notes = note` + full note in audit detail. For **any other** status the
  behaviour is unchanged and no note is read/required.
- **`resolve_issue`** — note **optional**: set `issue.notes = note` only when
  non-empty (never blank out an existing note); append to the audit `detail` when
  present.

Audit actions stay as-is (`lead.drop`, `lead.status_change`,
`issue.status_change`); only the `detail` string gains the reason.

### 3. Note-entry UX — shared modal in `templates/work_tasks.html`

One Bootstrap modal (`#noteModal`, title "Тэмдэглэл") with a `<textarea>` and
Хадгалах / Болих buttons. A helper:

```js
openNoteModal({ title, placeholder, required, onSave })
```

- Resets + focuses the textarea on open.
- When `required`, Хадгалах is disabled until the trimmed value is non-empty.
- On Хадгалах, calls `onSave(noteText)` and hides the modal.

Rewire the three existing handlers to open the modal first, then post the note in
the callback:

- `.hot-drop` → `openNoteModal({required:true, onSave:n => hotAction(..., {note:n})})`
- `.lead-status-option` where `newStatus==='dropped'` → open modal, then POST
  `/admin/api/lead-status` with `{user_id, status:'dropped', note}`. Non-terminal
  statuses keep the current immediate-POST path.
- `.resolve-btn` → `openNoteModal({required:false, onSave:n => post({action:'resolve_issue', id, note:n})})`

`hotAction` gains an optional extra-payload arg so it can carry `note`.
Takeover / promote / unmute flows are untouched. CSRF is already handled globally
(`templates/base.html` fetch interceptor).

### 4. Where notes are shown

- **Conversation viewer** (`templates/conversation.html`): add a "Тэмдэглэл" row
  to the Хэрэглэгч card (after Бүртгэлийн төлөв), shown only when
  `fb_user.notes`.
- **Issue rows** already render `issue.notes` in `work_tasks.html` — resolve notes
  appear there automatically.
- **Audit log** holds the durable full copy.

### 5. Dropped-leads archive tab

- **Backend** (`work_tasks` view): query
  `FacebookUser.query.filter_by(lead_status='dropped').order_by(updated_at.desc()).limit(100)`
  → `dropped_leads`. Covers **both** dropped hot-prospects (`is_lead=False`) and
  dropped leads (`is_lead=True`); a per-row badge distinguishes them. (Note:
  `'converted'` is a win, not a drop — exclude it; filter on `'dropped'` exactly.)
- **Template**: a 6th tab **"Орхисон"** with a table:
  Нэр · Утас · **Шалтгаан** (`notes`) · Орхисон огноо (`updated_at`) ·
  badge (Лид / Сонирхогч) · conversation link · **Сэргээх** button.
- **Restore**: `.restore-lead` button → POST `/admin/api/lead-status`
  `{user_id, status:'new'}` (reuses the existing endpoint; no new handler). On
  success the row fades out of the archive. Logged via the existing
  `lead.status_change` audit action.

### Out of scope

- No note on non-drop status changes (contacted/qualified/converted).
- No lost-lead analytics/report — the audit log already holds the data; build a
  filtered report later only if needed.
- Archive shows only the un-purged (~60-day) tail by design; the audit log is the
  permanent record.

## Testing (extend `tests/test_lead_status.py`)

1. `drop_prospect` with empty note → 400, `lead_status` unchanged.
2. `drop_prospect` with note → 200, `lead_status='dropped'`, `notes` saved, audit
   detail contains the note.
3. Lead drop via `/admin/api/lead-status` `status='dropped'` empty note → 400, not
   dropped.
4. Same with a note → dropped + `notes` saved.
5. Non-dropped status change (e.g. `contacted`) → succeeds with no note required.
6. `resolve_issue` with no note → still resolved; `notes` untouched.
7. `resolve_issue` with note → `issue.notes` saved.
8. Dropped tab / query returns dropped users with their notes.
9. Restore (`status='new'` on a dropped user) → user leaves the dropped set.

All 62 existing tests must continue to pass.

## Files touched

`models.py` · `services/_seed.py` · `routes/admin/work_tasks.py` ·
`templates/work_tasks.html` · `templates/conversation.html` ·
`tests/test_lead_status.py`

## Deploy notes

`ensure_schema()` runs on boot and adds `facebook_user.notes` automatically — no
manual migration. SQLite + Postgres compatible (`ADD COLUMN ... TEXT`). Deploy is
push-to-`master` on Render; this work stays on `feature/drop-resolve-notes` until
reviewed.
