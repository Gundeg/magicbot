# Staff-action Notes + Dropped Archive — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let staff attach a note when they resolve an issue (optional) or drop a hot prospect / lead (required), store it on the lead + the durable audit log, and add an "Орхисон" archive tab with a Restore button.

**Architecture:** New `FacebookUser.notes` column (auto-migrated by `ensure_schema`). Three existing POST handlers in `routes/admin/work_tasks.py` gain note handling. A shared Bootstrap modal in `templates/work_tasks.html` collects the note client-side. A 6th tab lists dropped users (prospects + leads) with their reason and a Restore button that reuses the existing `/admin/api/lead-status` endpoint.

**Tech Stack:** Flask, SQLAlchemy, Bootstrap 5, vanilla JS, pytest (in-memory SQLite).

**Branch:** `feature/drop-resolve-notes` (already created). Work stays here until reviewed; Render deploys on push to `master`.

**Test baseline:** 62 tests pass today. This plan adds 9 → expect 71 passing at the end. All must stay green.

---

## File structure

| File | Change |
|---|---|
| `models.py` | Add `FacebookUser.notes` (Text). |
| `services/_seed.py` | Register `notes TEXT` in `ensure_schema()`. |
| `routes/admin/work_tasks.py` | Note handling in `drop_prospect`, `update_lead_status`, `resolve_issue`; `dropped_leads` query + context; add `'dropped'` to `VALID_WORK_TASKS_TABS`. |
| `templates/work_tasks.html` | Archive tab (nav + pane + Restore button); shared note modal + `openNoteModal` helper; rewire the 3 action handlers; restore handler. |
| `templates/conversation.html` | "Тэмдэглэл" row in the user card. |
| `tests/test_action_notes.py` | New — all 9 tests + fixtures. |

---

## Task 1: Add `FacebookUser.notes` column + migration

**Files:**
- Modify: `models.py` (class `FacebookUser`, near line 43)
- Modify: `services/_seed.py` (the `facebook_user` block, near line 50)

- [ ] **Step 1: Add the column to the model**

In `models.py`, inside `class FacebookUser`, add the `notes` column right after the `conversation_topic` line (currently line 43):

```python
    # AI-classified topic of the conversation (updated after every bot reply)
    conversation_topic = db.Column(db.String(100))
    # Free-text staff note captured on a terminal action (drop reason, etc.).
    # Convenient short-term copy; the durable record is the audit log, which
    # outlives cleanup_old_records' 60-day purge of dropped leads.
    notes = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
```

- [ ] **Step 2: Register the migration**

In `services/_seed.py`, add the `notes` entry to the existing `add_columns('facebook_user', {...})` call (currently lines 50-56):

```python
    add_columns('facebook_user', {
        'funnel_stage': "funnel_stage VARCHAR(30) DEFAULT 'curious'",
        'last_nudge_at': 'last_nudge_at DATETIME',
        'bot_muted_until': 'bot_muted_until DATETIME',
        'conversation_topic': 'conversation_topic VARCHAR(100)',
        'last_mute_ack_at': 'last_mute_ack_at DATETIME',
        'notes': 'notes TEXT',
    })
```

- [ ] **Step 3: Run the full suite — nothing should break**

Run: `python -m pytest -q`
Expected: 62 passed (the new column is created by `create_all()` and unused so far).

- [ ] **Step 4: Commit**

```bash
git add models.py services/_seed.py
git commit -m "Add FacebookUser.notes column + ensure_schema migration

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 2: `drop_prospect` requires + persists a note

**Files:**
- Create: `tests/test_action_notes.py`
- Modify: `routes/admin/work_tasks.py:314-325` (the `drop_prospect` block)

- [ ] **Step 1: Create the test file with fixtures + the two drop_prospect tests**

Create `tests/test_action_notes.py`:

```python
"""Notes captured on terminal staff actions: resolve issue (optional),
drop prospect / drop lead (required), the dropped archive tab, and the
conversation-viewer note row. In-memory SQLite, admin logged in via the
Flask-Login session helper (mirrors tests/test_lead_status.py)."""
import pytest


@pytest.fixture
def admin_user(app, db_session):
    from extensions import db
    from models import User
    from werkzeug.security import generate_password_hash

    User.query.filter_by(username='pytest-notes-admin').delete()
    db.session.commit()
    user = User(
        username='pytest-notes-admin',
        password=generate_password_hash('not-used'),
        email='pytest-notes-admin@example.com',
        role='super_admin',
    )
    db.session.add(user)
    db.session.commit()
    yield user
    User.query.filter_by(id=user.id).delete()
    db.session.commit()


@pytest.fixture
def fb_user(app, db_session):
    from extensions import db
    from models import FacebookUser

    user = FacebookUser(facebook_id='psid-notes-test', name='Notes Test')
    db.session.add(user)
    db.session.commit()
    yield user
    FacebookUser.query.filter_by(id=user.id).delete()
    db.session.commit()


@pytest.fixture
def open_issue(app, db_session, fb_user):
    from extensions import db
    from models import AdminIssue

    issue = AdminIssue(
        facebook_user_id=fb_user.id,
        issue_type='unresolved_query',
        content='Test issue content',
        status='open',
    )
    db.session.add(issue)
    db.session.commit()
    yield issue
    AdminIssue.query.filter_by(id=issue.id).delete()
    db.session.commit()


def _login(client, admin_user):
    with client.session_transaction() as sess:
        sess['_user_id'] = str(admin_user.id)
        sess['_fresh'] = True


# ---- drop_prospect (note required) ----

def test_drop_prospect_without_note_rejected(client, admin_user, fb_user):
    from models import FacebookUser
    _login(client, admin_user)
    resp = client.post('/admin/work-tasks',
                       json={'action': 'drop_prospect', 'user_id': fb_user.id})
    assert resp.status_code == 400
    assert resp.get_json()['success'] is False
    refreshed = FacebookUser.query.get(fb_user.id)
    assert refreshed.lead_status != 'dropped'


def test_drop_prospect_with_note_persists(client, admin_user, fb_user):
    from models import FacebookUser, AuditEntry
    _login(client, admin_user)
    resp = client.post('/admin/work-tasks',
                       json={'action': 'drop_prospect', 'user_id': fb_user.id,
                             'note': 'Дугаар буруу'})
    assert resp.status_code == 200
    refreshed = FacebookUser.query.get(fb_user.id)
    assert refreshed.lead_status == 'dropped'
    assert refreshed.notes == 'Дугаар буруу'
    entry = (AuditEntry.query.filter_by(action='lead.drop')
             .order_by(AuditEntry.id.desc()).first())
    assert entry is not None
    assert 'Дугаар буруу' in (entry.detail or '')
```

- [ ] **Step 2: Run the new tests — verify they fail**

Run: `python -m pytest tests/test_action_notes.py -v`
Expected: `test_drop_prospect_without_note_rejected` FAILS (handler returns 200, not 400) and `test_drop_prospect_with_note_persists` FAILS (`notes` is None).

- [ ] **Step 3: Implement note handling in `drop_prospect`**

Replace the `drop_prospect` block in `routes/admin/work_tasks.py` (currently lines 314-325) with:

```python
        if action == 'drop_prospect':
            user = db.session.get(FacebookUser, data.get('user_id'))
            if not user:
                return jsonify({'success': False}), 404
            note = (data.get('note') or '').strip()
            if not note:
                return jsonify({
                    'success': False,
                    'error': 'Шалтгаан заавал бичнэ үү.',
                }), 400
            user.lead_status = 'dropped'
            user.notes = note
            db.session.commit()
            log_admin_action(
                'lead.drop', 'facebook_user', user.id,
                user.name or user.facebook_id,
                detail='Hot Prospect-оос орхив. Шалтгаан: ' + note
            )
            return jsonify({'success': True})
```

- [ ] **Step 4: Run the new tests — verify they pass**

Run: `python -m pytest tests/test_action_notes.py -v`
Expected: both drop_prospect tests PASS.

- [ ] **Step 5: Run the full suite**

Run: `python -m pytest -q`
Expected: 64 passed.

- [ ] **Step 6: Commit**

```bash
git add tests/test_action_notes.py routes/admin/work_tasks.py
git commit -m "drop_prospect: require + persist a reason note

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 3: `update_lead_status` requires + persists a note when status is `dropped`

**Files:**
- Modify: `tests/test_action_notes.py` (append 3 tests; fixtures from Task 2)
- Modify: `routes/admin/work_tasks.py:797-816` (inside `update_lead_status`)

- [ ] **Step 1: Append the tests**

Add to the end of `tests/test_action_notes.py`:

```python
# ---- lead drop via /admin/api/lead-status (note required) ----

def test_lead_drop_without_note_rejected(client, admin_user, fb_user):
    from models import FacebookUser
    _login(client, admin_user)
    resp = client.post('/admin/api/lead-status',
                       json={'user_id': fb_user.id, 'status': 'dropped'})
    assert resp.status_code == 400
    assert resp.get_json()['success'] is False
    refreshed = FacebookUser.query.get(fb_user.id)
    assert refreshed.lead_status != 'dropped'


def test_lead_drop_with_note_persists(client, admin_user, fb_user):
    from models import FacebookUser
    _login(client, admin_user)
    resp = client.post('/admin/api/lead-status',
                       json={'user_id': fb_user.id, 'status': 'dropped',
                             'note': 'Сонирхолгүй болсон'})
    assert resp.status_code == 200
    refreshed = FacebookUser.query.get(fb_user.id)
    assert refreshed.lead_status == 'dropped'
    assert refreshed.notes == 'Сонирхолгүй болсон'


def test_non_dropped_status_needs_no_note(client, admin_user, fb_user):
    from models import FacebookUser
    _login(client, admin_user)
    resp = client.post('/admin/api/lead-status',
                       json={'user_id': fb_user.id, 'status': 'contacted'})
    assert resp.status_code == 200
    refreshed = FacebookUser.query.get(fb_user.id)
    assert refreshed.lead_status == 'contacted'
```

- [ ] **Step 2: Run — verify the two `dropped` tests fail**

Run: `python -m pytest tests/test_action_notes.py -k lead_drop -v`
Expected: `test_lead_drop_without_note_rejected` FAILS (returns 200). `test_non_dropped_status_needs_no_note` already PASSES (proves we won't regress non-drop changes).

- [ ] **Step 3: Implement the note rule**

In `routes/admin/work_tasks.py`, inside `update_lead_status`, replace this block (currently lines 797-809):

```python
        user = db.session.get(FacebookUser, user_id)
        if user is None:
            return jsonify({'success': False, 'error': 'Хэрэглэгч олдсонгүй.'}), 404

        previous = user.lead_status or 'new'
        user.lead_status = status
        db.session.commit()

        log_admin_action(
            'lead.status_change', 'facebook_user', user.id,
            user.name or user.facebook_id,
            detail=f'{previous} → {status}',
        )
```

with:

```python
        user = db.session.get(FacebookUser, user_id)
        if user is None:
            return jsonify({'success': False, 'error': 'Хэрэглэгч олдсонгүй.'}), 404

        # Dropping requires a reason; stored on the lead + the audit log.
        # Other status changes don't take a note.
        note = (data.get('note') or '').strip()
        if status == 'dropped' and not note:
            return jsonify({
                'success': False,
                'error': 'Шалтгаан заавал бичнэ үү.',
            }), 400

        previous = user.lead_status or 'new'
        user.lead_status = status
        if status == 'dropped':
            user.notes = note
        db.session.commit()

        detail = f'{previous} → {status}'
        if status == 'dropped':
            detail += '. Шалтгаан: ' + note
        log_admin_action(
            'lead.status_change', 'facebook_user', user.id,
            user.name or user.facebook_id,
            detail=detail,
        )
```

- [ ] **Step 4: Run — verify pass**

Run: `python -m pytest tests/test_action_notes.py -k lead_drop -v`
Expected: both lead_drop tests PASS.

- [ ] **Step 5: Full suite**

Run: `python -m pytest -q`
Expected: 67 passed.

- [ ] **Step 6: Commit**

```bash
git add tests/test_action_notes.py routes/admin/work_tasks.py
git commit -m "Lead drop: require + persist a reason note (dropped only)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 4: `resolve_issue` accepts an optional note

**Files:**
- Modify: `tests/test_action_notes.py` (append 2 tests)
- Modify: `routes/admin/work_tasks.py:327-351` (the `resolve_issue` block)

- [ ] **Step 1: Append the tests**

Add to the end of `tests/test_action_notes.py`:

```python
# ---- resolve_issue (note optional) ----

def test_resolve_issue_without_note_still_resolves(client, admin_user, open_issue):
    from models import AdminIssue
    _login(client, admin_user)
    resp = client.post('/admin/work-tasks',
                       json={'action': 'resolve_issue', 'id': open_issue.id})
    assert resp.status_code == 200
    refreshed = AdminIssue.query.get(open_issue.id)
    assert refreshed.status == 'resolved'
    assert refreshed.notes is None


def test_resolve_issue_with_note_saves(client, admin_user, open_issue):
    from models import AdminIssue
    _login(client, admin_user)
    resp = client.post('/admin/work-tasks',
                       json={'action': 'resolve_issue', 'id': open_issue.id,
                             'note': 'Утсаар холбогдож шийдсэн'})
    assert resp.status_code == 200
    refreshed = AdminIssue.query.get(open_issue.id)
    assert refreshed.status == 'resolved'
    assert refreshed.notes == 'Утсаар холбогдож шийдсэн'
```

- [ ] **Step 2: Run — verify `with_note` fails**

Run: `python -m pytest tests/test_action_notes.py -k resolve_issue -v`
Expected: `test_resolve_issue_with_note_saves` FAILS (`notes` is None — handler ignores the note). `test_resolve_issue_without_note_still_resolves` PASSES already.

- [ ] **Step 3: Implement optional-note handling**

In `routes/admin/work_tasks.py`, replace the `resolve_issue` block (currently lines 327-351) with:

```python
        if action == 'resolve_issue':
            issue = db.session.get(AdminIssue, data.get('id'))
            if not issue:
                return jsonify({'success': False}), 404
            note = (data.get('note') or '').strip()
            issue.status = 'resolved'
            issue.resolved_at = datetime.utcnow()
            issue.updated_by_id = current_user.id
            issue.updated_at = datetime.utcnow()
            # Optional note: only set it, never blank out an existing one.
            if note:
                issue.notes = note
            # If the bot was muted via "Take Over" for this user, resolving
            # the issue is the staff's "I'm done" signal — restore the bot
            # so future messages from this customer are handled normally.
            unmuted = False
            if issue.facebook_user and issue.facebook_user.bot_muted_until:
                issue.facebook_user.bot_muted_until = None
                unmuted = True
            db.session.commit()
            detail = 'Work Tasks-аас шийдсэн' + (' + ботыг асаасан' if unmuted else '')
            if note:
                detail += '. Тэмдэглэл: ' + note
            log_admin_action(
                'issue.status_change', 'issue', issue.id,
                (issue.facebook_user.name if issue.facebook_user else None) or f'#{issue.id}',
                detail=detail
            )
            return jsonify({'success': True, 'bot_unmuted': unmuted})
```

- [ ] **Step 4: Run — verify pass**

Run: `python -m pytest tests/test_action_notes.py -k resolve_issue -v`
Expected: both PASS.

- [ ] **Step 5: Full suite**

Run: `python -m pytest -q`
Expected: 69 passed.

- [ ] **Step 6: Commit**

```bash
git add tests/test_action_notes.py routes/admin/work_tasks.py
git commit -m "resolve_issue: accept an optional resolution note

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 5: Dropped-leads archive tab (backend query + nav + pane + Restore button)

**Files:**
- Modify: `routes/admin/work_tasks.py:265` (`VALID_WORK_TASKS_TABS`), `:508` (query), `:510-528` (context)
- Modify: `templates/work_tasks.html` (nav after line 179; pane after line 461)
- Modify: `tests/test_action_notes.py` (append 1 test)

- [ ] **Step 1: Append the rendering test**

Add to the end of `tests/test_action_notes.py`:

```python
# ---- dropped archive tab ----

def test_dropped_tab_lists_dropped_user_with_note(client, admin_user, fb_user):
    from extensions import db
    from models import FacebookUser
    _login(client, admin_user)
    u = FacebookUser.query.get(fb_user.id)
    u.lead_status = 'dropped'
    u.notes = 'Тест шалтгаан'
    db.session.commit()
    resp = client.get('/admin/work-tasks?tab=dropped')
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert 'Notes Test' in body
    assert 'Тест шалтгаан' in body
    assert 'restore-lead' in body  # the Restore button is present
```

- [ ] **Step 2: Run — verify it fails**

Run: `python -m pytest tests/test_action_notes.py -k dropped_tab -v`
Expected: FAILS (`Тест шалтгаан` / `restore-lead` not in the page — the tab doesn't exist yet).

- [ ] **Step 3: Register the tab key**

In `routes/admin/work_tasks.py` line 265, add `'dropped'`:

```python
VALID_WORK_TASKS_TABS = ('hot_prospects', 'leads', 'open_issues', 'aging', 'muted', 'dropped')
```

- [ ] **Step 4: Add the query**

In `routes/admin/work_tasks.py`, immediately after the `muted_users` query (currently ends line 508) and before `return render_template(`, insert:

```python
    # --- Tab 6: dropped archive (prospects + leads we dropped, with the
    # reason staff recorded). Shows only the un-purged tail — cleanup_old_records
    # hard-deletes dropped rows after CLEANUP_RETENTION_DAYS; the audit log keeps
    # the permanent record. 'converted' is a win, not a drop, so it's excluded.
    dropped_leads = (FacebookUser.query
                     .filter_by(lead_status='dropped')
                     .order_by(FacebookUser.updated_at.desc())
                     .limit(100)
                     .all())
```

- [ ] **Step 5: Pass it to the template**

In the `render_template('work_tasks.html', ...)` call, add this line next to `muted_users=muted_users,` (currently line 525):

```python
        muted_users=muted_users,
        dropped_leads=dropped_leads,
```

- [ ] **Step 6: Add the tab nav button**

In `templates/work_tasks.html`, after the Muted `<li>...</li>` nav item (closing `</li>` at line 179), insert:

```html
    <li class="nav-item" role="presentation">
        <button class="nav-link {% if tab == 'dropped' %}active{% endif %}"
                data-bs-toggle="tab" data-bs-target="#droppedTab" type="button" role="tab">
            <i class="bi bi-archive"></i> Орхисон
            <span class="count">{{ dropped_leads|length }}</span>
        </button>
    </li>
```

- [ ] **Step 7: Add the tab pane**

In `templates/work_tasks.html`, after the Muted tab-pane's closing `</div>` (line 461) and before the `</div>` that closes `.tab-content` (line 463), insert:

```html
    {# ── Tab 6: Dropped archive ──────────────────────────────────── #}
    <div class="tab-pane fade {% if tab == 'dropped' %}show active{% endif %}" id="droppedTab" role="tabpanel">
        {% if dropped_leads %}
        <div class="card">
            <div class="card-header">
                <i class="bi bi-archive"></i> Орхисон сонирхогч / лидүүд
                <span class="text-muted small">— сүүлийн ~60 хоног. Хуучин бичлэгийг audit log-оос үзнэ.</span>
            </div>
            <div class="card-body">
                <div class="table-responsive">
                    <table class="table table-hover align-middle">
                        <thead>
                            <tr>
                                <th>Нэр</th>
                                <th>Утас</th>
                                <th>Төрөл</th>
                                <th>Шалтгаан</th>
                                <th>Орхисон</th>
                                <th><span class="visually-hidden">Үйлдэл</span></th>
                            </tr>
                        </thead>
                        <tbody>
                            {% for u in dropped_leads %}
                            <tr data-user-id="{{ u.id }}">
                                <td>{{ u.name or 'Unknown' }}</td>
                                <td>
                                    {% if u.phone %}<a href="tel:{{ u.phone }}">{{ u.phone }}</a>
                                    {% else %}<span class="text-muted">—</span>{% endif %}
                                </td>
                                <td>
                                    {% if u.is_lead %}<span class="badge bg-success">Лид</span>
                                    {% else %}<span class="badge bg-secondary">Сонирхогч</span>{% endif %}
                                </td>
                                <td class="small">{{ u.notes or '—' }}</td>
                                <td class="small text-muted">{{ u.updated_at.strftime('%Y-%m-%d %H:%M') if u.updated_at else '—' }}</td>
                                <td class="text-end text-nowrap">
                                    <button type="button" class="btn btn-sm btn-outline-success restore-lead" data-user-id="{{ u.id }}" title="Дахин идэвхжүүлэх">
                                        <i class="bi bi-arrow-counterclockwise"></i> Сэргээх
                                    </button>
                                    <a class="btn btn-sm btn-light" href="{{ url_for('conversation', user_id=u.id) }}" title="Яриаг үзэх">
                                        <i class="bi bi-eye"></i>
                                    </a>
                                </td>
                            </tr>
                            {% endfor %}
                        </tbody>
                    </table>
                </div>
            </div>
        </div>
        {% else %}
            <div class="wt-empty">
                <i class="bi bi-archive"></i>
                Орхисон сонирхогч / лид одоогоор алга.
            </div>
        {% endif %}
    </div>
```

- [ ] **Step 8: Run — verify pass**

Run: `python -m pytest tests/test_action_notes.py -k dropped_tab -v`
Expected: PASS.

- [ ] **Step 9: Full suite**

Run: `python -m pytest -q`
Expected: 70 passed.

- [ ] **Step 10: Commit**

```bash
git add routes/admin/work_tasks.py templates/work_tasks.html tests/test_action_notes.py
git commit -m "Work Tasks: add Орхисон (dropped) archive tab with Restore button

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

> Note: the Restore **button markup** lands here; its **click handler** is wired in Task 7.

---

## Task 6: Conversation viewer shows the note

**Files:**
- Modify: `templates/conversation.html` (after line 153)
- Modify: `tests/test_action_notes.py` (append 1 test)

- [ ] **Step 1: Append the test**

Add to the end of `tests/test_action_notes.py`:

```python
# ---- conversation viewer note row ----

def test_conversation_shows_note(client, admin_user, fb_user):
    from extensions import db
    from models import FacebookUser
    _login(client, admin_user)
    u = FacebookUser.query.get(fb_user.id)
    u.notes = 'Ярианы тэмдэглэл'
    db.session.commit()
    resp = client.get(f'/admin/users/{fb_user.id}/conversation')
    assert resp.status_code == 200
    assert 'Ярианы тэмдэглэл' in resp.get_data(as_text=True)
```

- [ ] **Step 2: Run — verify it fails**

Run: `python -m pytest tests/test_action_notes.py -k conversation_shows_note -v`
Expected: FAILS (`Ярианы тэмдэглэл` not rendered).

- [ ] **Step 3: Add the note row**

In `templates/conversation.html`, after the "Бүртгэлийн төлөв" `side-value` block closes (the `</div>` on line 153) and before the "Эхэлсэн" label (line 155), insert:

```html
                {% if fb_user.notes %}
                <div class="side-label">Тэмдэглэл</div>
                <div class="side-value text-muted small">{{ fb_user.notes }}</div>
                {% endif %}
```

- [ ] **Step 4: Run — verify pass**

Run: `python -m pytest tests/test_action_notes.py -k conversation_shows_note -v`
Expected: PASS.

- [ ] **Step 5: Full suite**

Run: `python -m pytest -q`
Expected: 71 passed.

- [ ] **Step 6: Commit**

```bash
git add templates/conversation.html tests/test_action_notes.py
git commit -m "Conversation viewer: show staff note when present

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 7: Note-entry modal + JS wiring (client-side; manual verification)

No pytest coverage — this is browser JS. Verified manually in Step 6. All edits are in `templates/work_tasks.html`.

**Files:**
- Modify: `templates/work_tasks.html` (modal markup before `<script>` at line 465; helper + handlers inside the IIFE)

- [ ] **Step 1: Add the modal markup**

In `templates/work_tasks.html`, between the `.tab-content` closing `</div>` (line 463) and `<script>` (line 465), insert:

```html
<div class="modal fade" id="noteModal" tabindex="-1" aria-hidden="true">
  <div class="modal-dialog">
    <div class="modal-content">
      <div class="modal-header">
        <h5 class="modal-title" id="noteModalTitle">Тэмдэглэл</h5>
        <button type="button" class="btn-close" data-bs-dismiss="modal" aria-label="Хаах"></button>
      </div>
      <div class="modal-body">
        <textarea id="noteModalText" class="form-control" rows="3"
                  placeholder="Шалтгаан / тэмдэглэл..."></textarea>
        <div class="form-text" id="noteModalHint"></div>
      </div>
      <div class="modal-footer">
        <button type="button" class="btn btn-secondary" data-bs-dismiss="modal">Болих</button>
        <button type="button" class="btn btn-brand" id="noteModalSave">Хадгалах</button>
      </div>
    </div>
  </div>
</div>
```

- [ ] **Step 2: Add the `openNoteModal` helper**

In the `<script>` IIFE in `templates/work_tasks.html`, after the `fadeAndRemove` function (ends line 499), insert:

```javascript
    // Shared note modal. Calls onSave(noteText) on confirm. When `required`,
    // Хадгалах is disabled until the textarea holds non-whitespace text.
    const noteModalEl = document.getElementById('noteModal');
    const noteModal = noteModalEl ? new bootstrap.Modal(noteModalEl) : null;
    const noteTextEl = document.getElementById('noteModalText');
    const noteSaveEl = document.getElementById('noteModalSave');
    const noteTitleEl = document.getElementById('noteModalTitle');
    const noteHintEl = document.getElementById('noteModalHint');
    let noteOnSave = null;
    let noteRequired = false;

    function syncNoteSave() {
        if (noteSaveEl) noteSaveEl.disabled = noteRequired && noteTextEl.value.trim() === '';
    }
    if (noteTextEl) noteTextEl.addEventListener('input', syncNoteSave);
    if (noteSaveEl) noteSaveEl.addEventListener('click', () => {
        const val = noteTextEl.value.trim();
        if (noteRequired && val === '') return;
        const cb = noteOnSave;
        if (noteModal) noteModal.hide();
        if (cb) cb(val);
    });

    function openNoteModal({ title, placeholder, required, onSave }) {
        // Fallback to a prompt() if Bootstrap's modal isn't available.
        if (!noteModal) {
            const val = (prompt(title || 'Тэмдэглэл') || '').trim();
            if (required && val === '') { toast('Шалтгаан заавал бичнэ үү.', false); return; }
            onSave(val);
            return;
        }
        noteOnSave = onSave;
        noteRequired = !!required;
        noteTitleEl.textContent = title || 'Тэмдэглэл';
        noteTextEl.value = '';
        noteTextEl.placeholder = placeholder || 'Шалтгаан / тэмдэглэл...';
        noteHintEl.textContent = required ? 'Заавал бичнэ үү.' : 'Сонголтоор.';
        syncNoteSave();
        noteModal.show();
        noteModalEl.addEventListener('shown.bs.modal',
            () => noteTextEl.focus(), { once: true });
    }
```

- [ ] **Step 3: Make `hotAction` accept an extra payload and rewire `.hot-drop`**

In `templates/work_tasks.html`, change the `hotAction` signature + the `post(...)` call (currently lines 575-580). Replace:

```javascript
    function hotAction(btn, action, okMsg, redirectTo) {
        const row = btn.closest('.wt-row');
        const userId = btn.dataset.userId;
        const siblings = row ? row.querySelectorAll('button') : [btn];
        siblings.forEach(b => { b.disabled = true; });
        post({action: action, user_id: userId})
```

with:

```javascript
    function hotAction(btn, action, okMsg, redirectTo, extra) {
        const row = btn.closest('.wt-row');
        const userId = btn.dataset.userId;
        const siblings = row ? row.querySelectorAll('button') : [btn];
        siblings.forEach(b => { b.disabled = true; });
        post(Object.assign({action: action, user_id: userId}, extra || {}))
```

Then replace the `.hot-drop` wiring (currently lines 599-601):

```javascript
    document.querySelectorAll('.hot-drop').forEach(btn => {
        btn.addEventListener('click', () => hotAction(btn, 'drop_prospect', 'Орхисон гэж тэмдэглэлээ.'));
    });
```

with:

```javascript
    document.querySelectorAll('.hot-drop').forEach(btn => {
        btn.addEventListener('click', () => {
            openNoteModal({
                title: 'Сонирхогчийг орхих',
                placeholder: 'Яагаад орхиж байна вэ? (заавал)',
                required: true,
                onSave: (note) => hotAction(btn, 'drop_prospect', 'Орхисон гэж тэмдэглэлээ.', null, {note}),
            });
        });
    });
```

- [ ] **Step 4: Rewire `.resolve-btn` through the modal**

Replace the `.resolve-btn` wiring (currently lines 625-638):

```javascript
    document.querySelectorAll('.resolve-btn').forEach(btn => {
        btn.addEventListener('click', () => {
            btn.disabled = true;
            post({action: 'resolve_issue', id: btn.dataset.id}).then(r => {
                if (r.success) {
                    toast('Асуудлыг шийдсэн гэж тэмдэглэлээ.', true);
                    fadeAndRemove(btn.closest('.wt-row'));
                } else {
                    toast('Алдаа гарлаа.', false);
                    btn.disabled = false;
                }
            });
        });
    });
```

with:

```javascript
    document.querySelectorAll('.resolve-btn').forEach(btn => {
        btn.addEventListener('click', () => {
            openNoteModal({
                title: 'Асуудлыг шийдэх',
                placeholder: 'Хэрхэн шийдсэн тухай тэмдэглэл (сонголтоор)',
                required: false,
                onSave: (note) => {
                    btn.disabled = true;
                    post({action: 'resolve_issue', id: btn.dataset.id, note: note}).then(r => {
                        if (r.success) {
                            toast('Асуудлыг шийдсэн гэж тэмдэглэлээ.', true);
                            fadeAndRemove(btn.closest('.wt-row'));
                        } else {
                            toast('Алдаа гарлаа.', false);
                            btn.disabled = false;
                        }
                    });
                },
            });
        });
    });
```

- [ ] **Step 5: Refactor the lead-status handler to prompt on `dropped`, and add the Restore handler**

Replace the entire `.lead-status-option` block (currently lines 508-569) with the extracted `applyLeadStatus` + a thin click handler, then append the restore handler:

```javascript
    async function applyLeadStatus(dropdown, newStatus, note) {
        const userId = dropdown.dataset.userId;
        const badge = dropdown.querySelector('.lead-status-badge .badge');
        const previousHtml = badge ? badge.outerHTML : null;
        if (badge) badge.style.opacity = '0.5';

        try {
            const payload = {user_id: userId, status: newStatus};
            if (note != null) payload.note = note;
            const resp = await fetch(LEAD_STATUS_URL, {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(payload),
            });
            const text = await resp.text();
            let data;
            try { data = JSON.parse(text); }
            catch (_) { throw new Error(`Сервер JSON буцаасангүй (HTTP ${resp.status}).`); }
            if (!data.success) throw new Error(data.error || data.message || 'unknown error');
            toast(`Статус "${data.label}" болголоо.`, true);

            if (TERMINAL_STATUSES.has(newStatus)) {
                const row = dropdown.closest('.wt-row, tr');
                if (row) fadeAndRemove(row);
                return;
            }

            const selectedOpt = dropdown.querySelector(
                `.lead-status-option[data-status="${newStatus}"]`
            );
            const colorBadge = selectedOpt ? selectedOpt.querySelector('.badge') : null;
            const colorClass = colorBadge
                ? Array.from(colorBadge.classList).find(c => c.startsWith('bg-'))
                : 'bg-secondary';
            if (badge) {
                badge.className = `badge ${colorClass} fs-75`;
                badge.textContent = data.label;
                badge.style.opacity = '1';
            }
            dropdown.querySelectorAll('.lead-status-option')
                .forEach(o => o.classList.toggle('active', o.dataset.status === newStatus));
        } catch (err) {
            toast('Алдаа: ' + (err.message || err), false);
            if (badge) {
                badge.style.opacity = '1';
                if (previousHtml) badge.outerHTML = previousHtml;
            }
        }
    }

    document.querySelectorAll('.lead-status-option').forEach(opt => {
        opt.addEventListener('click', (e) => {
            e.preventDefault();
            const dropdown = opt.closest('.lead-status-dropdown');
            if (!dropdown) return;
            const newStatus = opt.dataset.status;
            if (newStatus === 'dropped') {
                openNoteModal({
                    title: 'Лидийг орхих',
                    placeholder: 'Яагаад орхиж байна вэ? (заавал)',
                    required: true,
                    onSave: (note) => applyLeadStatus(dropdown, newStatus, note),
                });
                return;
            }
            applyLeadStatus(dropdown, newStatus, null);
        });
    });

    // Dropped archive: restore a dropped prospect/lead back to active.
    // Reuses the lead-status endpoint with the non-terminal 'new' status,
    // which needs no note.
    document.querySelectorAll('.restore-lead').forEach(btn => {
        btn.addEventListener('click', () => {
            btn.disabled = true;
            fetch(LEAD_STATUS_URL, {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({user_id: btn.dataset.userId, status: 'new'}),
            })
            .then(r => r.json())
            .then(d => {
                if (d.success) {
                    toast('Сэргээлээ.', true);
                    fadeAndRemove(btn.closest('tr'));
                } else {
                    toast(d.error || 'Алдаа гарлаа.', false);
                    btn.disabled = false;
                }
            })
            .catch(err => { toast('Алдаа: ' + (err.message || err), false); btn.disabled = false; });
        });
    });
```

> `LEAD_STATUS_URL` and `TERMINAL_STATUSES` are already declared above the original handler (lines 505-506) — keep those declarations; only the `.lead-status-option` block below them is replaced.

- [ ] **Step 6: Manual verification (dev server)**

The user runs the dev server themselves — ask them to confirm, or if you have a running instance, verify each:

1. Hot Prospects → **Орхих**: modal opens, **Хадгалах disabled** until text typed; saving drops the row and the reason is recorded.
2. Leads tab → status dropdown → **Орхих/dropped**: modal opens & is required; other statuses (e.g. Холбогдсон) change instantly with **no** modal.
3. Open Issues → **Шийдэх**: modal opens, **note optional** (Хадгалах enabled while empty); resolving works with and without a note.
4. Орхисон tab → **Сэргээх**: row disappears; the user returns to the Leads tab list.
5. Conversation viewer of a dropped user shows the **Тэмдэглэл** row.

- [ ] **Step 7: Run the full suite (guards against template syntax errors that break rendering tests)**

Run: `python -m pytest -q`
Expected: 71 passed.

- [ ] **Step 8: Commit**

```bash
git add templates/work_tasks.html
git commit -m "Work Tasks UI: note modal for drop/resolve + Restore wiring

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 8: Document the feature + final verification

**Files:**
- Modify: `CLAUDE.md` (Common admin paths / behaviour notes)

- [ ] **Step 1: Add a short note to `CLAUDE.md`**

Under the "Bug surfaces seen recently" section (or near the Work Tasks notes), add:

```markdown
- **Staff-action notes:** dropping a hot prospect or a lead **requires** a reason
  (the note modal in `work_tasks.html`); resolving an issue takes an **optional**
  note. The reason is stored on `FacebookUser.notes` (or `AdminIssue.notes` for
  issues) AND the full text goes into the audit-log `detail` — the durable copy
  that survives `cleanup_old_records`' 60-day purge. The **Орхисон** tab lists
  dropped users with their reason and a **Сэргээх** (restore → `status='new'`)
  button. `update_lead_status` enforces the required note only when
  `status == 'dropped'`.
```

- [ ] **Step 2: Run the full suite one last time**

Run: `python -m pytest -q`
Expected: 71 passed.

- [ ] **Step 3: Commit**

```bash
git add CLAUDE.md
git commit -m "Docs: note the staff-action notes + dropped archive feature

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

- [ ] **Step 4: Summarize the change set for the user and await the explicit push instruction** (per project protocol, push only on request).

---

## Self-review notes (author)

- **Spec coverage:** model + migration (T1), drop_prospect required (T2), lead drop required (T3), resolve optional (T4), dropped archive + restore (T5/T7), conversation display (T6), audit-log full note (T2/T3 detail), tests (all), docs (T8). All spec sections map to a task.
- **Type/name consistency:** `openNoteModal({title, placeholder, required, onSave})`, `applyLeadStatus(dropdown, newStatus, note)`, `hotAction(btn, action, okMsg, redirectTo, extra)`, payload key `note`, column `FacebookUser.notes`, audit action strings `lead.drop` / `lead.status_change` / `issue.status_change` — used identically across tasks.
- **Test count:** 62 → 71 (+9). Per-task expected totals: 64, 67, 69, 70, 71.
