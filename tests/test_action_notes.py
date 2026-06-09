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
