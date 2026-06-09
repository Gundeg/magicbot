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
    # Consumed by the resolve_issue tests added in a later task.
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
    from extensions import db
    from models import FacebookUser
    _login(client, admin_user)
    resp = client.post('/admin/work-tasks',
                       json={'action': 'drop_prospect', 'user_id': fb_user.id})
    assert resp.status_code == 400
    assert resp.get_json()['success'] is False
    refreshed = db.session.get(FacebookUser, fb_user.id)
    assert refreshed.lead_status != 'dropped'


def test_drop_prospect_with_note_persists(client, admin_user, fb_user):
    from extensions import db
    from models import FacebookUser, AuditEntry
    _login(client, admin_user)
    resp = client.post('/admin/work-tasks',
                       json={'action': 'drop_prospect', 'user_id': fb_user.id,
                             'note': 'Дугаар буруу'})
    assert resp.status_code == 200
    refreshed = db.session.get(FacebookUser, fb_user.id)
    assert refreshed.lead_status == 'dropped'
    assert refreshed.notes == 'Дугаар буруу'
    entry = (AuditEntry.query.filter_by(action='lead.drop')
             .order_by(AuditEntry.id.desc()).first())
    assert entry is not None
    assert 'Дугаар буруу' in (entry.detail or '')


# ---- lead drop via /admin/api/lead-status (note required) ----

def test_lead_drop_without_note_rejected(client, admin_user, fb_user):
    from extensions import db
    from models import FacebookUser
    _login(client, admin_user)
    resp = client.post('/admin/api/lead-status',
                       json={'user_id': fb_user.id, 'status': 'dropped'})
    assert resp.status_code == 400
    assert resp.get_json()['success'] is False
    refreshed = db.session.get(FacebookUser, fb_user.id)
    assert refreshed.lead_status != 'dropped'


def test_lead_drop_with_note_persists(client, admin_user, fb_user):
    from extensions import db
    from models import FacebookUser, AuditEntry
    _login(client, admin_user)
    resp = client.post('/admin/api/lead-status',
                       json={'user_id': fb_user.id, 'status': 'dropped',
                             'note': 'Сонирхолгүй болсон'})
    assert resp.status_code == 200
    refreshed = db.session.get(FacebookUser, fb_user.id)
    assert refreshed.lead_status == 'dropped'
    assert refreshed.notes == 'Сонирхолгүй болсон'
    entry = (AuditEntry.query.filter_by(action='lead.status_change')
             .order_by(AuditEntry.id.desc()).first())
    assert entry is not None
    assert 'Сонирхолгүй болсон' in (entry.detail or '')


def test_non_dropped_status_needs_no_note(client, admin_user, fb_user):
    from extensions import db
    from models import FacebookUser
    _login(client, admin_user)
    resp = client.post('/admin/api/lead-status',
                       json={'user_id': fb_user.id, 'status': 'contacted'})
    assert resp.status_code == 200
    refreshed = db.session.get(FacebookUser, fb_user.id)
    assert refreshed.lead_status == 'contacted'


# ---- resolve_issue (note optional) ----

def test_resolve_issue_without_note_still_resolves(client, admin_user, open_issue):
    from extensions import db
    from models import AdminIssue
    _login(client, admin_user)
    resp = client.post('/admin/work-tasks',
                       json={'action': 'resolve_issue', 'id': open_issue.id})
    assert resp.status_code == 200
    refreshed = db.session.get(AdminIssue, open_issue.id)
    assert refreshed.status == 'resolved'
    assert refreshed.notes is None


def test_resolve_issue_with_note_saves(client, admin_user, open_issue):
    from extensions import db
    from models import AdminIssue, AuditEntry
    _login(client, admin_user)
    resp = client.post('/admin/work-tasks',
                       json={'action': 'resolve_issue', 'id': open_issue.id,
                             'note': 'Утсаар холбогдож шийдсэн'})
    assert resp.status_code == 200
    refreshed = db.session.get(AdminIssue, open_issue.id)
    assert refreshed.status == 'resolved'
    assert refreshed.notes == 'Утсаар холбогдож шийдсэн'
    entry = (AuditEntry.query.filter_by(action='issue.status_change')
             .order_by(AuditEntry.id.desc()).first())
    assert entry is not None
    assert 'Утсаар холбогдож шийдсэн' in (entry.detail or '')


# ---- dropped archive tab ----

def test_dropped_tab_lists_dropped_user_with_note(client, admin_user, fb_user):
    from extensions import db
    from models import FacebookUser
    _login(client, admin_user)
    u = db.session.get(FacebookUser, fb_user.id)
    u.lead_status = 'dropped'
    u.notes = 'Тест шалтгаан'
    db.session.commit()
    resp = client.get('/admin/work-tasks?tab=dropped')
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert 'Notes Test' in body
    assert 'Тест шалтгаан' in body
    assert 'restore-lead' in body  # the Restore button is present


# ---- conversation viewer note row ----

def test_conversation_shows_note(client, admin_user, fb_user):
    from extensions import db
    from models import FacebookUser
    _login(client, admin_user)
    u = db.session.get(FacebookUser, fb_user.id)
    u.notes = 'Ярианы тэмдэглэл'
    db.session.commit()
    resp = client.get(f'/admin/users/{fb_user.id}/conversation')
    assert resp.status_code == 200
    assert 'Ярианы тэмдэглэл' in resp.get_data(as_text=True)


# ---- update_status (rich issue editor) carries the note into the audit log ----

def test_update_status_note_in_audit(client, admin_user, open_issue):
    from extensions import db
    from models import AdminIssue, AuditEntry
    _login(client, admin_user)
    resp = client.post('/admin/work-tasks',
                       json={'action': 'update_status', 'id': open_issue.id,
                             'status': 'in_progress', 'notes': 'Шалгаж байна'})
    assert resp.status_code == 200
    refreshed = db.session.get(AdminIssue, open_issue.id)
    assert refreshed.status == 'in_progress'
    assert refreshed.notes == 'Шалгаж байна'
    entry = (AuditEntry.query.filter_by(action='issue.status_change')
             .order_by(AuditEntry.id.desc()).first())
    assert entry is not None
    assert 'Шалгаж байна' in (entry.detail or '')
