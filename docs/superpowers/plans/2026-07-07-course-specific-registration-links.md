# Course-specific Registration Links Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the bot give each course's own registration link (asking which course first), fall back to the company website when a course has none, retire the single global registration form entirely, and move the behaviour rule into an admin-editable snippet.

**Architecture:** The prompt already surfaces per-course `CourseLink`s and the website (`business_website_url`). We delete the redundant global `google_form_url` form (field + code + prompt block), rewrite the existing high-priority registration snippet to the new "clarify course → course link → website" rule, and make the defaults seed create-only so it never overwrites admin-managed links.

**Tech Stack:** Flask, SQLAlchemy, pytest (in-memory SQLite), Jinja2, Bootstrap 5.

## Global Constraints

- Run tests with `.venv/Scripts/python.exe -m pytest`. Full suite must stay green (baseline 118 tests, before this branch's WIP).
- Stage specific files only — never `git add -A`/`.`. Commit messages end with `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`.
- English for code/comments/commits. Mongolian chat-facing copy uses imperative, keeps loanwords (форм, линк) in Latin/as-is.
- Don't remove the `import netrc` at the top of `services/__init__.py`; don't touch the `app.py` WAL/busy_timeout block.
- `COURSE_REGISTRATION_LINK_DESCRIPTION = 'Сургалтанд сууя, бүртгүүлье'` already exists in `services/_seed.py` (added earlier on this branch) — reuse it, don't redefine.

---

### Task 1: Remove the obsolete sync-on-save patch and its test

Earlier on this branch, `business_management_general` gained a `google_form_url → CourseLink` propagation and a companion test. Both are obsolete now that the global form is being deleted. Revert them so `business.py` ends in its final state (also drop `google_form_url` from the General-tab keys).

**Files:**
- Modify: `routes/admin/business.py`
- Delete: `tests/test_registration_link_sync.py`

**Interfaces:**
- Produces: `BUSINESS_GENERAL_KEYS` no longer contains `'google_form_url'`; `business_management_general` writes only GeneralSetting rows with no CourseLink side effect.

- [ ] **Step 1: Remove the propagation block in `routes/admin/business.py`**

Replace the POST body (the version currently on the branch) so it no longer touches CourseLink:

```python
        allowed = {k: v for k, v in data.items() if k in BUSINESS_GENERAL_KEYS}
        for key, value in allowed.items():
            row = GeneralSetting.query.filter_by(key=key).first()
            if row:
                row.value = value
            else:
                row = GeneralSetting(key=key, value=value)
                db.session.add(row)
        db.session.commit()
        log_admin_action(
            'settings.save', 'setting', None, ', '.join(sorted(allowed.keys()))[:255],
            detail=f'{len(allowed)} business-general key шинэчилсэн'
        )
        return jsonify({'success': True, 'saved': sorted(allowed.keys())})
```

- [ ] **Step 2: Remove the now-unused import in `routes/admin/business.py`**

Delete this line (added earlier on the branch):

```python
from services._seed import COURSE_REGISTRATION_LINK_DESCRIPTION
```

- [ ] **Step 3: Remove `google_form_url` from `BUSINESS_GENERAL_KEYS`**

```python
BUSINESS_GENERAL_KEYS = (
    'center_name',
    'center_description',
    'center_email',
    'main_office_address',
    'main_office_phone',
    'business_website_url',
)
```

- [ ] **Step 4: Delete the obsolete test file**

Run: `git rm tests/test_registration_link_sync.py`

- [ ] **Step 5: Verify the app still imports and suite collects**

Run: `.venv/Scripts/python.exe -m pytest -q`
Expected: PASS (the two removed tests are gone; everything else green).

- [ ] **Step 6: Commit**

```bash
git add routes/admin/business.py tests/test_registration_link_sync.py
git commit -m "$(cat <<'EOF'
Remove obsolete google_form_url sync patch

The global registration form is being deleted, so syncing CourseLinks from it
is moot. Revert the propagation, drop google_form_url from the General-tab keys,
and remove its companion test.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
)"
```

---

### Task 2: Delete the global form — prompt block, getter, env constant, template input; repoint the nudge

**Files:**
- Modify: `services/_prompt.py` (remove `registration_block` build + its use in the assembled prompt)
- Modify: `services/__init__.py` (remove `GOOGLE_FORM_URL` const + `get_google_form_url()`; repoint `_nudge_message_for`)
- Modify: `templates/business/general.html` (delete the input)
- Test: `tests/test_registration_behaviour.py` (new)

**Interfaces:**
- Consumes: `_svc.get_business_website_url()` (existing, `services/__init__.py:917`).
- Produces: `get_google_form_url` and `GOOGLE_FORM_URL` no longer exist; the system prompt never contains the string `БҮРТГЭЛИЙН ЛИНК (ӨӨРӨӨ БӨГЛӨЖ`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_registration_behaviour.py`:

```python
"""Registration behaviour after retiring the global google_form_url form:
the prompt no longer injects the hard-coded registration block, and the
'ready'-stage nudge quotes the website instead of the dead form link.
"""
import pytest


@pytest.fixture
def _clean_settings(app, db_session):
    from extensions import db
    from models import GeneralSetting
    yield
    GeneralSetting.query.filter(
        GeneralSetting.key.in_(['google_form_url', 'business_website_url'])
    ).delete(synchronize_session=False)
    db.session.commit()


def test_prompt_has_no_hardcoded_registration_form_block(app, db_session, _clean_settings):
    from extensions import db
    from models import GeneralSetting
    from services._prompt import build_system_prompt

    # Even with a stale google_form_url row present, the block must be gone.
    db.session.add(GeneralSetting(key='google_form_url',
                                  value='https://example.com/OLD_FORM'))
    db.session.commit()

    prompt = build_system_prompt()
    assert 'БҮРТГЭЛИЙН ЛИНК (ӨӨРӨӨ БӨГЛӨЖ' not in prompt
    assert 'https://example.com/OLD_FORM' not in prompt


def test_ready_nudge_quotes_website_not_form(app, db_session, _clean_settings):
    from extensions import db
    from models import FacebookUser, GeneralSetting
    from services import _nudge_message_for

    db.session.add(GeneralSetting(key='business_website_url',
                                  value='https://magicgroup.mn'))
    db.session.commit()

    user = FacebookUser(facebook_id='psid-nudge-ready', name='Test',
                        funnel_stage='ready')
    msg = _nudge_message_for(user)
    assert 'https://magicgroup.mn' in msg
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/test_registration_behaviour.py -v`
Expected: `test_prompt_has_no_hardcoded_registration_form_block` FAILS (block still present); the nudge test FAILS (still calls `get_google_form_url`, message has no website URL).

- [ ] **Step 3: Remove the `registration_block` build in `services/_prompt.py`**

Delete this whole block (currently ~lines 463–479):

```python
    google_form_url = _svc.get_google_form_url()
    if google_form_url:
        registration_block = (
            f"БҮРТГЭЛИЙН ЛИНК (ӨӨРӨӨ БӨГЛӨЖ БҮРТГҮҮЛЭХ ЗАМ):\n"
            f"{google_form_url}\n"
            f"Энэ нь хэрэглэгчийн ӨӨРИЙН биеэр одоо бөглөж бүртгүүлж "
            f"болох форм. Хэрэглэгч 'шууд бүртгүүлж болох уу?', "
            f"'яаж бүртгүүлэх вэ?', 'би одоо бүртгүүлмээр байна' гэх "
            f"мэт асуувал ЭНЭ ЛИНКИЙГ ҮНДСЭН ХАРИУЛТ БОЛГОЖ ӨГ. "
            f"Хэрэглэгчид 'утсаа үлдээ' гэж шаардахгүйгээр, форм "
            f"бөглөж бүртгүүлэх нь биеэ дааж шууд бүртгүүлэх "
            f"бодит сонголт юм. Утасны дугаар үлдээх нь ХОЁР ДАХЬ "
            f"сонголт (ажилтан тантай эргэж холбогдоно) — хоёуланг "
            f"нь нэг хариултанд оруулж, хэрэглэгчид сонгох эрхийг өг.\n"
        )
    else:
        registration_block = ""
```

- [ ] **Step 4: Remove `{registration_block}` from the assembled prompt in `services/_prompt.py`**

The assembly line (~589) currently reads:

```python
{registration_block}{name_block}{funnel_rule}
```

Change it to:

```python
{name_block}{funnel_rule}
```

- [ ] **Step 5: Repoint the nudge in `services/__init__.py:_nudge_message_for`**

Replace the `ready` branch (currently ~lines 1770–1777):

```python
    if stage == 'ready':
        link = get_business_website_url()
        link_line = f"\n\nДэлгэрэнгүй мэдээлэл: {link}" if link else ""
        return (
            f"{name_prefix}та бүртгүүлэх талаар бодож үзсэн байх. "
            "Утасны дугаараа үлдээвэл бид өөрсдөө эргэж холбогдоно. "
            "Аль сургалтад хамрагдахаа хэлбэл тухайн ангийн бүртгэлийн "
            "мэдээллийг илгээе." + link_line
        )
```

- [ ] **Step 6: Delete the getter and env constant in `services/__init__.py`**

Delete the `GOOGLE_FORM_URL` constant and its comment (currently ~lines 200–203):

```python
# Loaded at import time as a fallback; the live value is fetched by
# get_google_form_url() which also checks the DB setting written from
# the admin panel (Business Management -> General Information).
GOOGLE_FORM_URL = os.environ.get('GOOGLE_FORM_URL', '')
```

Delete the entire `get_google_form_url()` function (currently ~lines 217–230):

```python
def get_google_form_url():
    """Self-service registration link the bot can quote. Priority order:
       1) Admin-panel value (GeneralSetting key 'google_form_url')
       2) GOOGLE_FORM_URL env var (set at import time)
       3) Empty string — the prompt drops the registration block entirely.

    Was previously hard-wired to env-var only, which meant admin changes
    in /business-management/general were silently ignored by the bot
    even though they showed up in the form.
    """
    db_value = get_setting('google_form_url', '')
    if db_value and db_value.strip():
        return db_value.strip()
    return GOOGLE_FORM_URL
```

- [ ] **Step 7: Delete the input in `templates/business/general.html`**

Remove the divider + field block (currently lines 73–83) so the form goes straight from the previous field to the submit button:

```html
            <div class="col-12"><hr class="my-2"></div>

            <div class="col-md-12">
                <label class="form-label" for="google_form_url">Нийтлэг бүртгэлийн форм (URL)</label>
                <input type="url" class="form-control" id="google_form_url" name="google_form_url"
                       value="{{ settings.google_form_url or '' }}">
                <div class="form-text">
                    Сургалт, үйлчилгээ салангид холбоосгүй үед бот энэхүү линкийг иш татан "бүртгүүлэх"
                    урсгал руу хэрэглэгчийг чиглүүлнэ.
                </div>
            </div>
```

- [ ] **Step 8: Run the new test — expect PASS**

Run: `.venv/Scripts/python.exe -m pytest tests/test_registration_behaviour.py -v`
Expected: PASS.

- [ ] **Step 9: Run the full suite to catch any dangling `get_google_form_url` reference**

Run: `.venv/Scripts/python.exe -m pytest -q`
Expected: PASS. If a `NameError`/`ImportError` for `get_google_form_url` appears, grep the repo (`git grep get_google_form_url -- '*.py'`) and remove the straggler.

- [ ] **Step 10: Commit**

```bash
git add services/_prompt.py services/__init__.py templates/business/general.html tests/test_registration_behaviour.py
git commit -m "$(cat <<'EOF'
Retire the global registration form

Delete google_form_url end to end: the hard-coded БҮРТГЭЛИЙН ЛИНК prompt block,
get_google_form_url()/GOOGLE_FORM_URL, and the General-tab input. The bot now
relies on per-course registration links + the website (already in the prompt).
Repoint the 'ready'-stage nudge to the website fallback.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
)"
```

---

### Task 3: Make the defaults seed create-only for course registration links

**Files:**
- Modify: `services/_seed.py:seed_default_magic_links`
- Test: `tests/test_registration_behaviour.py` (add cases)

**Interfaces:**
- Consumes: `COURSE_REGISTRATION_LINK_DESCRIPTION` (module constant).
- Produces: re-running `seed_default_magic_links()` never changes an existing course registration link's `url`, and never creates a `google_form_url` GeneralSetting row.

- [ ] **Step 1: Write the failing test (append to `tests/test_registration_behaviour.py`)**

```python
def test_seed_does_not_overwrite_admin_course_link(app, db_session):
    from extensions import db
    from models import Course, CourseLink, GeneralSetting
    from services import seed_default_magic_links
    from services._seed import COURSE_REGISTRATION_LINK_DESCRIPTION

    # Course has no business_line_id column (see models.py) — mirror the
    # minimal Course used in tests/test_seed_idempotency.py::_build_catalog.
    course = Course(name='Seed Reglink Course', course_type='Classroom',
                    price=100000, time='18:00', is_active=True)
    db.session.add(course)
    db.session.commit()
    admin_url = 'https://forms.example.com/ADMIN_EDITED'
    db.session.add(CourseLink(course_id=course.id,
                              description=COURSE_REGISTRATION_LINK_DESCRIPTION,
                              url=admin_url, is_active=True))
    db.session.commit()

    seed_default_magic_links()
    db.session.expire_all()

    links = CourseLink.query.filter_by(course_id=course.id).all()
    reg_links = [l for l in links
                 if l.description == COURSE_REGISTRATION_LINK_DESCRIPTION]
    assert len(reg_links) == 1, 'seed added a duplicate registration link'
    assert reg_links[0].url == admin_url, 'seed overwrote the admin link'
    assert GeneralSetting.query.filter_by(key='google_form_url').first() is None

    CourseLink.query.filter_by(course_id=course.id).delete()
    Course.query.filter_by(id=course.id).delete()
    db.session.commit()
```

- [ ] **Step 2: Run it — expect FAIL**

Run: `.venv/Scripts/python.exe -m pytest tests/test_registration_behaviour.py::test_seed_does_not_overwrite_admin_course_link -v`
Expected: FAIL — the seed currently creates a `google_form_url` row and adds a second registration link at the hard-coded URL.

- [ ] **Step 3: Remove the `google_form_url` seeding block in `seed_default_magic_links`**

Delete the block that writes the GeneralSetting (currently ~lines 677–694), including the `course_form_url` definition IF it is not used elsewhere in the function. Note: `course_form_url` is still referenced by the per-course link seeding below, so keep the URL literal but move it next to that loop. Concretely, delete:

```python
    # The course registration form is also the global БҮРТГЭЛИЙН ЛИНК
    # injected at the top of the system prompt. Keep the GeneralSetting in
    # sync alongside the per-course CourseLink rows below so the prompt's
    # registration block and the course list quote the same URL.
    course_form_url = (
        'https://docs.google.com/forms/d/e/'
        '1FAIpQLSejDvCSqo6J5cgqrdZdnzttz-1ahobmypNr0wLlPTRGehtEog/viewform'
    )
    existing = GeneralSetting.query.filter_by(key='google_form_url').first()
    if existing:
        if (existing.value or '').strip() != course_form_url:
            existing.value = course_form_url
            log.append(f'Updated GeneralSetting.google_form_url -> {course_form_url}')
        else:
            log.append('GeneralSetting.google_form_url already set; left unchanged.')
    else:
        db.session.add(GeneralSetting(key='google_form_url', value=course_form_url))
        log.append(f'Created GeneralSetting.google_form_url = {course_form_url}')
```

- [ ] **Step 4: Make the per-course link loop create-only**

The course-link section (currently ~lines 811–838) uses `_upsert_link` (matches by URL, so a changed URL spawns a duplicate). Replace it with a create-only-by-description version. Restore the `course_form_url` literal here as the default URL used only for brand-new links:

```python
    # ----- Course registration form: one CourseLink per active course -----
    # Create-only. The registration form is admin-managed per course; the seed
    # only bootstraps courses that have NO registration link yet (matched by
    # description), and never overwrites an existing one. Default URL is used
    # solely for freshly-created links.
    course_form_url = (
        'https://docs.google.com/forms/d/e/'
        '1FAIpQLSejDvCSqo6J5cgqrdZdnzttz-1ahobmypNr0wLlPTRGehtEog/viewform'
    )
    course_link_description = COURSE_REGISTRATION_LINK_DESCRIPTION
    course_link_note = (
        'Бүртгэл бөглөж бүртгүүлэх бодит зам — утас лавлахгүйгээр шууд '
        'бүртгэгдэх боломжтой. Бүх ангид нэг л форм.'
    )
    active_courses = Course.query.filter_by(is_active=True).all()
    if not active_courses:
        log.append('SKIPPED courses: no active Course rows (run seed_courses_and_faqs first).')
    else:
        c_added = c_skipped = 0
        for c in active_courses:
            has_reg = CourseLink.query.filter_by(
                course_id=c.id, description=course_link_description
            ).first()
            if has_reg:
                c_skipped += 1
                continue
            db.session.add(CourseLink(
                course_id=c.id,
                description=course_link_description,
                url=course_form_url,
                note=course_link_note,
                is_active=True,
                sort_order=0,
            ))
            c_added += 1
        log.append(
            f'course registration link: +{c_added} new, '
            f'={c_skipped} left untouched across {len(active_courses)} active course(s).'
        )
```

- [ ] **Step 5: Add the `CourseLink` import if missing**

`seed_default_magic_links` uses `CourseLink` now. Confirm the top-of-file import in `services/_seed.py` includes it:

```python
from models import (BusinessLine, ChatQuestionCluster, Course, CourseLink, FAQ,
                    GeneralSetting, HandoffKeyword, Product, ProductLink,
                    Service, ServiceLink, TrainingSnippet)
```

(If `CourseLink` was already imported, leave as-is.)

- [ ] **Step 6: Run the new test — expect PASS**

Run: `.venv/Scripts/python.exe -m pytest tests/test_registration_behaviour.py::test_seed_does_not_overwrite_admin_course_link -v`
Expected: PASS.

- [ ] **Step 7: Run the seed-idempotency suite (guards this function)**

Run: `.venv/Scripts/python.exe -m pytest tests/test_seed_idempotency.py -v`
Expected: PASS unchanged. `test_seed_default_magic_links_upserts_link_descriptions` asserts **ProductLink** description refresh (untouched by this task), and the idempotency test only counts rows (create-only keeps counts stable across reruns). If either unexpectedly fails, re-read the assertion before editing — do not weaken a genuine guard.

- [ ] **Step 8: Commit**

```bash
git add services/_seed.py tests/test_registration_behaviour.py tests/test_seed_idempotency.py
git commit -m "$(cat <<'EOF'
Seed course registration links create-only; stop seeding google_form_url

Re-running defaults no longer overwrites an admin-edited course registration
link or resurrects the retired global form setting. Links are bootstrapped
only for courses that have none yet (matched by description).

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
)"
```

---

### Task 4: Rewrite the registration snippet to the new behaviour

**Files:**
- Modify: `services/_seed.py:seed_discovery_phrasing_snippets`
- Test: `tests/test_registration_behaviour.py` (add cases)

**Interfaces:**
- Produces: after `seed_discovery_phrasing_snippets()`, the `'Сургалтад бүртгүүлэх асуултын чиглэл'` snippet body contains `вэбсайт` and no longer instructs "ЭХЛЭЭД БҮРТГЭЛИЙН ЛИНКийг үндсэн хариулт". A live row whose body equals the known-old text is updated; an admin-customised body is preserved.

- [ ] **Step 1: Write the failing tests (append to `tests/test_registration_behaviour.py`)**

```python
NEW_REG_SNIPPET_MARKER = 'аль сургалтад хамрагдахыг тодруул'


def test_registration_snippet_seeds_new_behaviour(app, db_session):
    from extensions import db
    from models import TrainingSnippet
    from services import seed_discovery_phrasing_snippets

    TrainingSnippet.query.delete()
    db.session.commit()

    seed_discovery_phrasing_snippets()
    db.session.expire_all()

    snip = TrainingSnippet.query.filter_by(
        title='Сургалтад бүртгүүлэх асуултын чиглэл').first()
    assert snip is not None
    assert snip.priority == 'high'
    assert NEW_REG_SNIPPET_MARKER in snip.body
    assert 'вэбсайт' in snip.body
    TrainingSnippet.query.delete()
    db.session.commit()


def test_registration_snippet_updates_known_old_body_but_keeps_admin_edit(app, db_session):
    from extensions import db
    from models import TrainingSnippet
    from services import seed_discovery_phrasing_snippets
    from services._seed import OLD_REGISTRATION_SNIPPET_BODY

    TrainingSnippet.query.delete()
    db.session.commit()

    # A live row still carrying the old seeded body -> should be upgraded.
    db.session.add(TrainingSnippet(
        title='Сургалтад бүртгүүлэх асуултын чиглэл',
        body=OLD_REGISTRATION_SNIPPET_BODY, category='course-routing',
        priority='high', is_active=True))
    db.session.commit()

    seed_discovery_phrasing_snippets()
    db.session.expire_all()
    snip = TrainingSnippet.query.filter_by(
        title='Сургалтад бүртгүүлэх асуултын чиглэл').first()
    assert NEW_REG_SNIPPET_MARKER in snip.body

    # An admin-customised body must NOT be touched.
    snip.body = 'МИНИЙ ГАРААР ЗАССАН ТЕКСТ'
    db.session.commit()
    seed_discovery_phrasing_snippets()
    db.session.expire_all()
    snip = TrainingSnippet.query.filter_by(
        title='Сургалтад бүртгүүлэх асуултын чиглэл').first()
    assert snip.body == 'МИНИЙ ГАРААР ЗАССАН ТЕКСТ'
    TrainingSnippet.query.delete()
    db.session.commit()
```

- [ ] **Step 2: Run — expect FAIL**

Run: `.venv/Scripts/python.exe -m pytest tests/test_registration_behaviour.py -k registration_snippet -v`
Expected: FAIL — `OLD_REGISTRATION_SNIPPET_BODY` doesn't exist yet and the seeded body lacks the new marker.

- [ ] **Step 3: Add the old/new body constants at module level in `services/_seed.py`**

Put these near the top of `services/_seed.py` (after `COURSE_REGISTRATION_LINK_DESCRIPTION`):

```python
# The pre-2026-07 seeded body of the registration-routing snippet. Kept so a
# defaults reseed can one-shot upgrade live installs still carrying it to
# NEW_REGISTRATION_SNIPPET_BODY while leaving admin-customised bodies alone
# (mirrors KNOWN_DEFAULT_MF_DESCRIPTIONS below).
OLD_REGISTRATION_SNIPPET_BODY = (
    "Хэрэглэгч 'бүртгүүлэх', 'элсэх', 'яаж бүртгүүлэх', "
    "'шууд бүртгүүлэх боломжтой юу?', 'register hiih', "
    "'register hiimer', 'burtguulj boloh uu?', 'enroll', "
    "'элсэлт' гэх мэт асуувал ЭХЛЭЭД БҮРТГЭЛИЙН ЛИНКийг "
    "үндсэн хариулт болгож үзүүл — энэ нь өөрөө бөглөж "
    "бүртгүүлэх форм. Эсвэл утсаа үлдээвэл ажилтан "
    "холбогдоно гэдгийг хоёрдогч сонголт болгож нэм. "
    "Хэрэглэгчээс заавал утас ШААРДАХГҮЙ — форм линк нь "
    "хүчинтэй бие даасан зам."
)
NEW_REGISTRATION_SNIPPET_BODY = (
    "Хэрэглэгч 'бүртгүүлэх', 'элсэх', 'яаж бүртгүүлэх', "
    "'register hiih', 'enroll', 'элсэлт' гэх мэт асуувал ЭХЛЭЭД "
    "аль сургалтад хамрагдахыг тодруул (ямар анги, ямар хэлбэр). "
    "Дараа нь тухайн сургалтын бүртгэлийн линкийг (курсын доор "
    "жагссан 'Сургалтанд сууя, бүртгүүлье' линк) хариултдаа өг. "
    "Хэрэв тухайн сургалтад бүртгэлийн линк олдохгүй бол манай "
    "вэбсайтын линкийг өг — тэнд бүх үйлчилгээ, мэдээлэл, холбоос "
    "байрладаг. Хэрэглэгчээс заавал утас ШААРДАХГҮЙ; утсаа үлдээвэл "
    "ажилтан холбогдоно гэдгийг хоёрдогч сонголт болгож нэмж болно."
)
```

- [ ] **Step 4: Use the new body in the seed dict**

In `seed_discovery_phrasing_snippets`, change the `'Сургалтад бүртгүүлэх асуултын чиглэл'` entry's `'body'` value to reference the constant:

```python
        {
            'title': 'Сургалтад бүртгүүлэх асуултын чиглэл',
            'category': 'course-routing',
            'priority': 'high',
            'body': NEW_REGISTRATION_SNIPPET_BODY,
        },
```

- [ ] **Step 5: Add the one-shot known-body upgrade before the insert loop**

In `seed_discovery_phrasing_snippets`, just before the `existing_titles = {...}` line, add:

```python
    # One-shot upgrade: bump live rows still on the old seeded body to the new
    # course-specific behaviour, but never clobber an admin-edited body.
    reg_row = TrainingSnippet.query.filter_by(
        title='Сургалтад бүртгүүлэх асуултын чиглэл').first()
    if reg_row and (reg_row.body or '').strip() == OLD_REGISTRATION_SNIPPET_BODY.strip():
        reg_row.body = NEW_REGISTRATION_SNIPPET_BODY
        log.append('Upgraded registration snippet body to course-specific rule.')
```

- [ ] **Step 6: Run the snippet tests — expect PASS**

Run: `.venv/Scripts/python.exe -m pytest tests/test_registration_behaviour.py -k registration_snippet -v`
Expected: PASS.

- [ ] **Step 7: Run the full suite**

Run: `.venv/Scripts/python.exe -m pytest -q`
Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add services/_seed.py tests/test_registration_behaviour.py
git commit -m "$(cat <<'EOF'
Rewrite registration snippet to clarify-course-then-link behaviour

The high-priority 'Сургалтад бүртгүүлэх асуултын чиглэл' snippet now tells the
bot to ask which course, give that course's registration link, and fall back
to the website when a course has none. A defaults reseed upgrades live installs
still on the old body, preserving admin edits.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
)"
```

---

### Task 5: Documentation cleanup

**Files:**
- Modify: `templates/docs.html`, `DEPLOY.md`, `Magic Bot - Facebook Page AI Assistant.md`, `Magic Bot - Алхам Алхмаар Суулгах Зааварчилгаа.md`, `CLAUDE.md`

**Interfaces:** none (docs only).

- [ ] **Step 1: Find every remaining reference**

Run: `git grep -n -i "google_form_url\|GOOGLE_FORM_URL"`
Expected: hits in the files above (plus this plan/spec, which stay).

- [ ] **Step 2: Remove the `GOOGLE_FORM_URL` env row in `templates/docs.html`**

Delete the table row:

```html
        <tr><td><code>GOOGLE_FORM_URL</code></td><td>Registration link the bot embeds for "ready" stage users</td></tr>
```

And in the `general_setting` key list row, drop `google_form_url` from the example key list (leave the other keys).

- [ ] **Step 3: Remove the `GOOGLE_FORM_URL` line in `DEPLOY.md`**

Delete:

```markdown
   | `GOOGLE_FORM_URL` | your registration form URL |
```

- [ ] **Step 4: Remove `GOOGLE_FORM_URL` from the two setup manuals**

In `Magic Bot - Facebook Page AI Assistant.md` delete both the env example line and the env-list description line:

```
GOOGLE_FORM_URL=https://docs.google.com/forms/d/e/1FAIpQLSerwmfsvdYbcgZBUTySCrx6ueA2thp_7-7n-uUDoRF4lvAXKw/viewform
```
```
GOOGLE_FORM_URL         - Registration form link
```

In `Magic Bot - Алхам Алхмаар Суулгах Зааварчилгаа.md` delete:

```
GOOGLE_FORM_URL=https://docs.google.com/forms/d/e/1FAIpQLSerwmfsvdYbcgZBUTySCrx6ueA2thp_7-7n-uUDoRF4lvAXKw/viewform
```

- [ ] **Step 5: Update `CLAUDE.md`**

In the "System prompt assembly" section, remove `registration-link block →` from the source-order line so it reads `... → FAQs → session-state rule → funnel rule → behavioral rules.` Add a bullet under the latent/legacy fields table row set:

```markdown
| `GeneralSetting.google_form_url` | Removed 2026-07-07. Registration is per-course (`CourseLink`) with the website (`business_website_url`) as fallback; the clarify-course rule lives in the `Сургалтад бүртгүүлэх асуултын чиглэл` snippet. |
```

- [ ] **Step 6: Verify no code/doc references remain except plan/spec**

Run: `git grep -n -i "google_form_url\|GOOGLE_FORM_URL" -- ':!docs/superpowers'`
Expected: no matches.

- [ ] **Step 7: Commit**

```bash
git add templates/docs.html DEPLOY.md "Magic Bot - Facebook Page AI Assistant.md" "Magic Bot - Алхам Алхмаар Суулгах Зааварчилгаа.md" CLAUDE.md
git commit -m "$(cat <<'EOF'
Docs: drop retired google_form_url references

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
)"
```

---

### Task 6: Full verification

- [ ] **Step 1: Run the whole suite**

Run: `.venv/Scripts/python.exe -m pytest -q`
Expected: PASS, count = prior baseline + net new tests in `test_registration_behaviour.py`.

- [ ] **Step 2: Eyeball the assembled prompt**

Run:
```bash
.venv/Scripts/python.exe -c "import os; os.environ.setdefault('SECRET_KEY','x'); os.environ.setdefault('FACEBOOK_ACCESS_TOKEN','x'); os.environ.setdefault('FACEBOOK_APP_SECRET','x'); os.environ.setdefault('OPENAI_API_KEY','sk-x'); os.environ.setdefault('SQLALCHEMY_DATABASE_URI','sqlite:///:memory:'); os.environ.setdefault('FLASK_SKIP_INIT_DB','1'); from app import app; from extensions import db;
with app.app_context():
    db.create_all()
    from services._prompt import build_system_prompt
    p = build_system_prompt()
    print('HAS OLD BLOCK:', 'БҮРТГЭЛИЙН ЛИНК (ӨӨРӨӨ БӨГЛӨЖ' in p)"
```
Expected: `HAS OLD BLOCK: False`.

- [ ] **Step 3: Confirm the branch diff is clean and scoped**

Run: `git status && git log --oneline master..HEAD`
Expected: only the files named in this plan; commits for Tasks 1–5.
