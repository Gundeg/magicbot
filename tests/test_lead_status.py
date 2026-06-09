"""POST /admin/api/lead-status — six allowed values + JSON-only errors.

The endpoint is admin-only (decorated with @admin_required), which
redirects unauthenticated users to /admin/login. To exercise the real
validation logic we log in a seeded admin user via Flask-Login's test
helpers before each POST.
"""
import pytest


@pytest.fixture
def admin_user(app, db_session):
    from extensions import db
    from models import User
    from werkzeug.security import generate_password_hash

    User.query.filter_by(username='pytest-admin').delete()
    db.session.commit()
    user = User(
        username='pytest-admin',
        password=generate_password_hash('not-used'),
        email='pytest-admin@example.com',
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

    user = FacebookUser(facebook_id='psid-status-test', name='Status Test')
    db.session.add(user)
    db.session.commit()
    yield user
    FacebookUser.query.filter_by(id=user.id).delete()
    db.session.commit()


def _login(client, admin_user):
    # Inject the user_id into the Flask-Login session without going
    # through the password flow. Mirrors what login_user() does.
    with client.session_transaction() as sess:
        sess['_user_id'] = str(admin_user.id)
        sess['_fresh'] = True


def test_valid_status_update_persists(client, admin_user, fb_user):
    from models import FacebookUser
    _login(client, admin_user)

    resp = client.post(
        '/admin/api/lead-status',
        json={'user_id': fb_user.id, 'status': 'converted'},
    )
    assert resp.status_code == 200
    data = resp.get_json()
    assert data['success'] is True
    assert data['status'] == 'converted'
    assert data['label'] == 'Бүртгүүлсэн'
    # Re-read from DB
    refreshed = FacebookUser.query.get(fb_user.id)
    assert refreshed.lead_status == 'converted'


def test_invalid_status_rejected_with_json_400(client, admin_user, fb_user):
    _login(client, admin_user)

    resp = client.post(
        '/admin/api/lead-status',
        json={'user_id': fb_user.id, 'status': 'closed_won'},
    )
    assert resp.status_code == 400
    data = resp.get_json()
    assert data['success'] is False
    assert 'lead_status must be one of' in data['error']


def test_missing_user_id_rejected(client, admin_user):
    _login(client, admin_user)
    resp = client.post('/admin/api/lead-status', json={'status': 'contacted'})
    assert resp.status_code == 400


def test_unknown_user_returns_404_json(client, admin_user):
    _login(client, admin_user)
    resp = client.post(
        '/admin/api/lead-status',
        json={'user_id': 9999999, 'status': 'contacted'},
    )
    assert resp.status_code == 404
    assert resp.get_json()['success'] is False


def test_all_six_statuses_accepted(client, admin_user, fb_user):
    from services import LEAD_STATUS_KEYS
    _login(client, admin_user)

    # Just sanity-check the configured set really is six, then run
    # through them all.
    assert len(LEAD_STATUS_KEYS) == 6
    for status in LEAD_STATUS_KEYS:
        payload = {'user_id': fb_user.id, 'status': status}
        if status == 'dropped':
            payload['note'] = 'Test drop reason'
        resp = client.post('/admin/api/lead-status', json=payload)
        assert resp.status_code == 200, f'status {status!r} rejected'
        assert resp.get_json()['status'] == status
