# Course-specific registration links, website fallback, admin-owned behaviour

**Date:** 2026-07-07
**Status:** Approved (design)
**Branch:** `fix/registration-link-course-desync`

## Problem

A customer asked to register and the bot quoted an **old** registration link even
though the admin had updated it. Root cause: the registration form URL was
**duplicated** — stored both in the global `GeneralSetting.google_form_url` (injected
as a hard-coded "БҮРТГЭЛИЙН ЛИНК" block) and in one `CourseLink` per active course.
The admin's General-tab edit wrote only the global copy, so every course still carried
the stale link right beside it in the prompt.

The single global Google Form was always the wrong model: registration links are
per-course, and there was no clean fallback when a course had none.

## Goals

1. The bot answers a registration question by **clarifying which course** the customer
   wants, then giving **that course's** registration link.
2. If the course has no registration link on file, the bot gives the **website**
   (`business_website_url`) — the hub where every service link and info lives.
3. This behaviour is **admin-editable content**, not hard-coded in Python.
4. Re-seeding defaults must **never replace** an admin-managed registration link.

## Non-goals

- No new admin field/table. Reuse existing editable surfaces (course links, website
  setting, training snippets).
- No change to the course link editor UI — per-course links are already editable there.

## Design

### 1. Retire the global registration form from the prompt
`services/_prompt.py`: remove the `registration_block` that injects `google_form_url`.
The prompt already surfaces (a) each course's own registration `CourseLink` under the
course, and (b) the website via `_format_company_contact_block()`
(`Албан ёсны вэбсайт: {business_website_url}`). No hard-coded form URL remains.

### 2. Behaviour lives in the ★high-priority registration Snippet
A high-priority snippet already exists — `seed_discovery_phrasing_snippets`'s
`'Сургалтад бүртгүүлэх асуултын чиглэл'` — but its body still points customers at the
retired global form. **Rewrite that snippet's body** to the new rule:

> Хэрэглэгч бүртгүүлэх/элсэх талаар асуувал ЭХЛЭЭД аль сургалтад хамрагдахыг тодруул
> (ямар анги, ямар хэлбэр). Дараа нь тухайн сургалтын бүртгэлийн линкийг (курсын доор
> жагссан "Сургалтанд сууя, бүртгүүлье" линк) хариултдаа өг. Хэрэв тухайн сургалтад
> бүртгэлийн линк олдохгүй бол манай вэбсайтын линкийг өг — тэнд бүх үйлчилгээ,
> мэдээлэл, холбоос байрладаг. Хэрэглэгчээс заавал утас ШААРДАХГҮЙ; утсаа үлдээвэл
> ажилтан холбогдоно гэдгийг хоёрдогч сонголт болгож нэмж болно.

Update the seed dict body to this text, AND add a `KNOWN_DEFAULT_REGISTRATION_BODIES`
one-shot update (mirroring `KNOWN_DEFAULT_MF_DESCRIPTIONS`): if the live snippet's body
matches the known old seeded text, replace it with the new text; otherwise leave it
(the admin has customised it). This corrects existing installs on the next defaults
reseed while preserving admin edits.

Chosen over persona because the snippet (a) already exists and is the documented home
for a behavioural rule, (b) works without overwriting the admin's customised persona,
and (c) is editable by `admin`, not only `super_admin`.

### 3. Seed never replaces links, and no longer seeds the global form
`services/_seed.py:seed_default_magic_links`:
- Course registration links: create **only** when the course has no link matching
  `COURSE_REGISTRATION_LINK_DESCRIPTION`; never overwrite an existing link's URL.
- Remove the `google_form_url` GeneralSetting seeding block entirely (§4 deletes the
  field).

### 4. Delete the global form field and all its code (full cleanup)
The single global registration form is removed outright, not kept as a no-op:
- `routes/admin/business.py`: remove the `google_form_url → CourseLink` propagation
  added earlier on this branch; remove `google_form_url` from `BUSINESS_GENERAL_KEYS`.
- `templates/business/general.html`: delete the "Нийтлэг бүртгэлийн форм (URL)" input.
- `services/__init__.py`: delete `get_google_form_url()` and the module-level
  `GOOGLE_FORM_URL` env constant.
- Docs: drop `GOOGLE_FORM_URL` / `google_form_url` references from `templates/docs.html`,
  `DEPLOY.md`, and the two setup manuals; remove the CLAUDE.md prompt-assembly mention.

The inert `google_form_url` GeneralSetting row (if present in a live DB) is left in
place — it is harmless and read by nothing after this change; no migration needed.

### 5. Nudge follow-up uses the website fallback
`services/__init__.py:_nudge_message_for`: the "ready"-stage nudge quotes
`get_google_form_url()`; switch to `get_business_website_url()` so retiring the form
leaves no dead link. Drop the link line when the website is unset.

## Testing

Replace `tests/test_registration_link_sync.py` with `tests/test_registration_behaviour.py`:
- Seed re-run does **not** overwrite an admin-edited course registration link (create-only).
- `build_system_prompt` no longer emits the hard-coded "БҮРТГЭЛИЙН ЛИНК" google-form block.
- The seeded registration-guidance snippet is present and high-priority.
- `_nudge_message_for` "ready" stage quotes the website, not the google form.

All existing tests must continue to pass (current: 118).

## Rollback

Git revert. The `google_form_url` GeneralSetting row is untouched, so restoring the
deleted getter, admin input, and prompt block reinstates prior behaviour from stored data.
